import ast
import hashlib
import unittest
from pathlib import Path

from app.domain import fulfilment
from app.domain.catalog import CURSOR_MAX
from app.domain.fulfilment import (
    DISPATCH,
    REFUND_RESTOCK,
    RETURN_RECEIPT,
    OrderLineTotals,
    QuantityLine,
    ReturnLine,
    apply_line,
    cursor_matches,
    exceeding_lines,
    format_order_stock_cursor,
    line_entries,
    line_limit,
    parse_order_stock_cursor,
    parse_return_quantities,
    requested_quantity,
    unknown_line_refs,
)
from app.domain.ledger import EntryType, apply_entry
from app.domain.quantities import Quantities
from app.domain.stock import QUANTITY_MAX

RESERVATION_ID = "rsv_01JD0000000000000000000000"
OTHER_RESERVATION_ID = "rsv_01JD0000000000000000000001"


def totals(
    line_ref="L1",
    sku_id="sku_a",
    quantity=10,
    committed=10,
    dispatched=0,
    refund_restocked=0,
    returned_sellable=0,
    returned_unsellable=0,
):
    return OrderLineTotals(
        line_ref=line_ref,
        sku_id=sku_id,
        quantity=quantity,
        committed_qty=committed,
        dispatched_qty=dispatched,
        refund_restocked_qty=refund_restocked,
        returned_sellable_qty=returned_sellable,
        returned_unsellable_qty=returned_unsellable,
    )


class ConstantsTest(unittest.TestCase):
    def test_document_types(self):
        self.assertEqual(fulfilment.DISPATCH, "dispatch")
        self.assertEqual(fulfilment.REFUND_RESTOCK, "refund_restock")
        self.assertEqual(fulfilment.RETURN_RECEIPT, "return_receipt")
        self.assertEqual(
            fulfilment.DOCUMENT_TYPES, ("dispatch", "refund_restock", "return_receipt")
        )
        self.assertEqual(fulfilment.SOURCE_TYPES, fulfilment.DOCUMENT_TYPES)

    def test_mappings_by_document_type(self):
        self.assertEqual(
            fulfilment.DOCUMENT_ID_PREFIXES,
            {DISPATCH: "shp", REFUND_RESTOCK: "rfr", RETURN_RECEIPT: "rtn"},
        )
        self.assertEqual(
            fulfilment.REF_FIELDS,
            {DISPATCH: "shipment_ref", REFUND_RESTOCK: "refund_ref", RETURN_RECEIPT: "return_ref"},
        )
        self.assertEqual(
            fulfilment.REF_TYPES,
            {DISPATCH: "shipment", REFUND_RESTOCK: "refund", RETURN_RECEIPT: "return"},
        )

    def test_module_has_no_io_imports(self):
        source = Path(fulfilment.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        allowed = {"collections", "dataclasses", "hashlib", "re", "app"}
        self.assertTrue(imported <= allowed, imported)


class LineLimitTest(unittest.TestCase):
    def test_dispatch_and_refund_restock(self):
        cases = [
            (totals(), 10),
            (totals(dispatched=3), 7),
            (totals(refund_restocked=4), 6),
            (totals(dispatched=3, refund_restocked=4), 3),
            (totals(dispatched=6, refund_restocked=4), 0),
            (totals(dispatched=10), 0),
        ]
        for document_type in (DISPATCH, REFUND_RESTOCK):
            for line, expected in cases:
                with self.subTest(document_type=document_type, line=line):
                    self.assertEqual(line_limit(document_type, line), expected)

    def test_dispatch_ignores_returns(self):
        line = totals(dispatched=5, returned_sellable=2, returned_unsellable=1)
        self.assertEqual(line_limit(DISPATCH, line), 5)

    def test_uncommitted_line_has_no_outstanding(self):
        line = totals(committed=0)
        self.assertEqual(line_limit(DISPATCH, line), 0)
        self.assertEqual(line_limit(REFUND_RESTOCK, line), 0)

    def test_return_receipt(self):
        cases = [
            (totals(dispatched=0), 0),
            (totals(dispatched=8), 8),
            (totals(dispatched=8, returned_sellable=3), 5),
            (totals(dispatched=8, returned_unsellable=2), 6),
            (totals(dispatched=8, returned_sellable=3, returned_unsellable=2), 3),
            (totals(dispatched=8, returned_sellable=5, returned_unsellable=3), 0),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(line_limit(RETURN_RECEIPT, line), expected)

    def test_return_receipt_ignores_refund_restock(self):
        line = totals(dispatched=4, refund_restocked=6)
        self.assertEqual(line_limit(RETURN_RECEIPT, line), 4)

    def test_unknown_type(self):
        for document_type in ("receipt", "DISPATCH", "", None):
            with self.subTest(document_type=document_type):
                with self.assertRaises(ValueError):
                    line_limit(document_type, totals())


class ParseReturnQuantitiesTest(unittest.TestCase):
    def test_accepts(self):
        cases = [(0, 1), (1, 0), (2, 3), (QUANTITY_MAX, 0), (0, QUANTITY_MAX)]
        cases.append((QUANTITY_MAX, QUANTITY_MAX))
        for sellable, unsellable in cases:
            with self.subTest(sellable=sellable, unsellable=unsellable):
                self.assertEqual(
                    parse_return_quantities(sellable, unsellable), (sellable, unsellable)
                )

    def test_rejects(self):
        cases = [
            (0, 0),
            (-1, 2),
            (2, -1),
            (QUANTITY_MAX + 1, 0),
            (0, QUANTITY_MAX + 1),
            (1.0, 0),
            (0, 1.5),
            (True, 0),
            (0, True),
            (False, 1),
            ("1", 0),
            (0, "1"),
            (None, 1),
        ]
        for sellable, unsellable in cases:
            with self.subTest(sellable=sellable, unsellable=unsellable):
                with self.assertRaises(ValueError):
                    parse_return_quantities(sellable, unsellable)


class RequestedQuantityTest(unittest.TestCase):
    def test_quantity_lines(self):
        for document_type in (DISPATCH, REFUND_RESTOCK):
            with self.subTest(document_type=document_type):
                self.assertEqual(requested_quantity(document_type, QuantityLine("L1", 4)), 4)

    def test_return_line_is_sum(self):
        self.assertEqual(requested_quantity(RETURN_RECEIPT, ReturnLine("L1", 2, 3)), 5)
        self.assertEqual(requested_quantity(RETURN_RECEIPT, ReturnLine("L1", 0, 3)), 3)

    def test_invalid_quantity(self):
        for quantity in (0, -1, QUANTITY_MAX + 1, True, 1.0, "1"):
            with self.subTest(quantity=quantity):
                with self.assertRaises(ValueError):
                    requested_quantity(DISPATCH, QuantityLine("L1", quantity))

    def test_invalid_return_quantities(self):
        with self.assertRaises(ValueError):
            requested_quantity(RETURN_RECEIPT, ReturnLine("L1", 0, 0))

    def test_line_shape_must_match_type(self):
        with self.assertRaises(ValueError):
            requested_quantity(DISPATCH, ReturnLine("L1", 1, 0))
        with self.assertRaises(ValueError):
            requested_quantity(RETURN_RECEIPT, QuantityLine("L1", 1))

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            requested_quantity("receipt", QuantityLine("L1", 1))


class UnknownLineRefsTest(unittest.TestCase):
    def test_all_on_order(self):
        self.assertEqual(unknown_line_refs(["L2", "L1"], ["L1", "L2", "L3"]), [])

    def test_extra_refs_ascending(self):
        self.assertEqual(
            unknown_line_refs(["L9", "L1", "L3", "L0"], ["L1", "L2"]), ["L0", "L3", "L9"]
        )


class ExceedingLinesTest(unittest.TestCase):
    ORDER = [
        totals("L1", "sku_a", committed=10, dispatched=4),
        totals("L2", "sku_b", committed=5, refund_restocked=5),
        totals("L3", "sku_c", committed=6, dispatched=2, refund_restocked=1),
    ]

    def test_none_exceeding(self):
        request = [QuantityLine("L3", 3), QuantityLine("L1", 6)]
        self.assertEqual(exceeding_lines(DISPATCH, request, self.ORDER), [])

    def test_exceeding_in_request_order(self):
        request = [
            QuantityLine("L3", 4),
            QuantityLine("L1", 6),
            QuantityLine("L2", 1),
        ]
        self.assertEqual(
            exceeding_lines(REFUND_RESTOCK, request, self.ORDER),
            [
                {"line_ref": "L3", "sku_id": "sku_c", "quantity": 4, "limit": 3},
                {"line_ref": "L2", "sku_id": "sku_b", "quantity": 1, "limit": 0},
            ],
        )

    def test_exactly_limit_not_listed(self):
        request = [QuantityLine("L1", 6), QuantityLine("L3", 3)]
        self.assertEqual(exceeding_lines(DISPATCH, request, self.ORDER), [])

    def test_return_compares_sum(self):
        order = [
            totals("L1", "sku_a", dispatched=5, returned_sellable=1),
            totals("L2", "sku_b", dispatched=3),
        ]
        request = [ReturnLine("L2", 2, 2), ReturnLine("L1", 2, 2)]
        self.assertEqual(
            exceeding_lines(RETURN_RECEIPT, request, order),
            [{"line_ref": "L2", "sku_id": "sku_b", "quantity": 4, "limit": 3}],
        )

    def test_return_uses_order_sku(self):
        order = [totals("L1", "sku_canonical", dispatched=1)]
        result = exceeding_lines(RETURN_RECEIPT, [ReturnLine("L1", 1, 1)], order)
        self.assertEqual(result[0]["sku_id"], "sku_canonical")

    def test_unknown_line_ref_raises(self):
        request = [QuantityLine("L1", 1), QuantityLine("L9", 1)]
        with self.assertRaises(ValueError):
            exceeding_lines(DISPATCH, request, self.ORDER)


class LineEntriesTest(unittest.TestCase):
    def test_dispatch(self):
        self.assertEqual(line_entries(DISPATCH, QuantityLine("L1", 3)), [(EntryType.DISPATCH, 3)])

    def test_refund_restock(self):
        self.assertEqual(
            line_entries(REFUND_RESTOCK, QuantityLine("L1", 2)), [(EntryType.REFUND_RESTOCK, 2)]
        )

    def test_return_sellable_only(self):
        self.assertEqual(
            line_entries(RETURN_RECEIPT, ReturnLine("L1", 2, 0)),
            [(EntryType.RETURN_SELLABLE, 2)],
        )

    def test_return_unsellable_only(self):
        self.assertEqual(
            line_entries(RETURN_RECEIPT, ReturnLine("L1", 0, 3)),
            [(EntryType.RETURN_UNSELLABLE, 3)],
        )

    def test_return_both_sellable_first(self):
        self.assertEqual(
            line_entries(RETURN_RECEIPT, ReturnLine("L1", 2, 3)),
            [(EntryType.RETURN_SELLABLE, 2), (EntryType.RETURN_UNSELLABLE, 3)],
        )

    def test_invalid_line_raises(self):
        with self.assertRaises(ValueError):
            line_entries(DISPATCH, QuantityLine("L1", 0))
        with self.assertRaises(ValueError):
            line_entries(RETURN_RECEIPT, ReturnLine("L1", 0, 0))
        with self.assertRaises(ValueError):
            line_entries("receipt", QuantityLine("L1", 1))


class LedgerEffectTest(unittest.TestCase):
    """每种流水对四个账面量的增量与 §5.8 写的一致。"""

    BEFORE = Quantities(stock_balance=20, reserved=3, unavailable=2, paid_pending_dispatch=8)

    def _deltas(self, document_type, line):
        current = self.BEFORE
        for entry_type, quantity in line_entries(document_type, line):
            current = apply_entry(entry_type, current, quantity)
        return (
            current.stock_balance - self.BEFORE.stock_balance,
            current.reserved - self.BEFORE.reserved,
            current.unavailable - self.BEFORE.unavailable,
            current.paid_pending_dispatch - self.BEFORE.paid_pending_dispatch,
        )

    def test_dispatch_reduces_paid_pending_dispatch(self):
        self.assertEqual(self._deltas(DISPATCH, QuantityLine("L1", 3)), (0, 0, 0, -3))

    def test_refund_restock_moves_back_to_stock(self):
        self.assertEqual(self._deltas(REFUND_RESTOCK, QuantityLine("L1", 3)), (3, 0, 0, -3))

    def test_return_sellable_adds_stock(self):
        self.assertEqual(self._deltas(RETURN_RECEIPT, ReturnLine("L1", 4, 0)), (4, 0, 0, 0))

    def test_return_unsellable_adds_stock_and_unavailable(self):
        self.assertEqual(self._deltas(RETURN_RECEIPT, ReturnLine("L1", 0, 4)), (4, 0, 4, 0))

    def test_return_both(self):
        self.assertEqual(self._deltas(RETURN_RECEIPT, ReturnLine("L1", 2, 3)), (5, 0, 3, 0))


class ApplyLineTest(unittest.TestCase):
    def test_dispatch_adds_dispatched(self):
        before = totals(dispatched=2, refund_restocked=1)
        after = apply_line(DISPATCH, before, QuantityLine("L1", 3))
        self.assertEqual(after, totals(dispatched=5, refund_restocked=1))
        self.assertEqual(before, totals(dispatched=2, refund_restocked=1))

    def test_refund_restock_adds_refund_restocked(self):
        after = apply_line(REFUND_RESTOCK, totals(dispatched=2), QuantityLine("L1", 8))
        self.assertEqual(after, totals(dispatched=2, refund_restocked=8))

    def test_return_adds_both(self):
        before = totals(dispatched=6, returned_sellable=1)
        after = apply_line(RETURN_RECEIPT, before, ReturnLine("L1", 2, 3))
        self.assertEqual(after, totals(dispatched=6, returned_sellable=3, returned_unsellable=3))

    def test_return_unsellable_only(self):
        after = apply_line(RETURN_RECEIPT, totals(dispatched=6), ReturnLine("L1", 0, 2))
        self.assertEqual(after, totals(dispatched=6, returned_unsellable=2))

    def test_exceeding_raises(self):
        cases = [
            (DISPATCH, totals(dispatched=6, refund_restocked=3), QuantityLine("L1", 2)),
            (REFUND_RESTOCK, totals(dispatched=10), QuantityLine("L1", 1)),
            (DISPATCH, totals(committed=0), QuantityLine("L1", 1)),
            (RETURN_RECEIPT, totals(dispatched=4, returned_sellable=1), ReturnLine("L1", 2, 2)),
        ]
        for document_type, before, line in cases:
            with self.subTest(document_type=document_type, before=before, line=line):
                with self.assertRaises(ValueError):
                    apply_line(document_type, before, line)

    def test_exactly_limit_allowed(self):
        before = totals(dispatched=6, refund_restocked=3)
        after = apply_line(DISPATCH, before, QuantityLine("L1", 1))
        self.assertEqual(line_limit(DISPATCH, after), 0)

    def test_line_ref_mismatch_raises(self):
        with self.assertRaises(ValueError):
            apply_line(DISPATCH, totals(), QuantityLine("L2", 1))


class OrderStockCursorTest(unittest.TestCase):
    DIGEST = hashlib.sha256(RESERVATION_ID.encode("utf-8")).hexdigest()[:16]

    def test_format(self):
        self.assertEqual(format_order_stock_cursor(42, RESERVATION_ID), f"v1.42.{self.DIGEST}")

    def test_round_trip(self):
        for document_seq in (1, 42, CURSOR_MAX):
            with self.subTest(document_seq=document_seq):
                cursor = format_order_stock_cursor(document_seq, RESERVATION_ID)
                parsed_seq, digest = parse_order_stock_cursor(cursor)
                self.assertEqual(parsed_seq, document_seq)
                self.assertEqual(digest, self.DIGEST)
                self.assertTrue(cursor_matches(digest, RESERVATION_ID))
                self.assertFalse(cursor_matches(digest, OTHER_RESERVATION_ID))

    def test_parse_rejects(self):
        values = [
            f"v2.1.{self.DIGEST}",
            f"V1.1.{self.DIGEST}",
            f".1.{self.DIGEST}",
            f"v1.0.{self.DIGEST}",
            f"v1.01.{self.DIGEST}",
            f"v1.-1.{self.DIGEST}",
            f"v1.+1.{self.DIGEST}",
            f"v1.a.{self.DIGEST}",
            f"v1..{self.DIGEST}",
            f"v1.١.{self.DIGEST}",
            f"v1.{CURSOR_MAX + 1}.{self.DIGEST}",
            f"v1.{10**19}.{self.DIGEST}",
            f"v1.1.{self.DIGEST[:15]}",
            f"v1.1.{self.DIGEST}0",
            f"v1.1.{self.DIGEST.upper()}",
            "v1.1.000000000000000g",
            "v1.1.",
            f"v1.1.{self.DIGEST}.x",
            f"v1.1.2.{self.DIGEST}",
            f"1.{self.DIGEST}",
            "v1",
            "",
            None,
            1,
        ]
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_order_stock_cursor(value)

    def test_format_rejects(self):
        for document_seq in (0, -1, CURSOR_MAX + 1, True, 1.0, "1"):
            with self.subTest(document_seq=document_seq):
                with self.assertRaises(ValueError):
                    format_order_stock_cursor(document_seq, RESERVATION_ID)


if __name__ == "__main__":
    unittest.main()
