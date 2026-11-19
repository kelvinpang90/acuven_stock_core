import dataclasses
import unittest

from app.domain.invariants import (
    CHECKS,
    LEDGER_TOTALS,
    MAX_VIOLATIONS,
    ORDER_COMMIT_STATE,
    PENDING_DISPATCH_VS_ORDERS,
    RESERVED_VS_ORDERS,
    BalanceRow,
    LedgerTotals,
    OrderLine,
    OrderTotals,
    ReservationLines,
    check_balance_row,
    check_ledger_totals,
    check_order_commit_state,
    check_pending_dispatch_vs_orders,
    check_reserved_vs_orders,
    summarize_violations,
)


def row(pool_id="pool_A", sku_id="sku_A", **values):
    fields = dict(
        stock_balance=10, reserved=3, unavailable=2, paid_pending_dispatch=4, balance_version=5
    )
    fields.update(values)
    return BalanceRow(pool_id, sku_id, **fields)


def ledger(**values):
    fields = dict(
        stock_balance=10,
        reserved=3,
        unavailable=2,
        paid_pending_dispatch=4,
        entry_count=5,
        max_balance_version_after=5,
    )
    fields.update(values)
    return LedgerTotals(**fields)


ZERO_ROW = row(
    stock_balance=0, reserved=0, unavailable=0, paid_pending_dispatch=0, balance_version=0
)


def reservation(status, *lines, reservation_id="rsv_A", order_ref="order-1"):
    order_lines = tuple(OrderLine(*line) for line in lines)
    return ReservationLines(reservation_id, order_ref, status, order_lines)


class ConstantsTest(unittest.TestCase):
    def test_check_names_in_contract_order(self):
        self.assertEqual(
            CHECKS,
            (
                "ledger_totals",
                "reserved_vs_orders",
                "pending_dispatch_vs_orders",
                "order_commit_state",
            ),
        )
        self.assertEqual(
            (LEDGER_TOTALS, RESERVED_VS_ORDERS, PENDING_DISPATCH_VS_ORDERS, ORDER_COMMIT_STATE),
            CHECKS,
        )
        self.assertEqual(MAX_VIOLATIONS, 100)

    def test_inputs_are_immutable(self):
        for value in (
            row(),
            ledger(),
            OrderTotals(),
            OrderLine("1", 1, 0),
            reservation("active", ("1", 1, 0)),
        ):
            with self.subTest(value=value):
                field = dataclasses.fields(value)[0].name
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(value, field, None)

    def test_defaults_are_zero(self):
        self.assertEqual(dataclasses.astuple(LedgerTotals()), (0, 0, 0, 0, 0, 0))
        self.assertEqual(dataclasses.astuple(OrderTotals()), (0, 0))


class LedgerTotalsTest(unittest.TestCase):
    def test_matching_totals_pass(self):
        self.assertIsNone(check_ledger_totals(row(), ledger()))

    def test_row_without_ledger_passes(self):
        self.assertIsNone(check_ledger_totals(ZERO_ROW, LedgerTotals()))

    def test_violation_shape(self):
        violation = check_ledger_totals(row(), ledger(stock_balance=11))
        self.assertEqual(
            violation,
            {
                "check": "ledger_totals",
                "pool_id": "pool_A",
                "sku_id": "sku_A",
                "expected": {
                    "stock_balance": 11,
                    "reserved": 3,
                    "unavailable": 2,
                    "paid_pending_dispatch": 4,
                    "entry_count": 5,
                    "max_balance_version_after": 5,
                },
                "actual": {
                    "stock_balance": 10,
                    "reserved": 3,
                    "unavailable": 2,
                    "paid_pending_dispatch": 4,
                    "balance_version": 5,
                },
            },
        )
        self.assertEqual(list(violation), ["check", "pool_id", "sku_id", "expected", "actual"])

    def test_each_mismatch_reports_one(self):
        cases = (
            ledger(stock_balance=9),
            ledger(reserved=4),
            ledger(unavailable=1),
            ledger(paid_pending_dispatch=0),
            ledger(entry_count=4),
            ledger(entry_count=6),
            ledger(max_balance_version_after=4),
            ledger(max_balance_version_after=6),
            ledger(entry_count=4, max_balance_version_after=6),
        )
        for totals in cases:
            with self.subTest(totals=totals):
                violation = check_ledger_totals(row(), totals)
                self.assertIsNotNone(violation)
                self.assertEqual(violation["check"], LEDGER_TOTALS)
                self.assertEqual(violation["expected"]["entry_count"], totals.entry_count)
                self.assertEqual(
                    violation["expected"]["max_balance_version_after"],
                    totals.max_balance_version_after,
                )

    def test_version_mismatch_on_row_side(self):
        violation = check_ledger_totals(row(balance_version=6), ledger())
        self.assertEqual(violation["actual"]["balance_version"], 6)
        self.assertEqual(violation["expected"]["entry_count"], 5)

    def test_row_with_values_but_no_ledger(self):
        violation = check_ledger_totals(row(), LedgerTotals())
        self.assertEqual(
            violation["expected"],
            {
                "stock_balance": 0,
                "reserved": 0,
                "unavailable": 0,
                "paid_pending_dispatch": 0,
                "entry_count": 0,
                "max_balance_version_after": 0,
            },
        )


class OrderChecksTest(unittest.TestCase):
    def test_reserved_equal_passes(self):
        self.assertIsNone(check_reserved_vs_orders(row(reserved=3), OrderTotals(active_reserved=3)))

    def test_reserved_mismatch(self):
        self.assertEqual(
            check_reserved_vs_orders(row(reserved=3), OrderTotals(active_reserved=5)),
            {
                "check": "reserved_vs_orders",
                "pool_id": "pool_A",
                "sku_id": "sku_A",
                "expected": 5,
                "actual": 3,
            },
        )

    def test_pending_dispatch_equal_passes(self):
        self.assertIsNone(
            check_pending_dispatch_vs_orders(
                row(paid_pending_dispatch=4), OrderTotals(pending_dispatch=4)
            )
        )

    def test_pending_dispatch_mismatch(self):
        self.assertEqual(
            check_pending_dispatch_vs_orders(
                row(paid_pending_dispatch=4), OrderTotals(pending_dispatch=1)
            ),
            {
                "check": "pending_dispatch_vs_orders",
                "pool_id": "pool_A",
                "sku_id": "sku_A",
                "expected": 1,
                "actual": 4,
            },
        )

    def test_zero_row_without_orders_passes(self):
        self.assertEqual(check_balance_row(ZERO_ROW, LedgerTotals(), OrderTotals()), [])

    def test_balance_row_reports_in_check_order(self):
        violations = check_balance_row(
            row(), ledger(reserved=0), OrderTotals(active_reserved=0, pending_dispatch=0)
        )
        self.assertEqual(
            [v["check"] for v in violations],
            [LEDGER_TOTALS, RESERVED_VS_ORDERS, PENDING_DISPATCH_VS_ORDERS],
        )
        self.assertEqual(
            check_balance_row(row(), ledger(), OrderTotals(active_reserved=3, pending_dispatch=4)),
            [],
        )


class OrderCommitStateTest(unittest.TestCase):
    def test_committed_all_lines_confirmed_passes(self):
        committed = reservation("committed", ("1", 2, 2), ("2", 1, 1))
        self.assertIsNone(check_order_commit_state(committed))

    def test_committed_line_not_confirmed(self):
        violation = check_order_commit_state(
            reservation("committed", ("b", 2, 2), ("c", 1, 0), ("a", 3, 0))
        )
        self.assertEqual(
            violation,
            {
                "check": "order_commit_state",
                "reservation_id": "rsv_A",
                "order_ref": "order-1",
                "status": "committed",
                "line_refs": ["a", "c"],
            },
        )

    def test_other_statuses_with_zero_committed_pass(self):
        for status in ("active", "released", "expired", "late_payment_review", "closed_refunded"):
            with self.subTest(status=status):
                self.assertIsNone(
                    check_order_commit_state(reservation(status, ("1", 2, 0), ("2", 1, 0)))
                )

    def test_other_statuses_with_committed_line(self):
        for status in ("active", "released", "expired", "late_payment_review", "closed_refunded"):
            with self.subTest(status=status):
                violation = check_order_commit_state(
                    reservation(status, ("2", 2, 0), ("1", 1, 1), ("3", 4, 2))
                )
                self.assertEqual(violation["status"], status)
                self.assertEqual(violation["line_refs"], ["1", "3"])


class SummarizeTest(unittest.TestCase):
    def test_no_violations(self):
        self.assertEqual(summarize_violations([]), (0, []))

    def test_order_by_check_then_keys(self):
        commit_b = check_order_commit_state(reservation("active", ("1", 1, 1), reservation_id="b"))
        commit_a = check_order_commit_state(reservation("active", ("1", 1, 1), reservation_id="a"))
        reserved_b = check_reserved_vs_orders(row("pool_A", "sku_B"), OrderTotals())
        reserved_a = check_reserved_vs_orders(row("pool_A", "sku_A"), OrderTotals())
        pending = check_pending_dispatch_vs_orders(row("pool_A", "sku_A"), OrderTotals())
        ledger_b = check_ledger_totals(row("pool_B", "sku_A"), LedgerTotals())
        ledger_a2 = check_ledger_totals(row("pool_A", "sku_B"), LedgerTotals())
        ledger_a1 = check_ledger_totals(row("pool_A", "sku_A"), LedgerTotals())
        count, violations = summarize_violations(
            [commit_b, pending, reserved_b, ledger_b, commit_a, ledger_a2, reserved_a, ledger_a1]
        )
        self.assertEqual(count, 8)
        self.assertEqual(
            violations,
            [ledger_a1, ledger_a2, ledger_b, reserved_a, reserved_b, pending, commit_a, commit_b],
        )

    def test_truncates_to_100_and_counts_all(self):
        violations = [
            check_reserved_vs_orders(row("pool_A", f"sku_{index:03d}"), OrderTotals())
            for index in reversed(range(150))
        ]
        count, kept = summarize_violations(iter(violations))
        self.assertEqual(count, 150)
        self.assertEqual(len(kept), 100)
        self.assertEqual(kept, sorted(violations, key=lambda v: v["sku_id"])[:100])
        self.assertEqual((kept[0]["sku_id"], kept[-1]["sku_id"]), ("sku_000", "sku_099"))

    def test_exactly_100(self):
        violations = [
            check_reserved_vs_orders(row("pool_A", f"sku_{index:03d}"), OrderTotals())
            for index in range(100)
        ]
        count, kept = summarize_violations(violations)
        self.assertEqual((count, len(kept)), (100, 100))


if __name__ == "__main__":
    unittest.main()
