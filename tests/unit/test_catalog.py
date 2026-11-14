import hashlib
import unittest

from app.domain.catalog import (
    ACTIVE,
    AFTER,
    BARCODE_TAKEN,
    CLAIM_INSERT,
    CLAIM_OWNED,
    EVENT_TYPES,
    INACTIVE,
    LIMIT,
    PRODUCT,
    SKU,
    SKU_CODE_RETIRED,
    SKU_CODE_TAKEN,
    Conflict,
    InvalidField,
    InvalidParameter,
    NothingToUpdate,
    catalog_lock_name,
    code_key,
    conflict_code,
    decide_code_claim,
    diff_fields,
    format_cursor,
    merged_sku_update_allowed,
    normalize_barcode,
    normalize_name,
    normalize_reason,
    normalize_reference_price,
    normalize_sku_code,
    normalize_update,
    parse_cursor,
    parse_limit,
    product_created_changes,
    sku_created_changes,
    sort_conflicts,
    status_changes,
)

HEX64 = r"\A[0-9a-f]{64}\Z"
AMOUNT_MAX = 9007199254740991


class FieldErrorMixin:
    def assert_invalid(self, field, call, *args):
        with self.assertRaises(InvalidField) as caught:
            call(*args)
        # 导入按字段名记 invalid_format，接口回 422。
        self.assertEqual(caught.exception.field, field)


class NameTest(FieldErrorMixin, unittest.TestCase):
    def test_length_boundaries_after_strip(self):
        self.assertEqual(normalize_name("a" * 200), "a" * 200)
        self.assertEqual(normalize_name("  " + "a" * 200 + "\n"), "a" * 200)
        self.assert_invalid("name", normalize_name, "a" * 201)
        self.assert_invalid("name", normalize_name, "")

    def test_blank_is_rejected(self):
        for blank in (" ", "   ", "\t\n", "　"):
            with self.subTest(value=blank):
                self.assert_invalid("name", normalize_name, blank)

    def test_control_characters_are_rejected(self):
        for bad in ("a\nb", "a\rb", "a\tb", "a\x00b", "a\x1fb", "a\x7fb"):
            with self.subTest(value=bad):
                self.assert_invalid("name", normalize_name, bad)

    def test_non_ascii_and_inner_spaces_are_accepted(self):
        self.assertEqual(normalize_name(" Basic Tee 基本款 "), "Basic Tee 基本款")

    def test_non_string_is_rejected(self):
        for bad in (None, 1, True, ["a"], {"a": 1}):
            with self.subTest(value=bad):
                self.assert_invalid("name", normalize_name, bad)

    def test_import_field_name_is_reported(self):
        self.assert_invalid("product_name", normalize_name, "", "product_name")


class SkuCodeTest(FieldErrorMixin, unittest.TestCase):
    def test_length_boundaries_after_strip(self):
        self.assertEqual(normalize_sku_code("A" * 64), "A" * 64)
        self.assertEqual(normalize_sku_code(" AB-1 "), "AB-1")
        self.assert_invalid("sku_code", normalize_sku_code, "A" * 65)
        self.assert_invalid("sku_code", normalize_sku_code, "")
        self.assert_invalid("sku_code", normalize_sku_code, "   ")

    def test_inner_space_and_whole_printable_ascii_are_accepted(self):
        self.assertEqual(normalize_sku_code("TEE RED M"), "TEE RED M")
        visible = "".join(chr(c) for c in range(0x21, 0x7F))
        self.assertEqual(normalize_sku_code(visible[:64]), visible[:64])
        self.assertEqual(normalize_sku_code(visible[64:]), visible[64:])

    def test_non_ascii_and_control_characters_are_rejected(self):
        for bad in ("CAFÉ", "A\tB", "A\nB", "A\x7fB", "A\x00B", "Ａ1", "键"):
            with self.subTest(value=bad):
                self.assert_invalid("sku_code", normalize_sku_code, bad)

    def test_non_string_is_rejected(self):
        for bad in (None, 123, True, ["A"]):
            with self.subTest(value=bad):
                self.assert_invalid("sku_code", normalize_sku_code, bad)


class BarcodeTest(FieldErrorMixin, unittest.TestCase):
    def test_none_and_blank_become_none(self):
        for blank in (None, "", " ", "\t \n"):
            with self.subTest(value=blank):
                self.assertIsNone(normalize_barcode(blank))

    def test_non_empty_follows_sku_code_rules(self):
        self.assertEqual(normalize_barcode(" 9555 0001 "), "9555 0001")
        self.assertEqual(normalize_barcode("1" * 64), "1" * 64)
        for bad in ("1" * 65, "É123", "12\t34", 9555, True):
            with self.subTest(value=bad):
                self.assert_invalid("barcode", normalize_barcode, bad)


class ReferencePriceTest(FieldErrorMixin, unittest.TestCase):
    def price(self, amount=12900, currency="MYR"):
        return {"amount_minor": amount, "currency": currency}

    def test_none_is_accepted(self):
        self.assertIsNone(normalize_reference_price(None))

    def test_amount_boundaries(self):
        for amount in (0, AMOUNT_MAX):
            with self.subTest(amount=amount):
                self.assertEqual(normalize_reference_price(self.price(amount)), self.price(amount))
        for bad in (AMOUNT_MAX + 1, -1, True, False, "100", 1.0, None):
            with self.subTest(amount=bad):
                self.assert_invalid("reference_price", normalize_reference_price, self.price(bad))

    def test_currency_must_be_three_upper_ascii_letters(self):
        self.assertEqual(normalize_reference_price(self.price(currency="XYZ"))["currency"], "XYZ")
        for bad in ("myr", "Myr", "MYRX", "MY", "", " MYR", "MY1", "ÉUR", "ＭＹＲ", None, 458):
            with self.subTest(currency=bad):
                self.assert_invalid(
                    "reference_price", normalize_reference_price, self.price(currency=bad)
                )

    def test_exactly_two_keys(self):
        extra = {**self.price(), "note": "x"}
        for bad in (extra, {"amount_minor": 100}, {"currency": "MYR"}, {}, [100, "MYR"], 100):
            with self.subTest(value=bad):
                self.assert_invalid("reference_price", normalize_reference_price, bad)

    def test_returns_a_copy(self):
        original = self.price()
        normalized = normalize_reference_price(original)
        self.assertEqual(normalized, original)
        self.assertIsNot(normalized, original)


class ReasonTest(FieldErrorMixin, unittest.TestCase):
    def test_boundaries(self):
        self.assertIsNone(normalize_reason(None))
        self.assertEqual(normalize_reason(" " + "r" * 500 + " "), "r" * 500)
        self.assert_invalid("reason", normalize_reason, "r" * 501)
        self.assert_invalid("reason", normalize_reason, "")
        self.assert_invalid("reason", normalize_reason, "   ")

    def test_control_characters_and_non_strings_are_rejected(self):
        for bad in ("a\nb", "a\x7fb", 1, True, ["r"]):
            with self.subTest(value=bad):
                self.assert_invalid("reason", normalize_reason, bad)


class UpdateShapeTest(FieldErrorMixin, unittest.TestCase):
    def test_fields_are_normalized_without_meta_fields(self):
        body = {
            "expected_version": 3,
            "reason": "fix",
            "sku_code": " TEE-1 ",
            "barcode": "  ",
            "name": " Tee ",
            "reference_price": None,
        }
        self.assertEqual(
            normalize_update(SKU, body),
            {"sku_code": "TEE-1", "barcode": None, "name": "Tee", "reference_price": None},
        )
        product_body = {"expected_version": 1, "name": " P "}
        self.assertEqual(normalize_update(PRODUCT, product_body), {"name": "P"})

    def test_only_meta_fields_is_rejected(self):
        for object_type in (PRODUCT, SKU):
            for body in ({"expected_version": 1, "reason": "x"}, {"expected_version": 1}, {}):
                with self.subTest(object_type=object_type, body=body):
                    with self.assertRaises(NothingToUpdate):
                        normalize_update(object_type, body)

    def test_name_and_sku_code_do_not_accept_none(self):
        for object_type, field in ((PRODUCT, "name"), (SKU, "name"), (SKU, "sku_code")):
            with self.subTest(object_type=object_type, field=field):
                body = {"expected_version": 1, field: None}
                self.assert_invalid(field, normalize_update, object_type, body)

    def test_clearable_fields_accept_none(self):
        self.assertEqual(normalize_update(SKU, {"barcode": None}), {"barcode": None})
        self.assertEqual(
            normalize_update(SKU, {"reference_price": None}), {"reference_price": None}
        )

    def test_unknown_fields_are_rejected(self):
        for field in ("status", "product_id", "merged_into_sku_id", "version"):
            with self.subTest(field=field):
                self.assert_invalid(field, normalize_update, SKU, {field: "x", "name": "n"})
        self.assert_invalid("sku_code", normalize_update, PRODUCT, {"sku_code": "A"})


class MergedSkuUpdateTest(unittest.TestCase):
    def test_clearing_barcode_is_allowed(self):
        for body in (
            {"expected_version": 2, "barcode": None},
            {"expected_version": 2, "barcode": "   "},
            {"expected_version": 2, "barcode": "", "reason": "free it"},
            {"barcode": None},
        ):
            with self.subTest(body=body):
                self.assertTrue(merged_sku_update_allowed(body))

    def test_normalized_fields_are_accepted_too(self):
        fields = normalize_update(SKU, {"expected_version": 2, "barcode": " "})
        self.assertTrue(merged_sku_update_allowed(fields))

    def test_anything_else_is_rejected(self):
        for body in (
            {"expected_version": 2, "name": "x"},
            {"expected_version": 2, "barcode": None, "name": "x"},
            {"expected_version": 2, "barcode": "9555"},
            {"expected_version": 2, "reference_price": None},
            {"expected_version": 2, "reason": "x"},
            {},
        ):
            with self.subTest(body=body):
                self.assertFalse(merged_sku_update_allowed(body))


class CodeKeyTest(unittest.TestCase):
    def test_ascii_case_insensitive(self):
        self.assertEqual(code_key("Tee-RED m"), "tee-red m")
        self.assertEqual(code_key("ABC"), code_key("abc"))
        self.assertNotEqual(code_key("ABC"), code_key("ABD"))


class DiffTest(unittest.TestCase):
    CURRENT = {
        "sku_code": "TEE-1",
        "barcode": "9555",
        "name": "Tee",
        "reference_price": {"amount_minor": 12900, "currency": "MYR"},
    }

    def test_same_values_are_not_changes(self):
        same = {
            "sku_code": "TEE-1",
            "barcode": "9555",
            "name": "Tee",
            "reference_price": {"amount_minor": 12900, "currency": "MYR"},
        }
        self.assertEqual(diff_fields(self.CURRENT, same), {})
        self.assertEqual(diff_fields(self.CURRENT, {}), {})

    def test_case_only_change_counts(self):
        self.assertEqual(
            diff_fields(self.CURRENT, {"sku_code": "tee-1"}),
            {"sku_code": {"before": "TEE-1", "after": "tee-1"}},
        )

    def test_reference_price_is_compared_as_a_whole(self):
        before = self.CURRENT["reference_price"]
        for after in (
            {"amount_minor": 13900, "currency": "MYR"},
            {"amount_minor": 12900, "currency": "SGD"},
            None,
        ):
            with self.subTest(after=after):
                self.assertEqual(
                    diff_fields(self.CURRENT, {"reference_price": after}),
                    {"reference_price": {"before": before, "after": after}},
                )

    def test_clearing_barcode(self):
        self.assertEqual(
            diff_fields(self.CURRENT, {"barcode": None}),
            {"barcode": {"before": "9555", "after": None}},
        )

    def test_only_changed_fields_are_listed(self):
        self.assertEqual(
            diff_fields(self.CURRENT, {"name": "Tee", "barcode": "9556"}),
            {"barcode": {"before": "9555", "after": "9556"}},
        )

    def test_product_created(self):
        self.assertEqual(
            product_created_changes("Tee"),
            {
                "name": {"before": None, "after": "Tee"},
                "status": {"before": None, "after": "active"},
            },
        )

    def test_sku_created_lists_all_fields_with_null_before(self):
        price = {"amount_minor": 0, "currency": "MYR"}
        changes = sku_created_changes("TEE-1", None, "Tee", price)
        self.assertEqual(
            list(changes), ["sku_code", "barcode", "name", "reference_price", "status"]
        )
        self.assertEqual(
            changes,
            {
                "sku_code": {"before": None, "after": "TEE-1"},
                "barcode": {"before": None, "after": None},
                "name": {"before": None, "after": "Tee"},
                "reference_price": {"before": None, "after": price},
                "status": {"before": None, "after": "active"},
            },
        )

    def test_status_changes(self):
        self.assertEqual(
            status_changes(ACTIVE, INACTIVE),
            {"status": {"before": "active", "after": "inactive"}},
        )
        self.assertEqual(
            status_changes(INACTIVE, ACTIVE),
            {"status": {"before": "inactive", "after": "active"}},
        )
        # 已是目标状态：不加版本、不写记录。
        self.assertEqual(status_changes(ACTIVE, ACTIVE), {})
        self.assertEqual(status_changes(INACTIVE, INACTIVE), {})
        with self.assertRaises(ValueError):
            status_changes(ACTIVE, "deleted")


class CodeClaimTest(unittest.TestCase):
    def test_unclaimed_code_is_inserted(self):
        self.assertEqual(decide_code_claim(None, False, None), CLAIM_INSERT)
        self.assertEqual(decide_code_claim(None, False, "sku_1"), CLAIM_INSERT)

    def test_own_code_is_not_inserted_again(self):
        # 只改大小写：归属自己，比较键与自己的当前编码相同。
        self.assertEqual(decide_code_claim("sku_1", True, "sku_1"), CLAIM_OWNED)
        # 改回自己用过的编码：归属自己，但不是当前编码。
        self.assertEqual(decide_code_claim("sku_1", False, "sku_1"), CLAIM_OWNED)

    def test_current_code_of_another_sku_is_taken(self):
        self.assertEqual(decide_code_claim("sku_2", True, "sku_1"), SKU_CODE_TAKEN)
        self.assertEqual(decide_code_claim("sku_2", True, None), SKU_CODE_TAKEN)

    def test_retired_code_of_another_sku_is_retired(self):
        self.assertEqual(decide_code_claim("sku_2", False, "sku_1"), SKU_CODE_RETIRED)
        self.assertEqual(decide_code_claim("sku_2", False, None), SKU_CODE_RETIRED)


class ConflictTest(unittest.TestCase):
    def test_top_code_order(self):
        all_three = [
            Conflict("barcode", "9555", BARCODE_TAKEN, 0),
            Conflict("sku_code", "B", SKU_CODE_RETIRED, 1),
            Conflict("sku_code", "C", SKU_CODE_TAKEN, 2),
        ]
        self.assertEqual(conflict_code(all_three), SKU_CODE_TAKEN)
        retired_and_barcode = [
            Conflict("barcode", "9555", BARCODE_TAKEN, 0),
            Conflict("sku_code", "B", SKU_CODE_RETIRED, 1),
        ]
        self.assertEqual(conflict_code(retired_and_barcode), SKU_CODE_RETIRED)
        barcode_only = [Conflict("barcode", "9555", BARCODE_TAKEN)]
        self.assertEqual(conflict_code(barcode_only), BARCODE_TAKEN)
        self.assertIsNone(conflict_code([]))

    def test_all_conflicts_kept_and_ordered(self):
        conflicts = [
            Conflict("barcode", "9557", BARCODE_TAKEN, 2),
            Conflict("barcode", "9555", BARCODE_TAKEN, 0),
            Conflict("sku_code", "C", SKU_CODE_TAKEN, 2),
            Conflict("sku_code", "A", SKU_CODE_RETIRED, 0),
            Conflict("sku_code", "B", SKU_CODE_TAKEN, 1),
        ]
        self.assertEqual(
            [(c.index, c.field) for c in sort_conflicts(conflicts)],
            [(0, "sku_code"), (0, "barcode"), (1, "sku_code"), (2, "sku_code"), (2, "barcode")],
        )

    def test_without_index(self):
        conflicts = [
            Conflict("barcode", "9555", BARCODE_TAKEN),
            Conflict("sku_code", "A", SKU_CODE_TAKEN),
        ]
        ordered = sort_conflicts(conflicts)
        self.assertEqual([c.field for c in ordered], ["sku_code", "barcode"])
        self.assertEqual(
            ordered[0].as_dict(), {"field": "sku_code", "value": "A", "code": SKU_CODE_TAKEN}
        )
        self.assertEqual(
            Conflict("barcode", "9555", BARCODE_TAKEN, 3).as_dict(),
            {"index": 3, "field": "barcode", "value": "9555", "code": BARCODE_TAKEN},
        )

    def test_unknown_field_or_code_is_rejected(self):
        with self.assertRaises(ValueError):
            Conflict("name", "x", SKU_CODE_TAKEN)
        with self.assertRaises(ValueError):
            Conflict("sku_code", "x", "VERSION_CONFLICT")


class CatalogLockNameTest(unittest.TestCase):
    def test_is_sha256_of_database_catalog_tenant_joined_by_newline(self):
        # 期望值按契约拼法从字面量字节算出，不经被测函数的拼接逻辑。
        expected = hashlib.sha256(b"stock_core_demo\ncatalog\nten_1").hexdigest()
        name = catalog_lock_name("stock_core_demo", "ten_1")
        self.assertRegex(name, HEX64)
        self.assertEqual(name, expected)

    def test_parts_change_the_name(self):
        base = catalog_lock_name("stock_core_demo", "ten_1")
        self.assertNotEqual(catalog_lock_name("stock_core_prod", "ten_1"), base)
        self.assertNotEqual(catalog_lock_name("stock_core_demo", "ten_2"), base)

    def test_empty_or_newline_parts_are_rejected(self):
        for database, tenant in (("", "ten_1"), ("db", ""), ("d\nb", "ten_1"), ("db", None)):
            with self.subTest(database=database, tenant=tenant), self.assertRaises(ValueError):
                catalog_lock_name(database, tenant)


class EventTypeTest(unittest.TestCase):
    def test_eight_event_types(self):
        self.assertEqual(
            EVENT_TYPES,
            {
                "product_created",
                "product_updated",
                "product_deactivated",
                "product_reactivated",
                "sku_created",
                "sku_updated",
                "sku_deactivated",
                "sku_reactivated",
            },
        )


class CursorTest(unittest.TestCase):
    def assert_invalid(self, value):
        with self.assertRaises(InvalidParameter) as caught:
            parse_cursor(value)
        self.assertEqual(caught.exception.parameter, AFTER)

    def test_absent_starts_from_the_beginning(self):
        self.assertIsNone(parse_cursor(None))

    def test_positive_decimal_integers(self):
        self.assertEqual(parse_cursor("1"), 1)
        self.assertEqual(parse_cursor("42"), 42)
        self.assertEqual(parse_cursor("9223372036854775807"), 9223372036854775807)

    def test_malformed_cursors_are_rejected(self):
        for bad in (
            "0",
            "01",
            "+1",
            "-1",
            "",
            " 1",
            "1 ",
            "1\n",
            "1.0",
            "1e3",
            "0x1",
            "１",
            "12345678901234567890",
            "9223372036854775808",
            "99999999999999999999",
            1,
        ):
            with self.subTest(value=bad):
                self.assert_invalid(bad)

    def test_format_round_trips(self):
        self.assertEqual(format_cursor(42), "42")
        self.assertEqual(parse_cursor(format_cursor(9223372036854775807)), 9223372036854775807)


class LimitTest(unittest.TestCase):
    def test_default_and_range(self):
        self.assertEqual(parse_limit(None), 100)
        self.assertEqual(parse_limit("1"), 1)
        self.assertEqual(parse_limit("50"), 50)
        self.assertEqual(parse_limit("100"), 100)

    def test_malformed_limits_are_rejected(self):
        for bad in ("0", "101", "01", "001", "+5", "-5", "", " 5", "5 ", "5.0", "1000", "５", 5):
            with self.subTest(value=bad):
                with self.assertRaises(InvalidParameter) as caught:
                    parse_limit(bad)
                self.assertEqual(caught.exception.parameter, LIMIT)


if __name__ == "__main__":
    unittest.main()
