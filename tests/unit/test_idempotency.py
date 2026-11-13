import hashlib
import re
import unittest

from app.domain.idempotency import (
    Decision,
    UndigestableInput,
    canonical_request,
    decide,
    is_final_result,
    lock_name,
    request_digest,
)

HEX64 = re.compile(r"[0-9a-f]{64}\Z")
PATH = {"order_ref": "o-1"}


def line(ref, sku, quantity):
    return {"line_ref": ref, "sku_id": sku, "quantity": quantity}


class CanonicalRequestTest(unittest.TestCase):
    def test_serialization_is_sorted_compact_and_keeps_non_ascii(self):
        # 等价于 json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)。
        self.assertEqual(
            canonical_request(PATH, {"note": "库存 é", "a": [3, 1], "b": None, "c": True}),
            '{"body":{"a":[3,1],"b":null,"c":true,"note":"库存 é"},"path":{"order_ref":"o-1"}}',
        )

    def test_digest_is_sha256_of_utf8_canonical_json(self):
        body = {"note": "é"}
        expected = hashlib.sha256(canonical_request(PATH, body).encode("utf-8")).hexdigest()
        self.assertEqual(request_digest(PATH, body), expected)
        self.assertRegex(request_digest(PATH, body), HEX64)

    def test_body_is_not_modified(self):
        body = {"lines": [line("L2", "sku_b", 1), line("L1", "sku_a", 2)]}
        request_digest(PATH, body)
        self.assertEqual([item["line_ref"] for item in body["lines"]], ["L2", "L1"])


class SameDigestTest(unittest.TestCase):
    """同一内容换种写法重试，摘要必须相同，否则会被误判为幂等冲突。"""

    def test_key_order_does_not_matter(self):
        self.assertEqual(
            request_digest(PATH, {"payment_ref": "p1", "paid_at": "2026-11-13T00:00:00Z"}),
            request_digest(PATH, {"paid_at": "2026-11-13T00:00:00Z", "payment_ref": "p1"}),
        )

    def test_top_level_lines_order_does_not_matter(self):
        a = {"lines": [line("L1", "sku_a", 2), line("L2", "sku_b", 1)]}
        b = {"lines": [line("L2", "sku_b", 1), line("L1", "sku_a", 2)]}
        self.assertEqual(request_digest(PATH, a), request_digest(PATH, b))

    def test_duplicate_line_refs_still_give_a_fixed_digest(self):
        # 重复 line_ref 本该 422；万一漏到这里，按元素的规范化 JSON 排序，结果也确定。
        a = {"lines": [line("L1", "sku_a", 2), line("L1", "sku_a", 1)]}
        b = {"lines": [line("L1", "sku_a", 1), line("L1", "sku_a", 2)]}
        self.assertEqual(request_digest(PATH, a), request_digest(PATH, b))

    def test_characters_outside_the_bmp_are_kept_as_is(self):
        # JSON 里成对的 😀 解析后是一个码点，不是孤立代理项，照常算摘要。
        self.assertIn('"n":"😀"', canonical_request(PATH, {"n": "😀"}))


class DifferentDigestTest(unittest.TestCase):
    """真正不同的请求，摘要必须不同，否则会把别的请求的结果重放回去。"""

    def assert_differ(self, path_a, body_a, path_b, body_b):
        self.assertNotEqual(request_digest(path_a, body_a), request_digest(path_b, body_b))

    def test_path_parameters(self):
        body = {"payment_ref": "p1"}
        self.assert_differ({"order_ref": "o-1"}, body, {"order_ref": "o-2"}, body)
        # 参数名也进摘要。
        self.assert_differ({"order_ref": "o-1"}, body, {"order_id": "o-1"}, body)

    def test_deeper_arrays_keep_order(self):
        a = {"lines": [{"line_ref": "L1", "serials": ["s1", "s2"]}]}
        b = {"lines": [{"line_ref": "L1", "serials": ["s2", "s1"]}]}
        self.assert_differ(PATH, a, PATH, b)

    def test_nested_lines_keep_order(self):
        a = {"batch": {"lines": [line("L1", "sku_a", 1), line("L2", "sku_b", 1)]}}
        b = {"batch": {"lines": [line("L2", "sku_b", 1), line("L1", "sku_a", 1)]}}
        self.assert_differ(PATH, a, PATH, b)

    def test_other_top_level_arrays_keep_order(self):
        a = {"targets": [{"sku_id": "sku_a", "target": 1}, {"sku_id": "sku_b", "target": 2}]}
        b = {"targets": [{"sku_id": "sku_b", "target": 2}, {"sku_id": "sku_a", "target": 1}]}
        self.assert_differ(PATH, a, PATH, b)

    def test_lines_not_all_keyed_by_string_line_ref_keep_order(self):
        a = {"lines": [{"line_ref": "L1"}, {"sku_id": "sku_b"}]}
        b = {"lines": [{"sku_id": "sku_b"}, {"line_ref": "L1"}]}
        self.assert_differ(PATH, a, PATH, b)
        a = {"lines": [{"line_ref": 1}, {"line_ref": 2}]}
        b = {"lines": [{"line_ref": 2}, {"line_ref": 1}]}
        self.assert_differ(PATH, a, PATH, b)

    def test_extra_null_key(self):
        # 不补默认值，也不丢 null：调用方多发一个 null 字段就是不同的内容。
        self.assert_differ(PATH, {"a": 1}, PATH, {"a": 1, "note": None})

    def test_no_body_differs_from_empty_object(self):
        self.assert_differ(PATH, None, PATH, {})
        self.assertIn('"body":null', canonical_request(PATH, None))

    def test_value_types_are_kept(self):
        self.assert_differ(PATH, {"q": 1}, PATH, {"q": "1"})
        self.assert_differ(PATH, {"q": 1}, PATH, {"q": True})


class UndigestableInputTest(unittest.TestCase):
    """这些输入本应在请求体校验时 422；漏到这里要大声失败，不能算出一个不确定的摘要。"""

    def test_floats_are_rejected(self):
        for bad in (1.0, 1.5, 1e3, float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad), self.assertRaises(UndigestableInput):
                request_digest(PATH, {"lines": [{"line_ref": "L1", "quantity": bad}]})

    def test_lone_surrogates_are_rejected(self):
        for bad in ("\ud800", "a\udfffb"):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(UndigestableInput):
                    request_digest(PATH, {"note": bad})
                with self.assertRaises(UndigestableInput):
                    request_digest(PATH, {bad: 1})
                with self.assertRaises(UndigestableInput):
                    request_digest({"order_ref": bad}, None)

    def test_non_json_types_are_rejected(self):
        for bad in ((1, 2), {1, 2}, b"x", object()):
            with self.subTest(value=bad), self.assertRaises(UndigestableInput):
                request_digest(PATH, {"x": bad})
        with self.assertRaises(UndigestableInput):
            request_digest(PATH, {1: "x"})


class LockNameTest(unittest.TestCase):
    PARTS = ("stock_core_demo", "ten_1", "cred_1", "reservations.create", "shop:order:1:reserve:1")

    def test_is_sha256_of_parts_joined_by_newline(self):
        # 拼法定下后不改：新旧版本同时运行时锁名不同，互斥就失效了。
        name = lock_name(*self.PARTS)
        self.assertRegex(name, HEX64)
        self.assertEqual(name, hashlib.sha256("\n".join(self.PARTS).encode("utf-8")).hexdigest())

    def test_any_part_changes_the_name(self):
        base = lock_name(*self.PARTS)
        for index in range(len(self.PARTS)):
            with self.subTest(part=index):
                parts = list(self.PARTS)
                parts[index] += "x"
                self.assertNotEqual(lock_name(*parts), base)

    def test_demo_and_production_databases_do_not_share_locks(self):
        self.assertNotEqual(
            lock_name("stock_core_demo", *self.PARTS[1:]),
            lock_name("stock_core_prod", *self.PARTS[1:]),
        )

    def test_empty_or_newline_parts_are_rejected(self):
        # 否则 ("a\nb", "c") 与 ("a", "b\nc") 会拼成同一个锁名。
        for index in range(len(self.PARTS)):
            for bad in ("", "a\nb", None):
                with self.subTest(part=index, value=bad), self.assertRaises(ValueError):
                    parts = list(self.PARTS)
                    parts[index] = bad
                    lock_name(*parts)


class DecideTest(unittest.TestCase):
    def test_no_record_executes(self):
        self.assertEqual(decide(None, "a" * 64), Decision.EXECUTE)

    def test_same_digest_replays(self):
        self.assertEqual(decide("a" * 64, "a" * 64), Decision.REPLAY)

    def test_different_digest_conflicts(self):
        self.assertEqual(decide("a" * 64, "b" * 64), Decision.CONFLICT)


class FinalResultTest(unittest.TestCase):
    def test_saved_results(self):
        # 取锁之后命令给出的 2xx 与业务 4xx 是确定结果：同键只会重放。
        for status, code in (
            (200, None),
            (201, None),
            (404, "SKU_NOT_FOUND"),
            (422, "INVALID_REQUEST"),
            (409, "INSUFFICIENT_STOCK"),
            (409, "LATE_PAYMENT_INSUFFICIENT_STOCK"),
        ):
            with self.subTest(status=status, code=code):
                self.assertTrue(is_final_result(status, code))

    def test_unsaved_results(self):
        # 处理中、幂等冲突、限流与 5xx 都不是这个键的结果，调用方可以同键再试。
        for status, code in (
            (409, "REQUEST_IN_PROGRESS"),
            (409, "IDEMPOTENCY_CONFLICT"),
            (429, "RATE_LIMITED"),
            (500, "INTERNAL_ERROR"),
            (503, "UNAVAILABLE"),
            (199, None),
        ):
            with self.subTest(status=status, code=code):
                self.assertFalse(is_final_result(status, code))


if __name__ == "__main__":
    unittest.main()
