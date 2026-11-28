"""不变量自查的读库（契约 §4.4）。

在调用方的连接上运行，调用时连接上没有打开的事务；自己不提交、不回滚（调用方读完回滚结束事务）。
全部查询在一个 REPEATABLE READ、READ ONLY 的事务里，开启时即取读快照（WITH CONSISTENT SNAPSHOT），
不加锁：一笔命令一个事务，并发提交的命令要么整笔在快照里、要么整笔不在。
checked_at 是这个事务里的数据库时间。

只读本租户的：
- 余额行，与按余额行聚合的流水（四个增量之和、条数、最大的 balance_version_after）；
- 按 (pool_id, sku_id) 聚合的订单行：所属预留为 active 的 quantity 之和，
  与全部订单行的 committed_qty − dispatched_qty − refund_restocked_qty 之和；
- 全部预留的状态与它们订单行的 quantity、committed_qty，及订单行所指的 SKU 是否已合并
  （只看 skus 表的 merged_into_sku_id 非空，不读合并单）；
- 平台入口的配额行，与按 (所属平台订单的 channel_id, sku_id) 聚合的平台订单行：
  所属平台订单为 unpaid 或 held、sku_id 非空的 quantity − cancelled_qty 之和。
流水与订单行都有外键指向余额行，所以从余额行出发不会漏；没有流水或订单行的余额行按全 0 比对。
第 6 项从配额行出发：没有平台订单行的配额行按 0 比对；落在没有配额行的入口与 SKU 上的
平台订单行不报（契约 §4.4 只比对每个配额行）。
比对、排序与截断用 app/domain/invariants.py。
"""

from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import datetime

from sqlalchemy import Connection, and_, case, func, select, text

from app.domain.invariants import (
    PENDING_PLATFORM_STAGES,
    QUANTITY_NAMES,
    BalanceRow,
    ChannelPendingRow,
    LedgerTotals,
    OrderTotals,
    ReservationLines,
    check_balance_row,
    check_channel_pending_vs_orders,
    check_reservation,
    summarize_violations,
)
from app.domain.invariants import OrderLine as LineCommit
from app.domain.reservations import ACTIVE
from app.models.catalog import Sku
from app.models.inventory import LedgerEntry, StockBalance
from app.models.orders import OrderLine, Reservation
from app.models.platform import ChannelAllocation, PlatformOrder, PlatformOrderLine
from app.services.reservations import database_now

# 只对下一个事务生效；START TRANSACTION 开启时即取读快照，不等第一次读表。
_SNAPSHOT_ISOLATION = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
_START_SNAPSHOT = "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"

_BALANCE_COLUMNS = tuple(getattr(StockBalance, field.name) for field in fields(BalanceRow))
_LEDGER_NAMES = tuple(field.name for field in fields(LedgerTotals))
_ORDER_NAMES = tuple(field.name for field in fields(OrderTotals))
_CHANNEL_COLUMNS = tuple(
    getattr(ChannelAllocation, field.name) for field in fields(ChannelPendingRow)
)

# (pool_id, sku_id)：一个余额行。
BalanceKey = tuple[str, str]
# (channel_id, sku_id)：一个平台入口的配额行。
ChannelKey = tuple[str, str]


@dataclass(frozen=True)
class InvariantReport:
    """一次自查的结果：violations 已排序，至多 100 项；violation_count 是总数。"""

    checked_at: datetime
    violation_count: int
    violations: list[dict]


def _begin_snapshot(connection: Connection) -> datetime:
    connection.execute(text(_SNAPSHOT_ISOLATION))
    connection.execute(text(_START_SNAPSHOT))
    return database_now(connection)


def _read_balances(connection: Connection, tenant_id: str) -> list[BalanceRow]:
    statement = select(*_BALANCE_COLUMNS).where(StockBalance.tenant_id == tenant_id)
    return [BalanceRow(**row) for row in connection.execute(statement).mappings()]


def _read_ledger_totals(connection: Connection, tenant_id: str) -> dict[BalanceKey, LedgerTotals]:
    sums = [func.sum(getattr(LedgerEntry, f"{name}_delta")).label(name) for name in QUANTITY_NAMES]
    statement = (
        select(
            LedgerEntry.pool_id,
            LedgerEntry.sku_id,
            *sums,
            func.count().label("entry_count"),
            func.max(LedgerEntry.balance_version_after).label("max_balance_version_after"),
        )
        .where(LedgerEntry.tenant_id == tenant_id)
        .group_by(LedgerEntry.pool_id, LedgerEntry.sku_id)
    )
    found: dict[BalanceKey, LedgerTotals] = {}
    for row in connection.execute(statement).mappings():
        # BIGINT 的 SUM 在 MySQL 里是 DECIMAL，换回整数。
        values = {name: int(row[name]) for name in _LEDGER_NAMES}
        found[(row["pool_id"], row["sku_id"])] = LedgerTotals(**values)
    return found


def _read_order_totals(connection: Connection, tenant_id: str) -> dict[BalanceKey, OrderTotals]:
    active_quantity = case((Reservation.status == ACTIVE, OrderLine.quantity), else_=0)
    pending = OrderLine.committed_qty - OrderLine.dispatched_qty - OrderLine.refund_restocked_qty
    statement = (
        select(
            OrderLine.pool_id,
            OrderLine.sku_id,
            func.sum(active_quantity).label("active_reserved"),
            func.sum(pending).label("pending_dispatch"),
        )
        .join(
            Reservation,
            and_(
                Reservation.tenant_id == OrderLine.tenant_id,
                Reservation.reservation_id == OrderLine.reservation_id,
            ),
        )
        .where(OrderLine.tenant_id == tenant_id)
        .group_by(OrderLine.pool_id, OrderLine.sku_id)
    )
    found: dict[BalanceKey, OrderTotals] = {}
    for row in connection.execute(statement).mappings():
        values = {name: int(row[name]) for name in _ORDER_NAMES}
        found[(row["pool_id"], row["sku_id"])] = OrderTotals(**values)
    return found


def _read_reservations(connection: Connection, tenant_id: str) -> list[ReservationLines]:
    lines: dict[str, list[LineCommit]] = defaultdict(list)
    statement = select(
        OrderLine.reservation_id,
        OrderLine.line_ref,
        OrderLine.quantity,
        OrderLine.committed_qty,
        Sku.merged_into_sku_id.is_not(None).label("sku_merged"),
    )
    # 外连接：订单行不因找不到 SKU 行而少读，找不到的按未合并。
    statement = statement.outerjoin(
        Sku, and_(Sku.tenant_id == OrderLine.tenant_id, Sku.sku_id == OrderLine.sku_id)
    )
    statement = statement.where(OrderLine.tenant_id == tenant_id)
    for row in connection.execute(statement).mappings():
        line = LineCommit(
            row["line_ref"], row["quantity"], row["committed_qty"], bool(row["sku_merged"])
        )
        lines[row["reservation_id"]].append(line)
    statement = select(Reservation.reservation_id, Reservation.order_ref, Reservation.status)
    statement = statement.where(Reservation.tenant_id == tenant_id)
    return [
        ReservationLines(
            row["reservation_id"],
            row["order_ref"],
            row["status"],
            tuple(lines[row["reservation_id"]]),
        )
        for row in connection.execute(statement).mappings()
    ]


def _read_channel_pending(connection: Connection, tenant_id: str) -> list[ChannelPendingRow]:
    statement = select(*_CHANNEL_COLUMNS).where(ChannelAllocation.tenant_id == tenant_id)
    return [ChannelPendingRow(**row) for row in connection.execute(statement).mappings()]


def _read_platform_open_quantities(connection: Connection, tenant_id: str) -> dict[ChannelKey, int]:
    remaining = PlatformOrderLine.quantity - PlatformOrderLine.cancelled_qty
    statement = (
        select(
            PlatformOrder.channel_id,
            PlatformOrderLine.sku_id,
            func.sum(remaining).label("open_quantity"),
        )
        .join(
            PlatformOrder,
            and_(
                PlatformOrder.tenant_id == PlatformOrderLine.tenant_id,
                PlatformOrder.platform_order_row_id == PlatformOrderLine.platform_order_row_id,
            ),
        )
        .where(
            PlatformOrderLine.tenant_id == tenant_id,
            PlatformOrderLine.sku_id.is_not(None),
            PlatformOrder.stage.in_(PENDING_PLATFORM_STAGES),
        )
        .group_by(PlatformOrder.channel_id, PlatformOrderLine.sku_id)
    )
    return {
        (row["channel_id"], row["sku_id"]): int(row["open_quantity"])
        for row in connection.execute(statement).mappings()
    }


def check_invariants(connection: Connection, tenant_id: str) -> InvariantReport:
    """开读快照、读本租户的数据，逐项比对后排序截断。"""
    checked_at = _begin_snapshot(connection)
    ledgers = _read_ledger_totals(connection, tenant_id)
    orders = _read_order_totals(connection, tenant_id)
    violations: list[dict] = []
    for row in _read_balances(connection, tenant_id):
        key = (row.pool_id, row.sku_id)
        ledger = ledgers.get(key, LedgerTotals())
        violations.extend(check_balance_row(row, ledger, orders.get(key, OrderTotals())))
    for reservation in _read_reservations(connection, tenant_id):
        violations.extend(check_reservation(reservation))
    open_quantities = _read_platform_open_quantities(connection, tenant_id)
    for channel_row in _read_channel_pending(connection, tenant_id):
        open_quantity = open_quantities.get((channel_row.channel_id, channel_row.sku_id), 0)
        violation = check_channel_pending_vs_orders(channel_row, open_quantity)
        if violation is not None:
            violations.append(violation)
    violation_count, shown = summarize_violations(violations)
    return InvariantReport(checked_at, violation_count, shown)
