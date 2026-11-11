import unittest

from app.domain.ledger import (
    QUANTITY_RULES,
    CountBelowCommitments,
    EntryType,
    InvalidQuantity,
    apply_count_adjustment,
    apply_demo_reset,
    apply_entry,
)
from app.domain.quantities import InvariantViolation, Quantities

E = EntryType
INVALID_QUANTITIES = (0, -1, True, False, 1.0, 1.5, "1", None)


def five(q: Quantities) -> tuple[int, int, int, int, int]:
    """契约示例表的五列：四个账面量加 available。"""
    return (q.stock_balance, q.reserved, q.unavailable, q.paid_pending_dispatch, q.available)


class ContractExampleTest(unittest.TestCase):
    """逐行复算契约 §11.3、§11.5 的示例表：表和规则对不上，说明规则或契约有一边错了。"""

    def test_demo_cutover_table_11_3(self):
        q = apply_entry(E.OPENING_BALANCE, Quantities(0, 0, 0, 0), 20)
        self.assertEqual(five(q), (20, 0, 0, 0, 20))
        q = apply_entry(E.RESERVE, q, 3)
        self.assertEqual(five(q), (20, 3, 0, 0, 17))
        q = apply_entry(E.SALE_COMMIT, q, 3)
        self.assertEqual(five(q), (17, 0, 0, 3, 17))
        q = apply_entry(E.DISPATCH, q, 2)
        self.assertEqual(five(q), (17, 0, 0, 1, 17))
        q = apply_entry(E.REFUND_RESTOCK, q, 1)
        self.assertEqual(five(q), (18, 0, 0, 0, 18))
        q = apply_entry(E.RESERVE, q, 4)
        self.assertEqual(five(q), (18, 4, 0, 0, 14))
        before = q
        q = apply_demo_reset(q, 20)
        self.assertEqual(five(q), (20, 4, 0, 0, 16))
        # 表里写明 DEMO_RESET +2。
        self.assertEqual(q.stock_balance - before.stock_balance, 2)

    def test_real_shop_table_11_5(self):
        q = apply_entry(E.OPENING_BALANCE, Quantities(0, 0, 0, 0), 20)
        self.assertEqual(five(q), (20, 0, 0, 0, 20))
        q = apply_entry(E.MARK_UNAVAILABLE, q, 2)
        self.assertEqual(five(q), (20, 0, 2, 0, 18))
        q = apply_entry(E.SALE_COMMIT, apply_entry(E.RESERVE, q, 3), 3)
        self.assertEqual(five(q), (17, 0, 2, 3, 15))
        q = apply_entry(E.REFUND_RESTOCK, apply_entry(E.DISPATCH, q, 2), 1)
        self.assertEqual(five(q), (18, 0, 2, 0, 16))
        q = apply_entry(E.RETURN_SELLABLE, q, 1)
        self.assertEqual(five(q), (19, 0, 2, 0, 17))
        # 表下的核对：在库实物 20 − 2 + 1 = 19 = stock_balance + paid_pending_dispatch。
        self.assertEqual(q.stock_balance + q.paid_pending_dispatch, 19)


class QuantityRuleTest(unittest.TestCase):
    def test_every_quantity_entry_type_matches_contract_table_4_3(self):
        # §4.3 表逐行：从 10/2/3/4 出发加 1 件后的四个量。
        start = Quantities(10, 2, 3, 4)
        expected = {
            E.OPENING_BALANCE: (11, 2, 3, 4),
            E.RECEIPT: (11, 2, 3, 4),
            E.MARK_UNAVAILABLE: (10, 2, 4, 4),
            E.RESTORE_AVAILABLE: (10, 2, 2, 4),
            E.WRITE_OFF: (9, 2, 2, 4),
            E.RESERVE: (10, 3, 3, 4),
            E.RELEASE: (10, 1, 3, 4),
            E.SALE_COMMIT: (9, 1, 3, 5),
            E.LATE_SALE_COMMIT: (9, 2, 3, 5),
            E.DISPATCH: (10, 2, 3, 3),
            E.REFUND_RESTOCK: (11, 2, 3, 3),
            E.RETURN_SELLABLE: (11, 2, 3, 4),
            E.RETURN_UNSELLABLE: (11, 2, 4, 4),
        }
        self.assertEqual(set(expected), set(QUANTITY_RULES))
        for entry_type, values in expected.items():
            with self.subTest(entry_type=entry_type):
                self.assertEqual(five(apply_entry(entry_type, start, 1))[:4], values)

    def test_input_quantities_are_not_modified(self):
        start = Quantities(10, 2, 3, 4)
        apply_entry(E.SALE_COMMIT, start, 2)
        self.assertEqual(start, Quantities(10, 2, 3, 4))

    def test_count_and_reset_are_not_applied_by_plain_quantity(self):
        # 它们有自己的算法（§6、§6.1），按 q 落账会绕过差额与 target 规则。
        for entry_type in (E.COUNT_ADJUSTMENT, E.DEMO_RESET):
            with self.subTest(entry_type=entry_type), self.assertRaises(ValueError):
                apply_entry(entry_type, Quantities(10, 0, 0, 0), 1)


class OverLimitTest(unittest.TestCase):
    """每种会减少某个量（含可售量）的流水，超量时整笔拒绝，归到同一个不变量异常。"""

    def assert_rejected(self, entry_type, start, q):
        with self.assertRaises(InvariantViolation):
            apply_entry(entry_type, start, q)

    def test_release_beyond_reserved(self):
        self.assert_rejected(E.RELEASE, Quantities(10, 2, 0, 0), 3)

    def test_sale_commit_beyond_reserved(self):
        self.assert_rejected(E.SALE_COMMIT, Quantities(10, 2, 0, 0), 3)

    def test_late_sale_commit_beyond_available(self):
        # 晚到支付只能吃当前可售量（§5.2 第 6 条），不能吃别人的预留或不可售件。
        self.assert_rejected(E.LATE_SALE_COMMIT, Quantities(10, 4, 3, 0), 4)
        self.assertEqual(apply_entry(E.LATE_SALE_COMMIT, Quantities(10, 4, 3, 0), 3).available, 0)

    def test_reserve_beyond_available(self):
        self.assert_rejected(E.RESERVE, Quantities(10, 4, 3, 0), 4)

    def test_mark_unavailable_beyond_available(self):
        # §6：unavailable 不得超过 stock_balance − reserved。
        self.assert_rejected(E.MARK_UNAVAILABLE, Quantities(10, 4, 3, 0), 4)

    def test_restore_beyond_unavailable(self):
        self.assert_rejected(E.RESTORE_AVAILABLE, Quantities(10, 0, 2, 0), 3)

    def test_write_off_beyond_unavailable(self):
        # 报损只从不可售量出，可售的件不能被报损顺带扣掉。
        self.assert_rejected(E.WRITE_OFF, Quantities(10, 0, 2, 0), 3)

    def test_dispatch_beyond_paid_pending_dispatch(self):
        self.assert_rejected(E.DISPATCH, Quantities(10, 0, 0, 2), 3)

    def test_refund_restock_beyond_paid_pending_dispatch(self):
        # 退款回库只退未交运的件；超量会凭空造出库存。
        self.assert_rejected(E.REFUND_RESTOCK, Quantities(10, 0, 0, 2), 3)


class InvalidQuantityTest(unittest.TestCase):
    def test_non_positive_or_non_integer_quantity_is_rejected_for_every_type(self):
        # 0 件流水没有意义；负数会把加的规则反过来用；True 不能当 1 件。
        # 只增不减的类型（期初、收货、退货）没有超量可拦，这是它们唯一的拒绝路径。
        start = Quantities(10, 2, 3, 4)
        for entry_type in QUANTITY_RULES:
            for bad in INVALID_QUANTITIES:
                with self.subTest(entry_type=entry_type, q=bad):
                    with self.assertRaises(InvalidQuantity):
                        apply_entry(entry_type, start, bad)

    def test_invalid_quantity_is_not_an_invariant_violation(self):
        # 两者对应不同的错误（422 请求无效 / 409 超量），上层要能分开。
        with self.assertRaises(InvalidQuantity) as caught:
            apply_entry(E.RECEIPT, Quantities(0, 0, 0, 0), -5)
        self.assertNotIsInstance(caught.exception, InvariantViolation)


class CountAdjustmentTest(unittest.TestCase):
    def test_entries_after_submit_are_kept(self):
        # §6 的例子：提交时点到 20、之后收货 10，确认时差额为 0，账面仍是 30。
        snapshot = Quantities(20, 0, 0, 0)
        latest = apply_entry(E.RECEIPT, snapshot, 10)
        self.assertEqual(apply_count_adjustment(latest, snapshot, 20, 0), Quantities(30, 0, 0, 0))

    def test_sale_after_submit_is_kept(self):
        # 在架件数含已付款待发的件：提交后确认销售不改在架实物，差额仍为 0。
        snapshot = Quantities(20, 3, 0, 0)
        latest = apply_entry(E.SALE_COMMIT, snapshot, 3)
        self.assertEqual(apply_count_adjustment(latest, snapshot, 20, 0), latest)

    def test_shortage_and_damage_are_posted_as_deltas(self):
        # 少了 1 件、另有 2 件破损：stock_balance 只少 1，unavailable 多 2。
        snapshot = Quantities(20, 0, 0, 0)
        after = apply_count_adjustment(snapshot, snapshot, 17, 2)
        self.assertEqual(five(after), (19, 0, 2, 0, 17))

    def test_counted_shelf_includes_paid_pending_dispatch(self):
        # 契约第 14 节第 1 条的例子：17/0/2/3 盘到在架 4 件、不可售 2 件。
        # 落账后 stock_balance 3，加待发 3 件等于实物 4 + 2。
        snapshot = Quantities(17, 0, 2, 3)
        after = apply_count_adjustment(snapshot, snapshot, 4, 2)
        self.assertEqual(five(after), (3, 0, 2, 3, 1))

    def assert_below_commitments(self, latest, snapshot, shelf, unsellable, after, shortfall):
        with self.assertRaises(CountBelowCommitments) as caught:
            apply_count_adjustment(latest, snapshot, shelf, unsellable)
        error = caught.exception
        # 与其他超量同属不变量异常；另带落账后的四个量（可能为负）与 §6 的缺口数。
        self.assertIsInstance(error, InvariantViolation)
        self.assertEqual(
            (error.stock_balance, error.reserved, error.unavailable, error.paid_pending_dispatch),
            after,
        )
        self.assertEqual(error.shortfall, shortfall)

    def test_rejected_when_reservations_exceed_counted_stock(self):
        # 盘到 3 件但有 5 件预留：Core 不替管理员决定砍哪笔订单，整笔拒绝。
        snapshot = Quantities(20, 5, 0, 0)
        self.assert_below_commitments(snapshot, snapshot, 3, 0, (3, 5, 0, 0), 2)

    def test_rejected_when_unavailable_exceeds_balance(self):
        snapshot = Quantities(17, 0, 2, 3)
        self.assert_below_commitments(snapshot, snapshot, 2, 2, (1, 0, 2, 3), 1)

    def test_shortfall_is_positive_when_balance_goes_negative(self):
        # 实物少于待发货量：新 stock_balance = −3，缺口取 −新 stock_balance。
        snapshot = Quantities(17, 0, 2, 3)
        self.assert_below_commitments(snapshot, snapshot, 0, 0, (-3, 0, 0, 3), 3)

    def test_shortfall_is_positive_when_unavailable_goes_negative(self):
        # 提交后恢复了 3 件可售，再确认“不可售 0 件”：新 unavailable = −3。
        snapshot = Quantities(20, 0, 5, 0)
        latest = apply_entry(E.RESTORE_AVAILABLE, snapshot, 3)
        self.assert_below_commitments(latest, snapshot, 15, 0, (15, 0, -3, 0), 3)

    def test_counted_numbers_must_be_non_negative_integers(self):
        snapshot = Quantities(20, 0, 0, 0)
        for bad in (-1, True, 1.5, "1", None):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidQuantity):
                    apply_count_adjustment(snapshot, snapshot, bad, 0)
                with self.assertRaises(InvalidQuantity):
                    apply_count_adjustment(snapshot, snapshot, 20, bad)

    def test_zero_counts_are_valid(self):
        snapshot = Quantities(5, 0, 1, 0)
        self.assertEqual(apply_count_adjustment(snapshot, snapshot, 0, 0), Quantities(0, 0, 0, 0))


class DemoResetTest(unittest.TestCase):
    def test_balance_is_pulled_to_target(self):
        self.assertEqual(apply_demo_reset(Quantities(3, 1, 0, 2), 20), Quantities(20, 1, 0, 2))

    def test_balance_never_drops_below_reserved_plus_unavailable(self):
        # 未结预留与不可售件照常保留：target 0（当天售罄）时可售量为 0，而不是负数。
        after = apply_demo_reset(Quantities(18, 4, 1, 2), 0)
        self.assertEqual(five(after), (5, 4, 1, 2, 0))

    def test_zero_delta_is_allowed(self):
        start = Quantities(20, 4, 0, 0)
        self.assertEqual(apply_demo_reset(start, 20), start)

    def test_target_must_be_a_non_negative_integer(self):
        for bad in (-1, True, 1.5, "20", None):
            with self.subTest(target=bad), self.assertRaises(InvalidQuantity):
                apply_demo_reset(Quantities(20, 0, 0, 0), bad)


if __name__ == "__main__":
    unittest.main()
