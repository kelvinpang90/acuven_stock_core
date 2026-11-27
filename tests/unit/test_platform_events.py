import ast
import dataclasses
import hashlib
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.domain import platform_events
from app.domain.platform_events import (
    CANCELLED,
    COMMITTED,
    CREATED,
    HELD,
    READY_TO_SHIP,
    REFUND,
    RETURN,
    RETURN_RECEIVED,
    RETURN_REQUESTED,
    SHIPMENT,
    SHIPPED,
    UNPAID,
    CancelledLine,
    CancelledPayload,
    Classification,
    CoreLine,
    CreatedLine,
    CreatedPayload,
    Outcome,
    ReturnLine,
    ReturnReceivedPayload,
    ShippedLine,
    ShippedPayload,
    StatusPayload,
    assign_line_refs,
    classify,
    dispatch_body,
    document_ref,
    manual_document_ref,
    order_lock_name,
    order_ref,
    parse_payload,
    payment_ref,
    pull_backoff,
    refund_restock_body,
    reservation_body,
    return_receipt_body,
    sale_commit_body,
    sort_key,
)
from app.domain.stock import parse_occurred_at

ORDER_REF = "shp:123456:2611210ABCD"
RESERVATION_ID = "rsv_01JD0000000000000000000000"


def hashed(value):
    return "h" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def at(minute, second=0):
    return datetime(2026, 11, 21, 10, minute, second)


class ImportsTest(unittest.TestCase):
    def test_module_has_no_io_imports(self):
        source = Path(platform_events.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        stdlib = {"collections.abc", "dataclasses", "datetime", "enum", "hashlib", "re", "typing"}
        others = {name for name in imported if name not in stdlib}
        self.assertTrue(all(name.startswith("app.domain") for name in others), others)
        self.assertFalse(any(name.startswith("app.api") for name in imported))


class OrderRefTest(unittest.TestCase):
    def test_contract_example(self):
        ref = order_ref("shopee", "123456", "2611210ABCD")
        self.assertEqual(ref, ORDER_REF)
        self.assertEqual(payment_ref(ref), "shp:123456:2611210ABCD:c")
        self.assertEqual(document_ref(ref, SHIPMENT, "PKG1"), "shp:123456:2611210ABCD:d:PKG1")
        self.assertEqual(document_ref(ref, REFUND, "RF1"), "shp:123456:2611210ABCD:r:RF1")
        self.assertEqual(document_ref(ref, RETURN, "RT1"), "shp:123456:2611210ABCD:x:RT1")

    def test_platform_prefixes(self):
        self.assertEqual(order_ref("tiktok_shop", "7", "A.b_c-1"), "tts:7:A.b_c-1")
        with self.assertRaises(ValueError):
            order_ref("lazada", "7", "A")

    def test_invalid_segments_are_hashed(self):
        for value in ("a b", "店铺", "a/b", "", "a:b", "x\n"):
            with self.subTest(value=value):
                self.assertEqual(order_ref("shopee", value, "A"), f"shp:{hashed(value)}:A")
                self.assertEqual(order_ref("shopee", "S", value), f"shp:S:{hashed(value)}")
                self.assertEqual(
                    document_ref("shp:S:A", SHIPMENT, value), f"shp:S:A:d:{hashed(value)}"
                )
                self.assertEqual(order_ref("shopee", "S", value), order_ref("shopee", "S", value))
        self.assertRegex(hashed("店铺"), r"^h[0-9a-f]{32}$")

    def test_long_segment_is_hashed(self):
        self.assertEqual(order_ref("shopee", "S", "A" * 64), "shp:S:" + "A" * 64)
        self.assertEqual(order_ref("shopee", "S", "A" * 65), f"shp:S:{hashed('A' * 65)}")
        self.assertEqual(document_ref("o", REFUND, "B" * 65), f"o:r:{hashed('B' * 65)}")

    def test_hash_shaped_segment_is_hashed(self):
        lookalike = hashed("a b")
        self.assertEqual(order_ref("shopee", lookalike, "A"), f"shp:{hashed(lookalike)}:A")
        self.assertEqual(order_ref("shopee", "S", lookalike), f"shp:S:{hashed(lookalike)}")
        self.assertNotEqual(order_ref("shopee", "S", lookalike), order_ref("shopee", "S", "a b"))
        self.assertEqual(
            document_ref(ORDER_REF, SHIPMENT, lookalike), f"{ORDER_REF}:d:{hashed(lookalike)}"
        )

    def test_manual_shaped_document_id_is_hashed(self):
        for kind in (SHIPMENT, REFUND, RETURN):
            with self.subTest(kind=kind):
                self.assertEqual(
                    document_ref(ORDER_REF, kind, "m1"), f"{ORDER_REF}:{kind}:{hashed('m1')}"
                )
                self.assertNotEqual(
                    document_ref(ORDER_REF, kind, "m1"), manual_document_ref(ORDER_REF, kind, 1)
                )
        self.assertEqual(document_ref(ORDER_REF, SHIPMENT, "m1a"), f"{ORDER_REF}:d:m1a")
        # m 加数字只对单据 ID 换。
        self.assertEqual(order_ref("shopee", "m1", "m2"), "shp:m1:m2")

    def test_colon_cannot_shift_segments(self):
        left = document_ref(order_ref("shopee", "S", "A:d:B"), SHIPMENT, "X")
        right = document_ref(order_ref("shopee", "S", "A"), SHIPMENT, "B:d:X")
        self.assertNotEqual(left, right)
        self.assertNotEqual(
            document_ref("shp:S:A:d:B", SHIPMENT, "X"),
            document_ref("shp:S:A", SHIPMENT, "B:d:X"),
        )

    def test_length_limits(self):
        shop = "S" * 64
        long_order = "A" * 200
        ref = order_ref("shopee", shop, long_order)
        self.assertLessEqual(len(ref), 112)
        self.assertEqual(ref, f"shp:{shop}:{hashed(long_order)}")
        document = document_ref(ref, SHIPMENT, "P" * 64)
        self.assertLessEqual(len(document), 128)
        self.assertEqual(document, f"{hashed(ref)}:d:{'P' * 64}")
        self.assertEqual(document, document_ref(ref, SHIPMENT, "P" * 64))

    def test_valid_but_too_long_order_ref_hashes_order_id(self):
        shop = "S" * 64
        order = "A" * 64
        ref = order_ref("tiktok_shop", shop, order)
        self.assertEqual(ref, f"tts:{shop}:{hashed(order)}")
        self.assertLessEqual(len(ref), 112)

    def test_short_document_ref_keeps_order_ref(self):
        ref = order_ref("shopee", "S" * 20, "A" * 20)
        document = document_ref(ref, RETURN, "R" * 64)
        self.assertLessEqual(len(document), 128)
        self.assertTrue(document.startswith(ref + ":x:"))

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            document_ref(ORDER_REF, "c", "X")
        with self.assertRaises(ValueError):
            manual_document_ref(ORDER_REF, "z", 1)

    def test_manual_document_ref(self):
        self.assertEqual(manual_document_ref(ORDER_REF, SHIPMENT, 1), f"{ORDER_REF}:d:m1")
        self.assertEqual(manual_document_ref(ORDER_REF, REFUND, 2), f"{ORDER_REF}:r:m2")
        self.assertEqual(manual_document_ref(ORDER_REF, RETURN, 1), f"{ORDER_REF}:x:m1")
        for n in (0, -1, True, 1.0, "1"):
            with self.subTest(n=n), self.assertRaises(ValueError):
                manual_document_ref(ORDER_REF, SHIPMENT, n)
        long_ref = order_ref("shopee", "S" * 64, "A" * 200)
        manual = manual_document_ref(long_ref, SHIPMENT, 1)
        self.assertLessEqual(len(manual), 128)


class OrderLockNameTest(unittest.TestCase):
    PARTS = ("stock_core", "ten_1", "chn_1", "2611210ABCD")

    def test_matches_independent_hash(self):
        expected = hashlib.sha256(
            b"stock_core\nplatform_order\nten_1\nchn_1\n2611210ABCD"
        ).hexdigest()
        self.assertEqual(order_lock_name(*self.PARTS), expected)
        self.assertRegex(order_lock_name(*self.PARTS), r"^[0-9a-f]{64}$")

    def test_any_part_changes_the_name(self):
        base = order_lock_name(*self.PARTS)
        for index in range(len(self.PARTS)):
            parts = list(self.PARTS)
            parts[index] += "x"
            with self.subTest(index=index):
                self.assertNotEqual(order_lock_name(*parts), base)

    def test_literal_is_part_of_the_name(self):
        plain = hashlib.sha256("\n".join(self.PARTS).encode()).hexdigest()
        self.assertNotEqual(order_lock_name(*self.PARTS), plain)

    def test_rejects_empty_or_newline(self):
        for index in range(len(self.PARTS)):
            for bad in ("", "a\nb", None):
                parts = list(self.PARTS)
                parts[index] = bad
                with self.subTest(index=index, bad=bad), self.assertRaises(ValueError):
                    order_lock_name(*parts)


def created_line(line_id="1", quantity=2, **extra):
    line = {
        "platform_line_id": line_id,
        "platform_item_id": "item",
        "platform_model_id": "model",
        "quantity": quantity,
    }
    line.update(extra)
    return line


class PlatformTimeTest(unittest.TestCase):
    def test_valid(self):
        cases = [
            ("2026-11-21T10:00:00Z", datetime(2026, 11, 21, 10)),
            ("2026-11-21T10:00:00.5Z", datetime(2026, 11, 21, 10, 0, 0, 500000)),
            ("2026-11-21T10:00:00.123456Z", datetime(2026, 11, 21, 10, 0, 0, 123456)),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                parsed = parse_payload(READY_TO_SHIP, {"platform_time": text})
                self.assertEqual(parsed.platform_time, expected)
                self.assertIsNone(parsed.platform_time.tzinfo)

    def test_invalid(self):
        for text in (
            "2026-11-21T10:00:00",
            "2026-11-21T10:00:00+00:00",
            "2026-11-21T10:00:00z",
            "2026-11-21t10:00:00Z",
            "2026-11-21T10:00:00.Z",
            "2026-11-21T10:00:00.1234567Z",
            "2026-11-21T10:00Z",
            "2026-02-30T10:00:00Z",
            "2026-11-21T24:00:00Z",
            "0999-11-21T10:00:00Z",
            "２026-11-21T10:00:00Z",
            None,
            1,
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_payload(READY_TO_SHIP, {"platform_time": text})


class CreatedPayloadTest(unittest.TestCase):
    def test_valid(self):
        parsed = parse_payload(
            CREATED,
            {
                "platform_time": "2026-11-21T10:00:00Z",
                "lines": [created_line(), created_line("2")],
            },
        )
        expected_lines = (
            CreatedLine("1", "item", "model", 2),
            CreatedLine("2", "item", "model", 2),
        )
        self.assertEqual(
            parsed,
            CreatedPayload(platform_time=datetime(2026, 11, 21, 10), lines=expected_lines),
        )
        self.assertIsInstance(parsed.lines, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            parsed.lines = ()

    def test_no_platform_time(self):
        self.assertIsNone(parse_payload(CREATED, {"lines": [created_line()]}).platform_time)

    def test_ids_have_no_charset_check(self):
        line = created_line("行/1", platform_item_id="a b", platform_model_id="x" * 128)
        parsed = parse_payload(CREATED, {"lines": [line]})
        self.assertEqual(parsed.lines[0].platform_line_id, "行/1")

    def test_up_to_1000_lines(self):
        lines = [created_line(str(i)) for i in range(1000)]
        self.assertEqual(len(parse_payload(CREATED, {"lines": lines}).lines), 1000)
        with self.assertRaises(ValueError):
            parse_payload(CREATED, {"lines": lines + [created_line("x")]})

    def test_invalid(self):
        line = created_line()
        missing_model = {k: v for k, v in line.items() if k != "platform_model_id"}
        cases = {
            "missing lines": {},
            "empty lines": {"lines": []},
            "lines not a list": {"lines": line},
            "unknown key": {"lines": [line], "buyer": "x"},
            "unknown line key": {"lines": [created_line(price=1)]},
            "missing line key": {"lines": [missing_model]},
            "quantity 0": {"lines": [created_line(quantity=0)]},
            "quantity 1000001": {"lines": [created_line(quantity=1000001)]},
            "bool quantity": {"lines": [created_line(quantity=True)]},
            "string quantity": {"lines": [created_line(quantity="2")]},
            "float quantity": {"lines": [created_line(quantity=2.0)]},
            "duplicate line": {"lines": [created_line(), created_line()]},
            "empty id": {"lines": [created_line("")]},
            "long id": {"lines": [created_line("x" * 129)]},
            "control char": {"lines": [created_line("a\tb")]},
            "time without Z": {"lines": [line], "platform_time": "2026-11-21T10:00:00"},
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                parse_payload(CREATED, payload)

    def test_not_an_object(self):
        for payload in (None, [], "x"):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_payload(CREATED, payload)

    def test_unknown_action(self):
        with self.assertRaises(ValueError):
            parse_payload("refunded", {})


class StatusPayloadTest(unittest.TestCase):
    def test_valid(self):
        for action in (READY_TO_SHIP, RETURN_REQUESTED):
            with self.subTest(action=action):
                self.assertEqual(parse_payload(action, {}), StatusPayload(None))
                self.assertEqual(
                    parse_payload(action, {"platform_time": "2026-11-21T10:00:00Z"}),
                    StatusPayload(datetime(2026, 11, 21, 10)),
                )

    def test_invalid(self):
        for action in (READY_TO_SHIP, RETURN_REQUESTED):
            for payload in (
                {"lines": [created_line()]},
                {"status": "READY"},
                {"platform_time": "2026-11-21T10:00:00"},
                None,
            ):
                with self.subTest(action=action, payload=payload), self.assertRaises(ValueError):
                    parse_payload(action, payload)


class CancelledPayloadTest(unittest.TestCase):
    def test_order_level(self):
        self.assertEqual(parse_payload(CANCELLED, {}), CancelledPayload(None, None, None))

    def test_order_level_with_refund(self):
        self.assertEqual(
            parse_payload(CANCELLED, {"refund_id": "RF1"}), CancelledPayload(None, "RF1", None)
        )

    def test_lines_without_refund(self):
        parsed = parse_payload(
            CANCELLED,
            {
                "platform_time": "2026-11-21T10:00:00Z",
                "lines": [
                    {"platform_line_id": "1", "cancelled_total": 0},
                    {"platform_line_id": "2", "cancelled_total": 1000000},
                ],
            },
        )
        self.assertEqual(
            parsed.lines, (CancelledLine("1", 0, None), CancelledLine("2", 1000000, None))
        )
        self.assertIsNone(parsed.refund_id)

    def test_lines_with_refund(self):
        parsed = parse_payload(
            CANCELLED,
            {
                "refund_id": "RF1",
                "lines": [{"platform_line_id": "1", "cancelled_total": 3, "quantity": 1}],
            },
        )
        self.assertEqual(parsed, CancelledPayload(None, "RF1", (CancelledLine("1", 3, 1),)))

    def test_invalid(self):
        line = {"platform_line_id": "1", "cancelled_total": 1}
        refund_line = {**line, "quantity": 1}
        cases = {
            "empty lines": {"lines": []},
            "unknown key": {"reason": "buyer"},
            "unknown line key": {"lines": [{**line, "sku": "x"}]},
            "missing cancelled_total": {"lines": [{"platform_line_id": "1"}]},
            "quantity without refund": {"lines": [refund_line]},
            "refund without quantity": {"refund_id": "RF1", "lines": [line]},
            "cancelled_total negative": {"lines": [{**line, "cancelled_total": -1}]},
            "cancelled_total 1000001": {"lines": [{**line, "cancelled_total": 1000001}]},
            "bool cancelled_total": {"lines": [{**line, "cancelled_total": False}]},
            "quantity 0": {"refund_id": "RF1", "lines": [{**refund_line, "quantity": 0}]},
            "quantity 1000001": {
                "refund_id": "RF1",
                "lines": [{**refund_line, "quantity": 1000001}],
            },
            "bool quantity": {
                "refund_id": "RF1",
                "lines": [{**refund_line, "quantity": True}],
            },
            "duplicate line": {"lines": [line, line]},
            "101 lines": {
                "lines": [{"platform_line_id": str(i), "cancelled_total": 1} for i in range(101)]
            },
            "empty refund_id": {"refund_id": ""},
            "null refund_id": {"refund_id": None},
            "time without Z": {"platform_time": "2026-11-21T10:00:00"},
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                parse_payload(CANCELLED, payload)

    def test_100_lines(self):
        lines = [{"platform_line_id": str(i), "cancelled_total": 1} for i in range(100)]
        self.assertEqual(len(parse_payload(CANCELLED, {"lines": lines}).lines), 100)


class ShippedPayloadTest(unittest.TestCase):
    def test_valid(self):
        parsed = parse_payload(
            SHIPPED,
            {
                "package_id": "PKG1",
                "platform_time": "2026-11-21T10:00:00Z",
                "lines": [{"platform_line_id": "1", "quantity": 1000000}],
            },
        )
        self.assertEqual(
            parsed,
            ShippedPayload(datetime(2026, 11, 21, 10), "PKG1", (ShippedLine("1", 1000000),)),
        )

    def test_without_package_id(self):
        parsed = parse_payload(SHIPPED, {"lines": [{"platform_line_id": "1", "quantity": 1}]})
        self.assertIsNone(parsed.package_id)

    def test_invalid(self):
        line = {"platform_line_id": "1", "quantity": 1}
        cases = {
            "missing lines": {"package_id": "PKG1"},
            "empty lines": {"lines": []},
            "unknown key": {"lines": [line], "tracking_no": "T"},
            "missing quantity": {"lines": [{"platform_line_id": "1"}]},
            "quantity 0": {"lines": [{**line, "quantity": 0}]},
            "quantity 1000001": {"lines": [{**line, "quantity": 1000001}]},
            "bool quantity": {"lines": [{**line, "quantity": True}]},
            "duplicate line": {"lines": [line, line]},
            "101 lines": {
                "lines": [{"platform_line_id": str(i), "quantity": 1} for i in range(101)]
            },
            "long package_id": {"lines": [line], "package_id": "P" * 129},
            "time without Z": {"lines": [line], "platform_time": "2026-11-21T10:00:00"},
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                parse_payload(SHIPPED, payload)


class ReturnReceivedPayloadTest(unittest.TestCase):
    def test_valid(self):
        parsed = parse_payload(
            RETURN_RECEIVED,
            {
                "return_id": "RT1",
                "lines": [
                    {"platform_line_id": "1", "sellable_quantity": 1, "unsellable_quantity": 0},
                    {"platform_line_id": "2", "sellable_quantity": 0, "unsellable_quantity": 2},
                ],
            },
        )
        self.assertEqual(
            parsed,
            ReturnReceivedPayload(None, "RT1", (ReturnLine("1", 1, 0), ReturnLine("2", 0, 2))),
        )

    def test_invalid(self):
        line = {"platform_line_id": "1", "sellable_quantity": 1, "unsellable_quantity": 0}
        cases = {
            "missing lines": {"return_id": "RT1"},
            "empty lines": {"lines": []},
            "unknown key": {"lines": [line], "reason": "x"},
            "missing unsellable": {"lines": [{"platform_line_id": "1", "sellable_quantity": 1}]},
            "both 0": {"lines": [{**line, "sellable_quantity": 0}]},
            "negative": {"lines": [{**line, "unsellable_quantity": -1}]},
            "sellable 1000001": {"lines": [{**line, "sellable_quantity": 1000001}]},
            "bool sellable": {"lines": [{**line, "sellable_quantity": True}]},
            "duplicate line": {"lines": [line, line]},
            "101 lines": {"lines": [{**line, "platform_line_id": str(i)} for i in range(101)]},
            "control char return_id": {"lines": [line], "return_id": "a\x00"},
            "time without Z": {"lines": [line], "platform_time": "2026-11-21 10:00:00Z"},
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                parse_payload(RETURN_RECEIVED, payload)


class SortKeyTest(unittest.TestCase):
    def ordered(self, events):
        return [event[3] for event in sorted(events, key=lambda event: sort_key(*event))]

    def test_platform_time_wins_over_chain(self):
        # 先付款前取消一行、后进入待发货：按平台时间保持这个顺序。
        events = [
            (at(5), READY_TO_SHIP, at(1), "pev_b"),
            (at(3), CANCELLED, at(2), "pev_a"),
        ]
        self.assertEqual(self.ordered(events), ["pev_a", "pev_b"])

    def test_same_time_by_chain(self):
        events = [
            (at(5), RETURN_RECEIVED, at(1), "pev_1"),
            (at(5), RETURN_REQUESTED, at(1), "pev_2"),
            (at(5), SHIPPED, at(1), "pev_3"),
            (at(5), READY_TO_SHIP, at(1), "pev_4"),
            (at(5), CREATED, at(1), "pev_5"),
        ]
        self.assertEqual(self.ordered(events), ["pev_5", "pev_4", "pev_3", "pev_2", "pev_1"])

    def test_shipped_and_cancelled_tie_then_received_at_then_id(self):
        events = [
            (at(5), SHIPPED, at(2), "pev_a"),
            (at(5), CANCELLED, at(1), "pev_b"),
            (at(5), CANCELLED, at(2), "pev_d"),
            (at(5), SHIPPED, at(2), "pev_c"),
        ]
        self.assertEqual(self.ordered(events), ["pev_b", "pev_a", "pev_c", "pev_d"])

    def test_without_platform_time_uses_received_at(self):
        events = [
            (None, CREATED, at(9), "pev_a"),
            (at(5), SHIPPED, at(1), "pev_b"),
            (None, READY_TO_SHIP, at(5), "pev_c"),
        ]
        self.assertEqual(self.ordered(events), ["pev_c", "pev_b", "pev_a"])

    def test_invalid(self):
        with self.assertRaises(ValueError):
            sort_key(at(1), "refunded", at(1), "pev_a")
        with self.assertRaises(ValueError):
            sort_key(datetime(2026, 1, 1, tzinfo=UTC), CREATED, at(1), "pev_a")
        with self.assertRaises(ValueError):
            sort_key(None, CREATED, None, "pev_a")


APPLY = Classification(Outcome.APPLY)
IGNORED = Classification(Outcome.IGNORED)
BLOCKED = Classification(Outcome.BLOCKED)
ATTENTION = Classification(Outcome.ATTENTION)
NEED_CREATED = Classification(Outcome.MISSING_PRIOR, CREATED)
NEED_READY = Classification(Outcome.MISSING_PRIOR, READY_TO_SHIP)

ACTION_COLUMNS = (CREATED, READY_TO_SHIP, CANCELLED, SHIPPED, RETURN_REQUESTED, RETURN_RECEIVED)
# (阶段, ready_at 记过没有) → 按 ACTION_COLUMNS 的结果。
CLASSIFY_TABLE = {
    (None, False): (APPLY, NEED_CREATED, NEED_CREATED, NEED_CREATED, IGNORED, NEED_CREATED),
    (UNPAID, False): (IGNORED, APPLY, APPLY, NEED_READY, IGNORED, NEED_READY),
    (HELD, False): (IGNORED, APPLY, APPLY, NEED_READY, IGNORED, NEED_READY),
    (HELD, True): (IGNORED, IGNORED, APPLY, BLOCKED, IGNORED, BLOCKED),
    (COMMITTED, True): (IGNORED, IGNORED, APPLY, APPLY, IGNORED, APPLY),
    (CANCELLED, False): (IGNORED, ATTENTION, IGNORED, ATTENTION, IGNORED, ATTENTION),
}


class ClassifyTest(unittest.TestCase):
    def test_table(self):
        for (stage, ready_at_set), row in CLASSIFY_TABLE.items():
            for action, expected in zip(ACTION_COLUMNS, row, strict=True):
                with self.subTest(stage=stage, ready_at_set=ready_at_set, action=action):
                    self.assertEqual(classify(stage, action, ready_at_set), expected)

    def test_ready_at_only_matters_when_held(self):
        for stage in (None, UNPAID, COMMITTED, CANCELLED):
            for action in ACTION_COLUMNS:
                with self.subTest(stage=stage, action=action):
                    with_ready_at = classify(stage, action, True)
                    self.assertEqual(with_ready_at, classify(stage, action, False))

    def test_missing_only_on_missing_prior(self):
        self.assertIsNone(APPLY.missing)
        self.assertEqual(classify(UNPAID, SHIPPED, False).missing, READY_TO_SHIP)
        self.assertEqual(classify(None, CANCELLED, False).missing, CREATED)

    def test_unknown(self):
        with self.assertRaises(ValueError):
            classify("paid", CREATED, False)
        with self.assertRaises(ValueError):
            classify(UNPAID, "refunded", False)


class AssignLineRefsTest(unittest.TestCase):
    def test_merges_by_sku_and_numbers_by_sku(self):
        assignment = assign_line_refs(
            [("p1", "sku_b", 2), ("p2", "sku_a", 1), ("p3", "sku_b", 3), ("p4", None, 0)]
        )
        self.assertEqual(assignment.lines, (CoreLine("L1", "sku_a", 1), CoreLine("L2", "sku_b", 5)))
        self.assertEqual(assignment.line_refs, {"p1": "L2", "p2": "L1", "p3": "L2"})

    def test_zero_remaining_not_listed(self):
        assignment = assign_line_refs([("p1", "sku_a", 0), ("p2", "sku_b", 1)])
        self.assertEqual(assignment.lines, (CoreLine("L1", "sku_b", 1),))
        self.assertEqual(assignment.line_refs, {"p2": "L1"})

    def test_all_zero(self):
        assignment = assign_line_refs([("p1", "sku_a", 0)])
        self.assertEqual(assignment.lines, ())
        self.assertEqual(assignment.line_refs, {})

    def test_order_independent(self):
        lines = [("p1", "sku_b", 2), ("p2", "sku_a", 1), ("p3", "sku_c", 3)]
        self.assertEqual(assign_line_refs(lines), assign_line_refs(list(reversed(lines))))

    def test_invalid(self):
        for lines in (
            [("p1", None, 1)],
            [("p1", "sku_a", -1)],
            [("p1", "sku_a", True)],
            [("p1", "sku_a", 1), ("p1", "sku_b", 1)],
        ):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                assign_line_refs(lines)


LINE_REFS = {"p1": "L1", "p2": "L2", "p3": "L1"}


def shipped(*lines, package_id="PKG1"):
    return parse_payload(
        SHIPPED,
        {
            "package_id": package_id,
            "lines": [{"platform_line_id": i, "quantity": q} for i, q in lines],
        },
    )


def refunded(*lines):
    return parse_payload(
        CANCELLED,
        {
            "refund_id": "RF1",
            "lines": [
                {"platform_line_id": i, "cancelled_total": q, "quantity": q} for i, q in lines
            ],
        },
    )


def returned(*lines):
    return parse_payload(
        RETURN_RECEIVED,
        {
            "return_id": "RT1",
            "lines": [
                {"platform_line_id": i, "sellable_quantity": s, "unsellable_quantity": u}
                for i, s, u in lines
            ],
        },
    )


class BodyTest(unittest.TestCase):
    CORE_LINES = (CoreLine("L2", "sku_b", 5), CoreLine("L1", "sku_a", 1))

    def assert_no_forbidden_keys(self, body):
        for key in ("reason_code", "note", "occurred_at"):
            self.assertNotIn(key, body)

    def test_reservation_body(self):
        path, body = reservation_body(ORDER_REF, self.CORE_LINES)
        self.assertEqual(path, {})
        self.assertEqual(set(body), {"order_ref", "lines"})
        self.assertEqual(
            body,
            {
                "order_ref": ORDER_REF,
                "lines": [
                    {"line_ref": "L1", "sku_id": "sku_a", "quantity": 1},
                    {"line_ref": "L2", "sku_id": "sku_b", "quantity": 5},
                ],
            },
        )
        self.assert_no_forbidden_keys(body)

    def test_reservation_body_drops_zero_and_none_when_empty(self):
        lines = [CoreLine("L1", "sku_a", 0), CoreLine("L2", "b", 1)]
        _, body = reservation_body(ORDER_REF, lines)
        self.assertEqual(body["lines"], [{"line_ref": "L2", "sku_id": "b", "quantity": 1}])
        self.assertIsNone(reservation_body(ORDER_REF, [CoreLine("L1", "sku_a", 0)]))
        self.assertIsNone(reservation_body(ORDER_REF, []))

    def test_reservation_body_from_assignment(self):
        assignment = assign_line_refs([("p1", "sku_b", 2), ("p2", "sku_a", 1), ("p3", "sku_b", 3)])
        _, body = reservation_body(ORDER_REF, assignment.lines)
        self.assertEqual(
            body["lines"],
            [
                {"line_ref": "L1", "sku_id": "sku_a", "quantity": 1},
                {"line_ref": "L2", "sku_id": "sku_b", "quantity": 5},
            ],
        )

    def test_sale_commit_body(self):
        ready_at = datetime(2026, 11, 21, 10, 5, 7)
        path, body = sale_commit_body(ORDER_REF, RESERVATION_ID, ready_at, self.CORE_LINES)
        self.assertEqual(path, {"order_ref": ORDER_REF})
        self.assertEqual(set(body), {"reservation_id", "payment_ref", "paid_at", "lines"})
        self.assertEqual(body["reservation_id"], RESERVATION_ID)
        self.assertEqual(body["payment_ref"], "shp:123456:2611210ABCD:c")
        self.assertEqual(body["paid_at"], "2026-11-21T10:05:07Z")
        self.assertEqual(parse_occurred_at(body["paid_at"]), ready_at)
        self.assertEqual(body["lines"], reservation_body(ORDER_REF, self.CORE_LINES)[1]["lines"])
        self.assert_no_forbidden_keys(body)

    def test_sale_commit_body_ready_at(self):
        for ready_at in (
            datetime(2026, 11, 21, 10, 5, 7, 1),
            datetime(2026, 11, 21, 10, 5, 7, tzinfo=UTC),
            "2026-11-21T10:05:07Z",
        ):
            with self.subTest(ready_at=ready_at), self.assertRaises(ValueError):
                sale_commit_body(ORDER_REF, RESERVATION_ID, ready_at, self.CORE_LINES)
        self.assertIsNone(
            sale_commit_body(ORDER_REF, RESERVATION_ID, at(1), [CoreLine("L1", "sku_a", 0)])
        )

    def test_dispatch_body(self):
        shipment_ref = document_ref(ORDER_REF, SHIPMENT, "PKG1")
        path, body = dispatch_body(
            ORDER_REF, shipment_ref, shipped(("p2", 1), ("p1", 2), ("p3", 3)), LINE_REFS
        )
        self.assertEqual(path, {"order_ref": ORDER_REF})
        self.assertEqual(set(body), {"shipment_ref", "lines"})
        self.assertEqual(
            body,
            {
                "shipment_ref": "shp:123456:2611210ABCD:d:PKG1",
                "lines": [{"line_ref": "L1", "quantity": 5}, {"line_ref": "L2", "quantity": 1}],
            },
        )
        self.assert_no_forbidden_keys(body)

    def test_dispatch_body_order_independent(self):
        ref = document_ref(ORDER_REF, SHIPMENT, "PKG1")
        first = dispatch_body(ORDER_REF, ref, shipped(("p1", 2), ("p2", 1), ("p3", 3)), LINE_REFS)
        second = dispatch_body(ORDER_REF, ref, shipped(("p3", 3), ("p2", 1), ("p1", 2)), LINE_REFS)
        self.assertEqual(first, second)

    def test_dispatch_body_unknown_platform_line(self):
        with self.assertRaises(ValueError):
            dispatch_body(ORDER_REF, "ref", shipped(("p1", 1), ("p9", 1)), LINE_REFS)

    def test_refund_restock_body(self):
        refund_ref = document_ref(ORDER_REF, REFUND, "RF1")
        payload = refunded(("p3", 1), ("p1", 2))
        path, body = refund_restock_body(ORDER_REF, refund_ref, payload, LINE_REFS)
        self.assertEqual(path, {"order_ref": ORDER_REF})
        self.assertEqual(set(body), {"refund_ref", "lines"})
        self.assertEqual(
            body,
            {
                "refund_ref": "shp:123456:2611210ABCD:r:RF1",
                "lines": [{"line_ref": "L1", "quantity": 3}],
            },
        )
        self.assert_no_forbidden_keys(body)

    def test_refund_restock_body_uses_this_refund_quantity(self):
        payload = parse_payload(
            CANCELLED,
            {
                "refund_id": "RF2",
                "lines": [{"platform_line_id": "p2", "cancelled_total": 5, "quantity": 2}],
            },
        )
        _, body = refund_restock_body(ORDER_REF, "ref", payload, LINE_REFS)
        self.assertEqual(body["lines"], [{"line_ref": "L2", "quantity": 2}])

    def test_refund_restock_body_needs_refund_and_lines(self):
        for payload in (
            parse_payload(CANCELLED, {"refund_id": "RF1"}),
            parse_payload(CANCELLED, {"lines": [{"platform_line_id": "p1", "cancelled_total": 1}]}),
            parse_payload(CANCELLED, {}),
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                refund_restock_body(ORDER_REF, "ref", payload, LINE_REFS)
        with self.assertRaises(ValueError):
            refund_restock_body(ORDER_REF, "ref", refunded(("p9", 1)), LINE_REFS)

    def test_return_receipt_body(self):
        return_ref = document_ref(ORDER_REF, RETURN, "RT1")
        path, body = return_receipt_body(
            ORDER_REF, return_ref, returned(("p3", 0, 2), ("p2", 1, 0), ("p1", 1, 1)), LINE_REFS
        )
        self.assertEqual(path, {"order_ref": ORDER_REF})
        self.assertEqual(set(body), {"return_ref", "lines"})
        self.assertEqual(
            body,
            {
                "return_ref": "shp:123456:2611210ABCD:x:RT1",
                "lines": [
                    {"line_ref": "L1", "sellable_quantity": 1, "unsellable_quantity": 3},
                    {"line_ref": "L2", "sellable_quantity": 1, "unsellable_quantity": 0},
                ],
            },
        )
        self.assert_no_forbidden_keys(body)

    def test_return_receipt_body_order_independent(self):
        first = return_receipt_body(
            ORDER_REF, "ref", returned(("p1", 1, 0), ("p2", 0, 1), ("p3", 2, 0)), LINE_REFS
        )
        second = return_receipt_body(
            ORDER_REF, "ref", returned(("p3", 2, 0), ("p2", 0, 1), ("p1", 1, 0)), LINE_REFS
        )
        self.assertEqual(first, second)

    def test_return_receipt_body_unknown_platform_line(self):
        with self.assertRaises(ValueError):
            return_receipt_body(ORDER_REF, "ref", returned(("p9", 1, 0)), LINE_REFS)

    def test_zero_lines_dropped_and_none_when_empty(self):
        # 合格的 payload 里件数都大于 0；直接构造的数据类测“0 件不列”与“一行不剩”。
        zero_shipped = ShippedPayload(None, "PKG1", (ShippedLine("p1", 0), ShippedLine("p2", 1)))
        _, body = dispatch_body(ORDER_REF, "ref", zero_shipped, LINE_REFS)
        self.assertEqual(body["lines"], [{"line_ref": "L2", "quantity": 1}])
        all_zero = ShippedPayload(None, None, (ShippedLine("p1", 0),))
        self.assertIsNone(dispatch_body(ORDER_REF, "ref", all_zero, LINE_REFS))
        zero_refund = CancelledPayload(None, "RF1", (CancelledLine("p1", 0, 0),))
        self.assertIsNone(refund_restock_body(ORDER_REF, "ref", zero_refund, LINE_REFS))
        zero_return = ReturnReceivedPayload(None, "RT1", (ReturnLine("p1", 0, 0),))
        self.assertIsNone(return_receipt_body(ORDER_REF, "ref", zero_return, LINE_REFS))


class PullBackoffTest(unittest.TestCase):
    def test_sequence(self):
        expected = [1, 2, 4, 8, 16, 32, 60, 60, 60]
        for attempts, minutes in enumerate(expected):
            with self.subTest(attempts=attempts):
                self.assertEqual(pull_backoff(attempts), timedelta(minutes=minutes))
        self.assertEqual(pull_backoff(10**6), timedelta(minutes=60))

    def test_invalid(self):
        for attempts in (-1, True, 1.0, None):
            with self.subTest(attempts=attempts), self.assertRaises(ValueError):
                pull_backoff(attempts)


if __name__ == "__main__":
    unittest.main()
