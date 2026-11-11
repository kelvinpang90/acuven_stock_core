"""流水类型与它们对四个账面量的增减规则（契约 §4.3、§6、§6.1）。

这里是数据库层与接口层的唯一规则来源：给定当前账面与数量，算出新账面。
新账面违反 §4.1 不变量时抛 `InvariantViolation`：释放超过预留、发货超过待发货量、
标不可售超过可售等超量操作都在这里被拒。
SKU 合并迁移（`MERGE_TRANSFER_OUT/IN`）与 `CORRECTION` 的规则随合并、更正任务再做。
"""

from enum import StrEnum
from types import MappingProxyType

from app.domain.quantities import Balances, InvalidQuantity, is_strict_int


class EntryType(StrEnum):
    OPENING_BALANCE = "OPENING_BALANCE"
    RECEIPT = "RECEIPT"
    COUNT_ADJUSTMENT = "COUNT_ADJUSTMENT"
    MARK_UNAVAILABLE = "MARK_UNAVAILABLE"
    RESTORE_AVAILABLE = "RESTORE_AVAILABLE"
    WRITE_OFF = "WRITE_OFF"
    RESERVE = "RESERVE"
    RELEASE = "RELEASE"
    SALE_COMMIT = "SALE_COMMIT"
    LATE_SALE_COMMIT = "LATE_SALE_COMMIT"
    DISPATCH = "DISPATCH"
    REFUND_RESTOCK = "REFUND_RESTOCK"
    RETURN_SELLABLE = "RETURN_SELLABLE"
    RETURN_UNSELLABLE = "RETURN_UNSELLABLE"
    MERGE_TRANSFER_OUT = "MERGE_TRANSFER_OUT"
    MERGE_TRANSFER_IN = "MERGE_TRANSFER_IN"
    DEMO_RESET = "DEMO_RESET"
    CORRECTION = "CORRECTION"


# §4.3 表：每件 q 对 (stock_balance, reserved, unavailable, paid_pending_dispatch) 的系数。
QUANTITY_RULES = MappingProxyType(
    {
        EntryType.OPENING_BALANCE: (1, 0, 0, 0),
        EntryType.RECEIPT: (1, 0, 0, 0),
        EntryType.MARK_UNAVAILABLE: (0, 0, 1, 0),
        EntryType.RESTORE_AVAILABLE: (0, 0, -1, 0),
        EntryType.WRITE_OFF: (-1, 0, -1, 0),
        EntryType.RESERVE: (0, 1, 0, 0),
        EntryType.RELEASE: (0, -1, 0, 0),
        EntryType.SALE_COMMIT: (-1, -1, 0, 1),
        EntryType.LATE_SALE_COMMIT: (-1, 0, 0, 1),
        EntryType.DISPATCH: (0, 0, 0, -1),
        EntryType.REFUND_RESTOCK: (1, 0, 0, -1),
        EntryType.RETURN_SELLABLE: (1, 0, 0, 0),
        EntryType.RETURN_UNSELLABLE: (1, 0, 1, 0),
    }
)


def apply_entry(balances: Balances, entry_type: EntryType, q: int) -> Balances:
    """按 §4.3 把 q 件的流水加到账面上。

    q 必须是正整数：0 件的流水没有意义，负数会把加减方向反过来。
    """
    if not is_strict_int(q) or q <= 0:
        raise InvalidQuantity(f"{entry_type} quantity must be a positive integer, got {q!r}")
    if entry_type not in QUANTITY_RULES:
        raise ValueError(f"{entry_type} has no fixed per-quantity rule")
    d_balance, d_reserved, d_unavailable, d_pending = QUANTITY_RULES[entry_type]
    return balances.shift(d_balance * q, d_reserved * q, d_unavailable * q, d_pending * q)


def apply_count_adjustment(
    snapshot: Balances, latest: Balances, shelf_count: int, unavailable_count: int
) -> Balances:
    """§6 盘点确认的差额法：差额由提交实数时的快照算出，加到确认时的最新账面上。

    在架件数含已付款待发的件，所以与快照的
    `stock_balance − unavailable + paid_pending_dispatch` 比。
    落账后违反不变量时抛 `InvariantViolation`，带落账后的四个量与缺口数
    （409 `COUNT_BELOW_COMMITMENTS`）。
    """
    for name, value in (("shelf_count", shelf_count), ("unavailable_count", unavailable_count)):
        if not is_strict_int(value) or value < 0:
            raise InvalidQuantity(f"{name} must be a non-negative integer, got {value!r}")
    d_unavailable = unavailable_count - snapshot.unavailable
    d_shelf = shelf_count - (
        snapshot.stock_balance - snapshot.unavailable + snapshot.paid_pending_dispatch
    )
    return latest.shift(stock_balance=d_shelf + d_unavailable, unavailable=d_unavailable)


def apply_demo_reset(balances: Balances, target: int) -> Balances:
    """§6.1 Demo 每日重置：`stock_balance` 拉到 max(target, reserved + unavailable)。

    其余三个量不动；增量可以为 0（调用方不写流水，但在结果里列出）；结果总满足不变量。
    """
    if not is_strict_int(target) or target < 0:
        raise InvalidQuantity(f"target must be a non-negative integer, got {target!r}")
    new_balance = max(target, balances.reserved + balances.unavailable)
    return balances.shift(stock_balance=new_balance - balances.stock_balance)
