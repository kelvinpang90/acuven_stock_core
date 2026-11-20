import unittest

from app.domain.catalog import InvalidField
from app.domain.ledger import CountBelowCommitments
from app.domain.quantities import Quantities
from app.domain.sku_merges import (
    CANCELLED,
    CANONICAL,
    COMPLETED,
    COUNTED,
    MERGE_ID_PREFIX,
    OPEN_STATUSES,
    RECONCILE_PENDING,
    SELLING_PAUSED,
    SOURCE,
    SOURCE_TYPE,
    STATUSES,
    CommandDecision,
    SnapshotVersions,
    cancel_decision,
    complete_decision,
    count_decision,
    execute_decision,
    merge_deltas,
    parse_count_body,
    parse_create_body,
    pauses_selling,
    plan_execution,
    stale_sku_ids,
)

D = CommandDecision
Q = Quantities
ZERO = Q(0, 0, 0, 0)


class ConstantsTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(
            STATUSES, ("selling_paused", "counted", "reconcile_pending", "completed", "cancelled")
        )
        self.assertEqual(OPEN_STATUSES, {"selling_paused", "counted", "reconcile_pending"})
        self.assertEqual(MERGE_ID_PREFIX, "smg")
        self.assertEqual(SOURCE_TYPE, "sku_merge")


class DecisionTest(unittest.TestCase):
    def assert_decisions(self, decide, expected):
        self.assertEqual(set(expected), set(STATUSES))
        for status, decision in expected.items():
            with self.subTest(status=status):
                self.assertIs(decide(status), decision)

    def test_count(self):
        self.assert_decisions(
            count_decision,
            {
                SELLING_PAUSED: D.EXECUTE,
                COUNTED: D.EXECUTE,
                RECONCILE_PENDING: D.CONFLICT,
                COMPLETED: D.CONFLICT,
                CANCELLED: D.CONFLICT,
            },
        )

    def test_execute(self):
        self.assert_decisions(
            execute_decision,
            {
                SELLING_PAUSED: D.CONFLICT,
                COUNTED: D.EXECUTE,
                RECONCILE_PENDING: D.CURRENT,
                COMPLETED: D.CURRENT,
                CANCELLED: D.CONFLICT,
            },
        )

    def test_complete(self):
        self.assert_decisions(
            complete_decision,
            {
                SELLING_PAUSED: D.CONFLICT,
                COUNTED: D.CONFLICT,
                RECONCILE_PENDING: D.EXECUTE,
                COMPLETED: D.CURRENT,
                CANCELLED: D.CONFLICT,
            },
        )

    def test_cancel(self):
        self.assert_decisions(
            cancel_decision,
            {
                SELLING_PAUSED: D.EXECUTE,
                COUNTED: D.EXECUTE,
                RECONCILE_PENDING: D.CONFLICT,
                COMPLETED: D.CONFLICT,
                CANCELLED: D.CURRENT,
            },
        )

    def test_unknown_status(self):
        for decide in (count_decision, execute_decision, complete_decision, cancel_decision):
            with self.subTest(decide=decide.__name__):
                with self.assertRaises(ValueError):
                    decide("draft")


class PausesSellingTest(unittest.TestCase):
    def test_by_status_and_role(self):
        expected = {
            SELLING_PAUSED: (True, True),
            COUNTED: (True, True),
            RECONCILE_PENDING: (False, True),
            COMPLETED: (False, False),
            CANCELLED: (False, False),
        }
        self.assertEqual(set(expected), set(STATUSES))
        for status, (source, canonical) in expected.items():
            with self.subTest(status=status):
                self.assertIs(pauses_selling(status, SOURCE), source)
                self.assertIs(pauses_selling(status, CANONICAL), canonical)

    def test_unknown(self):
        with self.assertRaises(ValueError):
            pauses_selling("draft", SOURCE)
        with self.assertRaises(ValueError):
            pauses_selling(COUNTED, "other")


class CreateBodyTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(
            parse_create_body({"source_sku_id": "sku_a", "canonical_sku_id": "sku_b"}),
            {"source_sku_id": "sku_a", "canonical_sku_id": "sku_b", "reason": None},
        )
        self.assertEqual(
            parse_create_body(
                {"source_sku_id": "sku_a", "canonical_sku_id": "sku_b", "reason": "  dup  "}
            ),
            {"source_sku_id": "sku_a", "canonical_sku_id": "sku_b", "reason": "dup"},
        )
        self.assertIsNone(
            parse_create_body({"source_sku_id": "a", "canonical_sku_id": "b", "reason": None})[
                "reason"
            ]
        )

    def test_rejected(self):
        valid = {"source_sku_id": "sku_a", "canonical_sku_id": "sku_b"}
        cases = {
            "missing source": ({"canonical_sku_id": "sku_b"}, "source_sku_id"),
            "missing canonical": ({"source_sku_id": "sku_a"}, "canonical_sku_id"),
            "extra field": ({**valid, "pool_id": "pool_1"}, "pool_id"),
            "source not str": ({**valid, "source_sku_id": 1}, "source_sku_id"),
            "canonical null": ({**valid, "canonical_sku_id": None}, "canonical_sku_id"),
            "same ids": ({**valid, "canonical_sku_id": "sku_a"}, "canonical_sku_id"),
            "reason blank": ({**valid, "reason": "   "}, "reason"),
            "reason too long": ({**valid, "reason": "x" * 501}, "reason"),
            "reason control": ({**valid, "reason": "a\x00b"}, "reason"),
            "reason not str": ({**valid, "reason": 1}, "reason"),
        }
        for name, (body, field) in cases.items():
            with self.subTest(name):
                with self.assertRaises(InvalidField) as caught:
                    parse_create_body(body)
                self.assertEqual(caught.exception.field, field)

    def test_not_object(self):
        for body in (None, [], "x"):
            with self.subTest(body=body):
                with self.assertRaises(InvalidField):
                    parse_create_body(body)


class CountBodyTest(unittest.TestCase):
    VALID = {"pool_id": "pool_1", "shelf_count": 17, "unsellable_count": 0}

    def test_valid(self):
        self.assertEqual(parse_count_body(self.VALID), self.VALID)
        body = {"pool_id": "pool_1", "shelf_count": 1000000, "unsellable_count": 1000000}
        self.assertEqual(parse_count_body(body), body)

    def test_rejected(self):
        valid = self.VALID
        cases = {
            "missing pool": ({"shelf_count": 1, "unsellable_count": 0}, "pool_id"),
            "missing shelf": ({"pool_id": "p", "unsellable_count": 0}, "shelf_count"),
            "missing unsellable": ({"pool_id": "p", "shelf_count": 1}, "unsellable_count"),
            "extra field": ({**valid, "note": "x"}, "note"),
            "pool not str": ({**valid, "pool_id": 1}, "pool_id"),
        }
        for field in ("shelf_count", "unsellable_count"):
            for value in (-1, 1000001, "1", True, False, 1.0, None):
                cases[f"{field}={value!r}"] = ({**valid, field: value}, field)
        for name, (body, field) in cases.items():
            with self.subTest(name):
                with self.assertRaises(InvalidField) as caught:
                    parse_count_body(body)
                self.assertEqual(caught.exception.field, field)


class ContractExampleTest(unittest.TestCase):
    """§2.4 合并定数的例子表：A 为来源、B 为规范。"""

    def test_row_1_refund_restock_after_count(self):
        result = plan_execution(Q(5, 0, 0, 2), Q(10, 0, 0, 0), 17, 0, Q(6, 0, 0, 1), Q(10, 0, 0, 0))
        self.assertEqual(result.transfer, Q(6, 0, 0, 1))
        self.assertEqual(result.canonical_after_transfer, Q(16, 0, 0, 1))
        self.assertEqual((result.d_shelf, result.d_u), (0, 0))
        self.assertFalse(result.writes_adjustment)
        self.assertEqual(result.canonical_after, Q(16, 0, 0, 1))

    def test_row_2_commit_after_count(self):
        result = plan_execution(Q(5, 2, 0, 0), Q(10, 0, 0, 0), 14, 0, Q(3, 0, 0, 2), Q(10, 0, 0, 0))
        self.assertEqual(result.canonical_after_transfer, Q(13, 0, 0, 2))
        self.assertEqual((result.d_shelf, result.d_u), (-1, 0))
        self.assertTrue(result.writes_adjustment)
        self.assertEqual(result.canonical_after, Q(12, 0, 0, 2))

    def test_row_3_mark_unavailable_after_count(self):
        result = plan_execution(Q(5, 0, 1, 0), Q(10, 0, 0, 0), 14, 1, Q(5, 0, 2, 0), Q(10, 0, 0, 0))
        self.assertEqual(result.canonical_after_transfer, Q(15, 0, 2, 0))
        self.assertEqual((result.d_shelf, result.d_u), (0, 0))
        self.assertFalse(result.writes_adjustment)
        self.assertEqual(result.canonical_after, Q(15, 0, 2, 0))
        self.assertEqual(result.canonical_after.available, 13)

    def test_row_4_below_commitments(self):
        snapshots = (Q(2, 2, 0, 0), Q(1, 0, 0, 0))
        self.assertEqual(merge_deltas(*snapshots, 0, 0), (-3, 0))
        with self.assertRaises(CountBelowCommitments) as caught:
            plan_execution(*snapshots, 0, 0, Q(2, 2, 0, 0), Q(1, 0, 0, 0))
        error = caught.exception
        self.assertEqual(error.shortfall, 2)
        self.assertEqual(
            (error.stock_balance, error.reserved, error.unavailable, error.paid_pending_dispatch),
            (0, 2, 0, 0),
        )

    def test_row_5_stale_then_recount(self):
        # 快照时两行版本都是 3；之后另一张盘点单只在 A 上确认了 −2。
        stale = stale_sku_ids(SnapshotVersions("sku_a", 4, 3), SnapshotVersions("sku_b", 0, 3))
        self.assertEqual(stale, ["sku_a"])
        # 重新点数录入 13 / 0，新快照取 A 3/0/0/0。
        self.assertEqual(
            stale_sku_ids(SnapshotVersions("sku_a", 4, 4), SnapshotVersions("sku_b", 0, 4)), []
        )
        result = plan_execution(Q(3, 0, 0, 0), Q(10, 0, 0, 0), 13, 0, Q(3, 0, 0, 0), Q(10, 0, 0, 0))
        self.assertEqual(result.canonical_after_transfer, Q(13, 0, 0, 0))
        self.assertEqual((result.d_shelf, result.d_u), (0, 0))
        self.assertFalse(result.writes_adjustment)
        self.assertEqual(result.canonical_after, Q(13, 0, 0, 0))

    def test_row_6_retry_with_original_snapshot(self):
        snapshots = (Q(3, 3, 0, 2), Q(1, 0, 0, 0))
        with self.assertRaises(CountBelowCommitments) as caught:
            plan_execution(*snapshots, 4, 0, Q(3, 3, 0, 2), Q(1, 0, 0, 0))
        error = caught.exception
        self.assertEqual(error.shortfall, 1)
        self.assertEqual(
            (error.stock_balance, error.reserved, error.unavailable, error.paid_pending_dispatch),
            (2, 3, 0, 2),
        )
        # 释放预留、发出 2 件之后直接再执行，沿用原快照与实数。
        result = plan_execution(*snapshots, 4, 0, Q(3, 0, 0, 0), Q(1, 0, 0, 0))
        self.assertEqual(result.canonical_after_transfer, Q(4, 0, 0, 0))
        self.assertEqual((result.d_shelf, result.d_u), (-2, 0))
        self.assertTrue(result.writes_adjustment)
        self.assertEqual(result.canonical_after, Q(2, 0, 0, 0))


class TransferTest(unittest.TestCase):
    def test_zero_source_still_transfers(self):
        result = plan_execution(ZERO, Q(10, 0, 0, 0), 10, 0, ZERO, Q(10, 2, 1, 3))
        self.assertEqual(result.transfer, ZERO)
        self.assertEqual(result.canonical_after_transfer, Q(10, 2, 1, 3))

    def test_after_transfer_is_sum(self):
        source = Q(7, 2, 1, 4)
        canonical = Q(9, 3, 2, 1)
        result = plan_execution(source, canonical, 18, 3, source, canonical)
        self.assertEqual(result.transfer, source)
        self.assertEqual(result.canonical_after_transfer, Q(16, 5, 3, 5))

    def test_unavailable_delta(self):
        # 快照合计在架 15、不可售 1；点到在架 14、不可售 3。
        source = Q(5, 0, 1, 0)
        canonical = Q(11, 0, 0, 0)
        result = plan_execution(source, canonical, 14, 3, source, canonical)
        self.assertEqual((result.d_shelf, result.d_u), (-1, 2))
        self.assertEqual(result.canonical_after, Q(17, 0, 3, 0))


class StaleTest(unittest.TestCase):
    def test_both_and_order(self):
        self.assertEqual(
            stale_sku_ids(SnapshotVersions("sku_b", 5, 2), SnapshotVersions("sku_a", 3, 1)),
            ["sku_a", "sku_b"],
        )

    def test_equal_version_not_stale(self):
        self.assertEqual(
            stale_sku_ids(SnapshotVersions("sku_a", 2, 2), SnapshotVersions("sku_b", 1, 7)), []
        )


if __name__ == "__main__":
    unittest.main()
