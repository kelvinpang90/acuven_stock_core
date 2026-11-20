"""流水类型对四个账面量的增减规则（契约 §4.1、§4.3、§6、§6.1、§6.5、§6.7）。

每个函数收旧账面量、返回新账面量，不改原值；结果违反不变量时抛 InvariantViolation。
另有余额行两个判定列（last_book_only_version、last_reconciled_version）的默认更新规则、
不可售调整的上限检查与更正的判定。
SKU 合并迁移（MERGE_TRANSFER_OUT/IN）的落账规则不在这里，见 app/domain/sku_merges.py。
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


class CorrectionBreaksInvariants(InvariantViolation):
    """更正落账后会违反不变量（§6.7 第 3 步，409 CORRECTION_BREAKS_INVARIANTS）。

    四个量是落账后的值（可能为负），shortfall 的公式同 CountBelowCommitments，此时总大于 0。
    """

    def __init__(self, *, shortfall, **after):
        super().__init__(f"correction would break invariants, shortfall {shortfall}", **after)
        self.shortfall = shortfall


class AdjustmentExceedsLimit(ValueError):
    """不可售调整的件数超过这一行当前上限（§6.5）。

    这是命令事先的上限检查，不是落账后违反不变量，所以不继承 InvariantViolation。
    code 为 INSUFFICIENT_STOCK 或 QUANTITY_EXCEEDS_UNAVAILABLE，limit 是这一行当前最多能做的件数。
    """

    def __init__(self, *, code, limit, quantity):
        super().__init__(f"{code}: quantity {quantity} exceeds limit {limit}")
        self.code = code
        self.limit = limit
        self.quantity = quantity


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


def _shortfall(stock_balance, reserved, unavailable):
    """§6 的缺口数；reserved 不为负（盘点与更正都不动它）。"""
    return max(0, reserved + unavailable - stock_balance, -stock_balance, -unavailable)


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
    shortfall = _shortfall(new_balance, latest.reserved, new_unavailable)
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


# §4.1 的两个判定列。只改账的流水不按件数落账、或把账面另行定数，
# 写它们时 last_book_only_version 设为这条流水的版本（§6 的 COUNT_STALE 要认出它）。
BOOK_ONLY_TYPES = frozenset(
    {
        EntryType.OPENING_BALANCE,
        EntryType.COUNT_ADJUSTMENT,
        EntryType.CORRECTION,
        EntryType.DEMO_RESET,
        EntryType.MERGE_TRANSFER_OUT,
        EntryType.MERGE_TRANSFER_IN,
    }
)

# 定数类流水把账面对到实数或另行定数，吸收更早的错误；更正只反向一条流水、不核实账面（0.10）。
RECONCILING_TYPES = BOOK_ONLY_TYPES - {EntryType.CORRECTION}


def last_book_only_version_after(entry_type: EntryType, current: int, version_after: int) -> int:
    """写一条 entry_type 流水（版本 version_after）后 last_book_only_version 的默认新值。

    合并执行与 Demo 重置差额为 0 不写流水时的取值由服务层直接设（§6.1、§2.4）。
    """
    return version_after if entry_type in BOOK_ONLY_TYPES else current


def last_reconciled_version_after(entry_type: EntryType, current: int, version_after: int) -> int:
    """写一条 entry_type 流水（版本 version_after）后 last_reconciled_version 的默认新值。

    盘点确认取快照版本、差额为 0 的合并执行与 Demo 重置取当时的 balance_version，
    这些例外由服务层传入覆盖值（§4.1、§6.6 确认第 6 步）。
    """
    return version_after if entry_type in RECONCILING_TYPES else current


def is_stale(last_book_only_version: int, snapshot_version: int) -> bool:
    """快照之后写过只改账的流水（§6 的 COUNT_STALE、§2.4 合并执行的前置检查）。"""
    return last_book_only_version > snapshot_version


class AdjustmentAction(StrEnum):
    """§6.5 不可售调整的三种动作；值是请求里 action 的取值。"""

    MARK_UNAVAILABLE = "mark_unavailable"
    RESTORE_AVAILABLE = "restore_available"
    WRITE_OFF = "write_off"


# 每种动作写的流水类型与超上限时的错误码（§6.5）。
ADJUSTMENT_RULES: dict[AdjustmentAction, tuple[EntryType, str]] = {
    AdjustmentAction.MARK_UNAVAILABLE: (EntryType.MARK_UNAVAILABLE, "INSUFFICIENT_STOCK"),
    AdjustmentAction.RESTORE_AVAILABLE: (
        EntryType.RESTORE_AVAILABLE,
        "QUANTITY_EXCEEDS_UNAVAILABLE",
    ),
    AdjustmentAction.WRITE_OFF: (EntryType.WRITE_OFF, "QUANTITY_EXCEEDS_UNAVAILABLE"),
}


def _adjustment_rule(action):
    rule = ADJUSTMENT_RULES.get(action)
    if rule is None:
        raise ValueError(f"unknown adjustment action {action!r}")
    return rule


def adjustment_limit(action: AdjustmentAction, current: Quantities) -> int:
    """这一行当前最多能做的件数：标不可售看 available，恢复与报损看 unavailable。"""
    _adjustment_rule(action)
    if action == AdjustmentAction.MARK_UNAVAILABLE:
        return current.available
    return current.unavailable


def apply_adjustment(action: AdjustmentAction, current: Quantities, q: int) -> Quantities:
    """§6.5：先查 q 是正整数、不超过上限，再按 §4.3 落账。

    超过上限时抛 AdjustmentExceedsLimit，带错误码与上限。
    """
    entry_type, code = _adjustment_rule(action)
    _require_positive(q)
    limit = adjustment_limit(action, current)
    if q > limit:
        raise AdjustmentExceedsLimit(code=code, limit=limit, quantity=q)
    return apply_entry(entry_type, current, q)


# §6.7 第 1 步：能更正的类型。
CORRECTABLE_TYPES = frozenset(
    {
        EntryType.OPENING_BALANCE,
        EntryType.RECEIPT,
        EntryType.MARK_UNAVAILABLE,
        EntryType.RESTORE_AVAILABLE,
        EntryType.WRITE_OFF,
    }
)


def correction_blocker(
    entry_type: EntryType, entry_version: int, last_reconciled_version: int, pool_live: bool
) -> str | None:
    """按 §6.7 第 1、2 步的顺序判原流水能否更正；能则 None，否则返回 details.reason。

    entry_version 是原流水的版本，last_reconciled_version 取锁住的余额行上的值。
    """
    if entry_type not in CORRECTABLE_TYPES:
        return "entry_type"
    is_opening = entry_type == EntryType.OPENING_BALANCE
    if is_opening and pool_live:
        return "pool_live"
    # 等于 v 的期初是它自己设的这一列；其余可更正类型不写这一列，等于 v 只能是核实设的。
    if last_reconciled_version > entry_version or (
        last_reconciled_version == entry_version and not is_opening
    ):
        return "superseded"
    return None


def apply_correction(current: Quantities, deltas: tuple[int, int, int, int]) -> Quantities:
    """§6.7 第 3 步：四个量各减去原流水的增量。

    deltas 是原流水 (stock_balance, reserved, unavailable, paid_pending_dispatch) 的增量，可为负。
    能更正的流水都不动 reserved 与 paid_pending_dispatch，
    这两个增量不为 0 是程序错误，抛 ValueError。
    落账后违反不变量时抛 CorrectionBreaksInvariants，带落账后的四个量与缺口数。
    """
    d_balance, d_reserved, d_unavailable, d_ppd = deltas
    if d_reserved != 0 or d_ppd != 0:
        raise ValueError(
            f"correctable entries never change reserved or paid_pending_dispatch: {deltas}"
        )
    new_balance = current.stock_balance - d_balance
    new_unavailable = current.unavailable - d_unavailable
    shortfall = _shortfall(new_balance, current.reserved, new_unavailable)
    if shortfall > 0:
        raise CorrectionBreaksInvariants(
            shortfall=shortfall,
            stock_balance=new_balance,
            reserved=current.reserved,
            unavailable=new_unavailable,
            paid_pending_dispatch=current.paid_pending_dispatch,
        )
    return Quantities(new_balance, current.reserved, new_unavailable, current.paid_pending_dispatch)
