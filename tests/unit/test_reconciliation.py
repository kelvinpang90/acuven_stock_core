import hashlib
import unittest
from datetime import UTC, datetime, timedelta

from app.domain.catalog import InvalidParameter
from app.domain.ids import new_id
from app.domain.reconciliation import (
    CHANGED_TO_DELAY_SECONDS,
    REVIEW_STATUS,
    WINDOW_MIN,
    ChangedToTooLate,
    ReconciliationCursor,
    ReviewCursor,
    check_window,
    format_reconciliation_cursor,
    format_review_cursor,
    is_reservation_id,
    latest_changed_to,
    match_reconciliation_cursor,
    match_review_cursor,
    parse_changed_at,
    parse_page_limit,
    parse_reconciliation_cursor,
    parse_review_cursor,
    parse_review_status,
    parse_window,
    status_digest,
    window_digest,
)

RSV = "rsv_01JD8X3Q7Z4K9M2N5P6R8T0V1W"
FROM = datetime(2026, 11, 17)
TO = datetime(2026, 11, 18)
CHANGED = datetime(2026, 11, 17, 3, 4, 5, 678901)
CHANGED_US = 1794884645678901

BAD_RESERVATION_IDS = (
    "",
    "rsv_",
    "rsv_01JD8X3Q7Z4K9M2N5P6R8T0V1",  # 25 个字符
    "rsv_01JD8X3Q7Z4K9M2N5P6R8T0V1WX",  # 27 个字符
    "rsv_01jd8x3q7z4k9m2n5p6r8t0v1w",  # 小写
    "rsv_01JD8X3Q7Z4K9M2N5P6R8T0V1I",  # I 不在 Crockford Base32 里
    "rsv_01JD8X3Q7Z4K9M2N5P6R8T0V1L",
    "rsv_01JD8X3Q7Z4K9M2N5P6R8T0V1O",
    "rsv_01JD8X3Q7Z4K9M2N5P6R8T0V1U",
    "oln_01JD8X3Q7Z4K9M2N5P6R8T0V1W",
    "RSV_01JD8X3Q7Z4K9M2N5P6R8T0V1W",
    "rsv-01JD8X3Q7Z4K9M2N5P6R8T0V1W",
    "rsv01JD8X3Q7Z4K9M2N5P6R8T0V1W",
    " rsv_01JD8X3Q7Z4K9M2N5P6R8T0V1W",
)


class ParameterErrorMixin:
    def assert_bad_parameter(self, parameter, call, *args):
        with self.assertRaises(InvalidParameter) as caught:
            call(*args)
        self.assertEqual(caught.exception.parameter, parameter)


class ChangedAtTest(ParameterErrorMixin, unittest.TestCase):
    def test_normalizes_to_utc(self):
        cases = (
            ("2026-11-18T08:00:00+08:00", datetime(2026, 11, 18)),
            ("2026-11-17T19:30:00-04:30", datetime(2026, 11, 18)),
            ("2026-11-18T00:00:00Z", datetime(2026, 11, 18)),
            ("2026-11-18T00:00:00.5Z", datetime(2026, 11, 18, 0, 0, 0, 500000)),
            ("2026-11-18T00:00:00.123456+00:00", datetime(2026, 11, 18, 0, 0, 0, 123456)),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                parsed = parse_changed_at(value, "changed_from")
                self.assertEqual(parsed, expected)
                self.assertIsNone(parsed.tzinfo)

    def test_lower_bound_is_inclusive(self):
        self.assertEqual(WINDOW_MIN, datetime(2000, 1, 1))
        for value in ("2000-01-01T00:00:00Z", "2000-01-01T08:00:00+08:00"):
            with self.subTest(value=value):
                self.assertEqual(parse_changed_at(value, "changed_to"), WINDOW_MIN)

    def test_rejects_one_microsecond_before_lower_bound(self):
        for value in (
            "1999-12-31T23:59:59.999999Z",
            "2000-01-01T07:59:59.999999+08:00",
            "1001-01-01T00:00:00Z",
        ):
            for parameter in ("changed_from", "changed_to"):
                with self.subTest(value=value, parameter=parameter):
                    self.assert_bad_parameter(parameter, parse_changed_at, value, parameter)

    def test_rejects_missing_naive_and_malformed(self):
        for value in (
            None,
            "",
            "2026-11-18T00:00:00",
            "2026-11-18",
            "2026-11-18 00:00:00Z",
            "2026-11-18t00:00:00z",
            "2026-11-18T00:00Z",
            "2026-11-18T00:00:00.1234567Z",
            "2026-02-30T00:00:00Z",
            "2026-11-18T00:00:00+24:00",
            "yesterday",
            1763424000,
            True,
        ):
            for parameter in ("changed_from", "changed_to"):
                with self.subTest(value=value, parameter=parameter):
                    self.assert_bad_parameter(parameter, parse_changed_at, value, parameter)


class WindowTest(ParameterErrorMixin, unittest.TestCase):
    def test_parse_window_returns_both(self):
        self.assertEqual(
            parse_window("2026-11-17T00:00:00Z", "2026-11-18T08:00:00+08:00"), (FROM, TO)
        )

    def test_parse_window_names_the_bad_one(self):
        good = "2026-11-17T00:00:00Z"
        self.assert_bad_parameter("changed_from", parse_window, None, good)
        self.assert_bad_parameter("changed_to", parse_window, good, None)
        self.assert_bad_parameter("changed_to", parse_window, good, "2026-11-18T00:00:00")
        self.assert_bad_parameter("changed_from", parse_window, "1999-12-31T23:59:59Z", good)
        self.assert_bad_parameter("changed_from", parse_window, "x", "y")

    def test_parse_window_does_not_compare_order(self):
        self.assertEqual(parse_window("2026-11-18T00:00:00Z", "2026-11-17T00:00:00Z"), (TO, FROM))

    def test_accepts_end_exactly_delay_before_now(self):
        self.assertEqual(CHANGED_TO_DELAY_SECONDS, 300)
        now = TO + timedelta(seconds=300)
        self.assertEqual(latest_changed_to(now), TO)
        self.assertIsNone(check_window(FROM, TO, now))
        self.assertIsNone(check_window(FROM, TO, now + timedelta(days=1)))

    def test_rejects_end_one_microsecond_too_late(self):
        now = TO + timedelta(seconds=300)
        late = TO + timedelta(microseconds=1)
        with self.assertRaises(ChangedToTooLate) as caught:
            check_window(FROM, late, now)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertNotIsInstance(caught.exception, InvalidParameter)
        self.assertEqual(caught.exception.latest_changed_to, TO)
        self.assertEqual(
            caught.exception.details, {"latest_changed_to": "2026-11-18T00:00:00.000000Z"}
        )

    def test_latest_end_from_aware_now(self):
        now = datetime(2026, 11, 18, 8, 5, tzinfo=UTC)
        self.assertEqual(latest_changed_to(now), datetime(2026, 11, 18, 8, 0))
        with self.assertRaises(ChangedToTooLate) as caught:
            check_window(FROM, datetime(2026, 11, 18, 9), now)
        self.assertEqual(caught.exception.latest_changed_to, datetime(2026, 11, 18, 8, 0))

    def test_rejects_from_not_before_to(self):
        now = datetime(2026, 11, 19)
        self.assert_bad_parameter("changed_from", check_window, TO, TO, now)
        self.assert_bad_parameter("changed_from", check_window, TO, FROM, now)
        self.assert_bad_parameter(
            "changed_from", check_window, FROM + timedelta(microseconds=1), FROM, now
        )

    def test_order_error_before_end_too_late(self):
        now = datetime(2026, 11, 18)
        end = now + timedelta(days=1)
        self.assert_bad_parameter("changed_from", check_window, end, end, now)
        self.assert_bad_parameter("changed_from", check_window, end + timedelta(days=1), end, now)

    def test_limit_same_as_ledger(self):
        self.assertEqual(parse_page_limit(None), 100)
        self.assertEqual(parse_page_limit("1"), 1)
        self.assertEqual(parse_page_limit("100"), 100)
        for value in ("0", "101", "01", "-1", "", "abc"):
            with self.subTest(value=value):
                self.assert_bad_parameter("limit", parse_page_limit, value)


class WindowDigestTest(unittest.TestCase):
    def test_digest_of_normalized_times(self):
        text = "2026-11-17T00:00:00.000000Z\n2026-11-18T00:00:00.000000Z"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(window_digest(FROM, TO), expected)

    def test_aware_and_naive_utc_give_same_digest(self):
        self.assertEqual(
            window_digest(FROM.replace(tzinfo=UTC), TO.replace(tzinfo=UTC)),
            window_digest(FROM, TO),
        )

    def test_any_time_change_changes_digest(self):
        base = window_digest(FROM, TO)
        self.assertNotEqual(window_digest(FROM + timedelta(microseconds=1), TO), base)
        self.assertNotEqual(window_digest(FROM, TO + timedelta(microseconds=1)), base)
        self.assertNotEqual(window_digest(TO, FROM), base)


class ReservationIdTest(unittest.TestCase):
    def test_accepts_generated_and_sample_ids(self):
        self.assertTrue(is_reservation_id(RSV))
        self.assertTrue(is_reservation_id(new_id("rsv")))

    def test_rejects_bad_shapes(self):
        for value in (*BAD_RESERVATION_IDS, None, 1, new_id("oln")):
            with self.subTest(value=value):
                self.assertFalse(is_reservation_id(value))


class ReconciliationCursorTest(ParameterErrorMixin, unittest.TestCase):
    def cursor(self, timestamp=str(CHANGED_US), reservation_id=RSV, digest=None):
        digest = window_digest(FROM, TO) if digest is None else digest
        return f"v1.{timestamp}.{reservation_id}.{digest}"

    def test_format(self):
        self.assertEqual(format_reconciliation_cursor(CHANGED, RSV, FROM, TO), self.cursor())
        self.assertEqual(
            format_reconciliation_cursor(WINDOW_MIN, RSV, FROM, TO),
            self.cursor(timestamp="946684800000000"),
        )
        self.assertEqual(
            format_reconciliation_cursor(CHANGED.replace(tzinfo=UTC), RSV, FROM, TO),
            self.cursor(),
        )

    def test_format_rejects_bad_values(self):
        for reservation_id in BAD_RESERVATION_IDS:
            with self.subTest(reservation_id=reservation_id):
                with self.assertRaises(ValueError):
                    format_reconciliation_cursor(CHANGED, reservation_id, FROM, TO)
        with self.assertRaises(ValueError):
            format_reconciliation_cursor(datetime(1970, 1, 1), RSV, FROM, TO)

    def test_round_trip(self):
        value = format_reconciliation_cursor(CHANGED, RSV, FROM, TO)
        cursor = parse_reconciliation_cursor(value)
        self.assertEqual(cursor, ReconciliationCursor(CHANGED, RSV, window_digest(FROM, TO)))
        self.assertIsNone(cursor.last_changed_at.tzinfo)
        self.assertIs(match_reconciliation_cursor(cursor, FROM, TO), cursor)

    def test_round_trip_generated_id(self):
        reservation_id = new_id("rsv")
        value = format_reconciliation_cursor(WINDOW_MIN, reservation_id, FROM, TO)
        cursor = parse_reconciliation_cursor(value)
        self.assertEqual(cursor.last_changed_at, WINDOW_MIN)
        self.assertEqual(cursor.reservation_id, reservation_id)

    def test_absent(self):
        self.assertIsNone(parse_reconciliation_cursor(None))
        self.assertIsNone(match_reconciliation_cursor(None, FROM, TO))

    def test_other_window_does_not_match(self):
        cursor = parse_reconciliation_cursor(format_reconciliation_cursor(CHANGED, RSV, FROM, TO))
        for start, end in (
            (FROM + timedelta(microseconds=1), TO),
            (FROM, TO + timedelta(microseconds=1)),
            (FROM - timedelta(days=1), TO),
        ):
            with self.subTest(start=start, end=end):
                self.assert_bad_parameter("after", match_reconciliation_cursor, cursor, start, end)

    def test_rejects_bad_prefix(self):
        for prefix in ("v2", "V1", "", "v1 ", "v"):
            value = f"{prefix}.{CHANGED_US}.{RSV}.{window_digest(FROM, TO)}"
            with self.subTest(value=value):
                self.assert_bad_parameter("after", parse_reconciliation_cursor, value)

    def test_rejects_wrong_segment_count(self):
        digest = window_digest(FROM, TO)
        for value in (
            "",
            "v1",
            f"v1.{CHANGED_US}.{RSV}",
            f"v1.{RSV}.{digest}",
            f"v1.{CHANGED_US}.{RSV}.{digest}.",
            f"v1.{CHANGED_US}.{RSV}.{digest}.x",
            f"v1..{CHANGED_US}.{RSV}.{digest}",
        ):
            with self.subTest(value=value):
                self.assert_bad_parameter("after", parse_reconciliation_cursor, value)

    def test_rejects_bad_timestamp(self):
        for timestamp in (
            "",
            "0",
            "0" + str(CHANGED_US),
            "00",
            "-1",
            "+" + str(CHANGED_US),
            "abc",
            "12a",
            " 1",
            "１２３",
            "1e9",
            "9" * 18,  # 超出 datetime 的范围
            "1" * 19,
        ):
            with self.subTest(timestamp=timestamp):
                self.assert_bad_parameter(
                    "after", parse_reconciliation_cursor, self.cursor(timestamp=timestamp)
                )

    def test_rejects_bad_reservation_id(self):
        for reservation_id in BAD_RESERVATION_IDS:
            with self.subTest(reservation_id=reservation_id):
                self.assert_bad_parameter(
                    "after",
                    parse_reconciliation_cursor,
                    self.cursor(reservation_id=reservation_id),
                )

    def test_rejects_bad_digest(self):
        digest = window_digest(FROM, TO)
        for bad in ("", digest[:15], digest + "0", "0" * 17, "ABCDEF0123456789", "g" * 16):
            with self.subTest(digest=bad):
                self.assert_bad_parameter(
                    "after", parse_reconciliation_cursor, self.cursor(digest=bad)
                )
        if digest.upper() != digest:
            self.assert_bad_parameter(
                "after", parse_reconciliation_cursor, self.cursor(digest=digest.upper())
            )

    def test_rejects_non_strings(self):
        for value in (1, True, [self.cursor()], b"v1"):
            with self.subTest(value=value):
                self.assert_bad_parameter("after", parse_reconciliation_cursor, value)


class ReviewTest(ParameterErrorMixin, unittest.TestCase):
    def test_status_constant(self):
        self.assertEqual(REVIEW_STATUS, "late_payment_review")
        self.assertEqual(parse_review_status("late_payment_review"), "late_payment_review")

    def test_rejects_other_statuses(self):
        for value in (
            None,
            "",
            "active",
            "committed",
            "expired",
            "LATE_PAYMENT_REVIEW",
            " late_payment_review",
            "late_payment_review,active",
        ):
            with self.subTest(value=value):
                self.assert_bad_parameter("status", parse_review_status, value)

    def test_status_digest(self):
        expected = hashlib.sha256(b"late_payment_review").hexdigest()[:16]
        self.assertEqual(status_digest(REVIEW_STATUS), expected)

    def test_format(self):
        self.assertEqual(
            format_review_cursor(RSV, REVIEW_STATUS), f"v1.{RSV}.{status_digest(REVIEW_STATUS)}"
        )
        for reservation_id in BAD_RESERVATION_IDS:
            with self.subTest(reservation_id=reservation_id):
                with self.assertRaises(ValueError):
                    format_review_cursor(reservation_id, REVIEW_STATUS)

    def test_round_trip_and_match(self):
        cursor = parse_review_cursor(format_review_cursor(RSV, REVIEW_STATUS))
        self.assertEqual(cursor, ReviewCursor(RSV, status_digest(REVIEW_STATUS)))
        self.assertIs(match_review_cursor(cursor, REVIEW_STATUS), cursor)

    def test_absent(self):
        self.assertIsNone(parse_review_cursor(None))
        self.assertIsNone(match_review_cursor(None, REVIEW_STATUS))

    def test_other_status_does_not_match(self):
        cursor = parse_review_cursor(format_review_cursor(RSV, "committed"))
        self.assert_bad_parameter("after", match_review_cursor, cursor, REVIEW_STATUS)

    def cursor(self, prefix="v1", reservation_id=RSV, digest=None):
        digest = status_digest(REVIEW_STATUS) if digest is None else digest
        return f"{prefix}.{reservation_id}.{digest}"

    def test_rejects_bad_prefix(self):
        for prefix in ("v2", "V1", "", "v"):
            with self.subTest(prefix=prefix):
                self.assert_bad_parameter("after", parse_review_cursor, self.cursor(prefix=prefix))

    def test_rejects_wrong_segment_count(self):
        digest = status_digest(REVIEW_STATUS)
        for value in (
            "",
            "v1",
            f"v1.{RSV}",
            f"v1.{CHANGED_US}.{RSV}.{digest}",
            f"v1.{RSV}.{digest}.",
            f"v1..{RSV}.{digest}",
        ):
            with self.subTest(value=value):
                self.assert_bad_parameter("after", parse_review_cursor, value)

    def test_rejects_bad_reservation_id(self):
        for reservation_id in BAD_RESERVATION_IDS:
            with self.subTest(reservation_id=reservation_id):
                self.assert_bad_parameter(
                    "after", parse_review_cursor, self.cursor(reservation_id=reservation_id)
                )

    def test_rejects_bad_digest(self):
        digest = status_digest(REVIEW_STATUS)
        for bad in ("", digest[:15], digest + "0", "ABCDEF0123456789", "g" * 16):
            with self.subTest(digest=bad):
                self.assert_bad_parameter("after", parse_review_cursor, self.cursor(digest=bad))
        if digest.upper() != digest:
            self.assert_bad_parameter(
                "after", parse_review_cursor, self.cursor(digest=digest.upper())
            )

    def test_rejects_non_strings(self):
        for value in (1, True, [self.cursor()]):
            with self.subTest(value=value):
                self.assert_bad_parameter("after", parse_review_cursor, value)


if __name__ == "__main__":
    unittest.main()
