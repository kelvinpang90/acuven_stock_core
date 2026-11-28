import ast
import hashlib
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.domain import login_throttle
from app.domain.login_throttle import (
    FAILURE_OUTCOMES,
    SOURCE,
    SOURCE_LIMIT,
    THROTTLE,
    THROTTLE_LIMIT,
    WINDOW,
    count_failures,
    in_window,
    is_failure,
    lock_reason,
    normalize_source,
    require_whole_second,
    source_key,
    throttle_key,
    window_start,
)

NOW = datetime(2026, 11, 24, 11, 0, 0)
OUTCOMES = ("success", "bad_password", "unknown_user", "disabled", "locked")


class ModuleTest(unittest.TestCase):
    def test_module_has_no_io_imports(self):
        source = Path(login_throttle.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= {"collections", "datetime", "hashlib", "ipaddress"}, imported)

    def test_limits_and_window(self):
        self.assertEqual(WINDOW, timedelta(minutes=60))
        self.assertEqual(SOURCE_LIMIT, 30)
        self.assertEqual(THROTTLE_LIMIT, 10)


class NormalizeSourceTest(unittest.TestCase):
    def test_ipv4_is_its_canonical_text(self):
        for text in ("203.0.113.7", "0.0.0.0", "255.255.255.255"):
            with self.subTest(text=text):
                self.assertEqual(normalize_source(text), text)

    def test_ipv4_mapped_ipv6_is_the_ipv4_inside(self):
        for text in ("::ffff:203.0.113.7", "::FFFF:203.0.113.7", "::ffff:cb00:7107"):
            with self.subTest(text=text):
                self.assertEqual(normalize_source(text), "203.0.113.7")

    def test_other_ipv6_is_its_64_network(self):
        # L8：同一个 /64 里换地址不算新来源，换一个 /64 才算。
        for text in (
            "2001:db8:1:2::5",
            "2001:db8:1:2::9",
            "2001:0DB8:0001:0002:ffff:ffff:ffff:ffff",
            "2001:db8:1:2::",
        ):
            with self.subTest(text=text):
                self.assertEqual(normalize_source(text), "2001:db8:1:2::/64")
        self.assertEqual(normalize_source("2001:db8:1:3::5"), "2001:db8:1:3::/64")
        self.assertEqual(normalize_source("::1"), "::/64")
        self.assertEqual(normalize_source("fe80::1%eth0"), "fe80::/64")

    def test_anything_else_is_kept_as_is(self):
        for text in (
            "unknown",
            "",
            " 203.0.113.7",
            "203.0.113.7\n",
            "203.0.113.256",
            "203.0.113.07",
            "2001:db8::1::2",
            "2001:db8::/64",
            "example.com",
            "\ud800",
        ):
            with self.subTest(text=text):
                self.assertEqual(normalize_source(text), text)

    def test_non_strings_are_not_parsed_as_addresses(self):
        # ipaddress 会把整数当地址；来源原文总是字符串。
        with self.assertRaises(TypeError):
            normalize_source(3405803783)


class KeysTest(unittest.TestCase):
    def test_throttle_key_is_sha256_of_source_newline_username(self):
        expected = hashlib.sha256(b"203.0.113.7\nkelvin").hexdigest()
        self.assertEqual(throttle_key("203.0.113.7", "kelvin"), expected)
        self.assertEqual(len(expected), 64)

    def test_source_key_is_sha256_of_the_source(self):
        expected = hashlib.sha256(b"2001:db8:1:2::/64").hexdigest()
        self.assertEqual(source_key("2001:db8:1:2::/64"), expected)

    def test_keys_differ_by_source_and_username(self):
        key = throttle_key("203.0.113.7", "kelvin")
        self.assertNotEqual(key, throttle_key("203.0.113.8", "kelvin"))
        self.assertNotEqual(key, throttle_key("203.0.113.7", "kélvin"))
        self.assertNotEqual(source_key("203.0.113.7"), source_key("203.0.113.8"))

    def test_text_not_encodable_as_utf8_is_encoded_with_replace(self):
        self.assertEqual(
            throttle_key("unknown", "kel\ud800vin"),
            hashlib.sha256(b"unknown\nkel?vin").hexdigest(),
        )
        self.assertEqual(throttle_key("\udcff", "kelvin"), hashlib.sha256(b"?\nkelvin").hexdigest())
        self.assertEqual(source_key("a\ud800"), hashlib.sha256(b"a?").hexdigest())


class WindowTest(unittest.TestCase):
    def test_lower_bound_is_exclusive(self):
        self.assertEqual(window_start(NOW), datetime(2026, 11, 24, 10, 0, 0))
        self.assertFalse(in_window(datetime(2026, 11, 24, 10, 0, 0), NOW))
        self.assertTrue(in_window(datetime(2026, 11, 24, 10, 0, 1), NOW))
        self.assertFalse(in_window(datetime(2026, 11, 24, 9, 59, 59), NOW))

    def test_upper_bound_includes_now(self):
        self.assertTrue(in_window(NOW, NOW))

    def test_attempts_after_now_do_not_count(self):
        self.assertFalse(in_window(NOW + timedelta(seconds=1), NOW))
        self.assertFalse(in_window(NOW + timedelta(hours=1), NOW))

    def test_now_must_be_naive_and_whole_seconds(self):
        for bad in (
            NOW.replace(tzinfo=UTC),
            NOW.replace(microsecond=1),
            NOW.replace(microsecond=999999),
        ):
            with self.subTest(now=bad):
                with self.assertRaises(ValueError):
                    require_whole_second(bad)
                with self.assertRaises(ValueError):
                    window_start(bad)
                with self.assertRaises(ValueError):
                    count_failures([], bad)
        with self.assertRaises(ValueError):
            require_whole_second("2026-11-24T11:00:00")
        require_whole_second(NOW)


class FailureTest(unittest.TestCase):
    def test_locked_and_success_are_not_failures(self):
        self.assertEqual(FAILURE_OUTCOMES, {"bad_password", "unknown_user", "disabled"})
        for outcome in OUTCOMES:
            with self.subTest(outcome=outcome):
                self.assertEqual(is_failure(outcome), outcome not in ("success", "locked"))

    def test_count_failures_in_the_window(self):
        # L1/L2：10:00:00～10:09:00 每分钟一次失败。
        attempts = [(datetime(2026, 11, 24, 10, minute), "bad_password") for minute in range(10)]
        self.assertEqual(count_failures(attempts, datetime(2026, 11, 24, 10, 15)), 10)
        self.assertEqual(count_failures(attempts, NOW), 9)
        self.assertEqual(count_failures(attempts, datetime(2026, 11, 24, 10, 4)), 5)

    def test_success_does_not_reset_and_locked_does_not_extend(self):
        # L5：9 条失败、1 条 success；之后再失败一次满 10 条。
        at = datetime(2026, 11, 24, 10, 30)
        attempts = [(at, "bad_password")] * 9 + [(at, "success")]
        self.assertEqual(count_failures(attempts, NOW), 9)
        attempts.append((at, "unknown_user"))
        self.assertEqual(count_failures(attempts, NOW), 10)
        attempts += [(at, "locked")] * 5
        self.assertEqual(count_failures(attempts, NOW), 10)

    def test_every_failure_outcome_counts(self):
        at = datetime(2026, 11, 24, 10, 30)
        attempts = [(at, outcome) for outcome in OUTCOMES]
        self.assertEqual(count_failures(attempts, NOW), 3)

    def test_out_of_window_attempts_are_ignored(self):
        attempts = [
            (window_start(NOW), "bad_password"),
            (NOW + timedelta(seconds=1), "bad_password"),
            (NOW, "bad_password"),
        ]
        self.assertEqual(count_failures(attempts, NOW), 1)


class LockReasonTest(unittest.TestCase):
    def test_under_both_limits_is_not_locked(self):
        self.assertIsNone(lock_reason(source_failures=0, throttle_failures=0))
        self.assertIsNone(lock_reason(source_failures=29, throttle_failures=9))

    def test_throttle_limit_is_reached_at_ten(self):
        self.assertEqual(lock_reason(source_failures=10, throttle_failures=10), THROTTLE)
        self.assertEqual(lock_reason(source_failures=12, throttle_failures=11), THROTTLE)

    def test_source_limit_is_reached_at_thirty(self):
        # L7：30 个不同用户名各失败一次。
        self.assertEqual(lock_reason(source_failures=30, throttle_failures=0), SOURCE)
        self.assertEqual(lock_reason(source_failures=31, throttle_failures=1), SOURCE)

    def test_source_limit_is_checked_first(self):
        self.assertEqual(lock_reason(source_failures=30, throttle_failures=10), SOURCE)


if __name__ == "__main__":
    unittest.main()
