import dataclasses
import unittest

from app.domain.quantities import InvariantViolation, Quantities


class AvailableTest(unittest.TestCase):
    def test_available_is_balance_minus_reserved_minus_unavailable(self):
        # 契约 §4.1：可售量只能派生；待发货量已从 stock_balance 扣出，不再减。
        self.assertEqual(Quantities(20, 3, 2, 5).available, 15)

    def test_available_cannot_be_written(self):
        # 可写的可售量会和四个账面量对不上，网店就会按错的数卖。
        q = Quantities(20, 0, 0, 0)
        with self.assertRaises(AttributeError):
            q.available = 5
        with self.assertRaises(TypeError):
            Quantities(20, 0, 0, 0, available=5)

    def test_quantities_are_immutable(self):
        # 余额只能由流水规则产生新值，不能在原对象上改。
        q = Quantities(20, 0, 0, 0)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            q.stock_balance = 30


class InvariantTest(unittest.TestCase):
    def test_reserved_plus_unavailable_may_equal_balance(self):
        self.assertEqual(Quantities(5, 3, 2, 0).available, 0)

    def test_reserved_plus_unavailable_above_balance_is_rejected(self):
        with self.assertRaises(InvariantViolation) as caught:
            Quantities(5, 3, 3, 0)
        # 异常带上本想写入的四个量，上层报错时不用再猜。
        self.assertEqual(
            (
                caught.exception.stock_balance,
                caught.exception.reserved,
                caught.exception.unavailable,
                caught.exception.paid_pending_dispatch,
            ),
            (5, 3, 3, 0),
        )

    def test_each_quantity_must_be_non_negative(self):
        for values in ((-1, 0, 0, 0), (5, -1, 0, 0), (5, 0, -1, 0), (5, 0, 0, -1)):
            with self.subTest(values=values), self.assertRaises(InvariantViolation):
                Quantities(*values)

    def test_each_quantity_must_be_an_integer(self):
        # 计量单位是整数件（§4.1）；True 不能被当成 1 件悄悄入账。
        for bad in (1.0, 0.5, True, "1", None):
            for index in range(4):
                values = [10, 0, 0, 0]
                values[index] = bad
                with self.subTest(values=values), self.assertRaises(InvariantViolation):
                    Quantities(*values)


if __name__ == "__main__":
    unittest.main()
