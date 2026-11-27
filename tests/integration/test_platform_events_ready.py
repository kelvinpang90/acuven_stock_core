"""平台事件执行器：进入待发货（契约 §5.10 派生的命令正文、第 3–6 步、命令体共用，§3.1）。

§5.10 例子第 1、3、4、6 种，§3.1 例子一第 9、11 步，派生正文的摘要与 INVALID_REQUEST，
命令撞上业务引用（BusinessRefTaken）、按业务引用返回原结果与业务引用冲突的缺陷标记。

在真 MySQL 上跑。每个测试用 platform_fixtures 建自己的租户，
结束时在 finally 里 purge_tenant 删掉它的全部行。
"""

from collections.abc import Iterator
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select, text, update

from app.api.errors import ApiError
from app.domain.idempotency import request_digest
from app.domain.platform_events import order_ref
from app.models.inventory import LedgerEntry, StockBalance
from app.models.orders import OrderLine, Reservation
from app.models.platform import (
    ChannelAllocation,
    PlatformEvent,
    PlatformOrder,
    PlatformOrderLine,
    PoolAllocation,
)
from app.models.service_auth import SecurityEvent
from app.services import platform_commands, platform_events
from app.services.idempotency import BusinessRefTaken
from app.services.platform_events import FAILED, RoundResult, process_order, record_auto_event
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
T2_AT = datetime(2026, 11, 27, 9, 0)


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


def ready(platform_time: str | None = T2) -> dict:
    return {} if platform_time is None else {"platform_time": platform_time}


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
) -> str:
    """写一个事件进收件箱，返回传入的 event_key。"""
    event_key = event_key or f"evt-{uuid4().hex}"
    assert record_auto_event(
        engine,
        world.tenant_id,
        channel_id or world.channel_id,
        order_id,
        "",
        action,
        event_key,
        source,
        payload,
    )
    return event_key


def process(
    engine: Engine, world: PlatformWorld, order_id: str, channel_id: str | None = None
) -> RoundResult:
    return process_order(
        engine,
        world.tenant_id,
        channel_id or world.channel_id,
        order_id,
        reservation_ttl_seconds=TTL,
    )


def _rows(engine: Engine, model, *criteria) -> list[dict]:
    table = model.__table__
    statement = select(*table.columns).where(*criteria).order_by(*table.primary_key.columns)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def event_by_key(engine: Engine, world: PlatformWorld, event_key: str) -> dict:
    [event] = _rows(
        engine,
        PlatformEvent,
        PlatformEvent.tenant_id == world.tenant_id,
        PlatformEvent.event_key == event_key,
    )
    return event


def result_of(engine: Engine, world: PlatformWorld, event_key: str) -> tuple:
    event = event_by_key(engine, world, event_key)
    return event["result"], event["result_code"]


def platform_order(engine: Engine, world: PlatformWorld, order_id: str) -> dict:
    [row] = _rows(
        engine,
        PlatformOrder,
        PlatformOrder.tenant_id == world.tenant_id,
        PlatformOrder.platform_order_id == order_id,
    )
    return row


def attention_of(engine: Engine, world: PlatformWorld, order_id: str) -> tuple:
    order = platform_order(engine, world, order_id)
    return order["stage"], order["attention"], order["attention_code"]


def order_lines(engine: Engine, world: PlatformWorld, order_id: str) -> dict[str, dict]:
    order = platform_order(engine, world, order_id)
    rows = _rows(
        engine,
        PlatformOrderLine,
        PlatformOrderLine.tenant_id == world.tenant_id,
        PlatformOrderLine.platform_order_row_id == order["platform_order_row_id"],
    )
    return {row["platform_line_id"]: row for row in rows}


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


def balance(engine: Engine, world: PlatformWorld, sku_id: str) -> tuple:
    """stock_balance / reserved / unavailable / paid_pending_dispatch。"""
    [row] = _rows(
        engine,
        StockBalance,
        StockBalance.tenant_id == world.tenant_id,
        StockBalance.sku_id == sku_id,
    )
    return (
        row["stock_balance"],
        row["reserved"],
        row["unavailable"],
        row["paid_pending_dispatch"],
    )


def set_balance(engine: Engine, world: PlatformWorld, sku_id: str, **values) -> None:
    """直接改余额行的量（不写流水），造例子里某一步之前的账面。"""
    with engine.begin() as db:
        db.execute(
            update(StockBalance)
            .where(StockBalance.tenant_id == world.tenant_id, StockBalance.sku_id == sku_id)
            .values(**values)
        )


def unallocated_of(engine: Engine, world: PlatformWorld, sku_id: str) -> int:
    """F = available − S − ΣQ − Σpending（全部 platform 入口）。"""
    stock, reserved, unavailable, _ = balance(engine, world, sku_id)
    with engine.connect() as db:
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
    return stock - reserved - unavailable - safety - int(total)


def reservations_of(engine: Engine, world: PlatformWorld) -> list[dict]:
    return _rows(engine, Reservation, Reservation.tenant_id == world.tenant_id)


def core_lines_of(engine: Engine, world: PlatformWorld) -> list[dict]:
    return _rows(engine, OrderLine, OrderLine.tenant_id == world.tenant_id)


def ledger_of(engine: Engine, world: PlatformWorld) -> list[dict]:
    rows = _rows(engine, LedgerEntry, LedgerEntry.tenant_id == world.tenant_id)
    return sorted(rows, key=lambda row: (row["sku_id"], row["balance_version_after"]))


def security_events_of(engine: Engine, world: PlatformWorld) -> list[dict]:
    return _rows(engine, SecurityEvent, SecurityEvent.tenant_id == world.tenant_id)


def everything(engine: Engine, world: PlatformWorld) -> tuple:
    """这个租户的平台订单、订单行、配额行、余额行、预留、Core 订单行与流水。"""
    tenant_id = world.tenant_id
    models = (
        PlatformOrder,
        PlatformOrderLine,
        ChannelAllocation,
        PoolAllocation,
        StockBalance,
        Reservation,
        OrderLine,
        LedgerEntry,
    )
    return tuple(_rows(engine, model, model.tenant_id == tenant_id) for model in models)


def mapped_sku(
    engine: Engine, world: PlatformWorld, channel_id: str | None = None, **values
) -> tuple[str, str]:
    """一个 SKU、挂在这个入口（默认 world 的）上的平台商品与配额行；
    返回 (sku_id, platform_item_id)。
    """
    sku_id, item_id = add_sku(engine, world.tenant_id), new_item_id()
    add_listing(engine, world, sku_id, item_id, channel_id=channel_id)
    set_allocation(engine, world, sku_id, channel_id=channel_id, **values)
    return sku_id, item_id


def new_order(engine: Engine, world: PlatformWorld, item_id: str, quantity: int = 2) -> str:
    """一个处理过 created 的新订单（一行），返回平台订单号。"""
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, quantity)))
    assert process(engine, world, order_id) == RoundResult(1)
    return order_id


# --- §5.10 例子第 1、3 种 ---


def test_example_one_created_then_ready_to_ship_reserves_and_commits(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = new_order_id()

    # 第一笔：Q 10 → 8、pending 0 → 2。
    record(engine, world, order_id, created(line("L-1", item_id, 2)))
    assert process(engine, world, order_id) == RoundResult(1)
    assert amounts(engine, world, sku_id) == (8, 2)
    assert balance(engine, world, sku_id) == (20, 0, 0, 0)

    # 第二笔：预留并确认 2 件，pending 2 → 0，阶段 committed。
    key = record(engine, world, order_id, ready())
    rts_id = event_by_key(engine, world, key)["event_row_id"]
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("applied", None)
    assert amounts(engine, world, sku_id) == (8, 0)
    assert attention_of(engine, world, order_id) == ("committed", None, None)
    assert platform_order(engine, world, order_id)["ready_at"] == T2_AT
    assert order_lines(engine, world, order_id)["L-1"]["line_ref"] == "L1"

    # 账面、预留与订单行同 /v1 的预留加确认销售。
    assert balance(engine, world, sku_id) == (18, 0, 0, 2)
    ref = order_ref("shopee", world.platform_shop_id, order_id)
    [reservation] = reservations_of(engine, world)
    assert (reservation["order_ref"], reservation["status"]) == (ref, "committed")
    assert (reservation["channel_id"], reservation["pool_id"]) == (world.channel_id, world.pool_id)
    assert (reservation["payment_ref"], reservation["paid_at"]) == (f"{ref}:c", T2_AT)
    assert (reservation["late"], reservation["fault_mode"]) == (False, False)
    assert reservation["ttl_seconds"] == TTL
    for prefix in ("", "commit_"):
        assert reservation[f"{prefix}credential_id"] == world.system_credential_id
        assert reservation[f"{prefix}actor_ref"] is None
        assert reservation[f"{prefix}actor_role"] is None
        assert reservation[f"{prefix}request_id"] == rts_id
    [core_line] = core_lines_of(engine, world)
    assert (core_line["line_ref"], core_line["sku_id"]) == ("L1", sku_id)
    assert (core_line["quantity"], core_line["committed_qty"]) == (2, 2)

    entries = ledger_of(engine, world)
    assert [entry["entry_type"] for entry in entries] == ["RESERVE", "SALE_COMMIT"]
    for entry in entries:
        assert entry["credential_id"] == world.system_credential_id
        assert (entry["actor_ref"], entry["actor_role"]) == (None, None)
        assert entry["idempotency_key"] is None
        assert entry["request_id"] == rts_id
        assert entry["channel_id"] == world.channel_id
        assert (entry["source_ref"], entry["line_ref"]) == (ref, "L1")
    assert entries[1]["occurred_at"] == T2_AT
    assert security_events_of(engine, world) == []


def test_example_three_the_same_ready_to_ship_from_a_pull_is_ignored(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = new_order(engine, world, item_id)
    record(engine, world, order_id, ready())
    assert process(engine, world, order_id) == RoundResult(1)
    before = everything(engine, world)

    pulled = record(engine, world, order_id, ready(T3), event_key=T3, source="pull")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, pulled) == ("ignored", None)
    assert everything(engine, world) == before
    assert amounts(engine, world, sku_id) == (8, 0)


# --- §5.10 例子第 6 种、§3.1 例子一第 9、11 步 ---


def test_example_six_an_oversold_order_is_held_and_nothing_is_booked(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=1)
    order_id = new_order(engine, world, item_id)
    key = record(engine, world, order_id, ready())

    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("held", "INSUFFICIENT_STOCK")
    assert balance(engine, world, sku_id) == (1, 0, 0, 0)
    assert reservations_of(engine, world) == []
    assert ledger_of(engine, world) == []
    assert attention_of(engine, world, order_id) == ("held", "core_rejected", "INSUFFICIENT_STOCK")
    assert amounts(engine, world, sku_id) == (8, 2)
    order = platform_order(engine, world, order_id)
    assert order["ready_at"] == T2_AT
    assert order_lines(engine, world, order_id)["L-1"]["line_ref"] is None

    # 待发货只生效一次：同一动作从补拉再到，订单已在待处理、ready_at 已记过，记 ignored。
    before = everything(engine, world)
    pulled = record(engine, world, order_id, ready(T3), event_key=T3, source="pull")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, pulled) == ("ignored", None)
    assert everything(engine, world) == before


def test_example_one_step_eleven_order_e_is_held(engine, world):
    tts = add_platform_channel(engine, world)
    sku_id, item_id = mapped_sku(engine, world, channel_id=tts, quota=4, stock_balance=6)
    set_allocation(engine, world, sku_id, quota=0, safety_stock=2)
    # 第 10 步之后：6 / 0 / 4 / 17 / 2。
    set_balance(engine, world, sku_id, unavailable=4, paid_pending_dispatch=17)
    order_id = new_order_id()

    # TikTok 新订单 E，3 件，已付款、直接待发货。
    record(engine, world, order_id, created(line("L-1", item_id, 3)), channel_id=tts)
    key = record(engine, world, order_id, ready(), channel_id=tts)
    assert process(engine, world, order_id, tts) == RoundResult(2)

    assert result_of(engine, world, key) == ("held", "INSUFFICIENT_STOCK")
    assert balance(engine, world, sku_id) == (6, 0, 4, 17)
    assert amounts(engine, world, sku_id, tts) == (1, 3)
    assert amounts(engine, world, sku_id) == (0, 0)
    assert unallocated_of(engine, world, sku_id) == -4
    assert attention_of(engine, world, order_id) == ("held", "core_rejected", "INSUFFICIENT_STOCK")
    assert reservations_of(engine, world) == []


def test_example_one_step_nine_reserving_looks_only_at_available(engine, world):
    tts = add_platform_channel(engine, world)
    sku_id, item_id = mapped_sku(engine, world, quota=14, stock_balance=22, safety_stock=2)
    set_allocation(engine, world, sku_id, quota=4, channel_id=tts)
    # 第 7 步之后：22 / 0 / 0 / 1 / 22。
    set_balance(engine, world, sku_id, paid_pending_dispatch=1)
    order_id = new_order_id()

    # 第 8 步：D 16 件，平台多卖了 2 件，Q 只减到 0。
    record(engine, world, order_id, created(line("L-1", item_id, 16)))
    assert process(engine, world, order_id) == RoundResult(1)
    assert amounts(engine, world, sku_id) == (0, 16)
    assert order_lines(engine, world, order_id)["L-1"]["quota_taken"] == 14
    assert unallocated_of(engine, world, sku_id) == 0

    # 第 9 步：F 为 0、Q 为 0，预留只看 available 22。
    key = record(engine, world, order_id, ready())
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("applied", None)
    assert balance(engine, world, sku_id) == (6, 0, 0, 17)
    assert amounts(engine, world, sku_id) == (0, 0)
    assert amounts(engine, world, sku_id, tts) == (4, 0)
    assert unallocated_of(engine, world, sku_id) == 0
    assert platform_order(engine, world, order_id)["stage"] == "committed"


# --- 订单行的合并与找不到 SKU 的行 ---


def test_lines_of_the_same_sku_are_merged_into_one_core_line(engine, world):
    first, first_item = mapped_sku(engine, world, quota=10, stock_balance=20)
    second, second_item = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = new_order_id()
    payload = created(
        line("L-1", second_item, 1), line("L-2", first_item, 2), line("L-3", second_item, 3)
    )
    record(engine, world, order_id, payload)
    record(engine, world, order_id, ready())
    assert process(engine, world, order_id) == RoundResult(2)

    # 按 sku_id 升序编号。
    refs = {sku: f"L{index}" for index, sku in enumerate(sorted([first, second]), start=1)}
    lines = order_lines(engine, world, order_id)
    assert {line_id: row["line_ref"] for line_id, row in lines.items()} == {
        "L-1": refs[second],
        "L-2": refs[first],
        "L-3": refs[second],
    }
    core = core_lines_of(engine, world)
    assert {row["line_ref"]: (row["sku_id"], row["quantity"]) for row in core} == {
        refs[first]: (first, 2),
        refs[second]: (second, 4),
    }
    assert amounts(engine, world, first) == (8, 0)
    assert amounts(engine, world, second) == (6, 0)


def test_a_held_unmapped_order_records_ready_at_and_stays_held(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = new_order_id()
    payload = created(line("L-1", item_id, 2), line("L-2", new_item_id(), 1))
    record(engine, world, order_id, payload)
    assert process(engine, world, order_id) == RoundResult(1)
    key = record(engine, world, order_id, ready(None))
    received_at = event_by_key(engine, world, key)["received_at"]

    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("held", "unmapped")
    assert attention_of(engine, world, order_id) == ("held", "unmapped", None)
    # 没有平台时间：用 received_at，截到整秒。
    ready_at = platform_order(engine, world, order_id)["ready_at"]
    assert ready_at == received_at.replace(microsecond=0)
    assert amounts(engine, world, sku_id) == (8, 2)
    assert reservations_of(engine, world) == []


# --- 派生的正文与摘要 ---


def test_webhook_and_pull_derive_the_same_bodies_and_digests(engine, world, monkeypatch):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=1)
    order_id = new_order(engine, world, item_id)
    ref = order_ref("shopee", world.platform_shop_id, order_id)
    captured = []
    real = platform_events.reserve_and_commit

    def capture(connection, context, skus, commands):
        sale_commit = commands.sale_commit("res_given")
        captured.append((commands.reservation, sale_commit, context.request_id))
        return real(connection, context, skus, commands)

    monkeypatch.setattr(platform_events, "reserve_and_commit", capture)

    # webhook 先到，可售不够被拒：ready_at 记下 T2、订单进待处理。
    hook = record(engine, world, order_id, ready(T2))
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, hook) == ("held", "INSUFFICIENT_STOCK")

    # 补货之后订单回到可处理（后台的重试在第 3 组；这里直接改阶段），补拉以另一个平台时间再到。
    set_balance(engine, world, sku_id, stock_balance=20)
    with engine.begin() as db:
        db.execute(
            update(PlatformOrder)
            .where(PlatformOrder.tenant_id == world.tenant_id)
            .values(stage="unpaid", attention=None, attention_code=None)
        )
    pulled = record(engine, world, order_id, ready(T3), event_key=T3, source="pull")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, pulled) == ("applied", None)
    assert platform_order(engine, world, order_id)["ready_at"] == T2_AT

    [(reserve_hook, commit_hook, hook_id), (reserve_pull, commit_pull, pull_id)] = captured
    assert hook_id != pull_id
    assert reserve_hook.body == reserve_pull.body
    assert reserve_hook.body == {
        "order_ref": ref,
        "lines": [{"line_ref": "L1", "sku_id": sku_id, "quantity": 2}],
    }
    assert reserve_hook.digest == reserve_pull.digest == request_digest({}, reserve_hook.body)
    assert commit_hook.body == commit_pull.body
    assert commit_hook.body["paid_at"] == "2026-11-27T09:00:00Z"
    assert commit_hook.path == {"order_ref": ref}
    assert commit_hook.digest == commit_pull.digest
    assert commit_hook.digest == request_digest({"order_ref": ref}, commit_hook.body)
    # 预留真正用的是派生正文的摘要。
    [reservation] = reservations_of(engine, world)
    assert reservation["request_hash"] == reserve_pull.digest


def test_more_than_a_hundred_skus_hold_the_order_without_calling_core(engine, world, monkeypatch):
    items = [mapped_sku(engine, world, quota=10, stock_balance=20)[1] for _ in range(101)]
    order_id = new_order_id()
    lines = [line(f"L-{index:03d}", item_id, 1) for index, item_id in enumerate(items)]
    record(engine, world, order_id, created(*lines))
    assert process(engine, world, order_id) == RoundResult(1)

    def pendings() -> list[int]:
        rows = _rows(engine, ChannelAllocation, ChannelAllocation.tenant_id == world.tenant_id)
        return [row["pending"] for row in rows]

    assert pendings() == [1] * 101

    def not_called(*args, **kwargs):
        raise AssertionError("Core must not be called")

    monkeypatch.setattr(platform_events, "reserve_and_commit", not_called)
    key = record(engine, world, order_id, ready())
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("held", "INVALID_REQUEST")
    assert attention_of(engine, world, order_id) == ("held", "invalid_request", "INVALID_REQUEST")
    assert platform_order(engine, world, order_id)["ready_at"] == T2_AT
    assert pendings() == [1] * 101
    assert reservations_of(engine, world) == []


# --- §5.10 例子第 4 种：提交之前失败 ---


def test_example_four_a_failure_before_commit_leaves_nothing(engine, world, monkeypatch):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = new_order(engine, world, item_id)
    key = record(engine, world, order_id, ready())
    event = event_by_key(engine, world, key)
    before = everything(engine, world)
    real = platform_commands.create_sale_commit

    def failing(execution, *args):
        result = real(execution, *args)
        # 预留与确认销售都写了、还没提交：让事务失败。
        execution.connection.execute(text("SELECT no_such_column FROM platform_orders"))
        return result

    monkeypatch.setattr(platform_commands, "create_sale_commit", failing)
    assert process(engine, world, order_id) == RoundResult(0, event["event_row_id"], FAILED)
    assert everything(engine, world) == before
    assert reservations_of(engine, world) == []
    assert ledger_of(engine, world) == []
    assert amounts(engine, world, sku_id) == (8, 2)
    assert platform_order(engine, world, order_id)["ready_at"] is None
    assert event_by_key(engine, world, key) == event

    monkeypatch.undo()
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("applied", None)
    assert amounts(engine, world, sku_id) == (8, 0)
    assert balance(engine, world, sku_id) == (18, 0, 0, 2)
    assert len(reservations_of(engine, world)) == 1


# --- 缺陷：撞上业务引用、按业务引用返回原结果、业务引用冲突 ---


@pytest.mark.parametrize("rejudged", ["reserves", "rejects"])
def test_a_taken_order_reference_is_rejudged_as_a_defect(engine, world, monkeypatch, rejudged):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = new_order(engine, world, item_id)
    key = record(engine, world, order_id, ready())
    real = platform_commands.create_reservation
    executions = []

    def taken(execution, pool_id, order_ref, lines, ttl_seconds):
        executions.append(execution)
        real(execution, pool_id, order_ref, lines, ttl_seconds)

        def rejudge(again):
            executions.append(again)
            if rejudged == "rejects":
                raise ApiError(409, "INSUFFICIENT_STOCK", "not enough available stock")
            # 保存点已回滚了第一次的写入，这一次重新写入。
            return real(again, pool_id, order_ref, lines, ttl_seconds)

        raise BusinessRefTaken(rejudge)

    monkeypatch.setattr(platform_commands, "create_reservation", taken)
    assert process(engine, world, order_id) == RoundResult(1)
    first, second = executions
    assert second is not first
    assert second.request_digest == first.request_digest

    if rejudged == "reserves":
        assert result_of(engine, world, key) == ("applied", "defect")
        assert attention_of(engine, world, order_id) == ("committed", "defect", None)
        [reservation] = reservations_of(engine, world)
        assert reservation["status"] == "committed"
        # 第一次的 RESERVE 随保存点回滚，只剩 rejudge 写的一条与确认销售的一条。
        types = [entry["entry_type"] for entry in ledger_of(engine, world)]
        assert types == ["RESERVE", "SALE_COMMIT"]
        assert [row["committed_qty"] for row in core_lines_of(engine, world)] == [2]
        assert amounts(engine, world, sku_id) == (8, 0)
        assert balance(engine, world, sku_id) == (18, 0, 0, 2)
    else:
        assert result_of(engine, world, key) == ("held", "INSUFFICIENT_STOCK")
        assert attention_of(engine, world, order_id) == ("held", "defect", "INSUFFICIENT_STOCK")
        assert reservations_of(engine, world) == []
        assert ledger_of(engine, world) == []
        assert amounts(engine, world, sku_id) == (8, 2)
        assert balance(engine, world, sku_id) == (20, 0, 0, 0)


def _back_to_unpaid(engine: Engine, world: PlatformWorld, sku_id: str) -> None:
    """造“状态记录有缺陷”：Core 里已有这个订单，平台订单却回到 unpaid、件数回到 pending。"""
    with engine.begin() as db:
        db.execute(
            update(PlatformOrder)
            .where(PlatformOrder.tenant_id == world.tenant_id)
            .values(stage="unpaid", attention="unknown_line")
        )
    set_allocation(engine, world, sku_id, quota=8, pending=2)


def test_an_order_already_in_core_is_answered_by_reference_as_a_defect(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = new_order(engine, world, item_id)
    record(engine, world, order_id, ready())
    assert process(engine, world, order_id) == RoundResult(1)
    booked = (
        reservations_of(engine, world),
        core_lines_of(engine, world),
        ledger_of(engine, world),
    )
    _back_to_unpaid(engine, world, sku_id)

    pulled = record(engine, world, order_id, ready(T3), event_key=T3, source="pull")
    assert process(engine, world, order_id) == RoundResult(1)
    # 预留与确认销售都按业务引用返回原结果：照成功，pending 照减，另标缺陷（覆盖已有的原因）。
    assert result_of(engine, world, pulled) == ("applied", "defect")
    assert attention_of(engine, world, order_id) == ("committed", "defect", None)
    assert amounts(engine, world, sku_id) == (8, 0)
    assert balance(engine, world, sku_id) == (18, 0, 0, 2)
    after = (reservations_of(engine, world), core_lines_of(engine, world), ledger_of(engine, world))
    assert after == booked


def test_an_order_reference_conflict_is_held_as_a_defect(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = new_order(engine, world, item_id, quantity=3)
    record(engine, world, order_id, ready())
    assert process(engine, world, order_id) == RoundResult(1)
    # 平台订单行的剩余件数被改了：派生的预留正文与 Core 里记下的不同。
    with engine.begin() as db:
        db.execute(
            update(PlatformOrderLine)
            .where(PlatformOrderLine.tenant_id == world.tenant_id)
            .values(cancelled_qty=1)
        )
    _back_to_unpaid(engine, world, sku_id)
    before = (
        balance(engine, world, sku_id),
        reservations_of(engine, world),
        ledger_of(engine, world),
    )

    pulled = record(engine, world, order_id, ready(T3), event_key=T3, source="pull")
    pulled_id = event_by_key(engine, world, pulled)["event_row_id"]
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, pulled) == ("held", "ORDER_REFERENCE_CONFLICT")
    assert attention_of(engine, world, order_id) == ("held", "defect", "ORDER_REFERENCE_CONFLICT")
    assert amounts(engine, world, sku_id) == (8, 2)
    after = (
        balance(engine, world, sku_id),
        reservations_of(engine, world),
        ledger_of(engine, world),
    )
    assert after == before
    # 命令登记的安全事件随拒绝保留，在事务提交之后写；HTTP 的三项为空。
    [event] = security_events_of(engine, world)
    assert event["event_type"] == "business_ref_conflict"
    assert (event["request_id"], event["credential_id"]) == (pulled_id, world.system_credential_id)
    assert (event["actor_ref"], event["http_method"], event["path"]) == (None, None, None)
    ref = order_ref("shopee", world.platform_shop_id, order_id)
    assert event["details"] == {"ref_type": "order", "ref": ref}
