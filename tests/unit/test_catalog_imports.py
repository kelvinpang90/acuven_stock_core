import unittest

from app.domain.catalog import code_key
from app.domain.catalog_imports import (
    CONFLICTS_EXISTING,
    CREATED,
    DUPLICATE_BARCODE,
    DUPLICATE_SKU_CODE,
    FAILED,
    GROUP_BLOCKED,
    INVALID_FORMAT,
    OK,
    PREVIEW_RESULTS,
    SKIPPED,
    UNKNOWN_FIELD,
    ImportedSku,
    plan_confirm,
    preview_import,
    recheck_conflicts,
    summarize_preview,
)

# 传给 row() 表示不带这个字段。
_DROP = object()


def row(n, product_ref=None, **overrides):
    item = {
        "external_ref": f"var-{n}",
        "product_ref": product_ref or f"prd-{n}",
        "product_name": "Tee",
        "sku_code": f"SKU-{n}",
        "sku_name": f"Tee {n}",
        "barcode": f"BC{n}",
        "reference_price": {"amount_minor": 12900, "currency": "MYR"},
    }
    if product_ref is _DROP:
        del item["product_ref"]
    for key, value in overrides.items():
        if value is _DROP:
            del item[key]
        else:
            item[key] = value
    return item


def preview(rows, claimed=(), barcodes=()):
    return preview_import(rows, frozenset(claimed), frozenset(barcodes))


def results(previews):
    return [p.result for p in previews]


class FieldTests(unittest.TestCase):
    def test_valid_row_is_ok_with_normalized_values(self):
        [p] = preview([row(1, product_name="  Tee ", sku_code=" SKU-1 ", sku_name=" Tee 1 ")])
        self.assertEqual(p.result, OK)
        self.assertEqual(p.index, 0)
        self.assertEqual(p.invalid_fields, ())
        self.assertEqual(
            p.values,
            ImportedSku(
                external_ref="var-1",
                product_ref="prd-1",
                product_name="Tee",
                sku_code="SKU-1",
                sku_name="Tee 1",
                barcode="BC1",
                reference_price={"amount_minor": 12900, "currency": "MYR"},
            ),
        )

    def test_missing_required_field_is_invalid_format(self):
        for field in ("external_ref", "product_ref", "product_name", "sku_code", "sku_name"):
            with self.subTest(field=field):
                [p] = preview([row(1, **{field: _DROP})])
                self.assertEqual(p.result, INVALID_FORMAT)
                self.assertEqual(p.invalid_fields, (field,))
                self.assertIsNone(p.values)

    def test_missing_refs_are_reported_as_none(self):
        [p] = preview([row(1, external_ref=_DROP, product_ref=_DROP)])
        self.assertIsNone(p.external_ref)
        self.assertIsNone(p.product_ref)
        self.assertEqual(p.invalid_fields, ("external_ref", "product_ref"))

    def test_optional_fields_absent_or_null(self):
        rows = [
            row(1, barcode=_DROP, reference_price=_DROP),
            row(2, barcode=None, reference_price=None),
        ]
        for p in preview(rows):
            self.assertEqual(p.result, OK)
            self.assertIsNone(p.values.barcode)
            self.assertIsNone(p.values.reference_price)

    def test_extra_fields_recorded_once_without_caller_keys(self):
        [p] = preview([row(1, color="red", **{"<script>": 1})])
        self.assertEqual(p.result, INVALID_FORMAT)
        self.assertEqual(p.invalid_fields, (UNKNOWN_FIELD,))

    def test_wrong_type_or_value(self):
        cases = {
            "sku_code": 123,
            "sku_name": None,
            "product_name": "   ",
            "barcode": 42,
            "reference_price": {"amount_minor": 1.5, "currency": "MYR"},
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                [p] = preview([row(1, **{field: value})])
                self.assertEqual(p.result, INVALID_FORMAT)
                self.assertEqual(p.invalid_fields, (field,))

    def test_reference_format(self):
        cases = {"external_ref": "var 1", "product_ref": "p" * 129}
        for field, value in cases.items():
            with self.subTest(field=field):
                [p] = preview([row(1, **{field: value})])
                self.assertEqual(p.result, INVALID_FORMAT)
                self.assertEqual(p.invalid_fields, (field,))

    def test_invalid_fields_merged_and_ordered(self):
        rows = [
            row(1, product_ref="P", extra=1, barcode=7, sku_name=""),
            row(1, product_ref="P", product_name="Other", another=2),
        ]
        first, second = preview(rows)
        self.assertEqual(
            first.invalid_fields,
            ("external_ref", "product_name", "sku_name", "barcode", UNKNOWN_FIELD),
        )
        self.assertEqual(second.invalid_fields, ("external_ref", "product_name", UNKNOWN_FIELD))


class RuleTests(unittest.TestCase):
    def test_duplicate_external_ref_marks_every_row(self):
        rows = [row(1), row(2, external_ref="var-1"), row(3)]
        previews = preview(rows)
        self.assertEqual(results(previews), [INVALID_FORMAT, INVALID_FORMAT, OK])
        self.assertEqual(previews[1].invalid_fields, ("external_ref",))

    def test_external_ref_compared_case_sensitively(self):
        self.assertEqual(
            results(preview([row(1, external_ref="Var"), row(2, external_ref="var")])), [OK, OK]
        )

    def test_product_name_compared_after_strip(self):
        rows = [row(1, product_ref="P", product_name="Tee"), row(2, "P", product_name=" Tee ")]
        self.assertEqual(results(preview(rows)), [OK, OK])

    def test_product_name_mismatch_marks_whole_group(self):
        rows = [
            row(1, "P", product_name="Tee"),
            row(2, "P", product_name="tee"),
            row(3, "Q", product_name="Other"),
        ]
        previews = preview(rows)
        self.assertEqual(results(previews), [INVALID_FORMAT, INVALID_FORMAT, OK])
        self.assertEqual(previews[0].invalid_fields, ("product_name",))
        self.assertEqual(previews[1].invalid_fields, ("product_name",))

    def test_product_name_only_compared_between_valid_names(self):
        rows = [row(1, "P", product_name="Tee"), row(2, "P", product_name="")]
        previews = preview(rows)
        self.assertEqual(results(previews), [GROUP_BLOCKED, INVALID_FORMAT])
        self.assertEqual(previews[1].invalid_fields, ("product_name",))

    def test_product_ref_compared_case_sensitively(self):
        rows = [row(1, "P", product_name="Tee"), row(2, "p", product_name="Shirt")]
        self.assertEqual(results(preview(rows)), [OK, OK])

    def test_sku_code_differing_only_in_case_is_duplicate(self):
        rows = [row(1, sku_code="ABC"), row(2, sku_code=" abc ")]
        self.assertEqual(results(preview(rows)), [DUPLICATE_SKU_CODE, DUPLICATE_SKU_CODE])

    def test_non_empty_barcode_duplicate(self):
        rows = [row(1, barcode="X1"), row(2, barcode="x1")]
        self.assertEqual(results(preview(rows)), [DUPLICATE_BARCODE, DUPLICATE_BARCODE])

    def test_null_barcodes_are_not_duplicates(self):
        rows = [row(1, barcode=None), row(2, barcode=None), row(3, barcode="")]
        self.assertEqual(results(preview(rows)), [OK, OK, OK])

    def test_sku_code_duplicate_wins_over_barcode_duplicate(self):
        rows = [row(1, sku_code="A", barcode="B"), row(2, sku_code="A", barcode="B")]
        self.assertEqual(results(preview(rows)), [DUPLICATE_SKU_CODE, DUPLICATE_SKU_CODE])

    def test_invalid_row_does_not_take_part_in_duplicate_checks(self):
        rows = [
            row(1, sku_code="A", barcode="B", sku_name=""),
            row(2, sku_code="a", barcode="b"),
        ]
        self.assertEqual(results(preview(rows)), [INVALID_FORMAT, OK])

    def test_invalid_row_does_not_take_part_in_conflict_check(self):
        [p] = preview([row(1, sku_code="A", sku_name="")], claimed={"a"})
        self.assertEqual(p.invalid_fields, ("sku_name",))

    def test_request_duplicate_wins_over_existing_conflict(self):
        rows = [row(1, sku_code="A"), row(2, sku_code="A")]
        self.assertEqual(
            results(preview(rows, claimed={"a"})), [DUPLICATE_SKU_CODE, DUPLICATE_SKU_CODE]
        )


class ConflictAndGroupTests(unittest.TestCase):
    def test_sku_code_claimed_including_retired_and_case(self):
        # 归属集合里同时有当前与退役编码的比较键，服务层不区分。
        claimed = {code_key("SKU-CUR"), code_key("old-1")}
        rows = [row(1, sku_code="SKU-CUR"), row(2, sku_code="OLD-1"), row(3)]
        self.assertEqual(
            results(preview(rows, claimed=claimed)), [CONFLICTS_EXISTING, CONFLICTS_EXISTING, OK]
        )

    def test_barcode_in_existing_barcodes(self):
        rows = [row(1, barcode="X9"), row(2, barcode=None)]
        self.assertEqual(results(preview(rows, barcodes={"x9"})), [CONFLICTS_EXISTING, OK])

    def test_one_bad_row_blocks_rest_of_group(self):
        rows = [
            row(1, "P"),
            row(2, "P", sku_code=""),
            row(3, "P"),
            row(4, "Q"),
        ]
        expected = [GROUP_BLOCKED, INVALID_FORMAT, GROUP_BLOCKED, OK]
        self.assertEqual(results(preview(rows)), expected)

    def test_conflicting_row_blocks_rest_of_group(self):
        rows = [row(1, "P"), row(2, "P", sku_code="TAKEN")]
        self.assertEqual(
            results(preview(rows, claimed={"taken"})), [GROUP_BLOCKED, CONFLICTS_EXISTING]
        )

    def test_invalid_product_ref_row_belongs_to_no_group(self):
        rows = [
            row(1, product_ref="bad ref"),
            row(2, product_ref="bad ref", product_name="Other"),
        ]
        previews = preview(rows)
        self.assertEqual(results(previews), [INVALID_FORMAT, INVALID_FORMAT])
        self.assertEqual(previews[0].invalid_fields, ("product_ref",))
        self.assertEqual(previews[1].invalid_fields, ("product_ref",))
        self.assertEqual(previews[0].product_ref, "bad ref")

    def test_invalid_product_ref_row_does_not_block_others(self):
        # "P " 不合规，不与 "P" 同组；名称不同也不连累 "P" 的行。
        rows = [row(1, product_ref="P ", product_name="Other"), row(2, product_ref="P")]
        self.assertEqual(results(preview(rows)), [INVALID_FORMAT, OK])


class SummaryTests(unittest.TestCase):
    def test_counts_every_result_and_executable_groups(self):
        rows = [
            row(1, "P"),
            row(2, "P"),
            row(3, "Q"),
            row(4, "R", sku_code="DUP"),
            row(5, "S", sku_code="dup"),
            row(6, "T", barcode="BB"),
            row(7, "U", barcode="bb"),
            row(8, "V", sku_code="TAKEN"),
            row(9, "V"),
            row(10, "W", sku_name=""),
        ]
        summary = summarize_preview(preview(rows, claimed={"taken"}))
        self.assertEqual(
            summary.counts,
            {
                INVALID_FORMAT: 1,
                DUPLICATE_SKU_CODE: 2,
                DUPLICATE_BARCODE: 2,
                CONFLICTS_EXISTING: 1,
                GROUP_BLOCKED: 1,
                OK: 3,
            },
        )
        self.assertEqual(summary.executable_groups, 2)
        self.assertEqual(summary.executable_rows, 3)

    def test_empty_counts_have_all_keys(self):
        summary = summarize_preview(preview([row(1, sku_name="")]))
        self.assertEqual(set(summary.counts), set(PREVIEW_RESULTS))
        self.assertEqual(summary.counts[INVALID_FORMAT], 1)
        self.assertEqual(summary.executable_groups, 0)
        self.assertEqual(summary.executable_rows, 0)


class ConfirmPlanTests(unittest.TestCase):
    def test_two_ok_groups_created_in_first_appearance_order(self):
        rows = [
            row(1, "Q", product_name="Shirt"),
            row(2, "P", product_name="Tee"),
            row(3, "Q", product_name="Shirt"),
        ]
        plan = plan_confirm(preview(rows), frozenset())
        self.assertEqual([p.product_ref for p in plan.products], ["Q", "P"])
        self.assertEqual(plan.products[0].name, "Shirt")
        self.assertEqual([s.index for s in plan.products[0].skus], [0, 2])
        self.assertEqual([s.name for s in plan.products[0].skus], ["Tee 1", "Tee 3"])
        self.assertEqual(plan.products[1].skus[0].sku_code, "SKU-2")
        self.assertEqual([r.outcome for r in plan.rows], [CREATED, CREATED, CREATED])
        self.assertTrue(all(r.result is None and r.product_id is None for r in plan.rows))

    def test_created_row_takes_ids(self):
        plan = plan_confirm(preview([row(1)]), frozenset())
        created = plan.rows[0].with_ids("prd_01", "sku_01")
        self.assertEqual((created.product_id, created.sku_id), ("prd_01", "sku_01"))

    def test_recheck_conflict_fails_its_group_only(self):
        rows = [row(1, "P"), row(2, "P", sku_code="LATE"), row(3, "Q")]
        previews = preview(rows)
        conflicting = recheck_conflicts(previews, {code_key("late")}, frozenset())
        self.assertEqual(conflicting, frozenset({1}))
        plan = plan_confirm(previews, conflicting)
        self.assertEqual([p.product_ref for p in plan.products], ["Q"])
        self.assertEqual(
            [(r.outcome, r.result) for r in plan.rows],
            [(FAILED, GROUP_BLOCKED), (FAILED, CONFLICTS_EXISTING), (CREATED, None)],
        )

    def test_recheck_finds_barcode_conflict(self):
        previews = preview([row(1, barcode="NEW"), row(2, barcode=None)])
        self.assertEqual(recheck_conflicts(previews, frozenset(), {"new"}), frozenset({0}))

    def test_preview_rejects_are_skipped_with_preview_result(self):
        rows = [row(1, "P"), row(2, "P", sku_name=""), row(3, "Q")]
        plan = plan_confirm(preview(rows), frozenset())
        self.assertEqual([p.product_ref for p in plan.products], ["Q"])
        self.assertEqual(
            [(r.outcome, r.result) for r in plan.rows],
            [(SKIPPED, GROUP_BLOCKED), (SKIPPED, INVALID_FORMAT), (CREATED, None)],
        )
        self.assertEqual(plan.rows[1].external_ref, "var-2")
        self.assertEqual(plan.rows[1].product_ref, "P")

    def test_all_groups_conflicting_still_lists_every_row(self):
        rows = [row(1, "P"), row(2, "Q"), row(3, "R", sku_code="")]
        previews = preview(rows)
        plan = plan_confirm(previews, frozenset({0, 1}))
        self.assertEqual(plan.products, ())
        self.assertEqual([r.index for r in plan.rows], [0, 1, 2])
        expected = [
            (FAILED, CONFLICTS_EXISTING),
            (FAILED, CONFLICTS_EXISTING),
            (SKIPPED, INVALID_FORMAT),
        ]
        self.assertEqual([(r.outcome, r.result) for r in plan.rows], expected)

    def test_conflicting_index_must_be_ok_row(self):
        previews = preview([row(1, sku_name="")])
        with self.assertRaises(ValueError):
            plan_confirm(previews, frozenset({0}))


if __name__ == "__main__":
    unittest.main()
