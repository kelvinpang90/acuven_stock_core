import dataclasses
import unittest

from app.domain.quantities import Balances, InvariantViolation


class BalancesTest(unittest.TestCase):
    def test_available_is_derived_from_balance_reserved_and_unavailable(self):
        # 契约 §4.1：available = stock_balance − reserved − unavailable；
        # 待发货量已扣出账面，不再影响可售。
        self.assertEqual(Balances(20, 3, 2, 5).available, 15)

    def test_available_is_not_a_writable_field(self):
        # 可售量若能单独写入，就会与三个账面量脱节，出现“可售 10 件、账上只有 5 件”。
        with self.assertRaises(TypeError):
            Balances(20, 0, 0, 0, available=99)
        with self.assertRaises(TypeError):
            dataclasses.replace(Balances(20, 0, 0, 0), available=99)
        balances = Balances(20, 0, 0, 0)
        with self.assertRaises(AttributeError):
            balances.available = 99

    def test_balances_are_immutable(self):
        balances = Balances(20, 3, 0, 0)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            balances.reserved = 0
        self.assertEqual(balances, Balances(20, 3, 0, 0))

    def test_accepts_all_zero_and_fully_committed_balances(self):
        # 边界：reserved + unavailable 恰好等于 stock_balance 时可售为 0，仍合法。
        self.assertEqual(Balances(0, 0, 0, 0).available, 0)
        self.assertEqual(Balances(5, 3, 2, 7).available, 0)


class InvariantTest(unittest.TestCase):
    def test_rejects_any_negative_quantity(self):
        for values in ((-1, 0, 0, 0), (5, -1, 0, 0), (5, 0, -1, 0), (5, 0, 0, -1)):
            with self.subTest(values=values), self.assertRaises(InvariantViolation):
                Balances(*values)

    def test_rejects_non_integer_quantities(self):
        # 计量单位是整数件；True 虽然是 int 的子类，也不是“1 件”。
        for values in ((1.0, 0, 0, 0), (5, True, 0, 0), (5, 0, "1", 0), (5, 0, 0, None)):
            with self.subTest(values=values), self.assertRaises(InvariantViolation) as caught:
                Balances(*values)
            self.assertIsNone(caught.exception.shortfall)

    def test_rejects_reserved_plus_unavailable_above_balance(self):
        # 等价于 available < 0：账上 5 件，不能既预留 4 件又标 2 件不可售。
        with self.assertRaises(InvariantViolation) as caught:
            Balances(5, 4, 2, 0)
        self.assertEqual(caught.exception.shortfall, 1)

    def test_violation_reports_the_attempted_quantities(self):
        with self.assertRaises(InvariantViolation) as caught:
            Balances(5, 4, 2, 3)
        error = caught.exception
        self.assertEqual(
            (error.stock_balance, error.reserved, error.unavailable, error.paid_pending_dispatch),
            (5, 4, 2, 3),
        )

    def test_shortfall_is_positive_whenever_any_quantity_is_negative(self):
        # 缺口数为 0 会让调用方以为“没有缺口”，所以任一量为负时缺口都必须大于 0。
        cases = {(-3, 0, 0, 0): 3, (5, 0, -2, 0): 2, (5, -1, 0, 0): 1, (5, 0, 0, -4): 4}
        for values, expected in cases.items():
            with self.subTest(values=values), self.assertRaises(InvariantViolation) as caught:
                Balances(*values)
            self.assertEqual(caught.exception.shortfall, expected)


class ShiftTest(unittest.TestCase):
    def test_shift_returns_new_balances_and_leaves_original_untouched(self):
        before = Balances(20, 3, 0, 0)
        after = before.shift(stock_balance=-3, reserved=-3, paid_pending_dispatch=3)
        self.assertEqual(after, Balances(17, 0, 0, 3))
        self.assertEqual(before, Balances(20, 3, 0, 0))

    def test_violating_shift_raises_and_leaves_original_untouched(self):
        # 违反不变量时不能返回半改的账面：要么整份新账面，要么异常。
        before = Balances(5, 2, 0, 0)
        with self.assertRaises(InvariantViolation):
            before.shift(stock_balance=10, reserved=-3)
        self.assertEqual(before, Balances(5, 2, 0, 0))


if __name__ == "__main__":
    unittest.main()
