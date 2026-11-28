"""不变量自查不碰数据库的比对规则（契约 §4.4）。

读快照、聚合由上层在数据库里做；这里把聚合结果逐项比对成违反项，再排序截断。
违反项是可直接放进响应 violations 的 dict。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.platform_events import HELD, UNPAID
from app.domain.reservations import ACTIVE, COMMITTED, EXPIRED, LATE_PAYMENT_REVIEW

LEDGER_TOTALS = "ledger_totals"
RESERVED_VS_ORDERS = "reserved_vs_orders"
PENDING_DISPATCH_VS_ORDERS = "pending_dispatch_vs_orders"
ORDER_COMMIT_STATE = "order_commit_state"
MERGED_SKU_LINES = "merged_sku_lines"
CHANNEL_PENDING_VS_ORDERS = "channel_pending_vs_orders"
# 按契约的编号顺序；违反项先按这个顺序排。
CHECKS = (
    LEDGER_TOTALS,
    RESERVED_VS_ORDERS,
    PENDING_DISPATCH_VS_ORDERS,
    ORDER_COMMIT_STATE,
    MERGED_SKU_LINES,
    CHANNEL_PENDING_VS_ORDERS,
)

# 第 5 项查的预留状态：合并时订单行要改挂到规范 SKU 的那些（§2.4 第 1 步）。
MERGE_FOLLOWING_STATUSES = (ACTIVE, EXPIRED, LATE_PAYMENT_REVIEW, COMMITTED)

# 第 6 项计入的平台订单阶段：已售未扣的那些（§5.10）；committed 与 cancelled 不计。
PENDING_PLATFORM_STAGES = (UNPAID, HELD)

MAX_VIOLATIONS = 100

QUANTITY_NAMES = ("stock_balance", "reserved", "unavailable", "paid_pending_dispatch")

_CHECK_ORDER = {check: index for index, check in enumerate(CHECKS)}


@dataclass(frozen=True)
class BalanceRow:
    """余额行上的值。"""

    pool_id: str
    sku_id: str
    stock_balance: int
    reserved: int
    unavailable: int
    paid_pending_dispatch: int
    balance_version: int


@dataclass(frozen=True)
class LedgerTotals:
    """一个余额行全部流水的合计；没有流水为全 0（默认值）。

    四个量是各自增量之和，entry_count 是流水条数，
    max_balance_version_after 是最大的 balance_version_after。
    """

    stock_balance: int = 0
    reserved: int = 0
    unavailable: int = 0
    paid_pending_dispatch: int = 0
    entry_count: int = 0
    max_balance_version_after: int = 0


@dataclass(frozen=True)
class OrderTotals:
    """落在一个余额行（同池、同 sku_id）的订单行合计；没有订单行为全 0（默认值）。

    active_reserved：所属预留为 active 的订单行的 quantity 之和；
    pending_dispatch：全部订单行的 committed_qty − dispatched_qty − refund_restocked_qty 之和。
    """

    active_reserved: int = 0
    pending_dispatch: int = 0


@dataclass(frozen=True)
class OrderLine:
    """sku_merged：所指的 SKU 的 merged_into_sku_id 非空。"""

    line_ref: str
    quantity: int
    committed_qty: int
    sku_merged: bool = False


@dataclass(frozen=True)
class ReservationLines:
    """一个预留及其订单行。"""

    reservation_id: str
    order_ref: str
    status: str
    lines: tuple[OrderLine, ...]


@dataclass(frozen=True)
class ChannelPendingRow:
    """平台入口配额行上的已售未扣。"""

    channel_id: str
    pool_id: str
    sku_id: str
    pending: int


def _quantities(source) -> dict:
    return {name: getattr(source, name) for name in QUANTITY_NAMES}


def _balance_violation(check: str, row: BalanceRow, expected, actual) -> dict:
    return {
        "check": check,
        "pool_id": row.pool_id,
        "sku_id": row.sku_id,
        "expected": expected,
        "actual": actual,
    }


def check_ledger_totals(row: BalanceRow, ledger: LedgerTotals) -> dict | None:
    """第 1 项：四个量各等于流水合计，流水条数与最大的 balance_version_after 都等于版本。

    任何一处不等都报一项；expected、actual 的键名照契约 §4.4。
    """
    if (
        _quantities(ledger) == _quantities(row)
        and ledger.entry_count == row.balance_version
        and ledger.max_balance_version_after == row.balance_version
    ):
        return None
    expected = _quantities(ledger)
    expected["entry_count"] = ledger.entry_count
    expected["max_balance_version_after"] = ledger.max_balance_version_after
    actual = _quantities(row)
    actual["balance_version"] = row.balance_version
    return _balance_violation(LEDGER_TOTALS, row, expected, actual)


def check_reserved_vs_orders(row: BalanceRow, orders: OrderTotals) -> dict | None:
    """第 2 项：reserved 等于 active 预留的订单行 quantity 之和。"""
    if orders.active_reserved == row.reserved:
        return None
    return _balance_violation(RESERVED_VS_ORDERS, row, orders.active_reserved, row.reserved)


def check_pending_dispatch_vs_orders(row: BalanceRow, orders: OrderTotals) -> dict | None:
    """第 3 项：paid_pending_dispatch 等于订单行的未发未回库件数之和。"""
    if orders.pending_dispatch == row.paid_pending_dispatch:
        return None
    return _balance_violation(
        PENDING_DISPATCH_VS_ORDERS, row, orders.pending_dispatch, row.paid_pending_dispatch
    )


def check_balance_row(row: BalanceRow, ledger: LedgerTotals, orders: OrderTotals) -> list[dict]:
    """一个余额行的前三项，按编号顺序，只含违反的。"""
    results = (
        check_ledger_totals(row, ledger),
        check_reserved_vs_orders(row, orders),
        check_pending_dispatch_vs_orders(row, orders),
    )
    return [violation for violation in results if violation is not None]


def check_order_commit_state(reservation: ReservationLines) -> dict | None:
    """第 4 项：committed 时每行 committed_qty = quantity，其他状态时每行 committed_qty = 0。"""
    if reservation.status == COMMITTED:
        bad = [line for line in reservation.lines if line.committed_qty != line.quantity]
    else:
        bad = [line for line in reservation.lines if line.committed_qty != 0]
    return _reservation_violation(ORDER_COMMIT_STATE, reservation, bad)


def check_merged_sku_lines(reservation: ReservationLines) -> dict | None:
    """第 5 项：MERGE_FOLLOWING_STATUSES 里的预留，订单行不指向已合并的 SKU。"""
    if reservation.status not in MERGE_FOLLOWING_STATUSES:
        return None
    bad = [line for line in reservation.lines if line.sku_merged]
    return _reservation_violation(MERGED_SKU_LINES, reservation, bad)


def _reservation_violation(
    check: str, reservation: ReservationLines, bad: list[OrderLine]
) -> dict | None:
    if not bad:
        return None
    return {
        "check": check,
        "reservation_id": reservation.reservation_id,
        "order_ref": reservation.order_ref,
        "status": reservation.status,
        "line_refs": sorted(line.line_ref for line in bad),
    }


def check_reservation(reservation: ReservationLines) -> list[dict]:
    """一个预留的第 4、5 项，按编号顺序，只含违反的。"""
    results = (check_order_commit_state(reservation), check_merged_sku_lines(reservation))
    return [violation for violation in results if violation is not None]


def check_channel_pending_vs_orders(row: ChannelPendingRow, open_quantity: int) -> dict | None:
    """第 6 项：pending 等于这个入口上这个 SKU 的 unpaid、held 平台订单行的剩余件数之和。"""
    if open_quantity == row.pending:
        return None
    return {
        "check": CHANNEL_PENDING_VS_ORDERS,
        "pool_id": row.pool_id,
        "sku_id": row.sku_id,
        "expected": open_quantity,
        "actual": row.pending,
        "channel_id": row.channel_id,
    }


def _sort_key(violation: dict) -> tuple:
    # 只有第 6 项有 channel_id，前三项与第 6 项没有 reservation_id，第 4、5 项没有 pool_id、
    # sku_id；缺的按空串，同一项内不受影响。一个入口只绑一个池，第 6 项按 channel_id 排之后
    # pool_id 不再改变先后。
    return (
        _CHECK_ORDER[violation["check"]],
        violation.get("channel_id", ""),
        violation.get("pool_id", ""),
        violation.get("sku_id", ""),
        violation.get("reservation_id", ""),
    )


def summarize_violations(violations: Iterable[dict]) -> tuple[int, list[dict]]:
    """违反项排序截断，返回总数与至多 100 项。

    按 check 的编号，再按 pool_id、sku_id（前三项）、reservation_id（第 4、5 项）
    或 channel_id、sku_id（第 6 项）升序。
    """
    ordered = sorted(violations, key=_sort_key)
    return len(ordered), ordered[:MAX_VIOLATIONS]
