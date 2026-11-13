import re
import unittest

from app.domain.ids import new_id

ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


class NewIdTest(unittest.TestCase):
    def test_prefix_underscore_and_26_crockford_characters(self):
        for prefix in ("ten", "sku", "pool", "cred"):
            with self.subTest(prefix=prefix):
                value = new_id(prefix)
                head, _, ulid = value.partition("_")
                self.assertEqual(head, prefix)
                self.assertEqual(len(ulid), 26)
                # Crockford Base32 大写：不含 I、L、O、U，也没有小写。
                self.assertRegex(ulid, ULID)
                self.assertLessEqual(len(value), 32)

    def test_injected_time_and_randomness_give_a_fixed_id(self):
        # ULID 规范的时间戳例子：1469918176385 毫秒编码为 01ARYZ6S41。
        self.assertEqual(
            new_id("ten", now_ms=1469918176385, random_bytes=bytes(10)),
            "ten_01ARYZ6S41" + "0" * 16,
        )
        self.assertEqual(
            new_id("sku", now_ms=0, random_bytes=b"\xff" * 10),
            "sku_0000000000" + "Z" * 16,
        )

    def test_default_ids_differ(self):
        # 不注入时用系统随机数，同一毫秒内也不会撞。
        self.assertNotEqual(new_id("sku"), new_id("sku"))

    def test_later_id_sorts_after_earlier_regardless_of_randomness(self):
        # 时间戳在前：较晚的 ID 即使随机部分最小，按字符串比较也更大，数据库按主键插入近似有序。
        earlier = new_id("sku", now_ms=1_700_000_000_000, random_bytes=b"\xff" * 10)
        later = new_id("sku", now_ms=1_700_000_000_001, random_bytes=bytes(10))
        self.assertLess(earlier, later)

    def test_timestamp_must_fit_48_bits(self):
        self.assertEqual(
            new_id("sku", now_ms=(1 << 48) - 1, random_bytes=bytes(10))[4:14], "7ZZZZZZZZZ"
        )
        for bad in (-1, 1 << 48, True, 1.0):
            with self.subTest(now_ms=bad), self.assertRaises(ValueError):
                new_id("sku", now_ms=bad, random_bytes=bytes(10))

    def test_randomness_must_be_ten_bytes(self):
        for bad in (bytes(9), bytes(11), "0" * 10):
            with self.subTest(random_bytes=bad), self.assertRaises(ValueError):
                new_id("sku", now_ms=0, random_bytes=bad)

    def test_invalid_prefix_is_rejected(self):
        # 前缀只接受小写字母，整个 ID 不超过数据库列的 VARCHAR(32)：前缀最多 5 个字母。
        self.assertEqual(len(new_id("abcde")), 32)
        for bad in ("", "Sku", "SKU", "sk1", "sk_u", "sku_", " sku", "skü", "abcdef", None):
            with self.subTest(prefix=bad), self.assertRaises(ValueError):
                new_id(bad)


if __name__ == "__main__":
    unittest.main()
