"""平台事件执行器：扣数之前的取消、退货申请与乱序（契约 §5.10 第 4 步，§3.1）。

§3.1 例子一第 4 步、例子二第 1–3 步、恢复条件逐项，待处理中的订单被取消，
行级取消与超额、不认识的行，§5.10 第 4 步落后、越过阶段的三种情形，
一轮处理完之后清补拉的退避。

在真 MySQL 上跑。每个测试用 platform_fixtures 建自己的租户，
结束时在 finally 里 purge_tenant 删掉它的全部行。
"""

from collections.abc import Iterator
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, insert, select, text, update

from app.domain.ids import new_id
from app.models.inventory import StockBalance
from app.models.platform import (
    ChannelAllocation,
    PlatformEvent,
    PlatformOrder,
    PlatformOrderLine,
    PoolAllocation,
    SkuListing,
)
from app.models.platform_ops import ManualCheck, PlatformConnection
from app.services.platform_events import (
    BLOCKED,
    MISSING_PRIOR,
    NOT_HANDLED,
    RoundResult,
    process_order,
    record_auto_event,
)
from tests.integration.platform_fixtures import (
    PlatformWorld,
    add_listing,
    add_platform_channel,
    add_sku,
    create_world,
    purge_tenant,
    set_allocation,
)

TTL = 900
T1 = "2026-11-27T08:00:00Z"
T2 = "2026-11-27T09:00:00Z"
T3 = "2026-11-27T10:00:00Z"
DB_NOW = text("CURRENT_TIMESTAMP(6)")


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


@pytest.fixture
def world(engine: Engine) -> Iterator[PlatformWorld]:
    built = create_world(engine)
    try:
        yield built
    finally:
        purge_tenant(engine, built.tenant_id)


# --- 造数与观察 ---


def new_order_id() -> str:
    return f"2611{uuid4().hex[:10].upper()}"


def new_item_id() -> str:
    return f"item-{uuid4().hex[:12]}"


def line(platform_line_id: str, platform_item_id: str, quantity: int) -> dict:
    return {
        "platform_line_id": platform_line_id,
        "platform_item_id": platform_item_id,
        "platform_model_id": "-",
        "quantity": quantity,
    }


def created(*lines: dict, platform_time: str = T1) -> dict:
    return {"lines": list(lines), "platform_time": platform_time}


def cancelled(*totals: tuple[str, int], platform_time: str = T2) -> dict:
    """不给 totals 为订单级取消；否则各项为 (platform_line_id, cancelled_total)。"""
    payload: dict = {"platform_time": platform_time}
    if totals:
        payload["lines"] = [
            {"platform_line_id": line_id, "cancelled_total": total} for line_id, total in totals
        ]
    return payload


def shipped(platform_line_id: str, quantity: int, platform_time: str = T2) -> dict:
    return {
        "lines": [{"platform_line_id": platform_line_id, "quantity": quantity}],
        "package_id": "PKG-1",
        "platform_time": platform_time,
    }


def record(
    engine: Engine,
    world: PlatformWorld,
    order_id: str,
    payload: dict,
    *,
    action: str = "created",
    event_key: str | None = None,
    source: str = "webhook",
) -> str:
    """写一个事件进收件箱，返回传入的 event_key。"""
    event_key = event_key or f"evt-{uuid4().hex}"
    assert record_auto_event(
        engine, world.tenant_id, world.channel_id, order_id, "", action, event_key, source, payload
    )
    return event_key


def process(engine: Engine, world: PlatformWorld, order_id: str) -> RoundResult:
    return process_order(
        engine, world.tenant_id, world.channel_id, order_id, reservation_ttl_seconds=TTL
    )


def _rows(engine: Engine, model, *criteria) -> list[dict]:
    table = model.__table__
    statement = select(*table.columns).where(*criteria).order_by(*table.primary_key.columns)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def event_by_key(engine: Engine, world: PlatformWorld, event_key: str) -> dict:
    rows = _rows(
        engine,
        PlatformEvent,
        PlatformEvent.tenant_id == world.tenant_id,
        PlatformEvent.event_key.startswith(event_key),
    )
    [event] = rows
    return event


def result_of(engine: Engine, world: PlatformWorld, event_key: str) -> tuple:
    event = event_by_key(engine, world, event_key)
    return event["result"], event["result_code"]


def platform_order(engine: Engine, world: PlatformWorld, order_id: str) -> dict | None:
    rows = _rows(
        engine,
        PlatformOrder,
        PlatformOrder.tenant_id == world.tenant_id,
        PlatformOrder.platform_order_id == order_id,
    )
    return rows[0] if rows else None


def order_lines(engine: Engine, world: PlatformWorld, order_id: str) -> dict[str, dict]:
    order = platform_order(engine, world, order_id)
    assert order is not None
    rows = _rows(
        engine,
        PlatformOrderLine,
        PlatformOrderLine.tenant_id == world.tenant_id,
        PlatformOrderLine.platform_order_row_id == order["platform_order_row_id"],
    )
    return {row["platform_line_id"]: row for row in rows}


def line_amounts(engine: Engine, world: PlatformWorld, order_id: str, line_id: str) -> tuple:
    """(cancelled_qty, quota_taken, quota_returned)。"""
    row = order_lines(engine, world, order_id)[line_id]
    return row["cancelled_qty"], row["quota_taken"], row["quota_returned"]


def amounts(
    engine: Engine, world: PlatformWorld, sku_id: str, channel_id: str | None = None
) -> tuple:
    """这个入口上这个 SKU 的 (quota, pending)。"""
    [row] = _rows(
        engine,
        ChannelAllocation,
        ChannelAllocation.tenant_id == world.tenant_id,
        ChannelAllocation.channel_id == (channel_id or world.channel_id),
        ChannelAllocation.sku_id == sku_id,
    )
    return row["quota"], row["pending"]


def unallocated_of(engine: Engine, world: PlatformWorld, sku_id: str) -> int:
    """F = available − S − ΣQ − Σpending（全部 platform 入口；测试里没有预留与不可售）。"""
    with engine.connect() as db:
        stock = db.execute(
            select(StockBalance.stock_balance).where(
                StockBalance.tenant_id == world.tenant_id, StockBalance.sku_id == sku_id
            )
        ).scalar_one()
        safety = db.execute(
            select(PoolAllocation.safety_stock).where(
                PoolAllocation.tenant_id == world.tenant_id, PoolAllocation.sku_id == sku_id
            )
        ).scalar_one()
        total = db.execute(
            select(func.sum(ChannelAllocation.quota + ChannelAllocation.pending)).where(
                ChannelAllocation.tenant_id == world.tenant_id, ChannelAllocation.sku_id == sku_id
            )
        ).scalar_one()
    return stock - safety - int(total)


def quantities(engine: Engine, world: PlatformWorld) -> tuple:
    """这个租户的订单行与两级配额行（“不改任何量”时要相同的）。"""
    tenant_id = world.tenant_id
    return tuple(
        _rows(engine, model, model.tenant_id == tenant_id)
        for model in (PlatformOrderLine, ChannelAllocation, PoolAllocation)
    )


def mapped_sku(engine: Engine, world: PlatformWorld, **values) -> tuple[str, str]:
    """一个 SKU、挂在 world 入口上的平台商品与配额行；返回 (sku_id, platform_item_id)。"""
    sku_id, item_id = add_sku(engine, world.tenant_id), new_item_id()
    add_listing(engine, world, sku_id, item_id)
    set_allocation(engine, world, sku_id, **values)
    return sku_id, item_id


def update_rows(engine: Engine, statement) -> None:
    with engine.begin() as db:
        db.execute(statement)


def update_order(engine: Engine, world: PlatformWorld, order_id: str, **values) -> None:
    update_rows(
        engine,
        update(PlatformOrder)
        .where(PlatformOrder.tenant_id == world.tenant_id)
        .where(PlatformOrder.platform_order_id == order_id)
        .values(**values),
    )


def update_connection(engine: Engine, world: PlatformWorld, **values) -> None:
    update_rows(
        engine,
        update(PlatformConnection)
        .where(PlatformConnection.tenant_id == world.tenant_id)
        .where(PlatformConnection.channel_id == world.channel_id)
        .values(**values),
    )


def hours_ago(hours: int):
    """数据库时间 hours 小时之前（SQL 表达式）。"""
    return text(f"CURRENT_TIMESTAMP(6) - INTERVAL {int(hours)} HOUR")


def add_manual_check(engine: Engine, world: PlatformWorld, hours: int) -> None:
    """一次人工核对，checked_until 为数据库时间 hours 小时之前。"""
    with engine.begin() as db:
        db.execute(
            insert(ManualCheck).values(
                manual_check_id=new_id("mck"),
                tenant_id=world.tenant_id,
                channel_id=world.channel_id,
                checked_from=hours_ago(hours + 1),
                checked_until=hours_ago(hours),
                found_discrepancy=False,
                credential_id=world.system_credential_id,
                request_id=new_id("req"),
            )
        )


# --- §3.1 例子一第 4 步、例子二 ---


def test_example_one_step_four_a_cancel_before_payment_adds_the_quota_back(engine, world):
    tts = add_platform_channel(engine, world)
    sku_id, item_id = mapped_sku(engine, world, quota=8, stock_balance=17, safety_stock=2)
    set_allocation(engine, world, sku_id, quota=4, channel_id=tts)
    assert unallocated_of(engine, world, sku_id) == 3
    order_id = new_order_id()

    record(engine, world, order_id, created(line("L-1", item_id, 3)))
    assert process(engine, world, order_id) == RoundResult(1)
    assert amounts(engine, world, sku_id) == (5, 3)
    assert line_amounts(engine, world, order_id, "L-1") == (0, 3, 0)

    key = record(engine, world, order_id, cancelled(), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("applied", None)
    assert amounts(engine, world, sku_id) == (8, 0)
    assert amounts(engine, world, sku_id, tts) == (4, 0)
    assert line_amounts(engine, world, order_id, "L-1") == (3, 3, 3)
    assert platform_order(engine, world, order_id)["stage"] == "cancelled"
    assert unallocated_of(engine, world, sku_id) == 3


def test_example_two_only_what_was_taken_is_added_back(engine, world):
    tts = add_platform_channel(engine, world)
    sku_id, item_id = mapped_sku(engine, world, quota=1, stock_balance=20, safety_stock=2)
    set_allocation(engine, world, sku_id, quota=0, channel_id=tts)
    assert unallocated_of(engine, world, sku_id) == 17
    order_id = new_order_id()

    # 第 1 步：新订单 G 3 件，平台多卖了 2 件。
    record(engine, world, order_id, created(line("L-1", item_id, 3)))
    assert process(engine, world, order_id) == RoundResult(1)
    assert amounts(engine, world, sku_id) == (0, 3)
    assert line_amounts(engine, world, order_id, "L-1") == (0, 1, 0)
    assert unallocated_of(engine, world, sku_id) == 15

    # 第 2 步：先取消 1 件，加回 min(1, 1 − 0) = 1。
    record(engine, world, order_id, cancelled(("L-1", 1)), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert amounts(engine, world, sku_id) == (1, 2)
    assert line_amounts(engine, world, order_id, "L-1") == (1, 1, 1)
    assert unallocated_of(engine, world, sku_id) == 15
    assert platform_order(engine, world, order_id)["stage"] == "unpaid"

    # 第 3 步：剩下 2 件也取消，加回 min(2, 1 − 1) = 0，2 件进 F。
    record(engine, world, order_id, cancelled(("L-1", 3), platform_time=T3), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert amounts(engine, world, sku_id) == (1, 0)
    assert line_amounts(engine, world, order_id, "L-1") == (3, 1, 1)
    assert unallocated_of(engine, world, sku_id) == 17
    assert amounts(engine, world, sku_id, tts) == (0, 0)
    assert platform_order(engine, world, order_id)["stage"] == "cancelled"


# --- 恢复条件逐项 ---

RESTORE_CASES = [
    "all_met",
    "manual_checked",
    "connection_not_active",
    "manual_overdue",
    "listing_inactive",
    "review_hold",
    "negative_unallocated",
]


@pytest.mark.parametrize("case", RESTORE_CASES)
def test_each_restore_condition_alone_blocks_the_whole_add_back(engine, world, case):
    stock = 5 if case == "negative_unallocated" else 50
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=stock, safety_stock=2)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2), line("L-2", item_id, 1)))
    assert process(engine, world, order_id) == RoundResult(1)
    assert amounts(engine, world, sku_id) == (7, 3)
    key = record(engine, world, order_id, cancelled(), action="cancelled")

    if case == "manual_checked":
        # 切到人工 13 小时，最近一次核对在 1 小时前（更早的那次不算最近）：不逾期。
        update_connection(engine, world, mode="manual", mode_changed_at=hours_ago(13))
        add_manual_check(engine, world, 14)
        add_manual_check(engine, world, 1)
    elif case == "connection_not_active":
        update_connection(engine, world, status="auth_failed")
    elif case == "manual_overdue":
        # 切到人工之前的核对不算数：从切换时刻算起已 13 小时。
        update_connection(engine, world, mode="manual", mode_changed_at=hours_ago(13))
        add_manual_check(engine, world, 14)
    elif case == "listing_inactive":
        update_rows(
            engine,
            update(SkuListing).where(SkuListing.sku_id == sku_id).values(status="inactive"),
        )
    elif case == "review_hold":
        update_rows(
            engine,
            update(ChannelAllocation)
            .where(ChannelAllocation.tenant_id == world.tenant_id)
            .where(ChannelAllocation.sku_id == sku_id)
            .values(review_hold=True, review_reason="readback mismatch", review_since=DB_NOW),
        )

    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("applied", None)
    restored = case in ("all_met", "manual_checked")
    # pending 照减；不满足时两行的 3 件整份不加回。
    assert amounts(engine, world, sku_id) == ((10 if restored else 7), 0)
    returned = 2 if restored else 0
    assert line_amounts(engine, world, order_id, "L-1") == (2, 2, returned)
    assert line_amounts(engine, world, order_id, "L-2") == (1, 1, 1 if restored else 0)
    assert platform_order(engine, world, order_id)["stage"] == "cancelled"


def test_a_held_order_cancelled_adds_nothing_back(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    payload = created(line("L-1", item_id, 2), line("L-2", new_item_id(), 4))
    record(engine, world, order_id, payload)
    assert process(engine, world, order_id) == RoundResult(1)
    assert platform_order(engine, world, order_id)["stage"] == "held"
    assert amounts(engine, world, sku_id) == (8, 2)

    # 只取消找不到 SKU 的行：只加 cancelled_qty。
    record(engine, world, order_id, cancelled(("L-2", 4)), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert line_amounts(engine, world, order_id, "L-2") == (4, 0, 0)
    assert amounts(engine, world, sku_id) == (8, 2)
    assert platform_order(engine, world, order_id)["stage"] == "held"

    record(engine, world, order_id, cancelled(platform_time=T3), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert amounts(engine, world, sku_id) == (8, 0)
    assert line_amounts(engine, world, order_id, "L-1") == (2, 2, 0)
    order = platform_order(engine, world, order_id)
    assert order["stage"] == "cancelled"
    # attention 不清。
    assert (order["attention"], order["attention_code"]) == ("unmapped", None)


# --- 行级取消、超额与不认识的行 ---


def test_a_line_cancel_touches_only_the_given_lines(engine, world):
    first, first_item = mapped_sku(engine, world, quota=10, stock_balance=50)
    second, second_item = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    payload = created(line("L-1", first_item, 2), line("L-2", second_item, 3))
    record(engine, world, order_id, payload)
    assert process(engine, world, order_id) == RoundResult(1)

    record(engine, world, order_id, cancelled(("L-1", 1)), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert line_amounts(engine, world, order_id, "L-1") == (1, 2, 1)
    assert line_amounts(engine, world, order_id, "L-2") == (0, 3, 0)
    assert amounts(engine, world, first) == (9, 1)
    assert amounts(engine, world, second) == (7, 3)
    assert platform_order(engine, world, order_id)["stage"] == "unpaid"


def test_a_cancel_over_the_quantity_or_of_an_unknown_line_changes_nothing(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2), line("L-2", item_id, 1)))
    assert process(engine, world, order_id) == RoundResult(1)
    before = quantities(engine, world)

    # 一行超过下单件数，另一行本身合格：整个事件不改任何量。
    over = record(engine, world, order_id, cancelled(("L-1", 1), ("L-2", 2)), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, over) == ("attention", "cancel_exceeds_remaining")
    assert quantities(engine, world) == before
    order = platform_order(engine, world, order_id)
    assert (order["stage"], order["attention"]) == ("unpaid", "cancel_exceeds_remaining")
    assert order["attention_code"] is None

    # 不认识的行与超额同时出现记 unknown_line；订单已有的 attention 不被覆盖。
    both = cancelled(("L-1", 5), ("L-9", 1), platform_time=T3)
    unknown = record(engine, world, order_id, both, action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, unknown) == ("attention", "unknown_line")
    assert quantities(engine, world) == before
    assert platform_order(engine, world, order_id)["attention"] == "cancel_exceeds_remaining"


def test_an_unknown_line_is_written_when_the_order_has_no_attention(engine, world):
    _, item_id = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    assert process(engine, world, order_id) == RoundResult(1)
    before = quantities(engine, world)

    key = record(engine, world, order_id, cancelled(("L-9", 1)), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("attention", "unknown_line")
    assert quantities(engine, world) == before
    order = platform_order(engine, world, order_id)
    assert (order["attention"], order["attention_code"]) == ("unknown_line", None)


def test_a_cancel_after_the_whole_order_is_cancelled_is_ignored(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    record(engine, world, order_id, cancelled(), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(2)
    before = quantities(engine, world)

    again = record(
        engine, world, order_id, cancelled(platform_time=T3), action="cancelled", source="pull"
    )
    returned = record(engine, world, order_id, {"platform_time": T3}, action="return_requested")
    assert process(engine, world, order_id) == RoundResult(2)
    assert result_of(engine, world, again) == ("ignored", None)
    assert result_of(engine, world, returned) == ("ignored", None)
    assert quantities(engine, world) == before
    assert amounts(engine, world, sku_id) == (10, 0)


def test_a_return_request_only_keeps_the_event(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    assert process(engine, world, order_id) == RoundResult(1)
    before = quantities(engine, world), platform_order(engine, world, order_id)

    key = record(engine, world, order_id, {"platform_time": T2}, action="return_requested")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("ignored", None)
    assert (quantities(engine, world), platform_order(engine, world, order_id)) == before


def test_the_same_line_cancel_from_webhook_and_pull_applies_once(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 3)))
    hook = record(engine, world, order_id, cancelled(("L-1", 1)), action="cancelled")
    pulled = record(
        engine,
        world,
        order_id,
        cancelled(("L-1", 1), platform_time=T3),
        action="cancelled",
        event_key=T3,
        source="pull",
    )

    assert process(engine, world, order_id) == RoundResult(3)
    assert result_of(engine, world, hook) == ("applied", None)
    assert result_of(engine, world, pulled) == ("ignored", None)
    assert line_amounts(engine, world, order_id, "L-1") == (1, 3, 1)
    assert amounts(engine, world, sku_id) == (8, 2)


# --- 乱序（§5.10 第 4 步） ---


def test_a_cancel_of_a_line_before_ready_to_ship_is_processed_first(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    # 待发货先到收件箱，平台时间却晚于取消。
    rts = record(engine, world, order_id, {"platform_time": T3}, action="ready_to_ship")
    record(engine, world, order_id, created(line("L-1", item_id, 3)))
    cancel = record(engine, world, order_id, cancelled(("L-1", 1)), action="cancelled")
    rts_id = event_by_key(engine, world, rts)["event_row_id"]

    assert process(engine, world, order_id) == RoundResult(2, rts_id, NOT_HANDLED)
    assert result_of(engine, world, cancel) == ("applied", None)
    assert line_amounts(engine, world, order_id, "L-1") == (1, 3, 1)
    assert amounts(engine, world, sku_id) == (8, 2)
    assert event_by_key(engine, world, rts)["applied_at"] is None


def test_shipped_without_ready_to_ship_marks_a_pull_once(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    ship = record(engine, world, order_id, shipped("L-1", 2), action="shipped")
    ship_id = event_by_key(engine, world, ship)["event_row_id"]

    assert process(engine, world, order_id) == RoundResult(1, ship_id, MISSING_PRIOR)
    order = platform_order(engine, world, order_id)
    assert order["stage"] == "unpaid"
    assert order["next_pull_at"] is not None
    assert order["pull_attempts"] == 0
    assert event_by_key(engine, world, ship)["applied_at"] is None
    assert amounts(engine, world, sku_id) == (8, 2)

    # 补拉作业推后了 next_pull_at、记了次数；下一轮再撞不重设。
    pulled_at = datetime(2026, 11, 27, 7, 0)
    update_order(engine, world, order_id, next_pull_at=pulled_at, pull_attempts=2)
    assert process(engine, world, order_id) == RoundResult(0, ship_id, MISSING_PRIOR)
    order = platform_order(engine, world, order_id)
    assert (order["next_pull_at"], order["pull_attempts"]) == (pulled_at, 2)


def test_a_cancel_before_the_order_exists_waits_for_created(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    # 取消的平台时间早于新订单，排在前面。
    cancel = record(engine, world, order_id, cancelled(platform_time=T1), action="cancelled")
    cancel_id = event_by_key(engine, world, cancel)["event_row_id"]

    # 新订单还不在收件箱：停下、什么也不写。
    assert process(engine, world, order_id) == RoundResult(0, cancel_id, MISSING_PRIOR)
    assert platform_order(engine, world, order_id) is None
    assert event_by_key(engine, world, cancel)["applied_at"] is None

    new = record(engine, world, order_id, created(line("L-1", item_id, 3), platform_time=T2))
    assert process(engine, world, order_id) == RoundResult(2)
    assert result_of(engine, world, new) == ("applied", None)
    assert result_of(engine, world, cancel) == ("applied", None)
    order = platform_order(engine, world, order_id)
    assert order["stage"] == "cancelled"
    assert (order["next_pull_at"], order["pull_attempts"]) == (None, 0)
    assert line_amounts(engine, world, order_id, "L-1") == (3, 3, 3)
    assert amounts(engine, world, sku_id) == (10, 0)


def test_shipped_on_a_held_order_after_ready_to_ship_stops_without_a_pull(engine, world):
    _, item_id = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    assert process(engine, world, order_id) == RoundResult(1)
    # 待发货处理过、Core 拒绝预留，订单在待处理里。
    update_order(
        engine,
        world,
        order_id,
        stage="held",
        attention="core_rejected",
        attention_code="INSUFFICIENT_STOCK",
        ready_at=DB_NOW,
    )
    before = quantities(engine, world)
    ship = record(engine, world, order_id, shipped("L-1", 2), action="shipped")
    ship_id = event_by_key(engine, world, ship)["event_row_id"]

    assert process(engine, world, order_id) == RoundResult(0, ship_id, BLOCKED)
    order = platform_order(engine, world, order_id)
    assert (order["stage"], order["next_pull_at"], order["pull_attempts"]) == ("held", None, 0)
    assert event_by_key(engine, world, ship)["applied_at"] is None
    assert quantities(engine, world) == before


def test_ready_to_ship_on_a_cancelled_order_is_a_stage_conflict(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    record(engine, world, order_id, cancelled(), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(2)
    before = quantities(engine, world)

    rts = record(engine, world, order_id, {"platform_time": T3}, action="ready_to_ship")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, rts) == ("attention", "STATE_CONFLICT")
    order = platform_order(engine, world, order_id)
    assert order["stage"] == "cancelled"
    assert (order["attention"], order["attention_code"]) == ("stage_conflict", "STATE_CONFLICT")
    assert quantities(engine, world) == before
    assert amounts(engine, world, sku_id) == (10, 0)


def test_a_finished_round_clears_the_pull_backoff(engine, world):
    _, item_id = mapped_sku(engine, world, quota=10, stock_balance=50)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    assert process(engine, world, order_id) == RoundResult(1)
    update_order(
        engine, world, order_id, next_pull_at=datetime(2026, 11, 27, 7, 0), pull_attempts=3
    )

    record(engine, world, order_id, cancelled(("L-1", 1)), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    order = platform_order(engine, world, order_id)
    assert (order["next_pull_at"], order["pull_attempts"]) == (None, 0)
