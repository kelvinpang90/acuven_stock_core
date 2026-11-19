"""对账查询与待审列表的读库（契约 §5.9）。

两个查询都只读、不加锁，在调用方的连接上运行，自己不提交、不回滚（调用方读完结束事务）；
参数、游标与窗口由接口层先按 app/domain/reconciliation.py 判过。

对账查询（read_reconciliation_orders）：调用方先 begin_snapshot 开一个 REPEATABLE READ、
READ ONLY 的事务，判窗口用的数据库时间与本页的预留、订单行都出自同一个读快照。
按 (tenant_id, channel_id) 与 changed_from ≤ last_changed_at < changed_to 选预留，
按 (last_changed_at, reservation_id) 升序取游标之后的至多 limit 个（索引
(tenant_id, channel_id, last_changed_at, reservation_id)）。每项是预留的表示、last_changed_at
与订单行的累计量（同订单库存查询，§5.8），不读单据与流水。

待审列表（read_review_reservations）：普通读按 (tenant_id, channel_id, status) 取
reservation_id 在游标之后的至多 limit 个，按 reservation_id 升序（索引
(tenant_id, channel_id, status, reservation_id)），每项是预留的表示。
"""

from collections import defaultdict
from dataclasses import fields
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import Connection, and_, or_, select, text

from app.domain.fulfilment import OrderLineTotals
from app.domain.reconciliation import ReconciliationCursor, ReviewCursor
from app.models.orders import LINE_ACCUMULATORS, OrderLine, Reservation
from app.services.reservations import (
    ReservationLine,
    ReservationRecord,
    database_now,
    reservation_representation,
)
from app.services.stock import format_timestamp

# 只对下一个事务生效：一页的预留与订单行在同一个读快照里（§5.9）。
_READ_SNAPSHOT = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"

_RESERVATION_COLUMNS = tuple(
    getattr(Reservation, field.name) for field in fields(ReservationRecord)
)
_TOTALS_NAMES = tuple(field.name for field in fields(OrderLineTotals))
_TOTALS_COLUMNS = tuple(getattr(OrderLine, name) for name in _TOTALS_NAMES)


class ReconciliationOrder(NamedTuple):
    """对账查询的一项：游标要用的排序键与表示。"""

    last_changed_at: datetime
    reservation_id: str
    representation: dict[str, Any]


def begin_snapshot(connection: Connection) -> datetime:
    """开对账查询的只读事务（调用时连接上没有打开的事务），返回快照里的数据库时间。"""
    connection.execute(text(_READ_SNAPSHOT))
    return database_now(connection)


def _totals_representation(totals: OrderLineTotals) -> dict[str, Any]:
    """一行订单行：line_ref、sku_id、quantity 与 §5.1 的五个累计量（同订单库存查询）。"""
    return {
        "line_ref": totals.line_ref,
        "sku_id": totals.sku_id,
        "quantity": totals.quantity,
        **{name: getattr(totals, name) for name in LINE_ACCUMULATORS},
    }


def _read_totals(
    connection: Connection, tenant_id: str, reservation_ids: list[str]
) -> dict[str, list[OrderLineTotals]]:
    """普通读读这些预留的订单行与累计量，每个预留的按 line_ref 升序。"""
    found: dict[str, list[OrderLineTotals]] = defaultdict(list)
    if not reservation_ids:
        return found
    statement = (
        select(OrderLine.reservation_id, *_TOTALS_COLUMNS)
        .where(OrderLine.tenant_id == tenant_id, OrderLine.reservation_id.in_(reservation_ids))
        .order_by(OrderLine.reservation_id, OrderLine.line_ref)
    )
    for row in connection.execute(statement).mappings():
        values = {name: row[name] for name in _TOTALS_NAMES}
        found[row["reservation_id"]].append(OrderLineTotals(**values))
    return found


def _lines(totals: list[OrderLineTotals]) -> list[ReservationLine]:
    return [ReservationLine(t.line_ref, t.sku_id, t.quantity) for t in totals]


# --- 对账查询 ---


def read_reconciliation_orders(
    connection: Connection,
    tenant_id: str,
    channel_id: str,
    changed_from: datetime,
    changed_to: datetime,
    cursor: ReconciliationCursor | None,
    limit: int,
    now: datetime,
) -> list[ReconciliationOrder]:
    """begin_snapshot 之后调用；now 是它返回的数据库时间（表示判到期用）。"""
    statement = select(*_RESERVATION_COLUMNS, Reservation.last_changed_at).where(
        Reservation.tenant_id == tenant_id,
        Reservation.channel_id == channel_id,
        Reservation.last_changed_at >= changed_from,
        Reservation.last_changed_at < changed_to,
    )
    if cursor is not None:
        statement = statement.where(
            or_(
                Reservation.last_changed_at > cursor.last_changed_at,
                and_(
                    Reservation.last_changed_at == cursor.last_changed_at,
                    Reservation.reservation_id > cursor.reservation_id,
                ),
            )
        )
    statement = statement.order_by(Reservation.last_changed_at, Reservation.reservation_id)
    rows = list(connection.execute(statement.limit(limit)).mappings())
    totals = _read_totals(connection, tenant_id, [row["reservation_id"] for row in rows])

    orders = []
    for row in rows:
        values = dict(row)
        last_changed_at = values.pop("last_changed_at")
        reservation = ReservationRecord(**values)
        reservation_id = reservation.reservation_id
        lines = totals[reservation_id]
        representation = {
            "reservation": reservation_representation(reservation, _lines(lines), now),
            "last_changed_at": format_timestamp(last_changed_at),
            "lines": [_totals_representation(t) for t in lines],
        }
        orders.append(ReconciliationOrder(last_changed_at, reservation_id, representation))
    return orders


# --- 待审列表 ---


def read_review_reservations(
    connection: Connection,
    tenant_id: str,
    channel_id: str,
    status: str,
    cursor: ReviewCursor | None,
    limit: int,
) -> list[dict[str, Any]]:
    """普通读本入口 status 的预留，返回各自的表示，按 reservation_id 升序。"""
    statement = select(*_RESERVATION_COLUMNS).where(
        Reservation.tenant_id == tenant_id,
        Reservation.channel_id == channel_id,
        Reservation.status == status,
    )
    if cursor is not None:
        statement = statement.where(Reservation.reservation_id > cursor.reservation_id)
    statement = statement.order_by(Reservation.reservation_id).limit(limit)
    reservations = [ReservationRecord(**row) for row in connection.execute(statement).mappings()]
    if not reservations:
        return []
    totals = _read_totals(connection, tenant_id, [r.reservation_id for r in reservations])
    now = database_now(connection)
    return [
        reservation_representation(r, _lines(totals[r.reservation_id]), now) for r in reservations
    ]
