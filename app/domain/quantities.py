"""四个账面量与不变量（契约 §4.1）。

每个 `tenant × pool × sku` 一份账面：`stock_balance`、`reserved`、`unavailable`、
`paid_pending_dispatch`；`available` 由前三者派生，不是可写字段。
账面是不可变值对象：任何变化都产生一份新账面，新账面在构造时校验不变量，
违反就抛 `InvariantViolation`，调用方拿不到半改的结果。
"""

from dataclasses import dataclass


class InvalidQuantity(Exception):
    """流水数量、盘点实数或重置目标不是规定的整数（契约 §4.1：计量单位为整数件）。"""


class InvariantViolation(Exception):
    """变动后的账面违反 §4.1 不变量；带上变动后的四个量和缺口数，供 409 的 details 使用。

    缺口数 = max(0, `reserved` + `unavailable` − `stock_balance`, 任一量的相反数)，
    任一项违反时都大于 0。盘点只改 `stock_balance` 与 `unavailable`，另两个量取自
    合法的最新账面、不为负，所以盘点时它就是 §6 `COUNT_BELOW_COMMITMENTS` 的缺口数。
    四个量不全是整数时缺口数为 None。
    """

    def __init__(self, stock_balance, reserved, unavailable, paid_pending_dispatch):
        self.stock_balance = stock_balance
        self.reserved = reserved
        self.unavailable = unavailable
        self.paid_pending_dispatch = paid_pending_dispatch
        values = (stock_balance, reserved, unavailable, paid_pending_dispatch)
        self.shortfall = (
            max(0, reserved + unavailable - stock_balance, *(-value for value in values))
            if all(is_strict_int(value) for value in values)
            else None
        )
        super().__init__(
            "stock invariant violated: "
            f"stock_balance={stock_balance!r} reserved={reserved!r} "
            f"unavailable={unavailable!r} paid_pending_dispatch={paid_pending_dispatch!r} "
            f"shortfall={self.shortfall!r}"
        )


def is_strict_int(value) -> bool:
    """bool 是 int 的子类，但 True 不是“1 件”：数量一律拒绝 bool。"""
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class Balances:
    stock_balance: int
    reserved: int
    unavailable: int
    paid_pending_dispatch: int

    def __post_init__(self):
        values = (self.stock_balance, self.reserved, self.unavailable, self.paid_pending_dispatch)
        if (
            not all(is_strict_int(value) and value >= 0 for value in values)
            or self.reserved + self.unavailable > self.stock_balance
        ):
            raise InvariantViolation(*values)

    @property
    def available(self) -> int:
        return self.stock_balance - self.reserved - self.unavailable

    def shift(
        self,
        stock_balance: int = 0,
        reserved: int = 0,
        unavailable: int = 0,
        paid_pending_dispatch: int = 0,
    ) -> "Balances":
        """把四个增量加到这份账面上，返回新账面；结果违反不变量时抛 `InvariantViolation`。"""
        return Balances(
            self.stock_balance + stock_balance,
            self.reserved + reserved,
            self.unavailable + unavailable,
            self.paid_pending_dispatch + paid_pending_dispatch,
        )
