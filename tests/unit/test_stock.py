import hashlib
import unittest
from datetime import UTC, datetime

from app.domain.catalog import InvalidField, InvalidParameter, normalize_reason
from app.domain.stock import (
    format_ledger_cursor,
    is_non_negative_quantity,
    is_positive_quantity,
    ledger_filter_digest,
    normalize_note,
    normalize_reason_code,
    parse_ledger_cursor,
    parse_ledger_limit,
    parse_ledger_range,
    parse_non_negative_quantity,
    parse_occurred_at,
    parse_positive_quantity,
    parse_sku_ids,
)

BIGINT_MAX = 9223372036854775807


class FieldErrorMixin:
    def assert_invalid(self, field, call, *args):
        with self.assertRaises(InvalidField) as caught:
            call(*args)
        self.assertEqual(caught.exception.field, field)

    def assert_bad_parameter(self, parameter, call, *args):
        with self.assertRaises(InvalidParameter) as caught:
            call(*args)
        self.assertEqual(caught.exception.parameter, parameter)


class ReasonCodeTest(FieldErrorMixin, unittest.TestCase):
    def test_accepts_lowercase_digits_and_underscore(self):
        for value in ("damaged", "supplier_short", "a", "x" * 32, "r2_d2", "_"):
            with self.subTest(value=value):
                self.assertEqual(normalize_reason_code(value), value)

    def test_rejects_bad_values(self):
        for value in (
            "",
            "x" * 33,
            "Damaged",
            "DAMAGED",
            "supplier-short",
            "two words",
            " damaged",
            "damaged ",
            "damaged\n",
            "dämaged",
            "１",
        ):
            with self.subTest(value=value):
                self.assert_invalid("reason_code", normalize_reason_code, value)

    def test_rejects_non_strings(self):
        for value in (None, 1, True, ["damaged"], {"code": "damaged"}):
            with self.subTest(value=value):
                self.assert_invalid("reason_code", normalize_reason_code, value)


class NoteTest(FieldErrorMixin, unittest.TestCase):
    def test_same_as_catalog_reason(self):
        for value in (None, "ok", "  padded  ", "a" * 500, "  " + "a" * 500 + "\n", "中文备注"):
            with self.subTest(value=value):
                self.assertEqual(normalize_note(value), normalize_reason(value))

    def test_rejects_what_catalog_reason_rejects_under_note(self):
        for value in ("", "   ", "a" * 501, "a\x00b", "line\nbreak", "\x7f", 1, True, ["x"]):
            with self.subTest(value=value):
                with self.assertRaises(InvalidField):
                    normalize_reason(value)
                self.assert_invalid("note", normalize_note, value)


class OccurredAtTest(FieldErrorMixin, unittest.TestCase):
    def test_utc_z(self):
        self.assertEqual(
            parse_occurred_at("2026-11-15T08:30:00Z"), datetime(2026, 11, 15, 8, 30, 0)
        )

    def test_result_is_naive(self):
        self.assertIsNone(parse_occurred_at("2026-11-15T08:30:00+08:00").tzinfo)

    def test_offsets_convert_to_utc(self):
        cases = {
            "2026-11-15T08:30:00+08:00": datetime(2026, 11, 15, 0, 30, 0),
            "2026-11-15T08:30:00-05:30": datetime(2026, 11, 15, 14, 0, 0),
            "2026-11-15T08:30:00+00:00": datetime(2026, 11, 15, 8, 30, 0),
            "2026-11-15T08:30:00-00:00": datetime(2026, 11, 15, 8, 30, 0),
            "2026-01-01T03:00:00+08:00": datetime(2025, 12, 31, 19, 0, 0),
            "2026-12-31T20:00:00-05:00": datetime(2027, 1, 1, 1, 0, 0),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_occurred_at(value), expected)

    def test_fractional_seconds_up_to_six_digits(self):
        cases = {
            "2026-11-15T08:30:00.1Z": 100000,
            "2026-11-15T08:30:00.123Z": 123000,
            "2026-11-15T08:30:00.000001Z": 1,
            "2026-11-15T08:30:00.999999+08:00": 999999,
        }
        for value, microsecond in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_occurred_at(value).microsecond, microsecond)

    def test_year_bounds(self):
        self.assertEqual(parse_occurred_at("1001-01-01T00:00:00Z"), datetime(1001, 1, 1))
        self.assertEqual(
            parse_occurred_at("9998-12-31T23:59:59.999999Z"),
            datetime(9998, 12, 31, 23, 59, 59, 999999),
        )
        # 两头换算时区后仍在 DATETIME(6) 的范围内。
        self.assertEqual(
            parse_occurred_at("1001-01-01T00:00:00+23:59"), datetime(1000, 12, 31, 0, 1)
        )
        self.assertEqual(
            parse_occurred_at("9998-12-31T23:59:59-23:59"), datetime(9999, 1, 1, 23, 58, 59)
        )
        for value in ("1000-12-31T23:59:59Z", "9999-01-01T00:00:00Z", "0001-01-01T00:00:00Z"):
            with self.subTest(value=value):
                self.assert_invalid("occurred_at", parse_occurred_at, value)

    def test_leap_day(self):
        self.assertEqual(parse_occurred_at("2028-02-29T00:00:00Z"), datetime(2028, 2, 29))

    def test_rejects_malformed(self):
        for value in (
            "2026-11-15T08:30:00",  # 缺时区
            "2026-11-15T08:30Z",  # 缺秒
            "2026-11-15T08:30:00.1234567Z",  # 7 位小数秒
            "2026-11-15T08:30:00.Z",  # 有点没数字
            "2026-02-30T08:30:00Z",  # 非法日期
            "2026-02-29T08:30:00Z",
            "2026-13-01T08:30:00Z",
            "2026-00-10T08:30:00Z",
            "2026-11-00T08:30:00Z",
            "2026-11-15T24:00:00Z",
            "2026-11-15T08:60:00Z",
            "2026-11-15T08:30:60Z",  # 闰秒
            "2026-11-15t08:30:00Z",  # 小写 t
            "2026-11-15T08:30:00z",  # 小写 z
            "2026-11-15 08:30:00Z",
            "2026-11-15T08:30:00+0800",
            "2026-11-15T08:30:00+08",
            "2026-11-15T08:30:00+24:00",
            "2026-11-15T08:30:00+08:60",
            "2026-11-15T08:30:00Z ",
            " 2026-11-15T08:30:00Z",
            "2026-11-15T08:30:00Z\n",
            "2026-11-15",
            "20261115T083000Z",
            "+2026-11-15T08:30:00Z",
            "2026-11-15T08:30:00.１Z",
            "",
        ):
            with self.subTest(value=value):
                self.assert_invalid("occurred_at", parse_occurred_at, value)

    def test_rejects_non_strings(self):
        for value in (None, 1763195400, True, datetime(2026, 11, 15, tzinfo=UTC)):
            with self.subTest(value=value):
                self.assert_invalid("occurred_at", parse_occurred_at, value)


class QuantityTest(FieldErrorMixin, unittest.TestCase):
    def test_positive_bounds(self):
        self.assertEqual(parse_positive_quantity(1), 1)
        self.assertEqual(parse_positive_quantity(1000000), 1000000)
        self.assertTrue(is_positive_quantity(1))
        for value in (0, -1, 1000001, True, False, "1", 1.0, None):
            with self.subTest(value=value):
                self.assertFalse(is_positive_quantity(value))
                self.assert_invalid("quantity", parse_positive_quantity, value)

    def test_non_negative_bounds(self):
        self.assertEqual(parse_non_negative_quantity(0), 0)
        self.assertEqual(parse_non_negative_quantity(1000000), 1000000)
        self.assertTrue(is_non_negative_quantity(0))
        for value in (-1, 1000001, True, False, "0", 0.0, None):
            with self.subTest(value=value):
                self.assertFalse(is_non_negative_quantity(value))
                self.assert_invalid("quantity", parse_non_negative_quantity, value)

    def test_field_name_is_passed_through(self):
        self.assert_invalid("shelf_count", parse_non_negative_quantity, -1, "shelf_count")
        self.assert_invalid("target", parse_positive_quantity, 0, "target")


class SkuIdsTest(FieldErrorMixin, unittest.TestCase):
    def test_one_and_hundred(self):
        self.assertEqual(parse_sku_ids("sku_1"), ["sku_1"])
        ids = [f"sku_{n}" for n in range(100)]
        self.assertEqual(parse_sku_ids(",".join(ids)), ids)

    def test_keeps_order_and_does_not_check_format(self):
        self.assertEqual(parse_sku_ids("b,a, c"), ["b", "a", " c"])

    def test_rejects_bad_lists(self):
        for value in (
            ",".join(f"sku_{n}" for n in range(101)),
            "",
            "a,,b",
            "a,",
            ",a",
            "a,b,a",
            None,
        ):
            with self.subTest(value=value):
                self.assert_bad_parameter("sku_ids", parse_sku_ids, value)


class LedgerRangeTest(FieldErrorMixin, unittest.TestCase):
    def test_optional_bounds(self):
        self.assertEqual(parse_ledger_range(None, None), (None, None))
        self.assertEqual(
            parse_ledger_range("2026-11-15T08:00:00+08:00", None), (datetime(2026, 11, 15), None)
        )
        self.assertEqual(
            parse_ledger_range(None, "2026-11-15T00:00:00Z"), (None, datetime(2026, 11, 15))
        )

    def test_from_before_to(self):
        self.assertEqual(
            parse_ledger_range("2026-11-15T00:00:00Z", "2026-11-15T00:00:00.000001Z"),
            (datetime(2026, 11, 15), datetime(2026, 11, 15, 0, 0, 0, 1)),
        )

    def test_from_not_before_to_is_rejected(self):
        for from_, to in (
            ("2026-11-15T00:00:00Z", "2026-11-15T00:00:00Z"),
            ("2026-11-15T08:00:00+08:00", "2026-11-15T00:00:00Z"),  # 同一时刻
            ("2026-11-15T00:00:01Z", "2026-11-15T00:00:00Z"),
        ):
            with self.subTest(from_=from_, to=to):
                self.assert_bad_parameter("from", parse_ledger_range, from_, to)

    def test_malformed_bounds_name_the_parameter(self):
        self.assert_bad_parameter("from", parse_ledger_range, "2026-11-15T00:00:00", None)
        self.assert_bad_parameter("to", parse_ledger_range, None, "2026-11-15")
        self.assert_bad_parameter("to", parse_ledger_range, "2026-11-15T00:00:00Z", "x")

    def test_limit_same_as_catalog(self):
        self.assertEqual(parse_ledger_limit(None), 100)
        self.assertEqual(parse_ledger_limit("1"), 1)
        self.assertEqual(parse_ledger_limit("100"), 100)
        for value in ("0", "101", "01", "+1", " 1", ""):
            with self.subTest(value=value):
                self.assert_bad_parameter("limit", parse_ledger_limit, value)


POOL = "pool_01"
SKU = "sku_01"
FROM = datetime(2026, 11, 1)
TO = datetime(2026, 11, 15, 12, 30, 0, 250000)


class LedgerCursorTest(FieldErrorMixin, unittest.TestCase):
    def test_digest_spelling(self):
        joined = "pool_01\nsku_01\n2026-11-01T00:00:00.000000Z\n2026-11-15T12:30:00.250000Z"
        expected = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(ledger_filter_digest(POOL, SKU, FROM, TO), expected)

    def test_digest_without_bounds_uses_empty_strings(self):
        expected = hashlib.sha256(b"pool_01\nsku_01\n\n").hexdigest()[:16]
        self.assertEqual(ledger_filter_digest(POOL, SKU, None, None), expected)

    def test_format(self):
        cursor = format_ledger_cursor(42, POOL, SKU, FROM, TO)
        self.assertRegex(cursor, r"\Av1\.42\.[0-9a-f]{16}\Z")
        self.assertEqual(cursor, f"v1.42.{ledger_filter_digest(POOL, SKU, FROM, TO)}")

    def test_round_trip(self):
        for version in (1, 42, BIGINT_MAX):
            for from_, to in ((None, None), (FROM, None), (None, TO), (FROM, TO)):
                with self.subTest(version=version, from_=from_, to=to):
                    cursor = format_ledger_cursor(version, POOL, SKU, from_, to)
                    self.assertEqual(parse_ledger_cursor(cursor, POOL, SKU, from_, to), version)

    def test_round_trip_through_parsed_range(self):
        from_, to = parse_ledger_range("2026-11-01T08:00:00+08:00", "2026-11-15T12:30:00.25Z")
        cursor = format_ledger_cursor(7, POOL, SKU, from_, to)
        # 同一时刻换种写法，规范化后摘要相同。
        again = parse_ledger_range("2026-11-01T00:00:00.000Z", "2026-11-15T20:30:00.250+08:00")
        self.assertEqual(parse_ledger_cursor(cursor, POOL, SKU, *again), 7)

    def test_absent_after(self):
        self.assertIsNone(parse_ledger_cursor(None, POOL, SKU, FROM, TO))

    def test_changed_filter_rejects_cursor(self):
        cursor = format_ledger_cursor(42, POOL, SKU, FROM, TO)
        later = datetime(2026, 11, 16)
        for args in (
            ("pool_02", SKU, FROM, TO),
            (POOL, "sku_02", FROM, TO),
            (POOL, SKU, datetime(2026, 11, 2), TO),
            (POOL, SKU, None, TO),
            (POOL, SKU, FROM, later),
            (POOL, SKU, FROM, None),
            (POOL, SKU, TO, FROM),
        ):
            with self.subTest(args=args):
                self.assert_bad_parameter("after", parse_ledger_cursor, cursor, *args)

    def test_malformed_cursor(self):
        digest = ledger_filter_digest(POOL, SKU, FROM, TO)
        for value in (
            f"v2.42.{digest}",
            f"V1.42.{digest}",
            f"42.{digest}",
            f"v1.42.{digest}.x",
            "v1.42",
            "v1",
            "",
            f"v1.0.{digest}",
            f"v1.042.{digest}",
            f"v1.-42.{digest}",
            f"v1.+42.{digest}",
            f"v1. 42.{digest}",
            f"v1.{BIGINT_MAX + 1}.{digest}",
            f"v1.{'1' * 20}.{digest}",
            f"v1.42.{digest.upper()}",
            f"v1.42.{digest[:15]}",
            f"v1.42.{digest}0",
            "v1.42.ghijklmnopqrstuv",
            f"v1.42.{digest} ",
            f"v1..{digest}",
        ):
            with self.subTest(value=value):
                self.assert_bad_parameter("after", parse_ledger_cursor, value, POOL, SKU, FROM, TO)

    def test_non_string_cursor(self):
        self.assert_bad_parameter("after", parse_ledger_cursor, 42, POOL, SKU, FROM, TO)

    def test_format_rejects_bad_versions(self):
        for version in (0, -1, BIGINT_MAX + 1, True, "42"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    format_ledger_cursor(version, POOL, SKU, FROM, TO)


if __name__ == "__main__":
    unittest.main()
