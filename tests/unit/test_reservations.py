import unittest
from datetime import UTC, datetime, timedelta

from app.domain import reservations
from app.domain.ids import new_id
from app.domain.reservations import (
    ReleaseDecision,
    availability_status,
    compute_expires_at,
    displayed_status,
    find_shortfalls,
    is_due,
    parse_ttl_seconds,
    release_decision,
)

EXPIRES_AT = datetime(2026, 11, 16, 8, 15, 0)
ONE_MICROSECOND = timedelta(microseconds=1)
BEFORE = EXPIRES_AT - ONE_MICROSECOND
LATER = EXPIRES_AT + ONE_MICROSECOND

NOT_ACTIVE = ("released", "expired", "committed", "late_payment_review", "closed_refunded")


class ConstantsTest(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(
            reservations.STATUSES,
            (
                "active",
                "released",
                "expired",
                "committed",
                "late_payment_review",
                "closed_refunded",
            ),
        )

    def test_release_reasons_and_ledger(self):
        self.assertEqual(
            reservations.RELEASE_REASONS, ("cancelled", "payment_failed", "checkout_replaced")
        )
        self.assertEqual(reservations.EXPIRED_REASON_CODE, "expired")
        self.assertEqual(reservations.SOURCE_TYPE, "reservation")

    def test_id_prefixes_work_with_new_id(self):
        self.assertEqual(reservations.RESERVATION_ID_PREFIX, "rsv")
        self.assertEqual(reservations.ORDER_LINE_ID_PREFIX, "oln")
        self.assertTrue(new_id(reservations.RESERVATION_ID_PREFIX).startswith("rsv_"))
        self.assertTrue(new_id(reservations.ORDER_LINE_ID_PREFIX).startswith("oln_"))

    def test_bounds(self):
        self.assertEqual(
            (reservations.TTL_DEFAULT, reservations.TTL_MIN, reservations.TTL_MAX), (900, 60, 3600)
        )
        self.assertEqual(
            (reservations.LINE_QUANTITY_MIN, reservations.LINE_QUANTITY_MAX), (1, 1000000)
        )

    def test_availability_statuses(self):
        self.assertEqual(
            (reservations.MERGED, reservations.INACTIVE, reservations.SELLABLE),
            ("merged", "inactive", "sellable"),
        )


class TtlTest(unittest.TestCase):
    def test_accepts_bounds_and_default(self):
        for value in (60, 900, 3600):
            with self.subTest(value=value):
                self.assertEqual(parse_ttl_seconds(value), value)

    def test_default_is_valid(self):
        self.assertEqual(parse_ttl_seconds(reservations.TTL_DEFAULT), 900)

    def test_rejects_out_of_range(self):
        for value in (59, 3601, 0, -900):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_ttl_seconds(value)

    def test_rejects_other_types(self):
        for value in (True, False, "900", "60", 900.0, 60.5, None, [900]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_ttl_seconds(value)


class ExpiresAtTest(unittest.TestCase):
    def test_adds_seconds(self):
        created_at = datetime(2026, 11, 16, 8, 0, 0, 123456)
        self.assertEqual(
            compute_expires_at(created_at, 900), datetime(2026, 11, 16, 8, 15, 0, 123456)
        )
        self.assertEqual(
            compute_expires_at(created_at, 60), datetime(2026, 11, 16, 8, 1, 0, 123456)
        )

    def test_crosses_day_and_keeps_timezone(self):
        created_at = datetime(2026, 12, 31, 23, 30, 0, tzinfo=UTC)
        self.assertEqual(
            compute_expires_at(created_at, 3600), datetime(2027, 1, 1, 0, 30, 0, tzinfo=UTC)
        )


class DueTest(unittest.TestCase):
    def test_active_boundary(self):
        self.assertFalse(is_due("active", EXPIRES_AT, BEFORE))
        self.assertTrue(is_due("active", EXPIRES_AT, EXPIRES_AT))
        self.assertTrue(is_due("active", EXPIRES_AT, LATER))

    def test_only_active_becomes_due(self):
        for status in NOT_ACTIVE:
            for now in (BEFORE, EXPIRES_AT, LATER):
                with self.subTest(status=status, now=now):
                    self.assertFalse(is_due(status, EXPIRES_AT, now))

    def test_displayed_status_for_active(self):
        self.assertEqual(displayed_status("active", EXPIRES_AT, BEFORE), "active")
        self.assertEqual(displayed_status("active", EXPIRES_AT, EXPIRES_AT), "expired")
        self.assertEqual(displayed_status("active", EXPIRES_AT, LATER), "expired")

    def test_displayed_status_keeps_others(self):
        for status in NOT_ACTIVE:
            for now in (BEFORE, EXPIRES_AT, LATER):
                with self.subTest(status=status, now=now):
                    self.assertEqual(displayed_status(status, EXPIRES_AT, now), status)


class ReleaseDecisionTest(unittest.TestCase):
    def test_active_before_expiry_releases(self):
        self.assertIs(release_decision("active", EXPIRES_AT, BEFORE), ReleaseDecision.RELEASE)

    def test_active_at_or_after_expiry_expires(self):
        self.assertIs(release_decision("active", EXPIRES_AT, EXPIRES_AT), ReleaseDecision.EXPIRE)
        self.assertIs(release_decision("active", EXPIRES_AT, LATER), ReleaseDecision.EXPIRE)

    def test_released_or_expired_unchanged(self):
        for status in ("released", "expired"):
            for now in (BEFORE, LATER):
                with self.subTest(status=status, now=now):
                    self.assertIs(
                        release_decision(status, EXPIRES_AT, now), ReleaseDecision.UNCHANGED
                    )

    def test_committed_and_later_conflict(self):
        for status in ("committed", "late_payment_review", "closed_refunded"):
            for now in (BEFORE, LATER):
                with self.subTest(status=status, now=now):
                    self.assertIs(
                        release_decision(status, EXPIRES_AT, now), ReleaseDecision.CONFLICT
                    )

    def test_every_status_is_covered(self):
        for status in reservations.STATUSES:
            with self.subTest(status=status):
                self.assertIsInstance(release_decision(status, EXPIRES_AT, BEFORE), ReleaseDecision)

    def test_unknown_status(self):
        for status in ("rejected", "ACTIVE", "", " active", None):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    release_decision(status, EXPIRES_AT, BEFORE)


class ShortfallTest(unittest.TestCase):
    def test_all_enough(self):
        lines = [("l1", "sku_a", 2), ("l2", "sku_b", 5)]
        self.assertEqual(find_shortfalls(lines, {"sku_a": 10, "sku_b": 5}), [])

    def test_quantity_equal_to_available_is_enough(self):
        self.assertEqual(find_shortfalls([("l1", "sku_a", 7)], {"sku_a": 7}), [])

    def test_zero_available(self):
        self.assertEqual(
            find_shortfalls([("l1", "sku_a", 1)], {"sku_a": 0}),
            [{"line_ref": "l1", "sku_id": "sku_a", "quantity": 1, "available": 0}],
        )

    def test_lists_all_short_lines_in_request_order(self):
        lines = [
            ("l3", "sku_c", 4),
            ("l1", "sku_a", 1),
            ("l2", "sku_b", 9),
            ("l4", "sku_d", 3),
        ]
        available = {"sku_a": 5, "sku_b": 8, "sku_c": 0, "sku_d": 3}
        self.assertEqual(
            find_shortfalls(lines, available),
            [
                {"line_ref": "l3", "sku_id": "sku_c", "quantity": 4, "available": 0},
                {"line_ref": "l2", "sku_id": "sku_b", "quantity": 9, "available": 8},
            ],
        )

    def test_empty_lines(self):
        self.assertEqual(find_shortfalls([], {}), [])


class AvailabilityStatusTest(unittest.TestCase):
    def test_merged_comes_first(self):
        for sku_active in (True, False):
            for product_active in (True, False):
                with self.subTest(sku_active=sku_active, product_active=product_active):
                    status = availability_status("sku_canonical", sku_active, product_active)
                    self.assertEqual(status, "merged")

    def test_inactive(self):
        self.assertEqual(availability_status(None, True, False), "inactive")
        self.assertEqual(availability_status(None, False, True), "inactive")
        self.assertEqual(availability_status(None, False, False), "inactive")

    def test_sellable(self):
        self.assertEqual(availability_status(None, True, True), "sellable")


if __name__ == "__main__":
    unittest.main()
