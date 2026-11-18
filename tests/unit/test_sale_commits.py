import ast
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.domain import sale_commits
from app.domain.idempotency import request_digest
from app.domain.ledger import EntryType
from app.domain.sale_commits import (
    CommitDecision,
    commit_decision,
    commit_reason_code,
    merge_destination,
    mismatched_line_refs,
    reserve_request_digest,
    sku_shortfalls,
)

EXPIRES_AT = datetime(2026, 11, 16, 8, 15, 0)
ONE_MICROSECOND = timedelta(microseconds=1)
BEFORE = EXPIRES_AT - ONE_MICROSECOND
LATER = EXPIRES_AT + ONE_MICROSECOND


class ConstantsTest(unittest.TestCase):
    def test_ledger_constants(self):
        self.assertEqual(sale_commits.SOURCE_TYPE, "sale_commit")
        self.assertEqual(sale_commits.LATE_PAYMENT_REASON_CODE, "late_payment")
        self.assertEqual(sale_commits.FAULT_MODE_REASON_CODE, "fault_mode")

    def test_module_has_no_io_imports(self):
        source = Path(sale_commits.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= {"collections", "datetime", "enum", "app"}, imported)


class MergeDestinationTest(unittest.TestCase):
    MERGED_INTO = {"a": None, "b": "a", "c": "b", "x": "ghost"}

    def test_unmerged_is_itself(self):
        self.assertEqual(merge_destination("a", self.MERGED_INTO), "a")

    def test_one_level(self):
        self.assertEqual(merge_destination("b", self.MERGED_INTO), "a")

    def test_two_levels(self):
        self.assertEqual(merge_destination("c", self.MERGED_INTO), "a")

    def test_not_in_mapping(self):
        self.assertIsNone(merge_destination("missing", self.MERGED_INTO))

    def test_chain_reaches_sku_not_in_mapping(self):
        self.assertIsNone(merge_destination("x", self.MERGED_INTO))

    def test_cycle_raises(self):
        with self.assertRaises(ValueError):
            merge_destination("p", {"p": "q", "q": "p"})
        with self.assertRaises(ValueError):
            merge_destination("p", {"p": "p"})
        with self.assertRaises(ValueError):
            merge_destination("s", {"s": "p", "p": "q", "q": "p"})


class MismatchedLineRefsTest(unittest.TestCase):
    DESTINATIONS = {"sku_a": "sku_a", "sku_b": "sku_b", "sku_old": "sku_new", "sku_new": "sku_new"}
    ORDER_LINES = [("L1", "sku_a", 2), ("L2", "sku_b", 1), ("L3", "sku_new", 4)]

    def check(self, request_lines, order_lines=None):
        if order_lines is None:
            order_lines = self.ORDER_LINES
        return mismatched_line_refs(request_lines, order_lines, self.DESTINATIONS)

    def test_identical_is_empty(self):
        self.assertEqual(self.check(list(self.ORDER_LINES)), [])
        self.assertEqual(self.check(list(reversed(self.ORDER_LINES))), [])

    def test_extra_line(self):
        self.assertEqual(self.check([*self.ORDER_LINES, ("L0", "sku_a", 1)]), ["L0"])

    def test_missing_line(self):
        self.assertEqual(self.check([("L2", "sku_b", 1)]), ["L1", "L3"])

    def test_quantity_differs(self):
        request = [("L1", "sku_a", 3), ("L2", "sku_b", 1), ("L3", "sku_new", 4)]
        self.assertEqual(self.check(request), ["L1"])

    def test_request_sku_does_not_exist(self):
        request = [("L1", "sku_a", 2), ("L2", "sku_ghost", 1), ("L3", "sku_new", 4)]
        self.assertEqual(self.check(request), ["L2"])

    def test_request_sku_destination_differs(self):
        request = [("L1", "sku_b", 2), ("L2", "sku_b", 1), ("L3", "sku_new", 4)]
        self.assertEqual(self.check(request), ["L1"])

    def test_all_kinds_together_sorted(self):
        request = [
            ("L9", "sku_a", 1),
            ("L3", "sku_new", 5),
            ("L2", "sku_ghost", 1),
            ("L0", "sku_a", 1),
        ]
        self.assertEqual(self.check(request), ["L0", "L1", "L2", "L3", "L9"])

    def test_request_original_sku_matches_canonical_order_line(self):
        request = [("L1", "sku_a", 2), ("L2", "sku_b", 1), ("L3", "sku_old", 4)]
        self.assertEqual(self.check(request), [])

    def test_order_line_not_rehomed_matches_by_destination(self):
        order_lines = [("L1", "sku_old", 4)]
        self.assertEqual(self.check([("L1", "sku_old", 4)], order_lines), [])
        self.assertEqual(self.check([("L1", "sku_new", 4)], order_lines), [])


class CommitDecisionTest(unittest.TestCase):
    def decide(self, status, now=BEFORE, recorded="pay-1", payment_ref="pay-1"):
        return commit_decision(status, EXPIRES_AT, now, recorded, payment_ref)

    def test_active_before_expiry_commits(self):
        self.assertEqual(self.decide("active", BEFORE, None), CommitDecision.COMMIT)

    def test_active_at_expiry_is_due(self):
        self.assertEqual(self.decide("active", EXPIRES_AT, None), CommitDecision.EXPIRE_THEN_LATE)

    def test_active_after_expiry(self):
        self.assertEqual(self.decide("active", LATER, None), CommitDecision.EXPIRE_THEN_LATE)

    def test_expired_is_late(self):
        self.assertEqual(self.decide("expired", BEFORE, None), CommitDecision.LATE)
        self.assertEqual(self.decide("expired", LATER, None), CommitDecision.LATE)

    def test_review_same_payment_ref_is_late(self):
        self.assertEqual(self.decide("late_payment_review"), CommitDecision.LATE)

    def test_review_other_payment_ref_conflicts(self):
        self.assertEqual(
            self.decide("late_payment_review", payment_ref="pay-2"),
            CommitDecision.PAYMENT_REF_CONFLICT,
        )

    def test_committed_same_payment_ref_answers_existing(self):
        self.assertEqual(self.decide("committed"), CommitDecision.ANSWER_EXISTING)

    def test_committed_other_payment_ref_conflicts(self):
        self.assertEqual(
            self.decide("committed", payment_ref="pay-2"), CommitDecision.STATE_CONFLICT
        )

    def test_released_and_closed_refunded_conflict(self):
        for status in ("released", "closed_refunded"):
            with self.subTest(status=status):
                self.assertEqual(self.decide(status), CommitDecision.STATE_CONFLICT)
                self.assertEqual(self.decide(status, LATER), CommitDecision.STATE_CONFLICT)

    def test_unknown_status_raises(self):
        for status in ("pending", "", "ACTIVE"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.decide(status)


class SkuShortfallsTest(unittest.TestCase):
    def test_lines_on_same_sku_short_only_in_total(self):
        lines = [("L1", "sku_a", 3), ("L2", "sku_a", 3)]
        self.assertEqual(
            sku_shortfalls(lines, {"sku_a": 5}),
            [
                {"line_ref": "L1", "sku_id": "sku_a", "quantity": 3, "available": 5},
                {"line_ref": "L2", "sku_id": "sku_a", "quantity": 3, "available": 5},
            ],
        )

    def test_only_short_skus_in_request_order(self):
        lines = [
            ("L3", "sku_b", 2),
            ("L1", "sku_a", 1),
            ("L2", "sku_c", 4),
            ("L0", "sku_b", 1),
        ]
        available = {"sku_a": 10, "sku_b": 2, "sku_c": 3}
        self.assertEqual(
            sku_shortfalls(lines, available),
            [
                {"line_ref": "L3", "sku_id": "sku_b", "quantity": 2, "available": 2},
                {"line_ref": "L2", "sku_id": "sku_c", "quantity": 4, "available": 3},
                {"line_ref": "L0", "sku_id": "sku_b", "quantity": 1, "available": 2},
            ],
        )

    def test_zero_available(self):
        self.assertEqual(
            sku_shortfalls([("L1", "sku_a", 1)], {"sku_a": 0}),
            [{"line_ref": "L1", "sku_id": "sku_a", "quantity": 1, "available": 0}],
        )

    def test_exactly_enough(self):
        lines = [("L1", "sku_a", 2), ("L2", "sku_a", 3), ("L3", "sku_b", 1)]
        self.assertEqual(sku_shortfalls(lines, {"sku_a": 5, "sku_b": 1}), [])


class CommitReasonCodeTest(unittest.TestCase):
    def test_fault_mode_wins(self):
        for entry_type in (EntryType.SALE_COMMIT, EntryType.LATE_SALE_COMMIT):
            with self.subTest(entry_type=entry_type):
                self.assertEqual(commit_reason_code(entry_type, True), "fault_mode")

    def test_late_sale_commit(self):
        self.assertEqual(commit_reason_code(EntryType.LATE_SALE_COMMIT, False), "late_payment")

    def test_sale_commit(self):
        self.assertIsNone(commit_reason_code(EntryType.SALE_COMMIT, False))

    def test_other_entry_types_raise(self):
        for entry_type in (EntryType.RESERVE, EntryType.RELEASE, EntryType.DISPATCH):
            for fault_mode in (False, True):
                with self.subTest(entry_type=entry_type, fault_mode=fault_mode):
                    with self.assertRaises(ValueError):
                        commit_reason_code(entry_type, fault_mode)


class ReserveRequestDigestTest(unittest.TestCase):
    LINES = [("L2", "sku_b", 1), ("L1", "sku_a", 2)]

    def test_matches_request_digest_of_reserve_body(self):
        body = {
            "order_ref": "ord-1",
            "lines": [
                {"line_ref": "L2", "sku_id": "sku_b", "quantity": 1},
                {"line_ref": "L1", "sku_id": "sku_a", "quantity": 2},
            ],
        }
        self.assertEqual(reserve_request_digest("ord-1", self.LINES), request_digest({}, body))

    def test_independent_of_line_order(self):
        self.assertEqual(
            reserve_request_digest("ord-1", self.LINES),
            reserve_request_digest("ord-1", list(reversed(self.LINES))),
        )

    def test_depends_on_content(self):
        digest = reserve_request_digest("ord-1", self.LINES)
        self.assertNotEqual(reserve_request_digest("ord-2", self.LINES), digest)
        self.assertNotEqual(
            reserve_request_digest("ord-1", [("L2", "sku_b", 2), ("L1", "sku_a", 2)]), digest
        )


if __name__ == "__main__":
    unittest.main()
