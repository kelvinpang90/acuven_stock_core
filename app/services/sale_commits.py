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
2. FOR UPDATE 按 order_ref 读预留行：没有而 reservation_id 不为 null 404 RESERVATION_NOT_FOUND；
   reservation_id 不为 null 且不同 409 RESERVATION_MISMATCH（reason 为 reservation_id）；
3. FOR SHARE 读订单行，再普通读查请求 SKU 与订单行 SKU 的合并去向，
   行集不一致 409 RESERVATION_MISMATCH（reason 为 lines，line_refs 升序）；
4. 按 commit_decision：COMMIT 先 FOR SHARE 读池行、按升序锁余额行，每行一条 SALE_COMMIT，
   订单行 committed_qty 设为 quantity，预留改为 committed；ANSWER_EXISTING 按第 1 步回答；
   STATE_CONFLICT 与 PAYMENT_REF_CONFLICT 409 STATE_CONFLICT（details 带表示里的状态）。
写 payment_ref 撞唯一约束时抛 RedoCommand，执行器回滚后从余额行预建那一步重做。

尚未实现的分支先返回 503 UNAVAILABLE、不保存（确定未生效，可同键重试）：
晚到确认（LATE、EXPIRE_THEN_LATE），以及没有预留且 reservation_id 为 null 的故障补交新建。
报业务拒绝之前重查业务引用也尚未做。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, select, update
from sqlalchemy.exc import IntegrityError

from app.api.errors import ApiError
from app.api.requests import unavailable
from app.domain.ledger import EntryType
from app.domain.reservations import COMMITTED, displayed_status
from app.domain.sale_commits import (
    SOURCE_TYPE,
    CommitDecision,
    commit_decision,
    commit_reason_code,
    merge_destination,
    mismatched_line_refs,
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
    current_representation,
    find_reservation_by_payment_ref,
    lock_lines,
    lock_reservation_by_ref,
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
)

# 错误码（§5.7、§8.3）。
RESERVATION_MISMATCH = "RESERVATION_MISMATCH"
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


def _state_conflict(status: str, **details: Any) -> ApiError:
    return ApiError(
        409,
        STATE_CONFLICT,
        "the reservation cannot be committed in its current status",
        details={**details, "status": status},
    )


def _not_implemented_yet() -> ApiError:
    """晚到确认与故障补交新建尚未实现：503，不保存，同键重试会重新执行。"""
    return unavailable()


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
    connection.execute(
        update(OrderLine)
        .where(
            OrderLine.tenant_id == tenant_id,
            OrderLine.reservation_id == reservation.reservation_id,
        )
        .values(committed_qty=OrderLine.quantity)
    )

    access = execution.access
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
    statement = update(Reservation).where(
        Reservation.tenant_id == tenant_id,
        Reservation.reservation_id == reservation.reservation_id,
    )
    try:
        connection.execute(
            statement.values(
                status=committed.status,
                payment_ref=committed.payment_ref,
                paid_at=committed.paid_at,
                commit_request_hash=committed.commit_request_hash,
                committed_at=now,
                closed_at=now,
                late=False,
                fault_mode=committed.fault_mode,
                commit_credential_id=access.principal.credential_id,
                commit_actor_ref=None if access.actor is None else access.actor.ref,
                commit_actor_role=None if access.actor is None else access.actor.role,
                commit_request_id=execution.request.state.request_id,
            )
        )
    except IntegrityError as exc:
        # 并发的确认先用掉了这个 payment_ref：回滚后整个重做（§5.7 撞唯一约束）。
        if getattr(exc.orig, "args", ())[:1] == (_DUPLICATE_ENTRY,):
            raise RedoCommand() from None
        raise
    return CommandResult(reservation_representation(committed, lines, now), 200)


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
    paid = find_reservation_by_payment_ref(
        connection, tenant_id, principal.channel_id, commit.payment_ref
    )
    if paid is not None:
        if paid.order_ref != order_ref:
            raise _payment_ref_conflict(execution, commit.payment_ref)
        if paid.status == COMMITTED:
            return _answer_committed(execution, paid)

    # 第 2 步：锁预留行。
    reservation = lock_reservation_by_ref(connection, tenant_id, principal.channel_id, order_ref)
    if reservation is None:
        if commit.reservation_id is not None:
            # 不按这个 ID 另查，不记事件。
            raise ApiError(404, RESERVATION_NOT_FOUND, "reservation not found")
        raise _not_implemented_yet()
    if commit.reservation_id is not None and commit.reservation_id != reservation.reservation_id:
        details = {"reason": RESERVATION_ID}
        raise ApiError(
            409, RESERVATION_MISMATCH, "the reservation id does not match", details=details
        )

    # 第 3 步：行集比对（加锁读订单行之后再查合并去向）。
    lines = lock_lines(connection, tenant_id, reservation.reservation_id)
    requested = [(line.line_ref, line.sku_id, line.quantity) for line in commit.lines]
    existing = [(line.line_ref, line.sku_id, line.quantity) for line in lines]
    sku_ids = {line.sku_id for line in commit.lines} | {line.sku_id for line in lines}
    destinations = merge_destinations(connection, tenant_id, sku_ids)
    mismatched = mismatched_line_refs(requested, existing, destinations)
    if mismatched:
        details = {"reason": LINES, "line_refs": mismatched}
        raise ApiError(
            409, RESERVATION_MISMATCH, "the lines do not match the reservation", details=details
        )

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
        raise _state_conflict(status, reason=PAYMENT_REF)
    if decision is CommitDecision.STATE_CONFLICT:
        raise _state_conflict(status)
    # LATE 与 EXPIRE_THEN_LATE：晚到确认尚未实现。
    raise _not_implemented_yet()
