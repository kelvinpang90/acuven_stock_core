"""流水类型对四个账面量的增减规则（契约 §4.3、§6、§6.1）。

每个函数收旧账面量、返回新账面量，不改原值；结果违反不变量时抛 InvariantViolation。
SKU 合并迁移（MERGE_TRANSFER_OUT/IN）与 CORRECTION 不在这里，随合并与更正任务加。
"""

from enum import StrEnum

from app.domain.quantities import InvariantViolation, Quantities, is_non_negative_int


class InvalidQuantity(ValueError):
    """命令里的数量无效：q 不是正整数，或盘点实数、Demo target 不是非负整数。"""


class CountBelowCommitments(InvariantViolation):
    """盘点落账后会违反不变量（§6，409 COUNT_BELOW_COMMITMENTS）。

    四个量是落账后的值（可能为负），shortfall 是 §6 的缺口数，此时总大于 0。
    """

    def __init__(self, *, shortfall, **after):
        super().__init__(f"count would break invariants, shortfall {shortfall}", **after)
        self.shortfall = shortfall


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
    DEMO_RESET = "DEMO_RESET"


# §4.3 表的照抄：每种按件数 q 落账的流水，对
# (stock_balance, reserved, unavailable, paid_pending_dispatch) 各乘的系数。
QUANTITY_RULES: dict[EntryType, tuple[int, int, int, int]] = {
    EntryType.OPENING_BALANCE: (+1, 0, 0, 0),
    EntryType.RECEIPT: (+1, 0, 0, 0),
    EntryType.MARK_UNAVAILABLE: (0, 0, +1, 0),
    EntryType.RESTORE_AVAILABLE: (0, 0, -1, 0),
    EntryType.WRITE_OFF: (-1, 0, -1, 0),
    EntryType.RESERVE: (0, +1, 0, 0),
    EntryType.RELEASE: (0, -1, 0, 0),
    EntryType.SALE_COMMIT: (-1, -1, 0, +1),
    EntryType.LATE_SALE_COMMIT: (-1, 0, 0, +1),
    EntryType.DISPATCH: (0, 0, 0, -1),
    EntryType.REFUND_RESTOCK: (+1, 0, 0, -1),
    EntryType.RETURN_SELLABLE: (+1, 0, 0, 0),
    EntryType.RETURN_UNSELLABLE: (+1, 0, +1, 0),
}


def _require_positive(q):
    if not is_non_negative_int(q) or q == 0:
        raise InvalidQuantity(f"quantity must be a positive integer, got {q!r}")


def _require_non_negative(name, value):
    if not is_non_negative_int(value):
        raise InvalidQuantity(f"{name} must be a non-negative integer, got {value!r}")


def apply_entry(entry_type: EntryType, current: Quantities, q: int) -> Quantities:
    """按 §4.3 把 q 件的流水加到 current 上。

    盘点与 Demo 重置不按 q 落账，分别用 apply_count_adjustment、apply_demo_reset。
    """
    coefficients = QUANTITY_RULES.get(entry_type)
    if coefficients is None:
        raise ValueError(f"{entry_type} is not applied by quantity")
    _require_positive(q)
    sb, r, u, ppd = coefficients
    return Quantities(
        current.stock_balance + sb * q,
        current.reserved + r * q,
        current.unavailable + u * q,
        current.paid_pending_dispatch + ppd * q,
    )


def count_deltas(snapshot: Quantities, shelf_count: int, unsellable_count: int) -> tuple[int, int]:
    """§6 差额法：返回 (d_shelf, d_u)。

    在架件数含已付款待发的件，所以与快照的
    stock_balance − unavailable + paid_pending_dispatch 比。
    """
    _require_non_negative("shelf_count", shelf_count)
    _require_non_negative("unsellable_count", unsellable_count)
    snapshot_shelf = snapshot.stock_balance - snapshot.unavailable + snapshot.paid_pending_dispatch
    return shelf_count - snapshot_shelf, unsellable_count - snapshot.unavailable


def apply_count_adjustment(
    latest: Quantities, snapshot: Quantities, shelf_count: int, unsellable_count: int
) -> Quantities:
    """COUNT_ADJUSTMENT：把提交实数时相对快照的差额加到确认时的最新账面上（§6）。

    stock_balance 增 d_shelf + d_u，unavailable 增 d_u；reserved 与 paid_pending_dispatch 不动。
    落账后违反不变量时抛 CountBelowCommitments，带落账后的四个量与缺口数。
    """
    d_shelf, d_u = count_deltas(snapshot, shelf_count, unsellable_count)
    new_balance = latest.stock_balance + d_shelf + d_u
    new_unavailable = latest.unavailable + d_u
    shortfall = max(
        0,
        latest.reserved + new_unavailable - new_balance,
        -new_balance,
        -new_unavailable,
    )
    if shortfall > 0:
        raise CountBelowCommitments(
            shortfall=shortfall,
            stock_balance=new_balance,
            reserved=latest.reserved,
            unavailable=new_unavailable,
            paid_pending_dispatch=latest.paid_pending_dispatch,
        )
    return Quantities(new_balance, latest.reserved, new_unavailable, latest.paid_pending_dispatch)


def apply_demo_reset(current: Quantities, target: int) -> Quantities:
    """DEMO_RESET（§6.1）：stock_balance 拉到 max(target, reserved + unavailable)，其余不动。

    增量可以为 0（是否写流水由上层决定，§6.1 规定不写）；target 须为非负整数。
    """
    _require_non_negative("target", target)
    new_balance = max(target, current.reserved + current.unavailable)
    return Quantities(
        new_balance, current.reserved, current.unavailable, current.paid_pending_dispatch
    )
