import re
import unittest
from datetime import UTC, datetime, timedelta

from app.domain.service_auth import (
    CATALOG_READ,
    CATALOG_WRITE,
    DEMO_RESET,
    OPS_READ,
    ORDERS_READ,
    ORDERS_WRITE,
    SCOPES,
    STOCK_READ,
    STOCK_WRITE,
    InvalidScopes,
    PoolDecision,
    authorize_pool,
    authorize_sales_command,
    generate_token,
    is_token_format,
    is_token_usable,
    parse_authorization,
    scopes_cover,
    token_digest,
    validate_issued_scopes,
)

TOKEN = generate_token(bytes(range(32)))
NOW = datetime(2026, 11, 13, 8, 0, tzinfo=UTC)


class TokenFormatTest(unittest.TestCase):
    def test_generated_token_is_prefix_plus_43_base64url_characters(self):
        token = generate_token()
        self.assertEqual(len(token), 47)
        self.assertRegex(token, re.compile(r"csk_[A-Za-z0-9_-]{43}\Z"))
        self.assertNotIn("=", token)
        self.assertTrue(is_token_format(token))
        self.assertNotEqual(token, generate_token())

    def test_injected_bytes_give_a_fixed_token(self):
        self.assertEqual(generate_token(bytes(32)), "csk_" + "A" * 43)
        # 最后两字节 0xFFFF 是 16 位，补两个 0 位凑成第 43 个字符 “8”。
        self.assertEqual(generate_token(b"\xff" * 32), "csk_" + "_" * 42 + "8")

    def test_random_bytes_must_be_32(self):
        for bad in (bytes(31), bytes(33), "0" * 32):
            with self.subTest(random_bytes=bad), self.assertRaises(ValueError):
                generate_token(bad)

    def test_format_rejects_anything_that_is_not_an_encoded_32_byte_value(self):
        self.assertTrue(is_token_format(TOKEN))
        for bad in (
            TOKEN[:-1],
            TOKEN + "A",
            "CSK_" + TOKEN[4:],
            "csk-" + TOKEN[4:],
            TOKEN[4:],
            TOKEN[:-1] + "=",
            TOKEN[:-1] + "+",
            # 末位多出的两位不是 0：不是任何 32 字节的规范编码。
            "csk_" + "A" * 42 + "B",
            "",
            None,
        ):
            with self.subTest(value=bad):
                self.assertFalse(is_token_format(bad))


class TokenDigestTest(unittest.TestCase):
    def test_known_sha256_vectors(self):
        # FIPS 180-2 的 "abc" 与空串向量：摘要是 UTF-8 全文的 SHA-256 小写十六进制。
        self.assertEqual(
            token_digest("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertEqual(
            token_digest(""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_digest_covers_the_prefix(self):
        # 摘要取令牌全文（含 csk_），只取随机部分会与库里存的对不上。
        self.assertNotEqual(token_digest(TOKEN), token_digest(TOKEN[4:]))
        self.assertRegex(token_digest(TOKEN), re.compile(r"[0-9a-f]{64}\Z"))


class ParseAuthorizationTest(unittest.TestCase):
    def test_scheme_is_case_insensitive(self):
        for scheme in ("Bearer", "bearer", "BEARER", "bEaReR"):
            with self.subTest(scheme=scheme):
                self.assertEqual(parse_authorization(f"{scheme} {TOKEN}"), TOKEN)

    def test_unrecognized_headers_return_none(self):
        # 认不出一律返回 None，由上层统一 401，不区分原因、不抛出。
        for bad in (
            None,
            "",
            "Bearer",
            "Bearer ",
            TOKEN,
            f"Bearer  {TOKEN}",
            f"Bearer\t{TOKEN}",
            f" Bearer {TOKEN}",
            f"Bearer {TOKEN} ",
            f"Bearer {TOKEN}\n",
            f"Basic {TOKEN}",
            f"Bearer: {TOKEN}",
            f"Ｂearer {TOKEN}",
            "Bearer csk_short",
            f"Bearer {TOKEN[:-1]}",
            f"Bearer {TOKEN[4:]}",
        ):
            with self.subTest(header=bad):
                self.assertIsNone(parse_authorization(bad))


class TokenUsableTest(unittest.TestCase):
    def usable(self, **overrides):
        args = {
            "token_revoked_at": None,
            "expires_at": None,
            "credential_status": "active",
            "tenant_status": "active",
            "now": NOW,
        }
        args.update(overrides)
        return is_token_usable(**args)

    def test_active_token_without_deadline(self):
        self.assertTrue(self.usable())

    def test_revoked_token(self):
        self.assertFalse(self.usable(token_revoked_at=NOW - timedelta(days=1)))

    def test_revoked_credential(self):
        self.assertFalse(self.usable(credential_status="revoked"))

    def test_inactive_tenant(self):
        self.assertFalse(self.usable(tenant_status="inactive"))

    def test_deadline_is_exclusive(self):
        # 轮换重叠期：截止时间之前可用，到点即失效。
        self.assertTrue(self.usable(expires_at=NOW + timedelta(microseconds=1)))
        self.assertFalse(self.usable(expires_at=NOW))
        self.assertFalse(self.usable(expires_at=NOW - timedelta(seconds=1)))


class ScopeCoverTest(unittest.TestCase):
    def test_eight_scopes(self):
        self.assertEqual(
            SCOPES,
            {
                "catalog.read",
                "catalog.write",
                "stock.read",
                "stock.write",
                "orders.read",
                "orders.write",
                "demo.reset",
                "ops.read",
            },
        )

    def test_single_required_scope(self):
        self.assertTrue(scopes_cover({ORDERS_WRITE, ORDERS_READ}, {ORDERS_WRITE}))
        self.assertFalse(scopes_cover({ORDERS_READ}, {ORDERS_WRITE}))

    def test_sku_merge_needs_both_scopes(self):
        # SKU 合并要求 catalog.write 与 stock.write，缺一个都 403。
        merge = {CATALOG_WRITE, STOCK_WRITE}
        self.assertTrue(scopes_cover({CATALOG_WRITE, STOCK_WRITE, STOCK_READ}, merge))
        self.assertFalse(scopes_cover({CATALOG_WRITE}, merge))
        self.assertFalse(scopes_cover({STOCK_WRITE}, merge))

    def test_endpoint_without_required_scopes_is_not_covered(self):
        # 接口漏配 scope 时不放行，避免误配成无鉴权的写接口。
        self.assertFalse(scopes_cover(SCOPES, set()))


class IssuedScopesTest(unittest.TestCase):
    def test_shop_credential(self):
        scopes = [CATALOG_READ, CATALOG_WRITE, STOCK_READ, STOCK_WRITE, ORDERS_READ, ORDERS_WRITE]
        self.assertEqual(validate_issued_scopes(scopes, has_channel=True), frozenset(scopes))
        self.assertEqual(
            validate_issued_scopes([DEMO_RESET, ORDERS_WRITE], has_channel=True),
            {DEMO_RESET, ORDERS_WRITE},
        )

    def test_ops_credential(self):
        self.assertEqual(validate_issued_scopes([OPS_READ], has_channel=False), {OPS_READ})

    def assert_rejected(self, scopes, has_channel):
        with self.assertRaises(InvalidScopes):
            validate_issued_scopes(scopes, has_channel=has_channel)

    def test_empty_scopes_rejected(self):
        self.assert_rejected([], True)

    def test_unknown_scopes_rejected(self):
        # 未知值（含只差大小写）签发出去也永远用不上，多半是打错。
        for bad in ("stock.admin", "Stock.read", "stock.read ", ""):
            with self.subTest(scope=bad):
                self.assert_rejected([STOCK_READ, bad], True)

    def test_duplicate_scopes_rejected(self):
        self.assert_rejected([STOCK_READ, STOCK_READ], True)
        self.assert_rejected([OPS_READ, OPS_READ], False)

    def test_ops_read_must_stand_alone(self):
        # 运维接口用独立凭据：不能顺带有业务 scope，也不能带销售入口。
        self.assert_rejected([OPS_READ, STOCK_READ], False)
        self.assert_rejected([OPS_READ], True)


class PoolAuthorizationTest(unittest.TestCase):
    def test_sales_command_needs_a_sales_channel(self):
        self.assertEqual(authorize_sales_command("pool_A"), PoolDecision.ALLOWED)
        self.assertEqual(authorize_sales_command(None), PoolDecision.FORBIDDEN)

    def test_no_sales_channel_is_forbidden(self):
        self.assertEqual(
            authorize_pool(None, "pool_A", pool_in_tenant=True), PoolDecision.FORBIDDEN
        )

    def test_no_sales_channel_wins_over_foreign_pool(self):
        # 没有销售入口先判 403：不去查池属于谁，不向这种凭据暴露别的租户的池存在与否。
        self.assertEqual(
            authorize_pool(None, "pool_X", pool_in_tenant=False), PoolDecision.FORBIDDEN
        )

    def test_pool_of_another_tenant_is_not_found(self):
        # 跨租户对象一律 404，与不存在同一错误。
        self.assertEqual(
            authorize_pool("pool_A", "pool_X", pool_in_tenant=False), PoolDecision.NOT_FOUND
        )

    def test_own_tenant_pool_not_granted_is_forbidden(self):
        self.assertEqual(
            authorize_pool("pool_A", "pool_B", pool_in_tenant=True), PoolDecision.FORBIDDEN
        )

    def test_granted_pool_is_allowed(self):
        self.assertEqual(
            authorize_pool("pool_A", "pool_A", pool_in_tenant=True), PoolDecision.ALLOWED
        )

    def test_object_in_another_tenant_is_not_found(self):
        # 按对象判定：requested 是对象所属的池，pool_in_tenant 是对象是否属于本租户。
        # 别的租户的对象即使恰好写着同一个池 ID，也是 404。
        self.assertEqual(
            authorize_pool("pool_A", "pool_A", pool_in_tenant=False), PoolDecision.NOT_FOUND
        )


if __name__ == "__main__":
    unittest.main()
