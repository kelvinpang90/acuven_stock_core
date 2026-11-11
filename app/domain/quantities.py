"""库存账面量与不变量（契约 §4.1）。

每个 tenant × pool × sku 一行四个账面量；可售量由它们派生，不能写。
任何构造出来的账面量都已满足不变量：违反时直接抛 InvariantViolation，
调用方拿不到半改的结果。
"""

from dataclasses import dataclass


class InvariantViolation(ValueError):
    """账面量违反 §4.1 不变量。

    即任一量为负或不是整数，或 reserved + unavailable > stock_balance。
    超量操作（标不可售超过可售、恢复或报损超过不可售量、释放超过预留、
    发货超过待发货量…）都落到这里。带上本想写入的四个量，供上层报错与排查。
    """

    def __init__(self, message, *, stock_balance, reserved, unavailable, paid_pending_dispatch):
        super().__init__(message)
        self.stock_balance = stock_balance
        self.reserved = reserved
        self.unavailable = unavailable
        self.paid_pending_dispatch = paid_pending_dispatch


def is_non_negative_int(value) -> bool:
    # bool 是 int 的子类，True 不能当 1 件。
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class Quantities:
    """四个账面量（不可变）。

    字段顺序与契约示例表一致：stock_balance / reserved / unavailable / paid_pending_dispatch。
    """

    stock_balance: int
    reserved: int
    unavailable: int
    paid_pending_dispatch: int

    def __post_init__(self):
        values = {
            "stock_balance": self.stock_balance,
            "reserved": self.reserved,
            "unavailable": self.unavailable,
            "paid_pending_dispatch": self.paid_pending_dispatch,
        }
        bad = [name for name, value in values.items() if not is_non_negative_int(value)]
        if bad:
            raise InvariantViolation(f"quantities must be non-negative integers: {bad}", **values)
        if self.reserved + self.unavailable > self.stock_balance:
            raise InvariantViolation("reserved + unavailable exceeds stock_balance", **values)

    @property
    def available(self) -> int:
        """可售量：stock_balance − reserved − unavailable，只派生不存储。"""
        return self.stock_balance - self.reserved - self.unavailable
