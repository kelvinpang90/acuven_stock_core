"""发货、退款回库与退货验收（契约 §5.8，§5.1、§5.3），以及三种订单单据共用的读写与表示。

三个命令都走 create_document（退货验收经 create_return_receipt），
按单据类型取余量、要写的流水与累计量（app/domain/fulfilment.py 的 line_limit、
line_entries、apply_line），按 QUANTITY_RULES 取各流水的增量：
发货与退款回库的余量为 committed_qty − dispatched_qty − refund_restocked_qty；
退货验收的余量为 dispatched_qty − returned_sellable_qty − returned_unsellable_qty，
与两个数之和比较；每行可售大于 0 时先写一条 RETURN_SELLABLE（stock_balance 加），
不可售大于 0 时再写一条 RETURN_UNSELLABLE（stock_balance、unavailable 各加），
订单行的两个累计量各加。

命令在幂等执行器以 READ COMMITTED 开的事务里、用 execution.connection 运行，
自己不开、不提交、不回滚事务。没有开业务事务之前的步骤：不预建余额行
（committed 预留的订单行在预留时已有余额行），不先关到期（committed 的预留不会到期）。
规则（余量、要写的流水、累计量）取自 app/domain/fulfilment.py。

判定顺序（§5.8 事务里的判定与执行，create_document）：
1. 业务引用：普通读按 (tenant_id, channel_id, document_type, document_ref) 查订单单据；
   有则摘要相同 200 与它的 result，不同 409 BUSINESS_REF_CONFLICT
   （ref_type 为 shipment、refund 或 return）并记 business_ref_conflict；
2. FOR UPDATE 按 order_ref 读预留行：没有 404 RESERVATION_NOT_FOUND（不记事件）；
   不是 committed 409 STATE_CONFLICT（details 带表示里的状态，已到期未关的 active 为 expired）；
3. FOR UPDATE 读该预留的全部订单行：请求里有订单没有的 line_ref
   409 RESERVATION_MISMATCH（reason 为 lines，line_refs 升序）；
4. 余量：任何一行超过 409 QUANTITY_EXCEEDS_OUTSTANDING，details.lines 按请求顺序列全；
5. 按 (pool_id, sku_id) 升序锁所列订单行的 SKU 的余额行（同一 SKU 只锁一次），
   按 line_ref 升序逐行写流水、加订单行的累计量，写订单单据一行，
   预留行的 last_changed_at 设为命令时间（即单据的 created_at，§5.9），201。

锁序：预留行（FOR UPDATE）→ 订单行（FOR UPDATE）→ 余额行（FOR UPDATE）。
不读池行、SKU 行与商品行：停用、合并进行中都不拦；订单行的 sku_id 照加锁读读到的
（合并改挂之后就是规范 SKU），两行指向同一个 SKU 时余额行只锁一次、两行各写各的流水。

报业务拒绝之前重查业务引用：第 2–4 步要报拒绝之前，在同一事务里用普通读重做第 1 步
（READ COMMITTED 下看得到第 1 步之后才并发提交的同一引用；不回滚），查到就按第 1 步回答。
写订单单据撞唯一约束（并发的同一引用）时抛 BusinessRefTaken：执行器回滚，
另开事务按第 1 步重新判定，结果作为这个幂等键的确定结果保存。

流水：source_type 为单据类型，source_ref 为本命令的引用，line_ref 为订单行的 line_ref；
channel_id、凭据、幂等键、操作者与请求 ID 取本次请求；reason_code、note、occurred_at 取正文
（不带 occurred_at 时取命令时间）。同一命令的流水共用一个 command_id。

单据的表示（document_representation）与按类型查、写单据（find_order_document、
insert_order_document）、回答已有单据（answer_existing_order_document）三种类型通用，
订单库存查询也用它们。

订单库存查询（read_order_stock，§5.8）不经幂等执行器，在调用方的连接上开一个
REPEATABLE READ、READ ONLY 的事务，预留、订单行与单据都在同一个读快照里用普通读读出，
自己不提交、不回滚（调用方读完结束事务）。查不到预留 404 RESERVATION_NOT_FOUND（不记事件），
游标与本订单不符 422；参数格式由接口层先判。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.api.errors import ApiError, invalid_request
from app.domain.catalog import AFTER
from app.domain.fulfilment import (
    DOCUMENT_ID_PREFIXES,
    REF_FIELDS,
    REF_TYPES,
    RETURN_RECEIPT,
    OrderLineTotals,
    QuantityLine,
    ReturnLine,
    apply_line,
    cursor_matches,
    exceeding_lines,
    format_order_stock_cursor,
    line_entries,
    unknown_line_refs,
)
from app.domain.ids import new_id
from app.domain.ledger import QUANTITY_RULES, EntryType
from app.domain.reservations import COMMITTED, displayed_status
from app.models.orders import LINE_ACCUMULATORS, OrderDocument, OrderLine, Reservation
from app.services.idempotency import BusinessRefTaken, CommandResult, Execution
from app.services.reservations import (
    RESERVATION_NOT_FOUND,
    STATE_CONFLICT,
    ReservationLine,
    database_now,
    find_reservation_by_ref,
    lock_reservation_by_ref,
    reservation_representation,
)
from app.services.sale_commits import LINES, RESERVATION_MISMATCH
from app.services.stock import (
    BUSINESS_REF_CONFLICT,
    BUSINESS_REF_CONFLICT_EVENT,
    append_entry,
    command_now,
    format_timestamp,
    lock_balances,
    new_command_id,
)

# 错误码（§5.8、§8.3）。
QUANTITY_EXCEEDS_OUTSTANDING = "QUANTITY_EXCEEDS_OUTSTANDING"

_DUPLICATE_ENTRY = 1062  # MySQL ER_DUP_ENTRY
# 只对下一个事务生效：订单库存查询的三部分在同一个读快照里（§5.8）。
_READ_SNAPSHOT = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"


@dataclass(frozen=True)
class CommonFields:
    """三个命令的公共字段（§5.8，规则同 §6.2 第 6 条）。

    occurred_at 已换算成 UTC，没带为 None。
    """

    reason_code: str | None = None
    note: str | None = None
    occurred_at: datetime | None = None


# --- 订单单据（§5.8 存放） ---


@dataclass(frozen=True)
class OrderDocumentRecord:
    """order_documents 里已生效的一行（判定与查询要用的列）。"""

    document_seq: int
    document_id: str
    document_type: str
    document_ref: str
    reservation_id: str
    request_hash: str
    result: dict[str, Any]


def find_order_document(
    connection: Connection, tenant_id: str, channel_id: str, document_type: str, document_ref: str
) -> OrderDocumentRecord | None:
    """普通读按 (tenant_id, channel_id, document_type, document_ref) 查订单单据。"""
    statement = select(
        OrderDocument.document_seq,
        OrderDocument.document_id,
        OrderDocument.document_type,
        OrderDocument.document_ref,
        OrderDocument.reservation_id,
        OrderDocument.request_hash,
        OrderDocument.result,
    ).where(
        OrderDocument.tenant_id == tenant_id,
        OrderDocument.channel_id == channel_id,
        OrderDocument.document_type == document_type,
        OrderDocument.document_ref == document_ref,
    )
    row = connection.execute(statement).mappings().first()
    return None if row is None else OrderDocumentRecord(**row)


def _find(
    execution: Execution, document_type: str, document_ref: str
) -> OrderDocumentRecord | None:
    principal = execution.access.principal
    return find_order_document(
        execution.connection,
        principal.tenant_id,
        principal.channel_id,
        document_type,
        document_ref,
    )


def answer_existing_order_document(
    execution: Execution, document: OrderDocumentRecord
) -> CommandResult:
    """第 1 步：摘要相同 200 与它的 result（单据生效后不变，就是当前表示）；
    不同 409 BUSINESS_REF_CONFLICT，ref_type 按单据类型，并登记 business_ref_conflict。
    """
    if document.request_hash == execution.request_digest:
        return CommandResult(document.result, 200)
    details = {"ref_type": REF_TYPES[document.document_type], "ref": document.document_ref}
    execution.record_security_event(BUSINESS_REF_CONFLICT_EVENT, details)
    raise ApiError(
        409,
        BUSINESS_REF_CONFLICT,
        "the business reference was used with different content",
        details=details,
    )


def _outside_create(execution: Execution) -> CommandResult:
    """insert_order_document 抛的 BusinessRefTaken 由 create_document 换上真正的重新判定。"""
    raise RuntimeError("an order document reference was taken outside create_document")


def insert_order_document(
    execution: Execution,
    document_type: str,
    document_ref: str,
    document_id: str,
    reservation_id: str,
    pool_id: str,
    created_at: datetime,
    result: dict[str, Any],
) -> None:
    """写订单单据一行：销售入口、凭据、操作者与请求 ID 取本次请求，request_hash 是本请求的摘要。

    撞唯一约束（并发的同一引用先提交）时抛 BusinessRefTaken，在 create_document 里重新判定。
    """
    access = execution.access
    principal = access.principal
    values = {
        "document_id": document_id,
        "tenant_id": principal.tenant_id,
        "channel_id": principal.channel_id,
        "pool_id": pool_id,
        "reservation_id": reservation_id,
        "document_type": document_type,
        "document_ref": document_ref,
        "request_hash": execution.request_digest,
        "result": result,
        "credential_id": principal.credential_id,
        "actor_ref": None if access.actor is None else access.actor.ref,
        "actor_role": None if access.actor is None else access.actor.role,
        "request_id": execution.request.state.request_id,
        "created_at": created_at,
    }
    try:
        execution.connection.execute(insert(OrderDocument).values(values))
    except IntegrityError as exc:
        if getattr(exc.orig, "args", ())[:1] == (_DUPLICATE_ENTRY,):
            raise BusinessRefTaken(_outside_create) from None
        raise


# --- 表示（§5.8 成功） ---


def line_representation(
    document_type: str,
    line: QuantityLine | ReturnLine,
    sku_id: str,
    entry_ids: dict[EntryType, str],
) -> dict[str, Any]:
    """单据的一行：line_ref、sku_id（写流水用的订单行 SKU），
    发货与退款回库另带 quantity、entry_id；
    退货验收另带两个数与各自的 entry_id（数为 0 时为 null）。
    """
    if document_type == RETURN_RECEIPT:
        return {
            "line_ref": line.line_ref,
            "sku_id": sku_id,
            "sellable_quantity": line.sellable_quantity,
            "unsellable_quantity": line.unsellable_quantity,
            "sellable_entry_id": entry_ids.get(EntryType.RETURN_SELLABLE),
            "unsellable_entry_id": entry_ids.get(EntryType.RETURN_UNSELLABLE),
        }
    [entry_id] = entry_ids.values()
    return {
        "line_ref": line.line_ref,
        "sku_id": sku_id,
        "quantity": line.quantity,
        "entry_id": entry_id,
    }


def document_representation(
    *,
    document_id: str,
    document_type: str,
    order_ref: str,
    reservation_id: str,
    pool_id: str,
    document_ref: str,
    fields: CommonFields,
    created_at: datetime,
    lines: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """订单单据的表示：引用字段只出现本类型的一个，lines 按 line_ref 升序。
    不含订单行累计量。不读库。
    """
    occurred_at = created_at if fields.occurred_at is None else fields.occurred_at
    return {
        "document_id": document_id,
        "document_type": document_type,
        "order_ref": order_ref,
        "reservation_id": reservation_id,
        "pool_id": pool_id,
        REF_FIELDS[document_type]: document_ref,
        "reason_code": fields.reason_code,
        "note": fields.note,
        "occurred_at": format_timestamp(occurred_at),
        "created_at": format_timestamp(created_at),
        "lines": sorted(lines, key=lambda line: line["line_ref"]),
    }


# --- 订单行 ---

_TOTALS_COLUMNS = (
    OrderLine.line_ref,
    OrderLine.sku_id,
    OrderLine.quantity,
    *(getattr(OrderLine, name) for name in LINE_ACCUMULATORS),
)


def lock_order_lines(
    connection: Connection, tenant_id: str, reservation_id: str
) -> list[OrderLineTotals]:
    """FOR UPDATE 读预留的全部订单行与累计量，按 line_ref 升序（§5.8 第 3 步）。"""
    statement = (
        select(*_TOTALS_COLUMNS)
        .where(OrderLine.tenant_id == tenant_id, OrderLine.reservation_id == reservation_id)
        .order_by(OrderLine.line_ref)
        .with_for_update()
    )
    return [OrderLineTotals(**row) for row in connection.execute(statement).mappings()]


def _update_totals(
    connection: Connection, tenant_id: str, reservation_id: str, totals: OrderLineTotals
) -> None:
    statement = update(OrderLine).where(
        OrderLine.tenant_id == tenant_id,
        OrderLine.reservation_id == reservation_id,
        OrderLine.line_ref == totals.line_ref,
    )
    values = {name: getattr(totals, name) for name in LINE_ACCUMULATORS}
    connection.execute(statement.values(values))


def _deltas(entry_type: EntryType, quantity: int) -> tuple[int, int, int, int]:
    sb, r, u, ppd = QUANTITY_RULES[entry_type]
    return sb * quantity, r * quantity, u * quantity, ppd * quantity


# --- 命令 ---


def _reject(
    execution: Execution, document_type: str, document_ref: str, error: ApiError
) -> CommandResult:
    """第 2–4 步报拒绝之前，在同一事务里用普通读重做第 1 步：查到就按第 1 步回答，
    没查到才报原来的拒绝。不回滚。
    """
    existing = _find(execution, document_type, document_ref)
    if existing is not None:
        return answer_existing_order_document(execution, existing)
    raise error


def _execute(
    execution: Execution,
    document_type: str,
    order_ref: str,
    document_ref: str,
    lines: Sequence[QuantityLine | ReturnLine],
    fields: CommonFields,
) -> CommandResult:
    """第 2–5 步。"""
    connection = execution.connection
    principal = execution.access.principal
    tenant_id = principal.tenant_id

    # 第 2 步：锁预留行。
    reservation = lock_reservation_by_ref(connection, tenant_id, principal.channel_id, order_ref)
    if reservation is None:
        not_found = ApiError(404, RESERVATION_NOT_FOUND, "reservation not found")
        return _reject(execution, document_type, document_ref, not_found)
    now = command_now(execution)
    if reservation.status != COMMITTED:
        status = displayed_status(reservation.status, reservation.expires_at, now)
        conflict = ApiError(
            409,
            STATE_CONFLICT,
            "the reservation is not committed",
            details={"status": status},
        )
        return _reject(execution, document_type, document_ref, conflict)

    # 第 3 步：锁订单行，请求里不能有订单没有的 line_ref。
    order_lines = lock_order_lines(connection, tenant_id, reservation.reservation_id)
    unknown = unknown_line_refs(
        [line.line_ref for line in lines], [totals.line_ref for totals in order_lines]
    )
    if unknown:
        details = {"reason": LINES, "line_refs": unknown}
        mismatch = ApiError(
            409, RESERVATION_MISMATCH, "the lines are not on the order", details=details
        )
        return _reject(execution, document_type, document_ref, mismatch)

    # 第 4 步：余量。
    exceeding = exceeding_lines(document_type, lines, order_lines)
    if exceeding:
        exceeds = ApiError(
            409,
            QUANTITY_EXCEEDS_OUTSTANDING,
            "quantity exceeds the outstanding quantity of the line",
            details={"lines": exceeding},
        )
        return _reject(execution, document_type, document_ref, exceeds)

    # 第 5 步：升序锁余额行，按 line_ref 升序写流水、加累计量，写单据。
    by_ref = {totals.line_ref: totals for totals in order_lines}
    ordered = sorted(lines, key=lambda line: line.line_ref)
    balances = lock_balances(
        execution, reservation.pool_id, [by_ref[line.line_ref].sku_id for line in ordered]
    )
    command_id = new_command_id()
    represented = []
    for line in ordered:
        totals = by_ref[line.line_ref]
        entry_ids: dict[EntryType, str] = {}
        for entry_type, quantity in line_entries(document_type, line):
            entry = append_entry(
                execution,
                balances[totals.sku_id],
                entry_type,
                _deltas(entry_type, quantity),
                command_id,
                now=now,
                source_type=document_type,
                source_ref=document_ref,
                line_ref=line.line_ref,
                reason_code=fields.reason_code,
                note=fields.note,
                occurred_at=fields.occurred_at,
            )
            entry_ids[entry_type] = entry["entry_id"]
        _update_totals(
            connection,
            tenant_id,
            reservation.reservation_id,
            apply_line(document_type, totals, line),
        )
        represented.append(line_representation(document_type, line, totals.sku_id, entry_ids))

    document_id = new_id(DOCUMENT_ID_PREFIXES[document_type])
    result = document_representation(
        document_id=document_id,
        document_type=document_type,
        order_ref=order_ref,
        reservation_id=reservation.reservation_id,
        pool_id=reservation.pool_id,
        document_ref=document_ref,
        fields=fields,
        created_at=now,
        lines=represented,
    )
    insert_order_document(
        execution,
        document_type,
        document_ref,
        document_id,
        reservation.reservation_id,
        reservation.pool_id,
        now,
        result,
    )
    # 改了订单行、不写预留的时间列：最后变更时间取命令时间（§5.9）。预留行已 FOR UPDATE 锁住。
    connection.execute(
        update(Reservation)
        .where(
            Reservation.tenant_id == tenant_id,
            Reservation.reservation_id == reservation.reservation_id,
        )
        .values(last_changed_at=now)
    )
    return CommandResult(result, 201)


def create_document(
    execution: Execution,
    document_type: str,
    order_ref: str,
    document_ref: str,
    lines: Sequence[QuantityLine | ReturnLine],
    fields: CommonFields,
) -> CommandResult:
    """一条订单单据命令：lines 已经过请求校验（1–100 行，line_ref 不重复，件数合规）。

    已有同类型、同引用的单据时按 answer_existing_order_document 回答；否则按第 2–5 步。
    写单据撞唯一约束时抛 BusinessRefTaken，执行器另开事务按第 1 步重新判定。
    """

    def rejudge(execution: Execution) -> CommandResult:
        existing = _find(execution, document_type, document_ref)
        if existing is None:
            # 单据不删：撞上唯一约束之后，新事务里不会读不到。
            raise RuntimeError(f"{document_type} {document_ref} is not visible on rejudging")
        return answer_existing_order_document(execution, existing)

    # 第 1 步：业务引用。
    existing = _find(execution, document_type, document_ref)
    if existing is not None:
        return answer_existing_order_document(execution, existing)
    try:
        return _execute(execution, document_type, order_ref, document_ref, lines, fields)
    except BusinessRefTaken:
        raise BusinessRefTaken(rejudge) from None


def create_return_receipt(
    execution: Execution,
    order_ref: str,
    return_ref: str,
    lines: Sequence[QuantityLine | ReturnLine],
    fields: CommonFields,
) -> CommandResult:
    """退货验收（return_receipts.create）：按 create_document 的判定顺序与锁序。

    与发货、退款回库不同的只在按单据类型取的规则上：
    - 第 4 步余量（exceeding_lines → line_limit）为
      dispatched_qty − returned_sellable_qty − returned_unsellable_qty，
      与两个数之和比较，details.lines 的 quantity 为两数之和；退款回库的件数不算进去；
    - 第 5 步每行的流水（line_entries）：可售大于 0 时先一条 RETURN_SELLABLE
      （stock_balance 加），不可售大于 0 时再一条 RETURN_UNSELLABLE
      （stock_balance、unavailable 各加），增量取 QUANTITY_RULES；
    - 订单行（apply_line）returned_sellable_qty、returned_unsellable_qty 各加；
    - 表示（line_representation）每行带两个数与 sellable_entry_id、unsellable_entry_id，
      对应的数为 0、没写那条流水时为 null。
    """
    for line in lines:
        if not isinstance(line, ReturnLine):
            raise TypeError(f"a return receipt needs ReturnLine lines, got {line!r}")
    return create_document(execution, RETURN_RECEIPT, order_ref, return_ref, lines, fields)


# --- 订单库存查询（§5.8） ---


def read_order_lines(
    connection: Connection, tenant_id: str, reservation_id: str
) -> list[OrderLineTotals]:
    """普通读读预留的全部订单行与累计量，按 line_ref 升序。"""
    statement = (
        select(*_TOTALS_COLUMNS)
        .where(OrderLine.tenant_id == tenant_id, OrderLine.reservation_id == reservation_id)
        .order_by(OrderLine.line_ref)
    )
    return [OrderLineTotals(**row) for row in connection.execute(statement).mappings()]


def read_order_documents(
    connection: Connection,
    tenant_id: str,
    reservation_id: str,
    after_seq: int | None,
    limit: int,
) -> list[tuple[int, dict[str, Any]]]:
    """普通读读预留的单据，按 document_seq 升序取大于 after_seq 的至多 limit 张；
    返回 (document_seq, result)。
    """
    statement = select(OrderDocument.document_seq, OrderDocument.result).where(
        OrderDocument.tenant_id == tenant_id, OrderDocument.reservation_id == reservation_id
    )
    if after_seq is not None:
        statement = statement.where(OrderDocument.document_seq > after_seq)
    statement = statement.order_by(OrderDocument.document_seq).limit(limit)
    return [(row.document_seq, row.result) for row in connection.execute(statement)]


def _totals_representation(totals: OrderLineTotals) -> dict[str, Any]:
    """一行订单行：line_ref、sku_id、quantity 与 §5.1 的五个累计量。"""
    return {
        "line_ref": totals.line_ref,
        "sku_id": totals.sku_id,
        "quantity": totals.quantity,
        **{name: getattr(totals, name) for name in LINE_ACCUMULATORS},
    }


def read_order_stock(
    connection: Connection,
    tenant_id: str,
    channel_id: str,
    order_ref: str,
    after: str | None,
    cursor: tuple[int, str] | None,
    limit: int,
) -> dict[str, Any]:
    """订单库存查询的 data：reservation、lines、documents 与 next_cursor。

    after 是请求里的原值，cursor 是已校验过格式的 (document_seq, 摘要)，没带都为 None。
    调用时连接上没有打开的事务；返回或抛出时事务仍开着，由调用方结束。
    预留按 (tenant_id, channel_id, order_ref) 查，没有 404 RESERVATION_NOT_FOUND；
    游标不属于这个订单 422。没有新单据时 next_cursor 原样为 after。
    """
    connection.execute(text(_READ_SNAPSHOT))
    reservation = find_reservation_by_ref(connection, tenant_id, channel_id, order_ref)
    if reservation is None:
        raise ApiError(404, RESERVATION_NOT_FOUND, "reservation not found")
    reservation_id = reservation.reservation_id
    if cursor is not None and not cursor_matches(cursor[1], reservation_id):
        raise invalid_request(f"invalid {AFTER} parameter", {"parameter": AFTER})

    totals = read_order_lines(connection, tenant_id, reservation_id)
    after_seq = None if cursor is None else cursor[0]
    documents = read_order_documents(connection, tenant_id, reservation_id, after_seq, limit)
    lines = [ReservationLine(t.line_ref, t.sku_id, t.quantity) for t in totals]
    now = database_now(connection)

    next_cursor = after
    if documents:
        next_cursor = format_order_stock_cursor(documents[-1][0], reservation_id)
    return {
        "reservation": reservation_representation(reservation, lines, now),
        "lines": [_totals_representation(t) for t in totals],
        "documents": [result for _, result in documents],
        "next_cursor": next_cursor,
    }
