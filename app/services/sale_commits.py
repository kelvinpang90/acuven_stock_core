"""确认销售（契约 §5.7，§5.2 第 5–7 条）。

命令在幂等执行器以 READ COMMITTED 开的事务里、用 execution.connection 运行，
自己不开、不提交、不回滚事务。开业务事务之前的两步由每个请求一个的 CommitSkus 做：
- balance_rows（执行器的余额行预建）：普通读查所列 SKU 的合并去向，
  把所列 SKU 与它们的合并去向记进 prepared，并返回这些 SKU 的余额行；
  执行器重做（RedoCommand）时再调用一次，重新算出、覆盖 prepared；
- close_due（执行器的 before_transaction 钩子）：对 prepared 里的 SKU 先关到期，
  规则、上限与出错处理同新预留（close_due_before_reserving）。
命令从同一个对象取 prepared（判“合并去向不在开事务前处理过的 SKU 里”时用）。

判定顺序（§5.7 事务里的判定与执行）：
1. 业务引用：普通读按 (tenant_id, channel_id, payment_ref) 查预留；在别的订单上
   409 BUSINESS_REF_CONFLICT 并记 business_ref_conflict；在本订单上且已 committed，
   摘要与记下的确认摘要相同 200 与当前表示，不同同上 409；其他继续；
2. FOR UPDATE 按 order_ref 读预留行：没有而 reservation_id 不为 null 404 RESERVATION_NOT_FOUND，
   为 null 转第 5 步；
   reservation_id 不为 null 且不同 409 RESERVATION_MISMATCH（reason 为 reservation_id）；
3. FOR SHARE 读订单行，再普通读查请求 SKU 与订单行 SKU 的合并去向，
   行集不一致 409 RESERVATION_MISMATCH（reason 为 lines，line_refs 升序）；
4. 按 commit_decision：COMMIT 先 FOR SHARE 读池行、按升序锁余额行，每行一条 SALE_COMMIT，
   订单行 committed_qty 设为 quantity，预留改为 committed；
   EXPIRE_THEN_LATE 读池行后在同一事务里 close_if_due，再走晚到确认；LATE 读池行后直接走；
   ANSWER_EXISTING 按第 1 步回答；
   STATE_CONFLICT 与 PAYMENT_REF_CONFLICT 409 STATE_CONFLICT（details 带表示里的状态）；
5. 故障补交新建（没有预留且 reservation_id 为 null）：
   FOR SHARE 读池行，不是 live 409 POOL_NOT_LIVE；普通读查 SKU，
   不存在或属于别的租户 404 SKU_NOT_FOUND（属于别的租户的登记 cross_tenant）；
   各行取请求 SKU 的合并去向，按升序锁余额行，再按 sku_id 升序 FOR SHARE 读这些 SKU 行；
   按 SKU 合计判可售（停用、合并进行中都不拦），写一个 fault_mode 的预留与订单行：
   created_at 与 closed_at 为现在，expires_at 为现在加当前配置的有效期（形式值），
   request_hash 取同内容预留请求的摘要；
   够则 committed、late 为真、每行一条 LATE_SALE_COMMIT；
   不够则 late_payment_review、订单行 committed_qty 为 0、不写流水，
   由 keep_on_rejection 登记的写入做，409 LATE_PAYMENT_INSUFFICIENT_STOCK。

报业务拒绝之前重查业务引用：第 2–5 步要报不写东西的拒绝（404、RESERVATION_MISMATCH、
STATE_CONFLICT、POOL_NOT_LIVE）之前，在同一事务里用普通读重做第 1 步
（READ COMMITTED 下看得到第 1 步之后才并发提交的确认）：
命中别的订单或本订单已 committed 就按第 1 步回答，不回滚、不算重做；没命中才报原来的拒绝。
第 5 步命中时丢弃本次登记的 cross_tenant 事件，没命中时保留（同 §5.6 并发重报）。
LATE_PAYMENT_INSUFFICIENT_STOCK 不在此列。

重做（RedoCommand，执行器回滚后从余额行预建那一步重做，每个请求最多一次）：
- 写 payment_ref（正常确认、晚到确认、转入待审）撞唯一约束：重做时第 1 步读到已生效的那一条，
  按它回答（本订单同摘要 200 与当前表示，否则 409 BUSINESS_REF_CONFLICT）；
- 故障补交新建撞 order_ref 或 payment_ref 的唯一约束（并发的预留或确认）：
  重做时按已有的预留回答；
- 晚到确认要判可售而订单行 SKU 的合并去向不在 prepared 里（开事务前后之间有合并提交，
  新去向没有先关到期）：在写任何东西之前重做，重做时连新去向一起预建、先关到期；
- 故障补交新建时请求 SKU 的合并去向不在 prepared 里，或锁余额行之后读到它已合并
  （去向在算出之后被并发的合并改了）：同上，在写任何东西之前重做。

晚到确认（§5.7）：按升序锁订单行各 SKU 的余额行，按 SKU 合计判可售（停用不拦）。
- 够：每行一条 LATE_SALE_COMMIT，订单行 committed_qty 设为 quantity，预留改为 committed、late 为真；
  closed_at 保留（到期关单的时间），paid_at 已记下（待审重试）时不变，确认摘要与 commit_* 记本次；
- 不够：409 LATE_PAYMENT_INSUFFICIENT_STOCK。第一次转入待审由 keep_on_rejection 登记的写入做：
  执行器先回滚命令的保存点（连同同一事务里的到期关单）再执行它，所以 EXPIRE_THEN_LATE 时
  它先再调一次 close_if_due，使 RELEASE 流水、reserved 的减回与 closed_at 和待审一起提交；
  已在待审的（重试仍不够）不改预留的任何列。
reservation_id 为 null 的请求确认或转入待审时 fault_mode 设为真，之后不改回。

待审结案（close_late_payment_review，§5.7）：同样在执行器以 READ COMMITTED 开的事务里运行。
FOR UPDATE 按 order_ref 读预留行：没有 404 RESERVATION_NOT_FOUND；
late_payment_review 改为 closed_refunded，记下结案的时间与 review_closed_* 各列，不写流水，200；
已是 closed_refunded 不写任何东西，200 与当前表示；
其他状态 409 STATE_CONFLICT（details 带表示里的状态，已到期未关的 active 为 expired），不关单。

最后变更时间（§5.9）：写预留行的同一条 UPDATE 或 INSERT 里设 last_changed_at，
取本事务写库用到的最晚一次数据库时间：
- 正常确认为 committed_at；故障补交新建为 created_at（与 closed_at、committed_at 相同）；
  待审结案为 review_closed_at；
- 晚到确认在同一事务里先关到期时，关单的 closed_at 取在命令时间之后，取两者中晚的；
  否则为 committed_at；
- 第一次转入待审：同一事务里先关到期的，close_if_due 已设为关单的 closed_at，不再改；
  否则不写任何时间列，取本命令的时间（paid_at 是调用方给的业务时间，不用它）；
- 幂等重放、换键重报返回已有结果、待审重试仍不够、对已 closed_refunded 的再结案不设。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.api.errors import ApiError
from app.domain.ids import new_id
from app.domain.ledger import EntryType
from app.domain.reservations import (
    CLOSED_REFUNDED,
    COMMITTED,
    LATE_PAYMENT_REVIEW,
    ORDER_LINE_ID_PREFIX,
    RESERVATION_ID_PREFIX,
    compute_expires_at,
    displayed_status,
)
from app.domain.sale_commits import (
    SOURCE_TYPE,
    CommitDecision,
    commit_decision,
    commit_reason_code,
    merge_destination,
    mismatched_line_refs,
    reserve_request_digest,
    sku_shortfalls,
)
from app.models.catalog import Sku
from app.models.orders import OrderLine, Reservation
from app.services.idempotency import CommandResult, Execution, RedoCommand
from app.services.reservations import (
    RESERVATION_NOT_FOUND,
    STATE_CONFLICT,
    ReservationLine,
    ReservationRecord,
    close_due_before_reserving,
    close_if_due,
    current_representation,
    find_reservation_by_payment_ref,
    lock_lines,
    lock_reservation_by_ref,
    read_lines,
    read_reservation,
    reservation_representation,
)
from app.services.stock import (
    BUSINESS_REF_CONFLICT,
    BUSINESS_REF_CONFLICT_EVENT,
    append_entry,
    command_now,
    lock_balances,
    lock_pool,
    new_command_id,
    require_live,
    require_skus_exist,
)

# 错误码（§5.7、§8.3）。
RESERVATION_MISMATCH = "RESERVATION_MISMATCH"
LATE_PAYMENT_INSUFFICIENT_STOCK = "LATE_PAYMENT_INSUFFICIENT_STOCK"
# BUSINESS_REF_CONFLICT 的 details.ref_type。
PAYMENT = "payment"
# RESERVATION_MISMATCH 的 details.reason，与 PAYMENT_REF_CONFLICT 的 details.reason。
RESERVATION_ID = "reservation_id"
LINES = "lines"
PAYMENT_REF = "payment_ref"

_DUPLICATE_ENTRY = 1062  # MySQL ER_DUP_ENTRY


@dataclass(frozen=True)
class SaleCommit:
    """确认请求的正文；paid_at 已换算成 UTC、不带时区。"""

    reservation_id: str | None
    payment_ref: str
    paid_at: datetime
    lines: tuple[ReservationLine, ...]


# --- 合并去向（§5.7 通用） ---


def merge_destinations(
    connection: Connection, tenant_id: str, sku_ids: Iterable[str]
) -> dict[str, str | None]:
    """普通读顺着 merged_into_sku_id 查到底，返回每个 sku_id 的合并去向。

    不存在或属于别的租户的 SKU 没有去向（None）。
    """
    wanted = set(sku_ids)
    merged_into: dict[str, str | None] = {}
    pending = set(wanted)
    while pending:
        statement = select(Sku.sku_id, Sku.merged_into_sku_id).where(
            Sku.tenant_id == tenant_id, Sku.sku_id.in_(pending)
        )
        rows = connection.execute(statement).all()
        for sku_id, target in rows:
            merged_into[sku_id] = target
        pending = {target for _, target in rows if target is not None and target not in merged_into}
    return {sku_id: merge_destination(sku_id, merged_into) for sku_id in wanted}


# --- 开业务事务之前（每个请求一个） ---


class CommitSkus:
    """一个确认请求开事务前处理的 SKU：所列 SKU 及其合并去向。

    prepared 是最近一次 balance_rows 算出的集合；还没算过时为空。
    """

    def __init__(self, tenant_id: str, pool_id: str, sku_ids: Sequence[str]):
        self.tenant_id = tenant_id
        self.pool_id = pool_id
        self.sku_ids = tuple(sku_ids)
        self.prepared: frozenset[str] = frozenset()

    def balance_rows(self, connection: Connection) -> list[tuple[str, str]]:
        """执行器的余额行预建：算出并记下所列 SKU 及其合并去向，返回它们的余额行。"""
        destinations = merge_destinations(connection, self.tenant_id, self.sku_ids)
        prepared = set(self.sku_ids)
        prepared.update(target for target in destinations.values() if target is not None)
        self.prepared = frozenset(prepared)
        return [(self.pool_id, sku_id) for sku_id in sorted(self.prepared)]

    def close_due(self, connection: Connection) -> None:
        """执行器的 before_transaction 钩子：对记下的 SKU 先关到期（§5.7）。

        规则、上限与出错处理同 §5.6 预留的第 2 步。
        """
        close_due_before_reserving(connection, self.tenant_id, self.pool_id, self.prepared)


# --- 回答 ---


def _tenant_id(execution: Execution) -> str:
    return execution.access.principal.tenant_id


def _payment_ref_conflict(execution: Execution, payment_ref: str) -> ApiError:
    details = {"ref_type": PAYMENT, "ref": payment_ref}
    execution.record_security_event(BUSINESS_REF_CONFLICT_EVENT, details)
    return ApiError(
        409,
        BUSINESS_REF_CONFLICT,
        "the payment reference was used with different content",
        details=details,
    )


def _answer_committed(execution: Execution, reservation: ReservationRecord) -> CommandResult:
    """本订单已用这个 payment_ref 确认：摘要与记下的确认摘要相同 200 与当前表示，
    不同 409 BUSINESS_REF_CONFLICT。
    """
    if reservation.commit_request_hash == execution.request_digest:
        connection = execution.connection
        representation = current_representation(connection, _tenant_id(execution), reservation)
        return CommandResult(representation, 200)
    raise _payment_ref_conflict(execution, reservation.payment_ref)


def _answer_by_payment_ref(
    execution: Execution, order_ref: str, payment_ref: str
) -> CommandResult | None:
    """第 1 步：普通读按 payment_ref 查预留。

    在别的订单上 409 BUSINESS_REF_CONFLICT；
    在本订单上且已 committed 按 _answer_committed 回答；
    其他（待审、已结案或没查到）返回 None，继续判定。
    """
    principal = execution.access.principal
    paid = find_reservation_by_payment_ref(
        execution.connection, principal.tenant_id, principal.channel_id, payment_ref
    )
    if paid is None:
        return None
    if paid.order_ref != order_ref:
        raise _payment_ref_conflict(execution, payment_ref)
    if paid.status == COMMITTED:
        return _answer_committed(execution, paid)
    return None


def _reject(
    execution: Execution, order_ref: str, commit: SaleCommit, error: ApiError
) -> CommandResult:
    """报不写东西的业务拒绝之前重查业务引用（§5.7 第 3 步之后的一段）。

    同一事务里重做第 1 步：命中别的订单或本订单已 committed 就按第 1 步回答，
    让并发与先后到达的同一 payment_ref 得到相同的结果；没命中才报原来的拒绝。
    不回滚，不算“重新判定”的一次。
    """
    answered = _answer_by_payment_ref(execution, order_ref, commit.payment_ref)
    if answered is not None:
        return answered
    raise error


def _state_conflict(status: str, **details: Any) -> ApiError:
    return ApiError(
        409,
        STATE_CONFLICT,
        "the reservation cannot be committed in its current status",
        details={**details, "status": status},
    )


# --- 写预留行 ---


def _commit_columns(execution: Execution) -> dict[str, Any]:
    """确认（或转入待审）的摘要与 commit_* 各列，取本次请求（§5.7 存放）。"""
    access = execution.access
    return {
        "commit_request_hash": execution.request_digest,
        "commit_credential_id": access.principal.credential_id,
        "commit_actor_ref": None if access.actor is None else access.actor.ref,
        "commit_actor_role": None if access.actor is None else access.actor.role,
        "commit_request_id": execution.request.state.request_id,
    }


def _update_reservation(
    connection: Connection, tenant_id: str, reservation_id: str, values: dict[str, Any]
) -> None:
    """改预留行；写 payment_ref 撞唯一约束时抛 RedoCommand（§5.7 撞唯一约束）。"""
    statement = update(Reservation).where(
        Reservation.tenant_id == tenant_id,
        Reservation.reservation_id == reservation_id,
    )
    try:
        connection.execute(statement.values(**values))
    except IntegrityError as exc:
        # 并发的确认先用掉了这个 payment_ref：回滚后整个重做。
        if getattr(exc.orig, "args", ())[:1] == (_DUPLICATE_ENTRY,):
            raise RedoCommand() from None
        raise


def _set_lines_committed(connection: Connection, tenant_id: str, reservation_id: str) -> None:
    connection.execute(
        update(OrderLine)
        .where(OrderLine.tenant_id == tenant_id, OrderLine.reservation_id == reservation_id)
        .values(committed_qty=OrderLine.quantity)
    )


# --- 正常确认 ---


def _commit(
    execution: Execution,
    reservation: ReservationRecord,
    lines: Sequence[ReservationLine],
    commit: SaleCommit,
    now: datetime,
) -> CommandResult:
    """正常确认（active 且未到期）：每行一条 SALE_COMMIT，
    订单行 committed_qty 设为 quantity，预留改为 committed。

    锁序：池行（FOR SHARE）→ 余额行（按 sku_id 升序）；改预留行放在最后，
    写 payment_ref 撞唯一约束时抛 RedoCommand。
    """
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    lock_pool(execution, reservation.pool_id)
    balances = lock_balances(execution, reservation.pool_id, [line.sku_id for line in lines])
    from_fault = commit.reservation_id is None
    reason_code = commit_reason_code(EntryType.SALE_COMMIT, from_fault)
    command_id = new_command_id()
    for line in lines:
        append_entry(
            execution,
            balances[line.sku_id],
            EntryType.SALE_COMMIT,
            (-line.quantity, -line.quantity, 0, line.quantity),
            command_id,
            now=now,
            source_type=SOURCE_TYPE,
            source_ref=reservation.order_ref,
            line_ref=line.line_ref,
            reason_code=reason_code,
            occurred_at=commit.paid_at,
        )
    _set_lines_committed(connection, tenant_id, reservation.reservation_id)

    committed = replace(
        reservation,
        status=COMMITTED,
        payment_ref=commit.payment_ref,
        paid_at=commit.paid_at,
        commit_request_hash=execution.request_digest,
        committed_at=now,
        closed_at=now,
        late=False,
        # 一旦为真不再改回（§5.7 表示）。
        fault_mode=reservation.fault_mode or from_fault,
    )
    _update_reservation(
        connection,
        tenant_id,
        reservation.reservation_id,
        {
            "status": committed.status,
            "payment_ref": committed.payment_ref,
            "paid_at": committed.paid_at,
            "committed_at": now,
            "closed_at": now,
            "late": False,
            "fault_mode": committed.fault_mode,
            "last_changed_at": now,
            **_commit_columns(execution),
        },
    )
    return CommandResult(reservation_representation(committed, lines, now), 200)


# --- 晚到确认 ---


def _late_commit(
    execution: Execution,
    reservation: ReservationRecord,
    lines: Sequence[ReservationLine],
    commit: SaleCommit,
    now: datetime,
    *,
    closed_here: bool,
) -> CommandResult:
    """晚到确认（§5.7、§5.2 第 6 条）：池行已读过；reservation 是 expired 或待审
    （closed_here 为真时是本事务刚关的，执行器撤销它之后转入待审要重新关）。

    锁序：余额行（按 sku_id 升序；同一事务里关单已锁的再锁一次不另等）→ 最后改预留行。
    """
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    balances = lock_balances(execution, reservation.pool_id, [line.sku_id for line in lines])
    available = {sku_id: balance.quantities.available for sku_id, balance in balances.items()}
    # 行集已一致：按请求的顺序列订单行，sku_id 取订单行的（合并去向）。
    order_lines = {line.line_ref: line for line in lines}
    requested = [order_lines[line.line_ref] for line in commit.lines]
    shortfalls = sku_shortfalls(
        [(line.line_ref, line.sku_id, line.quantity) for line in requested], available
    )
    from_fault = commit.reservation_id is None
    fault_mode = reservation.fault_mode or from_fault
    if shortfalls:
        if reservation.status != LATE_PAYMENT_REVIEW:
            execution.keep_on_rejection(
                lambda kept: _to_review(
                    execution, kept, reservation, commit, fault_mode, closed_here, now
                )
            )
        # 已在待审的（重试仍不够）不改预留的任何列。
        raise ApiError(
            409,
            LATE_PAYMENT_INSUFFICIENT_STOCK,
            "not enough available stock for the late payment",
            details={"lines": shortfalls},
        )

    reason_code = commit_reason_code(EntryType.LATE_SALE_COMMIT, from_fault)
    # 待审重试时付款时间取第一次转入待审时记下的。
    paid_at = commit.paid_at if reservation.paid_at is None else reservation.paid_at
    command_id = new_command_id()
    for line in lines:
        append_entry(
            execution,
            balances[line.sku_id],
            EntryType.LATE_SALE_COMMIT,
            (-line.quantity, 0, 0, line.quantity),
            command_id,
            now=now,
            source_type=SOURCE_TYPE,
            source_ref=reservation.order_ref,
            line_ref=line.line_ref,
            reason_code=reason_code,
            occurred_at=paid_at,
        )
    _set_lines_committed(connection, tenant_id, reservation.reservation_id)

    # closed_at 保留到期关单的时间；payment_ref 与记下的相同（待审）或第一次记下。
    committed = replace(
        reservation,
        status=COMMITTED,
        payment_ref=commit.payment_ref,
        paid_at=paid_at,
        commit_request_hash=execution.request_digest,
        committed_at=now,
        late=True,
        fault_mode=fault_mode,
    )
    # 本事务先关到期时 closed_at 在命令时间之后取，取写库用到的最晚一次（§5.9）。
    last_changed_at = now
    if closed_here and reservation.closed_at is not None:
        last_changed_at = max(now, reservation.closed_at)
    _update_reservation(
        connection,
        tenant_id,
        reservation.reservation_id,
        {
            "status": committed.status,
            "payment_ref": committed.payment_ref,
            "paid_at": committed.paid_at,
            "committed_at": now,
            "late": True,
            "fault_mode": fault_mode,
            "last_changed_at": last_changed_at,
            **_commit_columns(execution),
        },
    )
    return CommandResult(reservation_representation(committed, lines, now), 200)


def _to_review(
    execution: Execution,
    connection: Connection,
    reservation: ReservationRecord,
    commit: SaleCommit,
    fault_mode: bool,
    close_first: bool,
    now: datetime,
) -> None:
    """第一次转入待审（keep_on_rejection 的写入，在命令的保存点回滚之后执行）。

    close_first 为真时命令里的到期关单已随保存点撤销：先再关一次，
    使 RELEASE 流水、reserved 的减回与 closed_at 和待审一起提交；
    last_changed_at 由关单设为它的 closed_at（在命令时间 now 之后取），这里不再改。
    否则这次不写任何时间列，last_changed_at 取命令时间 now（§5.9）。
    """
    tenant_id = _tenant_id(execution)
    reservation_id = reservation.reservation_id
    if close_first and not close_if_due(connection, tenant_id, reservation_id):
        raise RuntimeError(f"reservation {reservation_id} was due but not closed")
    values: dict[str, Any] = {
        "status": LATE_PAYMENT_REVIEW,
        "payment_ref": commit.payment_ref,
        "paid_at": commit.paid_at,
        "fault_mode": fault_mode,
        **_commit_columns(execution),
    }
    if not close_first:
        values["last_changed_at"] = now
    _update_reservation(connection, tenant_id, reservation_id, values)


# --- 故障补交新建 ---


def _reject_before_creating(
    execution: Execution, order_ref: str, commit: SaleCommit, error: ApiError, events_from: int
) -> CommandResult:
    """第 5 步报 POOL_NOT_LIVE 与 SKU_NOT_FOUND 之前重查业务引用（同 _reject）。

    execution.events 从 events_from 起是本步登记的 cross_tenant：命中改按第 1 步回答时丢弃，
    没命中时随原来的拒绝保留（同 §5.6 并发重报）。
    """
    registered = execution.events[events_from:]
    del execution.events[events_from:]
    answered = _answer_by_payment_ref(execution, order_ref, commit.payment_ref)
    if answered is not None:
        return answered
    execution.events.extend(registered)
    raise error


def _any_merged(execution: Execution, sku_ids: Iterable[str]) -> bool:
    """余额行锁住之后按 sku_id 升序 FOR SHARE 读 SKU 行（只为等正在进行的合并提交、
    复核合并去向，不判停用）；有一个已合并返回 True。
    """
    statement = (
        select(Sku.merged_into_sku_id)
        .where(Sku.tenant_id == _tenant_id(execution), Sku.sku_id.in_(set(sku_ids)))
        .order_by(Sku.sku_id)
        .with_for_update(read=True)
    )
    merged_into = execution.connection.execute(statement).scalars()
    return any(target is not None for target in merged_into)


def _insert_created(
    execution: Execution,
    connection: Connection,
    reservation: ReservationRecord,
    lines: Sequence[ReservationLine],
    ttl_seconds: int,
) -> None:
    """写故障补交新建的预留行与订单行；
    committed 时订单行 committed_qty 等于 quantity，否则为 0。

    撞 order_ref 或 payment_ref 的唯一约束（并发的预留或确认先提交）时抛 RedoCommand，
    重做时按已有的预留回答。
    """
    access = execution.access
    principal = access.principal
    values = {
        "reservation_id": reservation.reservation_id,
        "tenant_id": principal.tenant_id,
        "channel_id": reservation.channel_id,
        "pool_id": reservation.pool_id,
        "order_ref": reservation.order_ref,
        "status": reservation.status,
        "ttl_seconds": ttl_seconds,
        "expires_at": reservation.expires_at,
        "request_hash": reservation.request_hash,
        "credential_id": principal.credential_id,
        "actor_ref": None if access.actor is None else access.actor.ref,
        "actor_role": None if access.actor is None else access.actor.role,
        "request_id": execution.request.state.request_id,
        "created_at": reservation.created_at,
        "closed_at": reservation.closed_at,
        "payment_ref": reservation.payment_ref,
        "paid_at": reservation.paid_at,
        "committed_at": reservation.committed_at,
        "late": reservation.late,
        "fault_mode": reservation.fault_mode,
        # 与 created_at、closed_at（及 committed_at）同一个命令时间（§5.9）。
        "last_changed_at": reservation.created_at,
        **_commit_columns(execution),
    }
    try:
        connection.execute(insert(Reservation).values(values))
    except IntegrityError as exc:
        if getattr(exc.orig, "args", ())[:1] == (_DUPLICATE_ENTRY,):
            raise RedoCommand() from None
        raise
    committed = reservation.status == COMMITTED
    for line in lines:
        connection.execute(
            insert(OrderLine).values(
                order_line_id=new_id(ORDER_LINE_ID_PREFIX),
                tenant_id=principal.tenant_id,
                reservation_id=reservation.reservation_id,
                pool_id=reservation.pool_id,
                sku_id=line.sku_id,
                line_ref=line.line_ref,
                quantity=line.quantity,
                committed_qty=line.quantity if committed else 0,
            )
        )


def _create_from_fault(
    execution: Execution, skus: CommitSkus, order_ref: str, commit: SaleCommit
) -> CommandResult:
    """第 5 步，故障补交新建（§5.7、§5.5“找不到”的分支）。

    锁序：池行（FOR SHARE）→ 余额行（按 sku_id 升序）
    → SKU 行（FOR SHARE，按 sku_id 升序）。
    """
    connection = execution.connection
    principal = execution.access.principal
    tenant_id = principal.tenant_id
    pool_id = skus.pool_id
    sku_ids = [line.sku_id for line in commit.lines]
    events_from = len(execution.events)
    try:
        require_live(execution, pool_id)
        require_skus_exist(execution, sku_ids)
    except ApiError as error:
        return _reject_before_creating(execution, order_ref, commit, error, events_from)

    # 合并去向不在开事务前处理过的 SKU 里（新去向没有先关到期，也可能没有预建余额行）：
    # 在写任何东西之前回滚重做。
    canonical: dict[str, str] = {}
    for sku_id, target in merge_destinations(connection, tenant_id, sku_ids).items():
        if target is None or target not in skus.prepared:
            raise RedoCommand()
        canonical[sku_id] = target
    targets = set(canonical.values())
    balances = lock_balances(execution, pool_id, targets)
    # 去向在算出之后被并发的合并改了：回滚重做，重做时按新去向。
    if _any_merged(execution, targets):
        raise RedoCommand()

    # 订单行取合并去向（Q-59），按请求的顺序。
    lines = [
        ReservationLine(line.line_ref, canonical[line.sku_id], line.quantity)
        for line in commit.lines
    ]
    available = {sku_id: balance.quantities.available for sku_id, balance in balances.items()}
    shortfalls = sku_shortfalls(
        [(line.line_ref, line.sku_id, line.quantity) for line in lines], available
    )

    now = command_now(execution)
    ttl_seconds = execution.request.app.state.reservation_ttl_seconds
    enough = not shortfalls
    # 从未 active：closed_at 为创建时间，expires_at 只是形式值。
    created = ReservationRecord(
        reservation_id=new_id(RESERVATION_ID_PREFIX),
        channel_id=principal.channel_id,
        order_ref=order_ref,
        pool_id=pool_id,
        status=COMMITTED if enough else LATE_PAYMENT_REVIEW,
        # 同内容的预留请求的摘要：之后用同样的行补调预留得到 200 与这个预留。
        request_hash=reserve_request_digest(
            order_ref, [(line.line_ref, line.sku_id, line.quantity) for line in commit.lines]
        ),
        expires_at=compute_expires_at(now, ttl_seconds),
        created_at=now,
        release_reason=None,
        closed_at=now,
        payment_ref=commit.payment_ref,
        paid_at=commit.paid_at,
        commit_request_hash=execution.request_digest,
        committed_at=now if enough else None,
        late=enough,
        fault_mode=True,
    )
    if not enough:
        execution.keep_on_rejection(
            lambda kept: _insert_created(execution, kept, created, lines, ttl_seconds)
        )
        raise ApiError(
            409,
            LATE_PAYMENT_INSUFFICIENT_STOCK,
            "not enough available stock for the late payment",
            details={"lines": shortfalls},
        )

    _insert_created(execution, connection, created, lines, ttl_seconds)
    reason_code = commit_reason_code(EntryType.LATE_SALE_COMMIT, True)
    command_id = new_command_id()
    for line in lines:
        append_entry(
            execution,
            balances[line.sku_id],
            EntryType.LATE_SALE_COMMIT,
            (-line.quantity, 0, 0, line.quantity),
            command_id,
            now=now,
            source_type=SOURCE_TYPE,
            source_ref=order_ref,
            line_ref=line.line_ref,
            reason_code=reason_code,
            occurred_at=commit.paid_at,
        )
    return CommandResult(reservation_representation(created, lines, now), 200)


# --- 命令 ---


def create_sale_commit(
    execution: Execution, skus: CommitSkus, order_ref: str, commit: SaleCommit
) -> CommandResult:
    """确认销售：commit 已经过请求校验（line_ref、sku_id 不重复）。

    skus 是本请求开事务前处理过的 SKU（skus.prepared），晚到确认与故障补交新建判可售时要用。
    """
    connection = execution.connection
    principal = execution.access.principal
    tenant_id = principal.tenant_id

    # 第 1 步：业务引用。
    answered = _answer_by_payment_ref(execution, order_ref, commit.payment_ref)
    if answered is not None:
        return answered

    # 第 2 步：锁预留行。第 2–4 步不写东西的拒绝都经 _reject，先重查业务引用。
    reservation = lock_reservation_by_ref(connection, tenant_id, principal.channel_id, order_ref)
    if reservation is None:
        if commit.reservation_id is not None:
            # 不按这个 ID 另查，不记事件。
            not_found = ApiError(404, RESERVATION_NOT_FOUND, "reservation not found")
            return _reject(execution, order_ref, commit, not_found)
        return _create_from_fault(execution, skus, order_ref, commit)
    if commit.reservation_id is not None and commit.reservation_id != reservation.reservation_id:
        details = {"reason": RESERVATION_ID}
        mismatch = ApiError(
            409, RESERVATION_MISMATCH, "the reservation id does not match", details=details
        )
        return _reject(execution, order_ref, commit, mismatch)

    # 第 3 步：行集比对（加锁读订单行之后再查合并去向）。
    lines = lock_lines(connection, tenant_id, reservation.reservation_id)
    requested = [(line.line_ref, line.sku_id, line.quantity) for line in commit.lines]
    existing = [(line.line_ref, line.sku_id, line.quantity) for line in lines]
    sku_ids = {line.sku_id for line in commit.lines} | {line.sku_id for line in lines}
    destinations = merge_destinations(connection, tenant_id, sku_ids)
    mismatched = mismatched_line_refs(requested, existing, destinations)
    if mismatched:
        details = {"reason": LINES, "line_refs": mismatched}
        mismatch = ApiError(
            409, RESERVATION_MISMATCH, "the lines do not match the reservation", details=details
        )
        return _reject(execution, order_ref, commit, mismatch)

    # 第 4 步：按预留状态。
    now = command_now(execution)
    decision = commit_decision(
        reservation.status, reservation.expires_at, now, reservation.payment_ref, commit.payment_ref
    )
    status = displayed_status(reservation.status, reservation.expires_at, now)
    if decision is CommitDecision.COMMIT:
        return _commit(execution, reservation, lines, commit, now)
    if decision is CommitDecision.ANSWER_EXISTING:
        return _answer_committed(execution, reservation)
    if decision is CommitDecision.PAYMENT_REF_CONFLICT:
        return _reject(execution, order_ref, commit, _state_conflict(status, reason=PAYMENT_REF))
    if decision is CommitDecision.STATE_CONFLICT:
        return _reject(execution, order_ref, commit, _state_conflict(status))

    # LATE 与 EXPIRE_THEN_LATE 要判可售：订单行 SKU 的合并去向不在开事务前处理过的 SKU 里
    # （开事务前后之间有合并提交，新去向没有先关到期），在写任何东西之前回滚重做。
    if any(destinations.get(line.sku_id) not in skus.prepared for line in lines):
        raise RedoCommand()

    # 池行排在余额行之前（关单也锁余额行）。
    lock_pool(execution, reservation.pool_id)
    closed_here = decision is CommitDecision.EXPIRE_THEN_LATE
    if closed_here:
        reservation_id = reservation.reservation_id
        if not close_if_due(connection, tenant_id, reservation_id):
            raise RuntimeError(f"reservation {reservation_id} was due but not closed")
        closed = read_reservation(connection, tenant_id, reservation_id)
        if closed is None:
            raise RuntimeError(f"reservation {reservation_id} is not in the tenant")
        reservation = closed
    return _late_commit(execution, reservation, lines, commit, now, closed_here=closed_here)


# --- 待审结案 ---


def close_late_payment_review(execution: Execution, order_ref: str) -> CommandResult:
    """待审结案（§5.7）：网店已退款，Core 只记下结案；待审时没有扣过数，不写流水。"""
    connection = execution.connection
    principal = execution.access.principal
    tenant_id = principal.tenant_id
    reservation = lock_reservation_by_ref(connection, tenant_id, principal.channel_id, order_ref)
    if reservation is None:
        raise ApiError(404, RESERVATION_NOT_FOUND, "reservation not found")
    if reservation.status == CLOSED_REFUNDED:
        return CommandResult(current_representation(connection, tenant_id, reservation), 200)
    now = command_now(execution)
    if reservation.status != LATE_PAYMENT_REVIEW:
        status = displayed_status(reservation.status, reservation.expires_at, now)
        raise ApiError(
            409,
            STATE_CONFLICT,
            "the reservation is not awaiting a late payment review",
            details={"status": status},
        )

    access = execution.access
    connection.execute(
        update(Reservation)
        .where(
            Reservation.tenant_id == tenant_id,
            Reservation.reservation_id == reservation.reservation_id,
        )
        .values(
            status=CLOSED_REFUNDED,
            review_closed_at=now,
            review_closed_credential_id=principal.credential_id,
            review_closed_actor_ref=None if access.actor is None else access.actor.ref,
            review_closed_actor_role=None if access.actor is None else access.actor.role,
            review_closed_request_id=execution.request.state.request_id,
            last_changed_at=now,
        )
    )
    closed = replace(reservation, status=CLOSED_REFUNDED, review_closed_at=now)
    lines = read_lines(connection, tenant_id, reservation.reservation_id)
    return CommandResult(reservation_representation(closed, lines, now), 200)
