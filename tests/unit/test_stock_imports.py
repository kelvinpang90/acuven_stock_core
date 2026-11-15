import unittest

from app.domain.stock_imports import (
    ALREADY_OPENED,
    CREATED,
    DUPLICATE_SKU,
    FAILED,
    INVALID_FORMAT,
    OK,
    PREVIEW_RESULTS,
    SKIPPED,
    SKU_MERGED,
    SKU_NOT_FOUND,
    SkuState,
    judge_rows,
    plan_confirm,
    summarize,
)

PREVIEW_KEYS = {"index", "sku_id", "quantity", "result"}
CONFIRM_KEYS = {"index", "sku_id", "quantity", "outcome", "entry_id", "preview_result", "failure"}

# 本租户里存在的 SKU；不在这里的算不存在。
LOOKUP = {
    "sku_a": SkuState(),
    "sku_b": SkuState(),
    "sku_c": SkuState(),
    "sku_merged": SkuState(merged=True),
    "sku_opened": SkuState(opened=True),
    "sku_merged_opened": SkuState(merged=True, opened=True),
}


def row(sku_id, quantity=10):
    return {"sku_id": sku_id, "quantity": quantity}


def results(previews):
    return [p["result"] for p in previews]


class InvalidFormatTest(unittest.TestCase):
    def test_well_formed_rows_are_ok(self):
        previews = judge_rows([row("sku_a", 0), row("sku_b", 1000000)], LOOKUP)
        self.assertEqual(
            previews,
            [
                {"index": 0, "sku_id": "sku_a", "quantity": 0, "result": OK},
                {"index": 1, "sku_id": "sku_b", "quantity": 1000000, "result": OK},
            ],
        )

    def test_bad_rows(self):
        for bad in (
            "sku_a",
            None,
            ["sku_a", 10],
            {},
            {"sku_id": "sku_a"},
            {"quantity": 10},
            {"sku_id": "sku_a", "quantity": 10, "note": "x"},
            row(None),
            row(1),
            row(["sku_a"]),
            row("sku_a", -1),
            row("sku_a", 1000001),
            row("sku_a", True),
            row("sku_a", False),
            row("sku_a", 1.0),
            row("sku_a", "10"),
            row("sku_a", None),
        ):
            with self.subTest(row=bad):
                [p] = judge_rows([bad], LOOKUP)
                self.assertEqual(p["result"], INVALID_FORMAT)
                self.assertEqual(set(p), PREVIEW_KEYS)

    def test_invalid_format_wins_over_lookup(self):
        # 格式不合格时不看 lookup，哪怕 SKU 已合并。
        self.assertEqual(results(judge_rows([row("sku_merged", -1)], LOOKUP)), [INVALID_FORMAT])


class EchoTest(unittest.TestCase):
    def test_echo_only_strings_and_integers(self):
        previews = judge_rows(
            [
                row("sku_a", -5),
                row(7, 10),
                row("sku_b", True),
                row("sku_c", 2.5),
                "not an object",
                {"sku_id": "sku_x", "quantity": 3, "extra": 1},
                {"quantity": 4},
            ],
            LOOKUP,
        )
        self.assertEqual(
            [(p["sku_id"], p["quantity"]) for p in previews],
            [
                ("sku_a", -5),
                (None, 10),
                ("sku_b", None),
                ("sku_c", None),
                (None, None),
                ("sku_x", 3),
                (None, 4),
            ],
        )
        self.assertEqual(results(previews), [INVALID_FORMAT] * 7)

    def test_keys_are_fixed(self):
        previews = judge_rows([row("sku_a"), "x", row("missing"), row("sku_a", -1)], LOOKUP)
        for p in previews:
            self.assertEqual(set(p), PREVIEW_KEYS)
        self.assertEqual([p["index"] for p in previews], [0, 1, 2, 3])


class OrderTest(unittest.TestCase):
    def test_each_lookup_result(self):
        previews = judge_rows(
            [row("sku_a"), row("missing"), row("sku_merged"), row("sku_opened")], LOOKUP
        )
        self.assertEqual(results(previews), [OK, SKU_NOT_FOUND, SKU_MERGED, ALREADY_OPENED])

    def test_merged_before_already_opened(self):
        self.assertEqual(results(judge_rows([row("sku_merged_opened")], LOOKUP)), [SKU_MERGED])

    def test_all_duplicate_rows_are_marked(self):
        previews = judge_rows([row("sku_a", 1), row("sku_b"), row("sku_a", 2)], LOOKUP)
        self.assertEqual(results(previews), [DUPLICATE_SKU, OK, DUPLICATE_SKU])

    def test_duplicate_wins_over_lookup(self):
        for sku_id in ("sku_merged", "sku_opened", "missing"):
            with self.subTest(sku_id=sku_id):
                previews = judge_rows([row(sku_id), row(sku_id)], LOOKUP)
                self.assertEqual(results(previews), [DUPLICATE_SKU, DUPLICATE_SKU])

    def test_duplicate_compares_strings_as_is(self):
        previews = judge_rows([row("sku_a"), row("SKU_A"), row(" sku_a")], LOOKUP)
        self.assertEqual(results(previews), [OK, SKU_NOT_FOUND, SKU_NOT_FOUND])

    def test_invalid_rows_do_not_make_duplicates(self):
        previews = judge_rows(
            [
                row("sku_a"),
                row("sku_a", -1),
                {"sku_id": "sku_a", "quantity": 1, "extra": True},
                {"sku_id": "sku_a"},
            ],
            LOOKUP,
        )
        self.assertEqual(results(previews), [OK] + [INVALID_FORMAT] * 3)

    def test_rows_do_not_affect_each_other(self):
        previews = judge_rows([row("sku_a"), row("missing"), row("sku_merged"), "x"], LOOKUP)
        self.assertEqual(results(previews)[0], OK)

    def test_zero_quantity_is_ok(self):
        self.assertEqual(results(judge_rows([row("sku_a", 0)], LOOKUP)), [OK])

    def test_empty_string_sku_id_is_well_formed(self):
        self.assertEqual(results(judge_rows([row("")], LOOKUP)), [SKU_NOT_FOUND])


class SummarizeTest(unittest.TestCase):
    def test_all_keys_present_when_zero(self):
        summary = summarize([])
        self.assertEqual(summary["counts"], {result: 0 for result in PREVIEW_RESULTS})
        self.assertEqual(len(summary["counts"]), 6)
        self.assertEqual(summary["executable_rows"], 0)
        self.assertEqual(summary["executable_quantity"], 0)

    def test_counts_and_quantity_of_ok_rows_only(self):
        previews = judge_rows(
            [
                row("sku_a", 5),
                row("sku_b", 0),
                row("sku_c", 7),
                row("missing", 100),
                row("sku_merged", 200),
                row("sku_opened", 300),
                row("dup", 400),
                row("dup", 500),
                row("sku_x", -1),
            ],
            LOOKUP,
        )
        summary = summarize(previews)
        self.assertEqual(
            summary,
            {
                "counts": {
                    INVALID_FORMAT: 1,
                    DUPLICATE_SKU: 2,
                    SKU_NOT_FOUND: 1,
                    SKU_MERGED: 1,
                    ALREADY_OPENED: 1,
                    OK: 3,
                },
                "executable_rows": 3,
                "executable_quantity": 12,
            },
        )


class PlanConfirmTest(unittest.TestCase):
    def test_recheck_outcomes(self):
        previews = judge_rows(
            [row("sku_a", 5), row("sku_b", 6), row("sku_c", 7), row("gone", 8)],
            {**LOOKUP, "gone": SkuState()},
        )
        self.assertEqual(results(previews), [OK, OK, OK, OK])
        now = {
            "sku_a": SkuState(),
            "sku_b": SkuState(opened=True),
            "sku_c": SkuState(merged=True),
        }
        plan = plan_confirm(previews, now)
        self.assertEqual(
            plan,
            [
                {
                    "index": 0,
                    "sku_id": "sku_a",
                    "quantity": 5,
                    "outcome": CREATED,
                    "entry_id": None,
                    "preview_result": OK,
                    "failure": None,
                },
                {
                    "index": 1,
                    "sku_id": "sku_b",
                    "quantity": 6,
                    "outcome": FAILED,
                    "entry_id": None,
                    "preview_result": OK,
                    "failure": ALREADY_OPENED,
                },
                {
                    "index": 2,
                    "sku_id": "sku_c",
                    "quantity": 7,
                    "outcome": FAILED,
                    "entry_id": None,
                    "preview_result": OK,
                    "failure": SKU_MERGED,
                },
                {
                    "index": 3,
                    "sku_id": "gone",
                    "quantity": 8,
                    "outcome": FAILED,
                    "entry_id": None,
                    "preview_result": OK,
                    "failure": SKU_NOT_FOUND,
                },
            ],
        )

    def test_merged_before_already_opened_on_recheck(self):
        previews = judge_rows([row("sku_a")], LOOKUP)
        [item] = plan_confirm(previews, {"sku_a": SkuState(merged=True, opened=True)})
        self.assertEqual((item["outcome"], item["failure"]), (FAILED, SKU_MERGED))

    def test_preview_failures_are_skipped_with_preview_result(self):
        previews = judge_rows(
            [
                row("sku_a", -1),
                row("dup"),
                row("dup"),
                row("missing"),
                row("sku_merged"),
                row("sku_opened"),
                "x",
            ],
            LOOKUP,
        )
        # 重查时这些 SKU 都已正常，也不改变跳过的结果。
        everything_fine = {sku_id: SkuState() for sku_id in ("sku_a", "dup", "missing")}
        everything_fine.update(sku_merged=SkuState(), sku_opened=SkuState())
        plan = plan_confirm(previews, everything_fine)
        self.assertEqual(
            [(p["outcome"], p["preview_result"], p["failure"]) for p in plan],
            [
                (SKIPPED, INVALID_FORMAT, None),
                (SKIPPED, DUPLICATE_SKU, None),
                (SKIPPED, DUPLICATE_SKU, None),
                (SKIPPED, SKU_NOT_FOUND, None),
                (SKIPPED, SKU_MERGED, None),
                (SKIPPED, ALREADY_OPENED, None),
                (SKIPPED, INVALID_FORMAT, None),
            ],
        )
        self.assertEqual(plan[0]["sku_id"], "sku_a")
        self.assertEqual(plan[0]["quantity"], -1)
        self.assertIsNone(plan[6]["sku_id"])
        self.assertIsNone(plan[6]["quantity"])

    def test_keys_are_fixed_and_entry_id_is_null(self):
        previews = judge_rows([row("sku_a"), row("sku_opened"), row("sku_b", 0), "x"], LOOKUP)
        plan = plan_confirm(previews, {"sku_a": SkuState(), "sku_b": SkuState(opened=True)})
        self.assertEqual([p["outcome"] for p in plan], [CREATED, SKIPPED, FAILED, SKIPPED])
        for item in plan:
            self.assertEqual(set(item), CONFIRM_KEYS)
            self.assertIsNone(item["entry_id"])

    def test_zero_quantity_row_is_created(self):
        [item] = plan_confirm(judge_rows([row("sku_a", 0)], LOOKUP), LOOKUP)
        self.assertEqual((item["outcome"], item["quantity"]), (CREATED, 0))

    def test_rows_follow_index_order(self):
        previews = judge_rows([row("sku_a"), row("sku_b"), row("sku_c")], LOOKUP)
        plan = plan_confirm(list(reversed(previews)), LOOKUP)
        self.assertEqual([p["index"] for p in plan], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
