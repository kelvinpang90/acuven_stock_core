import unittest

from app.domain.ledger import (
    QUANTITY_RULES,
    EntryType,
    apply_count_adjustment,
    apply_demo_reset,
    apply_entry,
)
from app.domain.quantities import Balances, InvalidQuantity, InvariantViolation

INVALID_QUANTITIES = (0, -1, True, False, 1.0, 1.5, "1", None)


def row(balances):
    """按契约 §11 示例表的列顺序取出五个量（四个账面量加 available）。"""
    return (
        balances.stock_balance,
        balances.reserved,
        balances.unavailable,
        balances.paid_pending_dispatch,
        balances.available,
    )


class QuantityRulesTest(unittest.TestCase):
    def test_rules_match_contract_table(self):
        # 契约 §4.3 表逐行抄写：规则表被改动而契约没改时，这里会失败。
        expected = {
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
        self.assertEqual(dict(QUANTITY_RULES), expected)

    def test_every_fixed_quantity_entry_type_has_a_rule(self):
        # 盘点、Demo 重置不按 q 件计算，合并迁移与更正不在本任务；其余类型都必须有规则。
        not_per_quantity = {
            EntryType.COUNT_ADJUSTMENT,
            EntryType.DEMO_RESET,
            EntryType.MERGE_TRANSFER_OUT,
            EntryType.MERGE_TRANSFER_IN,
            EntryType.CORRECTION,
        }
        self.assertEqual(set(QUANTITY_RULES), set(EntryType) - not_per_quantity)

    def test_rules_cannot_be_modified_at_runtime(self):
        with self.assertRaises(TypeError):
            QUANTITY_RULES[EntryType.RECEIPT] = (2, 0, 0, 0)

    def test_entry_types_without_per_quantity_rule_are_refused(self):
        # 用 q 件去套盘点或合并会算出错误的账面，必须拒绝而不是当成“无变化”。
        for entry_type in (
            EntryType.COUNT_ADJUSTMENT,
            EntryType.DEMO_RESET,
            EntryType.MERGE_TRANSFER_OUT,
            EntryType.MERGE_TRANSFER_IN,
            EntryType.CORRECTION,
        ):
            with self.subTest(entry_type=entry_type), self.assertRaises(ValueError):
                apply_entry(Balances(10, 0, 0, 0), entry_type, 1)


class ContractExamplesTest(unittest.TestCase):
    def test_example_b_demo_switch_to_core(self):
        # 契约 §11.3 样例 B 的每一步。
        steps = [
            (lambda b: apply_entry(b, EntryType.OPENING_BALANCE, 20), (20, 0, 0, 0, 20)),
            (lambda b: apply_entry(b, EntryType.RESERVE, 3), (20, 3, 0, 0, 17)),
            (lambda b: apply_entry(b, EntryType.SALE_COMMIT, 3), (17, 0, 0, 3, 17)),
            (lambda b: apply_entry(b, EntryType.DISPATCH, 2), (17, 0, 0, 1, 17)),
            (lambda b: apply_entry(b, EntryType.REFUND_RESTOCK, 1), (18, 0, 0, 0, 18)),
            (lambda b: apply_entry(b, EntryType.RESERVE, 4), (18, 4, 0, 0, 14)),
            # 第 7 步：new_balance = max(20, 4) = 20，DEMO_RESET +2。
            (lambda b: apply_demo_reset(b, 20), (20, 4, 0, 0, 16)),
        ]
        balances = Balances(0, 0, 0, 0)
        for number, (step, expected) in enumerate(steps, start=1):
            balances = step(balances)
            with self.subTest(step=number):
                self.assertEqual(row(balances), expected)

    def test_example_real_shop_opening_from_physical_count(self):
        # 契约 §11.5：每一行是表中一步，一步含多个动作的逐个执行。
        steps = [
            ([(EntryType.OPENING_BALANCE, 20)], (20, 0, 0, 0, 20)),
            ([(EntryType.MARK_UNAVAILABLE, 2)], (20, 0, 2, 0, 18)),
            ([(EntryType.RESERVE, 3), (EntryType.SALE_COMMIT, 3)], (17, 0, 2, 3, 15)),
            ([(EntryType.DISPATCH, 2), (EntryType.REFUND_RESTOCK, 1)], (18, 0, 2, 0, 16)),
            ([(EntryType.RETURN_SELLABLE, 1)], (19, 0, 2, 0, 17)),
        ]
        balances = Balances(0, 0, 0, 0)
        for number, (entries, expected) in enumerate(steps, start=1):
            for entry_type, q in entries:
                balances = apply_entry(balances, entry_type, q)
            with self.subTest(step=number):
                self.assertEqual(row(balances), expected)
        # 核对：在库实物 = 20 − 2（发出）+ 1（退回）= 19 = stock_balance + paid_pending_dispatch。
        self.assertEqual(balances.stock_balance + balances.paid_pending_dispatch, 19)


class OverLimitTest(unittest.TestCase):
    # (流水类型, 操作前账面, 最多允许的 q)：恰好到上限可以，多 1 件就违反不变量。
    CASES = (
        # 恢复与报损只能动不可售量。
        (EntryType.RESTORE_AVAILABLE, Balances(10, 0, 2, 0), 2),
        (EntryType.WRITE_OFF, Balances(10, 0, 2, 0), 2),
        # 释放、确认销售只能动已有的预留。
        (EntryType.RELEASE, Balances(10, 3, 0, 0), 3),
        (EntryType.SALE_COMMIT, Balances(10, 3, 0, 0), 3),
        # 发货、退款回库只能动已付款待发的件。
        (EntryType.DISPATCH, Balances(10, 0, 0, 2), 2),
        (EntryType.REFUND_RESTOCK, Balances(10, 0, 0, 2), 2),
        # 标不可售、预留、晚到支付只能用可售量（10 − 6 − 2 = 2）。
        (EntryType.MARK_UNAVAILABLE, Balances(10, 6, 2, 0), 2),
        (EntryType.RESERVE, Balances(10, 6, 2, 0), 2),
        (EntryType.LATE_SALE_COMMIT, Balances(10, 6, 2, 0), 2),
    )

    def test_quantity_up_to_the_limit_is_accepted(self):
        for entry_type, before, limit in self.CASES:
            with self.subTest(entry_type=entry_type):
                apply_entry(before, entry_type, limit)

    def test_quantity_beyond_the_limit_is_rejected(self):
        for entry_type, before, limit in self.CASES:
            with self.subTest(entry_type=entry_type), self.assertRaises(InvariantViolation):
                apply_entry(before, entry_type, limit + 1)

    def test_every_rule_that_decreases_something_is_covered(self):
        # 新增一种会减少某个量（含可售量）的规则时，必须同时补上超量用例。
        decreasing = {
            entry_type
            for entry_type, deltas in QUANTITY_RULES.items()
            if min(deltas) < 0 or deltas[0] - deltas[1] - deltas[2] < 0
        }
        self.assertEqual(decreasing, {entry_type for entry_type, _, _ in self.CASES})


class InvalidQuantityTest(unittest.TestCase):
    def test_non_positive_or_non_integer_quantity_is_rejected_for_every_rule(self):
        # 0 件的流水没有意义；负数会把收货变成出库；True/1.0 不是整数件。
        for entry_type in QUANTITY_RULES:
            for q in INVALID_QUANTITIES:
                with self.subTest(entry_type=entry_type, q=q), self.assertRaises(InvalidQuantity):
                    apply_entry(Balances(10, 2, 2, 2), entry_type, q)

    def test_increase_only_rules_accept_any_positive_quantity(self):
        for entry_type in (
            EntryType.OPENING_BALANCE,
            EntryType.RECEIPT,
            EntryType.RETURN_SELLABLE,
            EntryType.RETURN_UNSELLABLE,
        ):
            with self.subTest(entry_type=entry_type):
                after = apply_entry(Balances(0, 0, 0, 0), entry_type, 1000)
                self.assertEqual(after.stock_balance, 1000)


class CountAdjustmentTest(unittest.TestCase):
    def test_receipt_after_submission_is_kept(self):
        # 契约 §6 的例子：提交时点到 20、之后收货 10，确认时差额为 0，账面仍是 30。
        snapshot = Balances(20, 0, 0, 0)
        latest = apply_entry(snapshot, EntryType.RECEIPT, 10)
        self.assertEqual(apply_count_adjustment(snapshot, latest, 20, 0), Balances(30, 0, 0, 0))

    def test_dispatch_after_submission_is_not_counted_twice(self):
        # 在架件数含已付款待发的件；提交后发走的 3 件已由 DISPATCH 记过，
        # 盘点不能再扣一次。
        snapshot = Balances(17, 0, 2, 3)
        latest = apply_entry(snapshot, EntryType.DISPATCH, 3)
        self.assertEqual(apply_count_adjustment(snapshot, latest, 18, 2), Balances(17, 0, 2, 0))

    def test_shelf_shortage_reduces_stock_balance(self):
        snapshot = Balances(17, 0, 2, 3)
        self.assertEqual(apply_count_adjustment(snapshot, snapshot, 16, 2), Balances(15, 0, 2, 3))

    def test_newly_found_damage_moves_items_to_unavailable(self):
        # 20 件中 2 件破损：总数不变，2 件转为不可售。
        snapshot = Balances(20, 0, 0, 0)
        self.assertEqual(apply_count_adjustment(snapshot, snapshot, 18, 2), Balances(20, 0, 2, 0))

    def test_count_below_commitments_is_rejected_with_quantities_and_shortfall(self):
        # 在架 4 件、预留 2 件、不可售 2 件：落账后 stock_balance 3 < 2 + 2，缺 1 件。
        snapshot = Balances(17, 2, 2, 3)
        with self.assertRaises(InvariantViolation) as caught:
            apply_count_adjustment(snapshot, snapshot, 4, 2)
        error = caught.exception
        self.assertEqual(
            (error.stock_balance, error.reserved, error.unavailable, error.paid_pending_dispatch),
            (3, 2, 2, 3),
        )
        self.assertEqual(error.shortfall, 1)

    def test_negative_stock_balance_has_positive_shortfall(self):
        # 在架 0 件，但有 3 件已付款待发：落账后 stock_balance 为 −3。
        snapshot = Balances(17, 0, 2, 3)
        with self.assertRaises(InvariantViolation) as caught:
            apply_count_adjustment(snapshot, snapshot, 0, 0)
        self.assertEqual(caught.exception.stock_balance, -3)
        self.assertEqual(caught.exception.shortfall, 3)

    def test_negative_unavailable_has_positive_shortfall(self):
        # 快照后 2 件已恢复可售，盘点又报不可售 0：差额 −2 落到最新的 0 上成了 −2。
        snapshot = Balances(10, 0, 2, 0)
        latest = apply_entry(snapshot, EntryType.RESTORE_AVAILABLE, 2)
        with self.assertRaises(InvariantViolation) as caught:
            apply_count_adjustment(snapshot, latest, 8, 0)
        self.assertEqual(caught.exception.unavailable, -2)
        self.assertEqual(caught.exception.shortfall, 2)

    def test_counts_must_be_non_negative_integers(self):
        snapshot = Balances(10, 0, 0, 0)
        for bad in (-1, True, 1.0, "3", None):
            with self.subTest(shelf_count=bad), self.assertRaises(InvalidQuantity):
                apply_count_adjustment(snapshot, snapshot, bad, 0)
            with self.subTest(unavailable_count=bad), self.assertRaises(InvalidQuantity):
                apply_count_adjustment(snapshot, snapshot, 10, bad)


class DemoResetTest(unittest.TestCase):
    def test_target_below_commitments_keeps_reserved_and_unavailable(self):
        # §6.1：new_balance = max(target, reserved + unavailable)，可售落到 0，未结预留不受影响。
        after = apply_demo_reset(Balances(18, 4, 2, 1), 3)
        self.assertEqual(row(after), (6, 4, 2, 1, 0))

    def test_target_zero_sells_out_the_sku(self):
        self.assertEqual(apply_demo_reset(Balances(18, 0, 0, 5), 0), Balances(0, 0, 0, 5))

    def test_zero_delta_is_allowed(self):
        balances = Balances(20, 4, 0, 0)
        self.assertEqual(apply_demo_reset(balances, 20), balances)

    def test_target_must_be_a_non_negative_integer(self):
        for bad in (-1, True, 1.5, "20", None):
            with self.subTest(target=bad), self.assertRaises(InvalidQuantity):
                apply_demo_reset(Balances(18, 0, 0, 0), bad)


if __name__ == "__main__":
    unittest.main()
