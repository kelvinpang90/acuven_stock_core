import ast
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.domain import allocations
from app.domain.allocations import (
    ALLOCATION_EXCEEDS_UNALLOCATED,
    AUTO,
    INVALID_REQUEST,
    MANUAL,
    MANUAL_CHECK_OVERDUE,
    NO_ACTIVE_LISTING,
    QUOTA_CHANGED,
    VERSION_CONFLICT,
    AllocationRequest,
    AllocationSnapshot,
    AllocationUpdate,
    ChannelAllocation,
    QuotaChange,
    Rejection,
    add_back,
    judge_allocation_update,
    manual_check_overdue,
    restore_allowed,
    take_quota,
    unallocated,
    validate_allocation_request,
)

SHP = "chn_01shopee"
TTS = "chn_02tiktok"
BACKOFFICE = "chn_03backoffice"


class ModuleTest(unittest.TestCase):
    def test_module_has_no_io_imports(self):
        source = Path(allocations.__file__).read_text(encoding="utf-8")
        stdlib = set()
        domain = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] == "app":
                    domain.add(name)
                else:
                    stdlib.add(name.split(".")[0])
        self.assertTrue(stdlib <= {"collections", "dataclasses", "datetime", "typing"}, stdlib)
        self.assertTrue(all(name.startswith("app.domain.") for name in domain), domain)


class Pool:
    """例子里的一个 SKU：available、S，与两个平台入口的 Q、pending。"""

    def __init__(self, available, safety_stock, quota_shp, quota_tts):
        self.available = available
        self.safety_stock = safety_stock
        self.quota = {SHP: quota_shp, TTS: quota_tts}
        self.pending = {SHP: 0, TTS: 0}

    def f(self, quota=None, pending=None):
        quota = self.quota if quota is None else quota
        pending = self.pending if pending is None else pending
        return unallocated(self.available, self.safety_stock, quota.values(), pending.values())

    def new_order(self, channel, quantity):
        """平台新订单：Q 减 min(q, Q)，pending 加 q；返回 quota_taken。"""
        taken, self.quota[channel] = take_quota(quantity, self.quota[channel])
        self.pending[channel] += quantity
        return taken

    def cancel_unpaid(self, channel, cancelled, quota_taken, quota_returned):
        """付款前取消：按恢复条件判，满足时加回；返回加回的件数。"""
        candidate = add_back(cancelled, quota_taken, quota_returned, True)
        quota_after = {**self.quota, channel: self.quota[channel] + candidate}
        pending_after = {**self.pending, channel: self.pending[channel] - cancelled}
        unallocated_after = self.f(quota_after, pending_after)
        restorable = restore_allowed(AUTO, "active", False, True, False, unallocated_after)
        returned = add_back(cancelled, quota_taken, quota_returned, restorable)
        self.quota[channel] += returned
        self.pending[channel] -= cancelled
        return returned

    def snapshot(self, version=0):
        return AllocationSnapshot(
            version=version,
            safety_stock=self.safety_stock,
            available=self.available,
            channels=tuple(
                ChannelAllocation(channel, self.quota[channel], self.pending[channel], True, False)
                for channel in (SHP, TTS)
            ),
        )


def quota_request(*changes, expected_version=0, safety_stock=None):
    return AllocationRequest(
        pool_id="pol_1",
        sku_id="sku_1",
        expected_version=expected_version,
        safety_stock=safety_stock,
        quotas=tuple(QuotaChange(*change) for change in changes) if changes else None,
        reason="r",
    )


class ExampleOneTest(unittest.TestCase):
    """契约 §3.1 例子一，逐行断言 Q[shp]、Q[tts]、pending[shp]、pending[tts] 与 F。"""

    def assert_row(self, pool, quota_shp, quota_tts, pending_shp, pending_tts, f):
        self.assertEqual(
            (pool.quota[SHP], pool.quota[TTS], pool.pending[SHP], pool.pending[TTS], pool.f()),
            (quota_shp, quota_tts, pending_shp, pending_tts, f),
        )

    def test_example_one(self):
        # 第 0 步：初始。
        pool = Pool(available=20, safety_stock=2, quota_shp=10, quota_tts=5)
        self.assert_row(pool, 10, 5, 0, 0, 3)

        # 第 1 步：Shopee 新订单 A，2 件，未付款。
        self.assertEqual(pool.new_order(SHP, 2), 2)
        self.assert_row(pool, 8, 5, 2, 0, 3)

        # 第 2 步：A 进入待发货，预留并确认 2 件。
        pool.available = 18
        pool.pending[SHP] -= 2
        self.assert_row(pool, 8, 5, 0, 0, 3)

        # 第 3 步：TikTok 新订单 B，1 件，货到付款、直接待发货。
        self.assertEqual(pool.new_order(TTS, 1), 1)
        pool.available = 17
        pool.pending[TTS] -= 1
        self.assert_row(pool, 8, 4, 0, 0, 3)

        # 第 4 步：Shopee 新订单 C，3 件，先 Q 5、pending 3；付款前取消，加回 3。
        taken = pool.new_order(SHP, 3)
        self.assertEqual(taken, 3)
        self.assert_row(pool, 5, 4, 3, 0, 3)
        self.assertEqual(pool.cancel_unpaid(SHP, 3, taken, 0), 3)
        self.assert_row(pool, 8, 4, 0, 0, 3)

        # 第 5 步：收货 5 件。
        pool.available = 22
        self.assert_row(pool, 8, 4, 0, 0, 8)

        # 第 6′ 步：若改为 17，F' = −1，不改。
        rejection = judge_allocation_update(pool.snapshot(), quota_request((SHP, 8, 17)))
        self.assertEqual(
            rejection,
            Rejection(
                ALLOCATION_EXCEEDS_UNALLOCATED,
                {
                    "available": 22,
                    "safety_stock": 2,
                    "quota_total": 21,
                    "pending_total": 0,
                    "unallocated": -1,
                },
            ),
        )
        self.assert_row(pool, 8, 4, 0, 0, 8)

        # 第 6 步：管理员把 Q[shp] 改为 14。
        update = judge_allocation_update(pool.snapshot(), quota_request((SHP, 8, 14)))
        self.assertEqual(update, AllocationUpdate(safety_stock=2, quotas={SHP: 14, TTS: 4}))
        pool.quota.update(update.quotas)
        self.assert_row(pool, 14, 4, 0, 0, 2)

        # 第 7 步：A 交运 2 件，available 不变。
        self.assert_row(pool, 14, 4, 0, 0, 2)

        # 第 8 步：Shopee 新订单 D，16 件，平台多卖 2 件；Q 只减到 0。
        self.assertEqual(pool.new_order(SHP, 16), 14)
        self.assert_row(pool, 0, 4, 16, 0, 0)

        # 第 9 步：D 进入待发货，预留并确认 16 件。
        pool.available = 6
        pool.pending[SHP] -= 16
        self.assert_row(pool, 0, 4, 0, 0, 0)

        # 第 10 步：4 件破损，标不可售；超额，TikTok 的配额不能再加。
        pool.available = 2
        self.assert_row(pool, 0, 4, 0, 0, -4)
        rejection = judge_allocation_update(pool.snapshot(), quota_request((TTS, 4, 5)))
        self.assertEqual(rejection.code, ALLOCATION_EXCEEDS_UNALLOCATED)

        # 第 11 步：TikTok 新订单 E，3 件，已付款；预留被拒，件数留在 pending。
        self.assertEqual(pool.new_order(TTS, 3), 3)
        self.assert_row(pool, 0, 1, 0, 3, -4)

        # 第 12 步：收货 5 件。
        pool.available = 7
        self.assert_row(pool, 0, 1, 0, 3, 1)

        # 第 13 步：在待处理里重试 E，预留并确认 3 件。
        pool.available = 4
        pool.pending[TTS] -= 3
        self.assert_row(pool, 0, 1, 0, 0, 1)


class ExampleTwoTest(unittest.TestCase):
    """契约 §3.1 例子二：取消时只加回扣走的件数。"""

    def assert_row(self, pool, quota_shp, pending_shp, f):
        self.assertEqual(
            (pool.quota[SHP], pool.pending[SHP], pool.f()), (quota_shp, pending_shp, f)
        )

    def test_example_two(self):
        # 第 0 步：初始。
        pool = Pool(available=20, safety_stock=2, quota_shp=1, quota_tts=0)
        self.assert_row(pool, 1, 0, 17)

        # 第 1 步：Shopee 新订单 G，3 件，平台多卖了 2 件。
        quota_taken = pool.new_order(SHP, 3)
        self.assertEqual(quota_taken, 1)
        self.assert_row(pool, 0, 3, 15)

        # 第 2 步：G 先取消 1 件，加回 min(1, 1−0) = 1。
        quota_returned = pool.cancel_unpaid(SHP, 1, quota_taken, 0)
        self.assertEqual(quota_returned, 1)
        self.assert_row(pool, 1, 2, 15)

        # 第 3 步：G 剩下 2 件也取消，加回 min(2, 1−1) = 0，2 件进 F。
        self.assertEqual(pool.cancel_unpaid(SHP, 2, quota_taken, quota_returned), 0)
        self.assert_row(pool, 1, 0, 17)

    def test_not_restorable_adds_nothing_back(self):
        self.assertEqual(add_back(1, 1, 0, False), 0)
        self.assertEqual(add_back(3, 3, 0, False), 0)


class UnallocatedTest(unittest.TestCase):
    def test_may_be_negative(self):
        self.assertEqual(unallocated(2, 2, [0, 4], [0, 0]), -4)
        self.assertEqual(unallocated(0, 0, [], []), 0)

    def test_rejects_negative_bool_and_non_integers(self):
        for args in (
            (-1, 0, [], []),
            (1, -1, [], []),
            (1, 0, [-1], []),
            (1, 0, [], [-1]),
            (True, 0, [], []),
            (1, False, [], []),
            (1, 0, [True], []),
            (1, 0, [], [1.0]),
            ("1", 0, [], []),
            (None, 0, [], []),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                unallocated(*args)


class TakeQuotaTest(unittest.TestCase):
    def test_takes_at_most_the_remaining_quota(self):
        self.assertEqual(take_quota(2, 10), (2, 8))
        self.assertEqual(take_quota(10, 10), (10, 0))
        self.assertEqual(take_quota(16, 14), (14, 0))
        self.assertEqual(take_quota(3, 0), (0, 0))

    def test_rejects_invalid_arguments(self):
        for args in ((-1, 1), (1, -1), (True, 1), (1, None)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                take_quota(*args)


class AddBackTest(unittest.TestCase):
    def test_adds_back_at_most_what_was_taken_and_not_returned(self):
        self.assertEqual(add_back(3, 3, 0, True), 3)
        self.assertEqual(add_back(1, 1, 0, True), 1)
        self.assertEqual(add_back(2, 1, 1, True), 0)
        self.assertEqual(add_back(5, 14, 10, True), 4)

    def test_rejects_invalid_arguments(self):
        for args in ((-1, 1, 0), (1, 1, 2), (True, 1, 0), (1, 1.0, 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                add_back(*args, True)


class RestoreAllowedTest(unittest.TestCase):
    def test_all_four_conditions_met(self):
        self.assertTrue(restore_allowed(AUTO, "active", False, True, False, 0))
        self.assertTrue(restore_allowed(MANUAL, "not_connected", False, True, False, 5))

    def test_auto_without_an_active_connection(self):
        for status in ("not_connected", "auth_failed", "revoked"):
            with self.subTest(status=status):
                self.assertFalse(restore_allowed(AUTO, status, False, True, False, 5))

    def test_manual_with_an_overdue_check(self):
        self.assertFalse(restore_allowed(MANUAL, "active", True, True, False, 5))

    def test_no_active_listing(self):
        self.assertFalse(restore_allowed(AUTO, "active", False, False, False, 5))

    def test_review_hold(self):
        self.assertFalse(restore_allowed(AUTO, "active", False, True, True, 5))

    def test_negative_unallocated_after_adding_back(self):
        self.assertFalse(restore_allowed(AUTO, "active", False, True, False, -1))

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            restore_allowed("other", "active", False, True, False, 0)


class ManualCheckOverdueTest(unittest.TestCase):
    CHANGED = datetime(2026, 11, 20, 8, 0)

    def test_exactly_12_hours_is_not_overdue_one_microsecond_more_is(self):
        checked = self.CHANGED + timedelta(hours=3)
        limit = checked + timedelta(hours=12)
        self.assertFalse(manual_check_overdue(MANUAL, checked, self.CHANGED, limit))
        self.assertTrue(
            manual_check_overdue(MANUAL, checked, self.CHANGED, limit + timedelta(microseconds=1))
        )

    def test_never_checked_counts_from_mode_changed_at(self):
        limit = self.CHANGED + timedelta(hours=12)
        self.assertFalse(manual_check_overdue(MANUAL, None, self.CHANGED, limit))
        self.assertTrue(
            manual_check_overdue(MANUAL, None, self.CHANGED, limit + timedelta(microseconds=1))
        )

    def test_checks_before_switching_to_manual_do_not_count(self):
        before = self.CHANGED - timedelta(hours=1)
        self.assertFalse(
            manual_check_overdue(MANUAL, before, self.CHANGED, self.CHANGED + timedelta(hours=12))
        )
        self.assertTrue(
            manual_check_overdue(
                MANUAL, before, self.CHANGED, self.CHANGED + timedelta(hours=12, microseconds=1)
            )
        )

    def test_auto_is_never_overdue(self):
        later = self.CHANGED + timedelta(days=30)
        self.assertFalse(manual_check_overdue(AUTO, None, self.CHANGED, later))
        self.assertFalse(manual_check_overdue(AUTO, self.CHANGED, self.CHANGED, later))

    def test_aware_datetimes_are_rejected(self):
        aware = self.CHANGED.replace(tzinfo=UTC)
        now = self.CHANGED + timedelta(hours=1)
        for mode in (MANUAL, AUTO):
            for args in (
                (aware, self.CHANGED, now),
                (None, aware, now),
                (None, self.CHANGED, now.replace(tzinfo=UTC)),
            ):
                with self.subTest(mode=mode, args=args), self.assertRaises(ValueError):
                    manual_check_overdue(mode, *args)

    def test_mode_changed_at_is_required(self):
        for mode in (MANUAL, AUTO):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                manual_check_overdue(mode, None, None, self.CHANGED)

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            manual_check_overdue("other", None, self.CHANGED, self.CHANGED)


def channel(channel_id, quota, pending=0, listing=True, overdue=False):
    return ChannelAllocation(channel_id, quota, pending, listing, overdue)


def snapshot(*channels, version=3, safety_stock=2, available=30):
    return AllocationSnapshot(version, safety_stock, available, tuple(channels))


class JudgeAllocationUpdateTest(unittest.TestCase):
    # F = 30 − 2 − 9 − 1 = 18。
    CURRENT = snapshot(channel(SHP, 5, pending=1), channel(TTS, 4))

    def judge(self, *changes, current=None, expected_version=3, safety_stock=None):
        request = quota_request(
            *changes, expected_version=expected_version, safety_stock=safety_stock
        )
        return judge_allocation_update(current or self.CURRENT, request)

    def test_passes_with_new_values_for_every_channel(self):
        result = self.judge((TTS, 4, 10), safety_stock=5)
        self.assertEqual(result, AllocationUpdate(safety_stock=5, quotas={SHP: 5, TTS: 10}))

    def test_f_may_reach_zero(self):
        result = self.judge((TTS, 4, 22))
        self.assertEqual(result, AllocationUpdate(safety_stock=2, quotas={SHP: 5, TTS: 22}))

    def test_version_conflict_comes_before_quota_changed(self):
        result = self.judge((SHP, 4, 6), expected_version=2)
        self.assertEqual(result, Rejection(VERSION_CONFLICT, {"current_version": 3}))

    def test_quota_changed_lists_changed_channels_in_order(self):
        result = self.judge((TTS, 3, 1), (SHP, 6, 6))
        self.assertEqual(
            result,
            Rejection(
                QUOTA_CHANGED,
                {"quotas": [{"channel_id": SHP, "quota": 5}, {"channel_id": TTS, "quota": 4}]},
            ),
        )

    def test_quota_changed_only_lists_the_mismatched_channels(self):
        result = self.judge((SHP, 5, 6), (TTS, 3, 4))
        self.assertEqual(
            result, Rejection(QUOTA_CHANGED, {"quotas": [{"channel_id": TTS, "quota": 4}]})
        )

    def test_channels_not_in_the_request_do_not_conflict(self):
        # 页面上看到 Q[shp] 是 7，平台事件已改为 5；只改 tts 不冲突。
        result = self.judge((TTS, 4, 6))
        self.assertEqual(result, AllocationUpdate(safety_stock=2, quotas={SHP: 5, TTS: 6}))

    def test_no_active_listing_names_the_first_increasing_channel(self):
        current = snapshot(channel(SHP, 5, listing=False), channel(TTS, 4, listing=False))
        result = self.judge((TTS, 4, 5), (SHP, 5, 6), current=current)
        self.assertEqual(result, Rejection(NO_ACTIVE_LISTING, {"channel_id": SHP}))

    def test_no_active_listing_only_looks_at_increasing_channels(self):
        current = snapshot(channel(SHP, 5, listing=False), channel(TTS, 4))
        result = self.judge((SHP, 5, 3), (TTS, 4, 6), current=current)
        self.assertEqual(result, AllocationUpdate(safety_stock=2, quotas={SHP: 3, TTS: 6}))

    def test_no_active_listing_comes_before_manual_check_overdue(self):
        current = snapshot(channel(SHP, 5, overdue=True), channel(TTS, 4, listing=False))
        result = self.judge((TTS, 4, 5), current=current)
        self.assertEqual(result, Rejection(NO_ACTIVE_LISTING, {"channel_id": TTS}))

    def test_overdue_channel_blocks_increasing_another(self):
        current = snapshot(channel(SHP, 5, overdue=True), channel(TTS, 4))
        result = self.judge((TTS, 4, 5), current=current)
        self.assertEqual(result, Rejection(MANUAL_CHECK_OVERDUE, {"channel_id": SHP}))

    def test_overdue_channel_may_increase_its_own_quota(self):
        current = snapshot(channel(SHP, 5, overdue=True), channel(TTS, 4))
        result = self.judge((SHP, 5, 6), current=current)
        self.assertEqual(result, AllocationUpdate(safety_stock=2, quotas={SHP: 6, TTS: 4}))

    def test_overdue_channel_increasing_itself_is_judged_by_items_3_and_5(self):
        current = snapshot(channel(SHP, 5, overdue=True, listing=False), channel(TTS, 4))
        result = self.judge((SHP, 5, 6), current=current)
        self.assertEqual(result, Rejection(NO_ACTIVE_LISTING, {"channel_id": SHP}))

        current = snapshot(channel(SHP, 5, overdue=True), channel(TTS, 4))
        result = self.judge((SHP, 5, 25), current=current)
        self.assertEqual(result.code, ALLOCATION_EXCEEDS_UNALLOCATED)

    def test_with_two_overdue_channels_increasing_one_counts_as_another(self):
        current = snapshot(channel(SHP, 5, overdue=True), channel(TTS, 4, overdue=True))
        result = self.judge((SHP, 5, 6), current=current)
        self.assertEqual(result, Rejection(MANUAL_CHECK_OVERDUE, {"channel_id": TTS}))
        result = self.judge((TTS, 4, 5), current=current)
        self.assertEqual(result, Rejection(MANUAL_CHECK_OVERDUE, {"channel_id": SHP}))

    def test_manual_check_overdue_comes_before_exceeding_unallocated(self):
        current = snapshot(channel(SHP, 5, overdue=True), channel(TTS, 4))
        result = self.judge((TTS, 4, 100), current=current)
        self.assertEqual(result, Rejection(MANUAL_CHECK_OVERDUE, {"channel_id": SHP}))

    def test_exceeding_unallocated_uses_the_new_safety_stock(self):
        result = self.judge((TTS, 4, 5), safety_stock=20)
        self.assertEqual(
            result,
            Rejection(
                ALLOCATION_EXCEEDS_UNALLOCATED,
                {
                    "available": 30,
                    "safety_stock": 20,
                    "quota_total": 10,
                    "pending_total": 1,
                    "unallocated": -1,
                },
            ),
        )

    def test_decreasing_quotas_or_changing_s_skips_items_3_to_5(self):
        # F = 2 − 2 − 9 − 1 = −10，两个入口都没有平台商品，shp 逾期。
        current = snapshot(
            channel(SHP, 5, pending=1, listing=False, overdue=True),
            channel(TTS, 4, listing=False),
            available=2,
        )
        for changes, safety_stock, expected in (
            (((TTS, 4, 3),), None, AllocationUpdate(2, {SHP: 5, TTS: 3})),
            (((SHP, 5, 0), (TTS, 4, 4)), None, AllocationUpdate(2, {SHP: 0, TTS: 4})),
            ((), 10, AllocationUpdate(10, {SHP: 5, TTS: 4})),
        ):
            with self.subTest(changes=changes, safety_stock=safety_stock):
                result = self.judge(*changes, current=current, safety_stock=safety_stock)
                self.assertEqual(result, expected)

    def test_rejects_channels_missing_from_the_snapshot(self):
        with self.assertRaises(ValueError):
            self.judge((BACKOFFICE, 0, 1))


class ValidateAllocationRequestTest(unittest.TestCase):
    CHANNELS = (SHP, TTS)

    def body(self, **changes):
        body = {
            "pool_id": "pol_1",
            "sku_id": "sku_1",
            "expected_version": 0,
            "safety_stock": 2,
            "quotas": [{"channel_id": SHP, "expected_quota": 8, "quota": 14}],
            "reason": "  restock  ",
        }
        for field, value in changes.items():
            if value is ...:
                del body[field]
            else:
                body[field] = value
        return body

    def assert_invalid(self, body, field):
        with self.subTest(body=body):
            self.assertEqual(
                validate_allocation_request(body, self.CHANNELS),
                Rejection(INVALID_REQUEST, {"field": field}),
            )

    def test_valid_body(self):
        self.assertEqual(
            validate_allocation_request(self.body(), self.CHANNELS),
            AllocationRequest(
                pool_id="pol_1",
                sku_id="sku_1",
                expected_version=0,
                safety_stock=2,
                quotas=(QuotaChange(SHP, 8, 14),),
                reason="restock",
            ),
        )

    def test_either_safety_stock_or_quotas_is_enough(self):
        result = validate_allocation_request(self.body(quotas=...), self.CHANNELS)
        self.assertEqual((result.safety_stock, result.quotas), (2, None))
        result = validate_allocation_request(self.body(safety_stock=...), self.CHANNELS)
        self.assertEqual((result.safety_stock, result.quotas), (None, (QuotaChange(SHP, 8, 14),)))

    def test_bounds_are_inclusive(self):
        result = validate_allocation_request(
            self.body(
                safety_stock=1000000,
                quotas=[
                    {"channel_id": SHP, "expected_quota": 0, "quota": 1000000},
                    {"channel_id": TTS, "expected_quota": 1000000, "quota": 0},
                ],
                reason="x" * 500,
            ),
            self.CHANNELS,
        )
        self.assertIsInstance(result, AllocationRequest)
        result = validate_allocation_request(self.body(safety_stock=0), self.CHANNELS)
        self.assertEqual(result.safety_stock, 0)

    def test_extra_keys_come_first_in_name_order(self):
        self.assert_invalid(self.body(zeta=1, alpha=None, pool_id=...), "alpha")

    def test_pool_id_and_sku_id_must_be_present_and_not_null(self):
        self.assert_invalid(self.body(pool_id=...), "pool_id")
        self.assert_invalid(self.body(pool_id=None, sku_id=None), "pool_id")
        self.assert_invalid(self.body(sku_id=...), "sku_id")
        self.assert_invalid(self.body(sku_id=None, expected_version=-1), "sku_id")

    def test_expected_version(self):
        for value in (..., None, -1, True, "0", 1.0):
            self.assert_invalid(self.body(expected_version=value, reason=""), "expected_version")

    def test_safety_stock_out_of_range_or_not_an_integer(self):
        for value in (None, -1, 1000001, True, "2", 2.0):
            self.assert_invalid(self.body(safety_stock=value, quotas=[]), "safety_stock")

    def test_missing_both_safety_stock_and_quotas(self):
        self.assert_invalid(self.body(safety_stock=..., quotas=...), "quotas")
        self.assert_invalid(self.body(safety_stock=..., quotas=..., reason=""), "quotas")

    def test_null_quotas_is_not_absent(self):
        self.assert_invalid(self.body(quotas=None), "quotas")

    def test_quotas_shape(self):
        for value in (
            [],
            {"channel_id": SHP, "expected_quota": 8, "quota": 14},
            "x",
            [None],
            [{"channel_id": SHP, "quota": 14}],
            [{"channel_id": SHP, "expected_quota": 8, "quota": 14, "note": "x"}],
            [{"channel_id": SHP, "expected_quota": None, "quota": 14}],
        ):
            self.assert_invalid(self.body(quotas=value, reason=""), "quotas")

    def test_duplicate_channel(self):
        self.assert_invalid(
            self.body(
                quotas=[
                    {"channel_id": SHP, "expected_quota": 8, "quota": 14},
                    {"channel_id": SHP, "expected_quota": 8, "quota": 9},
                ]
            ),
            "quotas",
        )

    def test_channel_must_be_a_platform_channel_of_this_pool(self):
        for channel_id in (BACKOFFICE, "chn_other", None, 1, [SHP]):
            self.assert_invalid(
                self.body(quotas=[{"channel_id": channel_id, "expected_quota": 8, "quota": 14}]),
                "quotas",
            )

    def test_quota_values_out_of_range_or_not_integers(self):
        for expected_quota, quota in (
            (8, 1000001),
            (8, -1),
            (-1, 8),
            (1000001, 8),
            (True, 1),
            (1, False),
            (1, 1.0),
        ):
            item = {"channel_id": SHP, "expected_quota": expected_quota, "quota": quota}
            self.assert_invalid(self.body(quotas=[item]), "quotas")

    def test_reason(self):
        for value in (
            ...,
            None,
            "",
            "   ",
            "x" * 501,
            " " + "x" * 501,
            "a\nb",
            "a\x00b",
            "a\x7fb",
            1,
        ):
            self.assert_invalid(self.body(reason=value), "reason")

    def test_body_must_be_an_object(self):
        self.assert_invalid([], "body")


if __name__ == "__main__":
    unittest.main()
