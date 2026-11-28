"""平台事件执行器的骨架与新订单（契约 §5.10，§3.1，0.24）。

§5.10 的存放、处理顺序第 1–4、7、8 步与例子第 1–3 种，
§3.1 下单扣配额与例子一第 8 步，0.24 的 manual_confirmed_quota。

在真 MySQL 上跑。每个测试用 platform_fixtures 建自己的租户
（live 的池、自动模式的 platform 入口、backoffice 入口与 system 主体），
结束时在 finally 里 purge_tenant 删掉它的全部行，
会话结束时 migrated_engine 才能照常降到 base。
"""

import hashlib
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, select, text, update

from app.domain.platform_events import order_lock_name, order_ref
from app.models.inventory import SalesChannel
from app.models.platform import (
    ChannelAllocation,
    PlatformEvent,
    PlatformOrder,
    PlatformOrderLine,
    PoolAllocation,
    SkuListing,
)
from app.models.platform_ops import PlatformConnection
from app.services import platform_events
from app.services.platform_events import (
    FAILED,
    LISTING_CHANGED,
    LOCK_BUSY,
    PendingOrder,
    RoundResult,
    orders_with_pending_events,
    process_order,
    record_auto_event,
)
from tests.integration.conftest import TEST_DATABASE_URL
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


def line(platform_line_id: str, platform_item_id: str, quantity: int, model: str = "-") -> dict:
    return {
        "platform_line_id": platform_line_id,
        "platform_item_id": platform_item_id,
        "platform_model_id": model,
        "quantity": quantity,
    }


def created(*lines: dict, platform_time: str | None = T1) -> dict:
    payload: dict = {"lines": list(lines)}
    if platform_time is not None:
        payload["platform_time"] = platform_time
    return payload


def record(
    engine: Engine,
    world: PlatformWorld,
    order_id: str,
    payload: dict,
    *,
    action: str = "created",
    event_key: str | None = None,
    source: str = "webhook",
    channel_id: str | None = None,
) -> bool:
    return record_auto_event(
        engine,
        world.tenant_id,
        channel_id or world.channel_id,
        order_id,
        "",
        action,
        event_key or f"evt-{uuid4().hex}",
        source,
        payload,
    )


def process(engine: Engine, world: PlatformWorld, order_id: str) -> RoundResult:
    return process_order(
        engine, world.tenant_id, world.channel_id, order_id, reservation_ttl_seconds=TTL
    )


def _rows(engine: Engine, model, *criteria) -> list[dict]:
    table = model.__table__
    statement = select(*table.columns).where(*criteria).order_by(*table.primary_key.columns)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def events_of(engine: Engine, world: PlatformWorld, order_id: str | None = None) -> list[dict]:
    criteria = [PlatformEvent.tenant_id == world.tenant_id]
    if order_id is not None:
        criteria.append(PlatformEvent.platform_order_id == order_id)
    return _rows(engine, PlatformEvent, *criteria)


def event_by_key(engine: Engine, world: PlatformWorld, event_key: str) -> dict:
    [event] = _rows(
        engine,
        PlatformEvent,
        PlatformEvent.tenant_id == world.tenant_id,
        PlatformEvent.event_key == event_key,
    )
    return event


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


def allocation(
    engine: Engine, world: PlatformWorld, sku_id: str, channel_id: str | None = None
) -> dict | None:
    rows = _rows(
        engine,
        ChannelAllocation,
        ChannelAllocation.tenant_id == world.tenant_id,
        ChannelAllocation.channel_id == (channel_id or world.channel_id),
        ChannelAllocation.sku_id == sku_id,
    )
    return rows[0] if rows else None


def amounts(engine: Engine, world: PlatformWorld, sku_id: str) -> tuple:
    """这个入口上这个 SKU 的 (quota, pending, manual_confirmed_quota)。"""
    row = allocation(engine, world, sku_id)
    assert row is not None
    return row["quota"], row["pending"], row["manual_confirmed_quota"]


def state(engine: Engine, world: PlatformWorld) -> tuple:
    """这个租户的平台订单、订单行与两级配额行（“不改任何量”时要相同的）。"""
    tenant_id = world.tenant_id
    return tuple(
        _rows(engine, model, model.tenant_id == tenant_id)
        for model in (PlatformOrder, PlatformOrderLine, ChannelAllocation, PoolAllocation)
    )


def mapped_sku(engine: Engine, world: PlatformWorld, *, quota: int = 10, **values) -> tuple:
    """一个 SKU、挂在 world 入口上的平台商品与配额行；
    返回 (sku_id, platform_item_id, listing_id)。
    """
    sku_id, item_id = add_sku(engine, world.tenant_id), new_item_id()
    listing_id = add_listing(engine, world, sku_id, item_id)
    set_allocation(engine, world, sku_id, quota=quota, **values)
    return sku_id, item_id, listing_id


def lock_name(engine: Engine, world: PlatformWorld, order_id: str) -> str:
    return order_lock_name(engine.url.database, world.tenant_id, world.channel_id, order_id)


# --- 新订单（例子第 1 种的前一半、§3.1 例子一第 8 步） ---


def test_a_new_order_takes_quota_and_adds_pending(engine, world):
    other_channel_id = add_platform_channel(engine, world)
    sku_id, item_id, _ = mapped_sku(engine, world, quota=10)
    order_id = new_order_id()

    assert record(engine, world, order_id, created(line("L-1", item_id, 2)))
    [event] = events_of(engine, world, order_id)
    assert event["event_row_id"].startswith("pev_")
    assert event["credential_id"] == world.system_credential_id
    assert (event["source"], event["platform_line_id"]) == ("webhook", "")
    assert event["received_at"] is not None
    assert (event["applied_at"], event["result"]) == (None, None)

    assert process(engine, world, order_id) == RoundResult(1)

    order = platform_order(engine, world, order_id)
    assert order["platform_order_row_id"].startswith("pfo_")
    assert order["order_ref"] == order_ref("shopee", world.platform_shop_id, order_id)
    assert order["stage"] == "unpaid"
    assert (order["attention"], order["attention_code"]) == (None, None)
    [row] = order_lines(engine, world, order_id).values()
    assert (row["sku_id"], row["quantity"], row["quota_taken"]) == (sku_id, 2, 2)
    assert (row["platform_item_id"], row["platform_model_id"]) == (item_id, "-")
    assert (row["cancelled_qty"], row["quota_returned"]) == (0, 0)
    assert amounts(engine, world, sku_id) == (8, 2, None)

    [event] = events_of(engine, world, order_id)
    assert event["applied_at"] is not None
    assert (event["result"], event["result_code"]) == ("applied", None)
    # 第 3 步给这个池的全部 platform 入口预建了配额行，取默认值。
    other = allocation(engine, world, sku_id, other_channel_id)
    assert (other["quota"], other["pending"]) == (0, 0)


@pytest.mark.parametrize(("manual_before", "manual_after"), [(20, 4), (3, 0), (None, None)])
def test_an_oversold_order_takes_quota_down_to_zero(engine, world, manual_before, manual_after):
    sku_id, item_id, _ = mapped_sku(
        engine, world, quota=14, pending=3, manual_confirmed_quota=manual_before
    )
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 16)))

    assert process(engine, world, order_id) == RoundResult(1)
    [row] = order_lines(engine, world, order_id).values()
    assert row["quota_taken"] == 14
    assert amounts(engine, world, sku_id) == (0, 19, manual_after)
    confirmed_at = allocation(engine, world, sku_id)["manual_confirmed_at"]
    assert (confirmed_at is None) == (manual_after is None)


def test_lines_of_the_same_sku_are_taken_in_line_order(engine, world):
    sku_id, item_id, _ = mapped_sku(engine, world, quota=5, manual_confirmed_quota=5)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-2", item_id, 3), line("L-1", item_id, 4)))

    assert process(engine, world, order_id) == RoundResult(1)
    lines = order_lines(engine, world, order_id)
    # 按 platform_line_id 升序：L-1 先扣走 4，L-2 只剩 1 可扣。
    assert (lines["L-1"]["quota_taken"], lines["L-2"]["quota_taken"]) == (4, 1)
    assert amounts(engine, world, sku_id) == (0, 7, 0)


def test_a_line_without_an_active_listing_holds_the_order(engine, world):
    sku_id, item_id, _ = mapped_sku(engine, world, quota=10)
    inactive_sku = add_sku(engine, world.tenant_id)
    inactive_item = new_item_id()
    add_listing(engine, world, inactive_sku, inactive_item, status="inactive")
    set_allocation(engine, world, inactive_sku, quota=7)
    order_id = new_order_id()
    payload = created(
        line("L-1", item_id, 2),
        line("L-2", new_item_id(), 3),
        line("L-3", inactive_item, 1),
    )
    record(engine, world, order_id, payload)

    assert process(engine, world, order_id) == RoundResult(1)
    order = platform_order(engine, world, order_id)
    assert order["stage"] == "held"
    assert (order["attention"], order["attention_code"]) == ("unmapped", None)
    lines = order_lines(engine, world, order_id)
    assert (lines["L-1"]["sku_id"], lines["L-1"]["quota_taken"]) == (sku_id, 2)
    for unmapped in ("L-2", "L-3"):
        assert (lines[unmapped]["sku_id"], lines[unmapped]["quota_taken"]) == (None, 0)
    # 找得到 SKU 的行照常扣配额、计 pending；找不到的不计。
    assert amounts(engine, world, sku_id) == (8, 2, None)
    assert amounts(engine, world, inactive_sku) == (7, 0, None)
    [event] = events_of(engine, world, order_id)
    assert (event["result"], event["result_code"]) == ("held", "unmapped")


# --- 去重与落后于阶段（例子第 2、3 种） ---


def test_a_duplicate_delivery_is_one_row_and_is_processed_once(engine, world):
    sku_id, item_id, _ = mapped_sku(engine, world, quota=10)
    order_id = new_order_id()
    payload = created(line("L-1", item_id, 2))
    assert record(engine, world, order_id, payload, event_key="evt-1")
    assert record(engine, world, order_id, payload, event_key="evt-1")
    [first] = events_of(engine, world, order_id)

    assert process(engine, world, order_id) == RoundResult(1)
    after = state(engine, world)
    [processed] = events_of(engine, world, order_id)

    # 处理之后再送达一次、再处理一轮：什么也不改。
    assert record(engine, world, order_id, payload, event_key="evt-1")
    assert process(engine, world, order_id) == RoundResult(0)
    assert events_of(engine, world, order_id) == [processed]
    assert processed["event_row_id"] == first["event_row_id"]
    assert state(engine, world) == after
    assert amounts(engine, world, sku_id) == (8, 2, None)


def test_an_event_applied_meanwhile_commits_an_empty_transaction(engine, world, monkeypatch):
    _, item_id, _ = mapped_sku(engine, world, quota=10)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    [event] = events_of(engine, world, order_id)
    before = state(engine, world)

    def apply_elsewhere(connection):
        # 第 2 步读到它之后、第 4 步加锁之前，它已被处理过。
        with engine.begin() as db:
            db.execute(
                update(PlatformEvent)
                .where(PlatformEvent.event_row_id == event["event_row_id"])
                .values(applied_at=text("CURRENT_TIMESTAMP(6)"), result="applied")
            )

    monkeypatch.setattr(platform_events, "_between_prepare_and_lock", apply_elsewhere)
    assert process(engine, world, order_id) == RoundResult(0)
    assert state(engine, world) == before
    assert platform_order(engine, world, order_id) is None


def test_the_same_new_order_from_a_pull_is_ignored(engine, world):
    sku_id, item_id, _ = mapped_sku(engine, world, quota=10)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)), event_key="evt-1")
    assert process(engine, world, order_id) == RoundResult(1)
    before = state(engine, world)

    pulled = created(line("L-1", item_id, 2), platform_time=T2)
    assert record(engine, world, order_id, pulled, event_key=T2, source="pull")
    assert process(engine, world, order_id) == RoundResult(1)

    event = event_by_key(engine, world, T2)
    assert event["source"] == "pull"
    assert event["applied_at"] is not None
    assert (event["result"], event["result_code"]) == ("ignored", None)
    assert state(engine, world) == before
    assert amounts(engine, world, sku_id) == (8, 2, None)


# --- 写收件箱 ---


def test_the_event_key_carries_the_document_id_and_is_hashed_when_too_long(engine, world):
    order_id = new_order_id()

    def shipped(package_id: str) -> dict:
        return {
            "lines": [{"platform_line_id": "L-1", "quantity": 1}],
            "package_id": package_id,
            "platform_time": T1,
        }

    def hashed(value: str) -> str:
        return "h" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    # 补拉用状态时间作 event_key：同一时刻的两个包裹不撞去重键。
    for package in ("PKG-1", "PKG-2"):
        assert record(engine, world, order_id, shipped(package), action="shipped", event_key=T1)
    cancelled = {
        "refund_id": "RF-1",
        "lines": [{"platform_line_id": "L-1", "cancelled_total": 1, "quantity": 1}],
    }
    assert record(engine, world, order_id, cancelled, action="cancelled", event_key=T1)
    long_key = "k" * 125
    payload = shipped("PKG-1")
    assert record(engine, world, order_id, payload, action="shipped", event_key=long_key)
    exact = "x" * 128
    assert record(engine, world, order_id, {}, action="ready_to_ship", event_key=exact)
    too_long = "y" * 129
    assert record(engine, world, order_id, {}, action="return_requested", event_key=too_long)

    keys = {event["event_key"] for event in events_of(engine, world, order_id)}
    assert keys == {
        f"{T1}|PKG-1",
        f"{T1}|PKG-2",
        f"{T1}|RF-1",
        hashed(f"{long_key}|PKG-1"),
        exact,
        hashed(too_long),
    }


def test_a_manual_connection_is_not_written(engine, world):
    with engine.begin() as db:
        db.execute(
            update(PlatformConnection)
            .where(PlatformConnection.channel_id == world.channel_id)
            .values(mode="manual")
        )
    _, item_id, _ = mapped_sku(engine, world)

    assert not record(engine, world, new_order_id(), created(line("L-1", item_id, 1)))
    assert events_of(engine, world) == []


@pytest.mark.parametrize("case", ["backoffice", "inactive", "missing", "foreign"])
def test_only_this_tenants_active_platform_channels_are_written(engine, world, case):
    order_id = new_order_id()
    payload = created(line("L-1", new_item_id(), 1))
    channel_id = world.channel_id
    other = None
    if case == "backoffice":
        channel_id = world.backoffice_channel_id
    elif case == "inactive":
        with engine.begin() as db:
            db.execute(
                update(SalesChannel)
                .where(SalesChannel.channel_id == world.channel_id)
                .values(status="inactive")
            )
    elif case == "missing":
        channel_id = "chan_missing"
    else:
        other = create_world(engine, platform_shop_id="shop-9")
        channel_id = other.channel_id
    try:
        assert not record(engine, world, order_id, payload, channel_id=channel_id)
        assert events_of(engine, world) == []
        if other is not None:
            assert events_of(engine, other) == []
    finally:
        if other is not None:
            purge_tenant(engine, other.tenant_id)


RETURN_LINE = {"platform_line_id": "L-1", "sellable_quantity": 1, "unsellable_quantity": 0}
BAD_ARGUMENTS = {
    "manual_source": {"source": "manual"},
    "unknown_source": {"source": "email"},
    "return_received": {"action": "return_received", "payload": {"lines": [RETURN_LINE]}},
    "bad_payload": {"payload": {"lines": [], "platform_time": T1}},
    "unknown_action": {"action": "refunded", "payload": {}},
    "created_with_line": {"platform_line_id": "L-1"},
    "empty_order_id": {"order_id": ""},
    "long_order_id": {"order_id": "9" * 129},
    "empty_event_key": {"event_key": ""},
}


@pytest.mark.parametrize("case", sorted(BAD_ARGUMENTS))
def test_bad_arguments_raise_and_write_nothing(engine, world, case):
    arguments = {
        "order_id": new_order_id(),
        "platform_line_id": "",
        "action": "created",
        "event_key": "evt-1",
        "source": "webhook",
        "payload": created(line("L-1", new_item_id(), 1)),
    }
    arguments.update(BAD_ARGUMENTS[case])
    with pytest.raises(ValueError):
        record_auto_event(
            engine,
            world.tenant_id,
            world.channel_id,
            arguments["order_id"],
            arguments["platform_line_id"],
            arguments["action"],
            arguments["event_key"],
            arguments["source"],
            arguments["payload"],
        )
    assert events_of(engine, world) == []


# --- 找有未处理事件的订单 ---


def test_orders_with_pending_events_come_oldest_first(engine, world):
    _, item_id, _ = mapped_sku(engine, world)
    first, second = new_order_id(), new_order_id()
    for order_id in (first, second):
        record(engine, world, order_id, created(line("L-1", item_id, 1)))
        record(engine, world, order_id, {"platform_time": T2}, action="return_requested")
    # 最早未处理事件的 received_at：second 早于 first。
    times = {first: "2026-11-27 08:00:02", second: "2026-11-27 08:00:01"}
    with engine.begin() as db:
        for order_id, received_at in times.items():
            db.execute(
                update(PlatformEvent)
                .where(
                    PlatformEvent.tenant_id == world.tenant_id,
                    PlatformEvent.platform_order_id == order_id,
                )
                .values(received_at=received_at)
            )

    def ours() -> list[PendingOrder]:
        found = orders_with_pending_events(engine, 1000)
        return [order for order in found if order.tenant_id == world.tenant_id]

    assert ours() == [
        PendingOrder(world.tenant_id, world.channel_id, second),
        PendingOrder(world.tenant_id, world.channel_id, first),
    ]
    assert len(orders_with_pending_events(engine, 1)) == 1
    with pytest.raises(ValueError):
        orders_with_pending_events(engine, 0)

    assert process(engine, world, second) == RoundResult(2)
    assert ours() == [PendingOrder(world.tenant_id, world.channel_id, first)]


# --- 锁、顺序与出错 ---


def test_a_held_order_lock_means_busy_and_changes_nothing(engine, world, monkeypatch):
    monkeypatch.setattr(platform_events, "ORDER_LOCK_WAIT_SECONDS", 0)
    sku_id, item_id, _ = mapped_sku(engine, world, quota=10)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    before = state(engine, world), events_of(engine, world)

    with engine.connect() as holder:
        name = lock_name(engine, world, order_id)
        assert holder.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": name}).scalar() == 1
        try:
            assert process(engine, world, order_id) == RoundResult(0, None, LOCK_BUSY)
            assert (state(engine, world), events_of(engine, world)) == before
        finally:
            holder.execute(text("SELECT RELEASE_ALL_LOCKS()"))
            holder.rollback()

    assert process(engine, world, order_id) == RoundResult(1)
    assert amounts(engine, world, sku_id) == (8, 2, None)


def test_events_are_processed_by_platform_time(engine, world):
    sku_id, item_id, _ = mapped_sku(engine, world, quota=10)
    order_id = new_order_id()
    # 平台时间晚的先到。
    later = created(line("L-1", item_id, 3), platform_time=T2)
    earlier = created(line("L-1", item_id, 2), platform_time=T1)
    record(engine, world, order_id, later, event_key="evt-later")
    record(engine, world, order_id, earlier, event_key=T1, source="pull")

    assert process(engine, world, order_id) == RoundResult(2)
    assert event_by_key(engine, world, T1)["result"] == "applied"
    assert event_by_key(engine, world, "evt-later")["result"] == "ignored"
    [row] = order_lines(engine, world, order_id).values()
    assert row["quantity"] == 2
    assert amounts(engine, world, sku_id) == (8, 2, None)


def test_every_action_of_an_order_is_handled_in_one_round(engine, world):
    sku_id, item_id, _ = mapped_sku(engine, world, quota=10, stock_balance=10)
    order_id = new_order_id()
    rts, rr = {"platform_time": T2}, {"platform_time": T3}
    ship = {
        "lines": [{"platform_line_id": "L-1", "quantity": 2}],
        "package_id": "PKG-1",
        "platform_time": T3,
    }
    record(engine, world, order_id, created(line("L-1", item_id, 2)), event_key="evt-created")
    record(engine, world, order_id, rts, action="ready_to_ship", event_key="rts")
    record(engine, world, order_id, ship, action="shipped", event_key="ship")
    record(engine, world, order_id, rr, action="return_requested", event_key="rr")

    # 交运在已确认之后调发货（CORE-TASK-089），这一轮不再停在它这里。
    assert process(engine, world, order_id) == RoundResult(4)
    events = {event["event_key"]: event for event in events_of(engine, world, order_id)}
    results = {key: (event["result"], event["result_code"]) for key, event in events.items()}
    assert results == {
        "evt-created": ("applied", None),
        "rts": ("applied", None),
        "ship|PKG-1": ("applied", None),
        "rr": ("ignored", None),
    }
    assert platform_order(engine, world, order_id)["stage"] == "committed"
    assert amounts(engine, world, sku_id) == (8, 0, None)

    # 再处理一轮没有事件，什么也不改。
    after = state(engine, world)
    assert process(engine, world, order_id) == RoundResult(0)
    assert state(engine, world) == after


def test_a_database_error_rolls_the_event_back(engine, world, monkeypatch):
    sku_id, item_id, _ = mapped_sku(engine, world, quota=10)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    [event] = events_of(engine, world, order_id)
    real = platform_events._insert_order

    def failing(connection, *args, **kwargs):
        real(connection, *args, **kwargs)
        connection.execute(text("SELECT no_such_column FROM platform_orders"))

    monkeypatch.setattr(platform_events, "_insert_order", failing)
    assert process(engine, world, order_id) == RoundResult(0, event["event_row_id"], FAILED)
    assert platform_order(engine, world, order_id) is None
    assert amounts(engine, world, sku_id) == (10, 0, None)
    assert events_of(engine, world, order_id) == [event]

    monkeypatch.undo()
    assert process(engine, world, order_id) == RoundResult(1)
    assert amounts(engine, world, sku_id) == (8, 2, None)


# --- 第 3、4 步之间改挂 ---


def _relink(engine: Engine, listing_id: str, sku_id: str) -> None:
    with engine.begin() as db:
        db.execute(
            update(SkuListing).where(SkuListing.listing_id == listing_id).values(sku_id=sku_id)
        )


def test_a_relink_before_the_lock_is_redone_with_the_new_sku(engine, world, monkeypatch):
    first, item_id, listing_id = mapped_sku(engine, world, quota=10)
    second = add_sku(engine, world.tenant_id)
    set_allocation(engine, world, second, quota=6)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    calls = []

    def relink_once(connection):
        calls.append(None)
        if len(calls) == 1:
            _relink(engine, listing_id, second)

    monkeypatch.setattr(platform_events, "_between_prepare_and_lock", relink_once)
    assert process(engine, world, order_id) == RoundResult(1)
    assert len(calls) == 2
    [row] = order_lines(engine, world, order_id).values()
    assert (row["sku_id"], row["quota_taken"]) == (second, 2)
    assert amounts(engine, world, second) == (4, 2, None)
    assert amounts(engine, world, first) == (10, 0, None)


def test_two_relinks_in_a_row_stop_the_round(engine, world, monkeypatch):
    first, item_id, listing_id = mapped_sku(engine, world, quota=10)
    second = add_sku(engine, world.tenant_id)
    set_allocation(engine, world, second, quota=6)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    [event] = events_of(engine, world, order_id)
    before = state(engine, world)
    calls = []

    def relink_each_time(connection):
        calls.append(None)
        _relink(engine, listing_id, second if len(calls) % 2 else first)

    monkeypatch.setattr(platform_events, "_between_prepare_and_lock", relink_each_time)
    result = process(engine, world, order_id)
    assert result == RoundResult(0, event["event_row_id"], LISTING_CHANGED)
    assert len(calls) == 2
    assert state(engine, world) == before
    assert events_of(engine, world, order_id) == [event]


# --- 一轮结束之后 ---


def test_a_round_resets_isolation_and_releases_the_lock(engine, world, monkeypatch):
    _, item_id, _ = mapped_sku(engine, world, quota=10)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    name = lock_name(engine, world, order_id)
    seen = []

    def observe(connection):
        statement = text(
            "SELECT @@SESSION.transaction_isolation, IS_USED_LOCK(:name) = CONNECTION_ID()"
        )
        seen.append(tuple(connection.execute(statement, {"name": name}).one()))
        connection.rollback()

    monkeypatch.setattr(platform_events, "_between_prepare_and_lock", observe)
    # 只有一个连接的连接池：一轮结束后取回的就是这一轮用过的连接。
    single = create_engine(
        TEST_DATABASE_URL,
        pool_size=1,
        max_overflow=0,
        connect_args={"init_command": "SET time_zone = '+00:00'"},
    )
    try:
        result = process_order(
            single, world.tenant_id, world.channel_id, order_id, reservation_ttl_seconds=TTL
        )
        assert result == RoundResult(1)
        assert seen == [("READ-COMMITTED", 1)]
        with single.connect() as db:
            session, default = db.execute(
                text("SELECT @@SESSION.transaction_isolation, @@GLOBAL.transaction_isolation")
            ).one()
            assert session == default
            used = db.execute(text("SELECT IS_USED_LOCK(:name)"), {"name": name}).scalar()
            assert used is None
    finally:
        single.dispose()
