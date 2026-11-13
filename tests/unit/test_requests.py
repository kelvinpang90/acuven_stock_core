import unittest

from app.domain.requests import (
    ACTOR_REF,
    ACTOR_ROLE,
    IDEMPOTENCY_KEY,
    REQUEST_ID,
    Actor,
    InvalidHeader,
    parse_actor,
    parse_idempotency_key,
    parse_request_id,
    truncate_user_agent,
)

# 两类格式都拒绝的越界字符：空格、制表符、换行、DEL、非 ASCII。
OUT_OF_RANGE = (" ", "\t", "\n", "\x7f", "é", "键")


class HeaderErrorMixin:
    def assert_invalid(self, header, call, *args):
        with self.assertRaises(InvalidHeader) as caught:
            call(*args)
        # 上层统一回 400 INVALID_REQUEST，头名用于报错与排查。
        self.assertEqual(caught.exception.header, header)


class IdempotencyKeyTest(HeaderErrorMixin, unittest.TestCase):
    def test_length_boundaries(self):
        self.assertEqual(parse_idempotency_key("k"), "k")
        self.assertEqual(parse_idempotency_key("k" * 128), "k" * 128)
        self.assert_invalid(IDEMPOTENCY_KEY, parse_idempotency_key, "")
        self.assert_invalid(IDEMPOTENCY_KEY, parse_idempotency_key, "k" * 129)

    def test_whole_visible_ascii_range_is_accepted(self):
        # 0x21–0x7E 全部可见字符都合格，网店的 shop:order:<id>:reserve:1 一类键可以照用。
        visible = "".join(chr(c) for c in range(0x21, 0x7F))
        self.assertEqual(parse_idempotency_key(visible), visible)
        self.assertEqual(
            parse_idempotency_key("shop:order:42:reserve:1"), "shop:order:42:reserve:1"
        )

    def test_characters_outside_visible_ascii_are_rejected(self):
        for bad in OUT_OF_RANGE:
            with self.subTest(char=bad):
                self.assert_invalid(IDEMPOTENCY_KEY, parse_idempotency_key, f"key{bad}1")
        # 不去首尾空格：去了会让两个不同的键落到同一条幂等记录上。
        self.assert_invalid(IDEMPOTENCY_KEY, parse_idempotency_key, " key")
        self.assert_invalid(IDEMPOTENCY_KEY, parse_idempotency_key, "key ")

    def test_missing_key_is_rejected(self):
        # 写请求必带；没带与不合格同样 400。
        self.assert_invalid(IDEMPOTENCY_KEY, parse_idempotency_key, None)


class RequestIdTest(HeaderErrorMixin, unittest.TestCase):
    def test_length_boundaries(self):
        self.assertEqual(parse_request_id("r"), "r")
        self.assertEqual(parse_request_id("r" * 128), "r" * 128)
        self.assert_invalid(REQUEST_ID, parse_request_id, "")
        self.assert_invalid(REQUEST_ID, parse_request_id, "r" * 129)

    def test_reference_characters_are_accepted(self):
        self.assertEqual(parse_request_id("req_01ARYZ6S41.a:b-c"), "req_01ARYZ6S41.a:b-c")

    def test_other_characters_are_rejected(self):
        # 可见 ASCII 但不在 [A-Za-z0-9._:-] 里的也拒，它会进日志与安全事件。
        for bad in (*OUT_OF_RANGE, "/", "+", "=", "@"):
            with self.subTest(char=bad):
                self.assert_invalid(REQUEST_ID, parse_request_id, f"req{bad}1")

    def test_missing_request_id_is_allowed(self):
        # 可选：没带由上层生成 req_ ID。
        self.assertIsNone(parse_request_id(None))


class ActorTest(HeaderErrorMixin, unittest.TestCase):
    def test_both_headers_present(self):
        self.assertEqual(parse_actor("admin:7", "stock_admin"), Actor("admin:7", "stock_admin"))
        for role in ("stock_operator", "stock_admin", "catalog_admin"):
            with self.subTest(role=role):
                self.assertEqual(parse_actor("u1", role).role, role)

    def test_both_headers_absent(self):
        self.assertIsNone(parse_actor(None, None))

    def test_only_one_header_is_rejected(self):
        # 没有角色的操作者或没有操作者的角色都无法记进流水与审计。
        self.assert_invalid(ACTOR_ROLE, parse_actor, "u1", None)
        self.assert_invalid(ACTOR_REF, parse_actor, None, "stock_admin")

    def test_actor_ref_length_boundaries(self):
        self.assertEqual(parse_actor("a", "stock_admin").ref, "a")
        self.assertEqual(parse_actor("a" * 128, "stock_admin").ref, "a" * 128)
        self.assert_invalid(ACTOR_REF, parse_actor, "", "stock_admin")
        self.assert_invalid(ACTOR_REF, parse_actor, "a" * 129, "stock_admin")

    def test_actor_ref_characters(self):
        for bad in (*OUT_OF_RANGE, "/", "@"):
            with self.subTest(char=bad):
                self.assert_invalid(ACTOR_REF, parse_actor, f"u{bad}1", "stock_admin")

    def test_role_is_case_sensitive_and_exact(self):
        # 只差大小写也拒：角色表按原值比对，猜成 stock_admin 会放大权限。
        for bad in ("Stock_Admin", "STOCK_ADMIN", "stock_Admin", "Catalog_admin"):
            with self.subTest(role=bad):
                self.assert_invalid(ACTOR_ROLE, parse_actor, "u1", bad)
        # 不去空格，也不接受表外的值。
        for bad in (" stock_admin", "stock_admin ", "", "shop_admin"):
            with self.subTest(role=bad):
                self.assert_invalid(ACTOR_ROLE, parse_actor, "u1", bad)


class UserAgentTest(unittest.TestCase):
    def test_truncated_to_200_characters(self):
        self.assertEqual(truncate_user_agent("a" * 200), "a" * 200)
        self.assertEqual(truncate_user_agent("a" * 201), "a" * 200)
        self.assertEqual(truncate_user_agent("acuven-shop/abc"), "acuven-shop/abc")

    def test_counts_characters_not_bytes(self):
        self.assertEqual(truncate_user_agent("é" * 201), "é" * 200)

    def test_missing_user_agent(self):
        self.assertIsNone(truncate_user_agent(None))


if __name__ == "__main__":
    unittest.main()
