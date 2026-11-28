"""平台事件执行器：已确认之后的交运、取消与退货验收（契约 §5.10 第 4、6 步、命令体共用，§3.1）。

§3.1 例子一第 7 步与已确认之后取消的加回，§5.10 例子第 7 种，同一单据换 event_key 再到，
缺单据 ID、缺 refund_id、订单级取消、平台行没有 line_ref，付款前的取消晚到，
退货验收先处理未处理的交运，unpaid 订单收到这三个动作，撞上业务引用与业务引用冲突的缺陷。

在真 MySQL 上跑。每个测试用 platform_fixtures 建自己的租户，
结束时在 finally 里 purge_tenant 删掉它的全部行。
退货验收只由后台登记（manual 来源）：这里直接插入 manual 来源的收件箱行再调 process_order。
"""

import hashlib
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, insert, select, text, update

from app.api.errors import ApiError
from app.domain.ids import new_id
from app.domain.platform_events import order_ref
from app.models.inventory import LedgerEntry, StockBalance
from app.models.orders import OrderDocument, OrderLine, Reservation
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
from app.services.platform_events import (
    MISSING_PRIOR,
    RoundResult,
    process_order,
    record_auto_event,
)
from tests.integration.platform_fixtures import (
    PlatformWorld,
    add_admin,
    add_listing,
    add_sku,
    create_world,
    purge_tenant,
    set_allocation,
)

TTL = 900
T1 = "2026-11-27T08:00:00Z"
T2 = "2026-11-27T09:00:00Z"
T3 = "2026-11-27T10:00:00Z"
T4 = "2026-11-27T11:00:00Z"


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


def created(*lines: dict) -> dict:
    return {"lines": list(lines), "platform_time": T1}


def shipped(package_id: str | None, *pairs: tuple[str, int], platform_time: str = T3) -> dict:
    """各项为 (platform_line_id, 这个包裹里的件数)；package_id 为 None 时不带。"""
    lines = [{"platform_line_id": line_id, "quantity": quantity} for line_id, quantity in pairs]
    payload: dict = {"lines": lines, "platform_time": platform_time}
    if package_id is not None:
        payload["package_id"] = package_id
    return payload


def refunded(refund_id: str, *lines: tuple[str, int, int], platform_time: str = T3) -> dict:
    """各项为 (platform_line_id, cancelled_total, 这次退款的件数)。"""
    return {
        "refund_id": refund_id,
        "lines": [
            {"platform_line_id": line_id, "cancelled_total": total, "quantity": quantity}
            for line_id, total, quantity in lines
        ],
        "platform_time": platform_time,
    }


def returned(return_id: str | None, *lines: tuple[str, int, int], platform_time: str = T4) -> dict:
    """各项为 (platform_line_id, 可售件数, 不可售件数)；return_id 为 None 时不带。"""
    payload: dict = {
        "lines": [
            {
                "platform_line_id": line_id,
                "sellable_quantity": sellable,
                "unsellable_quantity": unsellable,
            }
            for line_id, sellable, unsellable in lines
        ],
        "platform_time": platform_time,
    }
    if return_id is not None:
        payload["return_id"] = return_id
    return payload


def record(
    engine: Engine,
    world: PlatformWorld,
    order_id: str,
    payload: dict,
    *,
    action: str,
    source: str = "webhook",
) -> str:
    """webhook 或补拉写一个事件进收件箱，返回传入的 event_key（存的 event_key 以它开头）。"""
    event_key = f"evt-{uuid4().hex}"
    assert record_auto_event(
        engine, world.tenant_id, world.channel_id, order_id, "", action, event_key, source, payload
    )
    return event_key


def record_return(
    engine: Engine, world: PlatformWorld, order_id: str, payload: dict, credential_id: str
) -> str:
    """后台登记的退货验收：直接插入 manual 来源的收件箱行，返回它的 event_key。"""
    manual_key = f"key-{uuid4().hex}"
    with engine.begin() as db:
        db.execute(
            insert(PlatformEvent).values(
                event_row_id=new_id("pev"),
                tenant_id=world.tenant_id,
                channel_id=world.channel_id,
                platform_order_id=order_id,
                platform_line_id="",
                action="return_received",
                event_key=manual_key,
                source="manual",
                payload=payload,
                manual_key=manual_key,
                request_hash=hashlib.sha256(manual_key.encode()).hexdigest(),
                received_at=text("CURRENT_TIMESTAMP(6)"),
                credential_id=credential_id,
            )
        )
    return manual_key


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
    [event] = _rows(
        engine,
        PlatformEvent,
        PlatformEvent.tenant_id == world.tenant_id,
        PlatformEvent.event_key.startswith(event_key),
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


def line_amounts(engine: Engine, world: PlatformWorld, order_id: str, line_id: str) -> tuple:
    """(cancelled_qty, quota_taken, quota_returned)。"""
    row = order_lines(engine, world, order_id)[line_id]
    return row["cancelled_qty"], row["quota_taken"], row["quota_returned"]


def amounts(engine: Engine, world: PlatformWorld, sku_id: str) -> tuple:
    """world 入口上这个 SKU 的 (quota, pending)。"""
    [row] = _rows(
        engine,
        ChannelAllocation,
        ChannelAllocation.tenant_id == world.tenant_id,
        ChannelAllocation.channel_id == world.channel_id,
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


def core_line(engine: Engine, world: PlatformWorld) -> dict:
    [row] = _rows(engine, OrderLine, OrderLine.tenant_id == world.tenant_id)
    return row


def documents_of(engine: Engine, world: PlatformWorld) -> list[dict]:
    return _rows(engine, OrderDocument, OrderDocument.tenant_id == world.tenant_id)


def ledger_of(engine: Engine, world: PlatformWorld, entry_type: str) -> list[dict]:
    return _rows(
        engine,
        LedgerEntry,
        LedgerEntry.tenant_id == world.tenant_id,
        LedgerEntry.entry_type == entry_type,
    )


def security_events_of(engine: Engine, world: PlatformWorld) -> list[dict]:
    return _rows(engine, SecurityEvent, SecurityEvent.tenant_id == world.tenant_id)


def everything(engine: Engine, world: PlatformWorld) -> tuple:
    """这个租户的平台订单、订单行、配额行、余额行、预留、Core 订单行、单据与流水。"""
    models = (
        PlatformOrder,
        PlatformOrderLine,
        ChannelAllocation,
        PoolAllocation,
        StockBalance,
        Reservation,
        OrderLine,
        OrderDocument,
        LedgerEntry,
    )
    return tuple(_rows(engine, model, model.tenant_id == world.tenant_id) for model in models)


def mapped_sku(engine: Engine, world: PlatformWorld, **values) -> tuple[str, str]:
    """一个 SKU、挂在 world 入口上的平台商品与配额行；返回 (sku_id, platform_item_id)。"""
    sku_id, item_id = add_sku(engine, world.tenant_id), new_item_id()
    add_listing(engine, world, sku_id, item_id)
    set_allocation(engine, world, sku_id, **values)
    return sku_id, item_id


def committed_order(engine: Engine, world: PlatformWorld, *lines: dict) -> str:
    """处理过 created 与 ready_to_ship、已确认的新订单，返回平台订单号。"""
    order_id = new_order_id()
    record(engine, world, order_id, created(*lines), action="created")
    record(engine, world, order_id, {"platform_time": T2}, action="ready_to_ship")
    assert process(engine, world, order_id) == RoundResult(2)
    assert platform_order(engine, world, order_id)["stage"] == "committed"
    return order_id


def no_core(monkeypatch) -> None:
    def not_called(*args, **kwargs):
        raise AssertionError("Core must not be called")

    monkeypatch.setattr(platform_events, "create_order_document", not_called)


# --- §3.1 例子一第 7 步：交运 ---


def test_example_one_step_seven_dispatches_each_package_once(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = committed_order(engine, world, line("L-1", item_id, 3))
    assert balance(engine, world, sku_id) == (17, 0, 0, 3)
    assert amounts(engine, world, sku_id) == (7, 0)
    ref = order_ref("shopee", world.platform_shop_id, order_id)

    # A 交运 2 件：paid_pending_dispatch 3 → 1，Q 与 pending 不变。
    first = record(engine, world, order_id, shipped("PKG-A", ("L-1", 2)), action="shipped")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, first) == ("applied", None)
    assert balance(engine, world, sku_id) == (17, 0, 0, 1)
    assert amounts(engine, world, sku_id) == (7, 0)
    assert core_line(engine, world)["dispatched_qty"] == 2
    assert attention_of(engine, world, order_id) == ("committed", None, None)
    [document] = documents_of(engine, world)
    assert (document["document_type"], document["document_ref"]) == ("dispatch", f"{ref}:d:PKG-A")
    first_id = event_by_key(engine, world, first)["event_row_id"]
    assert (document["credential_id"], document["request_id"]) == (
        world.system_credential_id,
        first_id,
    )
    assert (document["actor_ref"], document["actor_role"]) == (None, None)
    [entry] = ledger_of(engine, world, "DISPATCH")
    assert (entry["source_ref"], entry["line_ref"], entry["request_id"]) == (
        f"{ref}:d:PKG-A",
        "L1",
        first_id,
    )

    # 第二个包裹另生效一次。
    second = record(
        engine, world, order_id, shipped("PKG-B", ("L-1", 1), platform_time=T4), action="shipped"
    )
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, second) == ("applied", None)
    assert balance(engine, world, sku_id) == (17, 0, 0, 0)
    assert core_line(engine, world)["dispatched_qty"] == 3

    # 同一包裹换 event_key 从补拉再到：按业务引用返回原结果，ignored，不改任何量。
    before = everything(engine, world)
    again = record(
        engine,
        world,
        order_id,
        shipped("PKG-A", ("L-1", 2), platform_time=T4),
        action="shipped",
        source="pull",
    )
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, again) == ("ignored", None)
    assert everything(engine, world) == before
    assert security_events_of(engine, world) == []


# --- §5.10 例子第 7 种：包裹件数超过 Core 行的余量 ---


def test_example_seven_a_package_over_the_outstanding_quantity_is_rejected(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = committed_order(engine, world, line("L-1", item_id, 3))
    record(engine, world, order_id, shipped("PKG-A", ("L-1", 1)), action="shipped")
    assert process(engine, world, order_id) == RoundResult(1)
    before = (balance(engine, world, sku_id), core_line(engine, world), documents_of(engine, world))

    over = record(
        engine, world, order_id, shipped("PKG-B", ("L-1", 3), platform_time=T4), action="shipped"
    )
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, over) == ("attention", "QUANTITY_EXCEEDS_OUTSTANDING")
    after = (balance(engine, world, sku_id), core_line(engine, world), documents_of(engine, world))
    assert after == before
    assert attention_of(engine, world, order_id) == (
        "committed",
        "core_rejected",
        "QUANTITY_EXCEEDS_OUTSTANDING",
    )
    assert amounts(engine, world, sku_id) == (7, 0)
    assert len(ledger_of(engine, world, "DISPATCH")) == 1


# --- 已确认之后的取消 ---


def test_a_cancel_after_commit_restocks_and_adds_the_quota_back(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = committed_order(engine, world, line("L-1", item_id, 3))
    assert line_amounts(engine, world, order_id, "L-1") == (0, 3, 0)

    # 取消 1 件未交运的：退款回库，Q 加回 min(1, 3 − 0) = 1。
    key = record(engine, world, order_id, refunded("RF-1", ("L-1", 1, 1)), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("applied", None)
    assert balance(engine, world, sku_id) == (18, 0, 0, 2)
    assert core_line(engine, world)["refund_restocked_qty"] == 1
    assert amounts(engine, world, sku_id) == (8, 0)
    # 已确认之后的取消不记 cancelled_qty。
    assert line_amounts(engine, world, order_id, "L-1") == (0, 3, 1)
    assert attention_of(engine, world, order_id) == ("committed", None, None)
    ref = order_ref("shopee", world.platform_shop_id, order_id)
    [document] = documents_of(engine, world)
    assert (document["document_type"], document["document_ref"]) == (
        "refund_restock",
        f"{ref}:r:RF-1",
    )

    # 同一 refund_id 换 event_key 再到：ignored，quota 不再变。
    before = everything(engine, world)
    again = record(
        engine,
        world,
        order_id,
        refunded("RF-1", ("L-1", 1, 1), platform_time=T4),
        action="cancelled",
        source="pull",
    )
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, again) == ("ignored", None)
    assert everything(engine, world) == before


def test_a_cancel_after_commit_adds_back_at_most_what_was_taken(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=1, stock_balance=20)
    order_id = committed_order(engine, world, line("L-1", item_id, 3))
    assert line_amounts(engine, world, order_id, "L-1") == (0, 1, 0)
    assert amounts(engine, world, sku_id) == (0, 0)

    record(engine, world, order_id, refunded("RF-1", ("L-1", 1, 1)), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert amounts(engine, world, sku_id) == (1, 0)
    assert line_amounts(engine, world, order_id, "L-1") == (0, 1, 1)

    # 第二次退款：min(1, 1 − 1) = 0，Core 照样退款回库。
    key = record(
        engine,
        world,
        order_id,
        refunded("RF-2", ("L-1", 2, 1), platform_time=T4),
        action="cancelled",
    )
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("applied", None)
    assert amounts(engine, world, sku_id) == (1, 0)
    assert line_amounts(engine, world, order_id, "L-1") == (0, 1, 1)
    assert core_line(engine, world)["refund_restocked_qty"] == 2


def test_a_cancel_after_commit_adds_nothing_back_when_the_conditions_fail(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = committed_order(engine, world, line("L-1", item_id, 3))
    with engine.begin() as db:
        db.execute(
            update(ChannelAllocation)
            .where(ChannelAllocation.tenant_id == world.tenant_id)
            .where(ChannelAllocation.sku_id == sku_id)
            .values(
                review_hold=True,
                review_reason="readback mismatch",
                review_since=text("CURRENT_TIMESTAMP(6)"),
            )
        )

    key = record(engine, world, order_id, refunded("RF-1", ("L-1", 1, 1)), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("applied", None)
    # Core 照样退款回库；加回整份不做，件数进 F。
    assert balance(engine, world, sku_id) == (18, 0, 0, 2)
    assert amounts(engine, world, sku_id) == (7, 0)
    assert line_amounts(engine, world, order_id, "L-1") == (0, 3, 0)


@pytest.mark.parametrize("with_refund_id", [True, False])
def test_a_late_cancel_from_before_payment_is_ignored(engine, world, with_refund_id):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 3)), action="created")
    cancel = {"lines": [{"platform_line_id": "L-1", "cancelled_total": 1}], "platform_time": T2}
    record(engine, world, order_id, cancel, action="cancelled")
    record(engine, world, order_id, {"platform_time": T3}, action="ready_to_ship")
    assert process(engine, world, order_id) == RoundResult(3)
    assert platform_order(engine, world, order_id)["stage"] == "committed"
    assert line_amounts(engine, world, order_id, "L-1") == (1, 3, 1)
    before = everything(engine, world)

    # 付款前那次取消从另一个来源晚到：cancelled_total 不大于 cancelled_qty。
    if with_refund_id:
        late = refunded("RF-0", ("L-1", 1, 1), platform_time=T4)
    else:
        late = dict(cancel, platform_time=T4)
    key = record(engine, world, order_id, late, action="cancelled", source="pull")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("ignored", None)
    assert everything(engine, world) == before


# --- 不调 Core 的待处理 ---

PROBLEMS = {
    "shipped_without_package_id": (
        "shipped",
        shipped(None, ("L-1", 1)),
        "missing_platform_id",
    ),
    "cancel_without_refund_id": (
        "cancelled",
        {"lines": [{"platform_line_id": "L-1", "cancelled_total": 1}], "platform_time": T3},
        "refund_id_missing",
    ),
    "order_cancel_without_refund_id": (
        "cancelled",
        {"platform_time": T3},
        "refund_id_missing",
    ),
    "order_cancel_with_refund_id": (
        "cancelled",
        {"refund_id": "RF-1", "platform_time": T3},
        "cancel_lines_missing",
    ),
    "shipped_line_of_another_order": (
        "shipped",
        shipped("PKG-A", ("L-9", 1)),
        "line_not_committed",
    ),
    "refund_of_a_line_without_line_ref": (
        "cancelled",
        # L-1 有新取消，不是晚到的付款前取消；L-2 没有 line_ref。
        refunded("RF-1", ("L-1", 1, 1), ("L-2", 1, 1)),
        "line_not_committed",
    ),
    "shipped_line_without_line_ref": (
        "shipped",
        shipped("PKG-A", ("L-1", 1), ("L-2", 1)),
        "line_not_committed",
    ),
}


@pytest.mark.parametrize("case", sorted(PROBLEMS))
def test_a_problem_after_commit_is_left_for_the_backoffice(engine, world, monkeypatch, case):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = new_order_id()
    # L-2 在付款前整行取消，进入待发货时没有 line_ref。
    record(
        engine,
        world,
        order_id,
        created(line("L-1", item_id, 2), line("L-2", item_id, 1)),
        action="created",
    )
    cancel = {"lines": [{"platform_line_id": "L-2", "cancelled_total": 1}], "platform_time": T1}
    record(engine, world, order_id, cancel, action="cancelled")
    record(engine, world, order_id, {"platform_time": T2}, action="ready_to_ship")
    assert process(engine, world, order_id) == RoundResult(3)
    assert order_lines(engine, world, order_id)["L-2"]["line_ref"] is None
    before = everything(engine, world)[1:]

    no_core(monkeypatch)
    action, payload, reason = PROBLEMS[case]
    key = record(engine, world, order_id, payload, action=action)
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("attention", reason)
    assert attention_of(engine, world, order_id) == ("committed", reason, None)
    # 平台订单之外什么也不改。
    assert everything(engine, world)[1:] == before


def test_a_return_without_return_id_is_left_for_the_backoffice(engine, world, monkeypatch):
    _, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    admin = add_admin(engine, world)
    order_id = committed_order(engine, world, line("L-1", item_id, 2))
    record(engine, world, order_id, shipped("PKG-A", ("L-1", 2)), action="shipped")
    assert process(engine, world, order_id) == RoundResult(1)
    before = everything(engine, world)[1:]

    no_core(monkeypatch)
    key = record_return(engine, world, order_id, returned(None, ("L-1", 1, 0)), admin)
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("attention", "missing_platform_id")
    assert attention_of(engine, world, order_id) == ("committed", "missing_platform_id", None)
    assert everything(engine, world)[1:] == before


def test_a_later_reason_does_not_overwrite_the_attention(engine, world, monkeypatch):
    _, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = committed_order(engine, world, line("L-1", item_id, 2))
    no_core(monkeypatch)
    first = record(engine, world, order_id, shipped(None, ("L-1", 1)), action="shipped")
    second = record(
        engine, world, order_id, {"refund_id": "RF-1", "platform_time": T4}, action="cancelled"
    )
    assert process(engine, world, order_id) == RoundResult(2)
    assert result_of(engine, world, first) == ("attention", "missing_platform_id")
    assert result_of(engine, world, second) == ("attention", "cancel_lines_missing")
    assert attention_of(engine, world, order_id) == ("committed", "missing_platform_id", None)


# --- 退货验收 ---


def test_a_return_receipt_books_what_the_admin_registered(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    admin = add_admin(engine, world)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 3)), action="created")
    record(engine, world, order_id, {"platform_time": T2}, action="ready_to_ship")
    # 退货排在交运之前（平台时间更早）：先处理这一轮还没处理的交运，否则 Core 会拒绝退货。
    package = shipped("PKG-A", ("L-1", 2), platform_time=T4)
    ship = record(engine, world, order_id, package, action="shipped")
    key = record_return(
        engine, world, order_id, returned("RT-1", ("L-1", 1, 1), platform_time=T3), admin
    )

    assert process(engine, world, order_id) == RoundResult(4)
    assert result_of(engine, world, ship) == ("applied", None)
    assert result_of(engine, world, key) == ("applied", None)
    # 交运 2 件：17 / 0 / 0 / 1；退货可售 1、不可售 1：stock_balance +2、unavailable +1。
    assert balance(engine, world, sku_id) == (19, 0, 1, 1)
    row = core_line(engine, world)
    assert (row["returned_sellable_qty"], row["returned_unsellable_qty"]) == (1, 1)
    # Q 与 pending 不变（退货增量进 F）。
    assert amounts(engine, world, sku_id) == (7, 0)
    assert attention_of(engine, world, order_id) == ("committed", None, None)

    # 执行身份为登记它的管理员，process_order 处理时操作者为空。
    ref = order_ref("shopee", world.platform_shop_id, order_id)
    event_row_id = event_by_key(engine, world, key)["event_row_id"]
    documents = documents_of(engine, world)
    [document] = [doc for doc in documents if doc["document_type"] == "return_receipt"]
    assert document["document_ref"] == f"{ref}:x:RT-1"
    assert (document["credential_id"], document["request_id"]) == (admin, event_row_id)
    assert (document["actor_ref"], document["actor_role"]) == (None, None)
    [sellable] = ledger_of(engine, world, "RETURN_SELLABLE")
    [unsellable] = ledger_of(engine, world, "RETURN_UNSELLABLE")
    entries = [sellable, unsellable]
    for entry in entries:
        assert (entry["credential_id"], entry["request_id"]) == (admin, event_row_id)
        assert entry["source_ref"] == f"{ref}:x:RT-1"

    # 同一 return_id 再登记一次：ignored，不改任何量。
    before = everything(engine, world)
    again = record_return(engine, world, order_id, returned("RT-1", ("L-1", 1, 1)), admin)
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, again) == ("ignored", None)
    assert everything(engine, world) == before


# --- unpaid 订单收到这三个动作（CORE-TASK-087 的乱序规则） ---


@pytest.mark.parametrize("action", ["shipped", "return_received"])
def test_shipped_or_returned_on_an_unpaid_order_waits_for_ready_to_ship(engine, world, action):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 2)), action="created")
    assert process(engine, world, order_id) == RoundResult(1)
    if action == "shipped":
        key = record(engine, world, order_id, shipped("PKG-A", ("L-1", 2)), action="shipped")
    else:
        admin = add_admin(engine, world)
        key = record_return(engine, world, order_id, returned("RT-1", ("L-1", 1, 0)), admin)
    event_row_id = event_by_key(engine, world, key)["event_row_id"]

    assert process(engine, world, order_id) == RoundResult(0, event_row_id, MISSING_PRIOR)
    order = platform_order(engine, world, order_id)
    assert order["stage"] == "unpaid"
    assert order["next_pull_at"] is not None
    assert event_by_key(engine, world, key)["applied_at"] is None
    assert amounts(engine, world, sku_id) == (8, 2)
    assert documents_of(engine, world) == []


def test_a_refund_on_an_unpaid_order_is_a_cancel_before_payment(engine, world, monkeypatch):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = new_order_id()
    record(engine, world, order_id, created(line("L-1", item_id, 3)), action="created")
    assert process(engine, world, order_id) == RoundResult(1)

    no_core(monkeypatch)
    key = record(engine, world, order_id, refunded("RF-1", ("L-1", 1, 1)), action="cancelled")
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, key) == ("applied", None)
    assert line_amounts(engine, world, order_id, "L-1") == (1, 3, 1)
    assert amounts(engine, world, sku_id) == (8, 2)
    assert platform_order(engine, world, order_id)["stage"] == "unpaid"
    assert documents_of(engine, world) == []


# --- 缺陷：撞上业务引用、业务引用冲突 ---


@pytest.mark.parametrize("rejudged", ["answers", "rejects"])
def test_a_taken_document_reference_is_rejudged_as_a_defect(engine, world, monkeypatch, rejudged):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = committed_order(engine, world, line("L-1", item_id, 3))
    key = record(engine, world, order_id, refunded("RF-1", ("L-1", 1, 1)), action="cancelled")
    real = platform_commands.create_document
    executions = []

    def taken(execution, *args):
        executions.append(execution)
        real(execution, *args)

        def rejudge(again):
            executions.append(again)
            if rejudged == "rejects":
                raise ApiError(409, "STATE_CONFLICT", "the reservation is not committed")
            # 保存点已回滚第一次的写入；fulfilment 的 rejudge 按业务引用返回原结果。
            result = real(again, *args)
            again.mark_answered_by_reference()
            return result

        raise BusinessRefTaken(rejudge)

    monkeypatch.setattr(platform_commands, "create_document", taken)
    assert process(engine, world, order_id) == RoundResult(1)
    first, second = executions
    assert second is not first
    assert second.request_digest == first.request_digest

    # 不论 rejudge 的结果都不加回配额、不改 pending。
    assert amounts(engine, world, sku_id) == (7, 0)
    assert line_amounts(engine, world, order_id, "L-1") == (0, 3, 0)
    if rejudged == "answers":
        assert result_of(engine, world, key) == ("applied", "defect")
        assert attention_of(engine, world, order_id) == ("committed", "defect", None)
        assert len(documents_of(engine, world)) == 1
    else:
        assert result_of(engine, world, key) == ("attention", "STATE_CONFLICT")
        assert attention_of(engine, world, order_id) == ("committed", "defect", "STATE_CONFLICT")
        assert documents_of(engine, world) == []
        assert balance(engine, world, sku_id) == (17, 0, 0, 3)


def test_a_package_reused_with_other_lines_is_a_defect(engine, world):
    sku_id, item_id = mapped_sku(engine, world, quota=10, stock_balance=20)
    order_id = committed_order(engine, world, line("L-1", item_id, 3))
    record(engine, world, order_id, shipped("PKG-A", ("L-1", 1)), action="shipped")
    assert process(engine, world, order_id) == RoundResult(1)
    before = (balance(engine, world, sku_id), documents_of(engine, world))

    reused = record(
        engine, world, order_id, shipped("PKG-A", ("L-1", 2), platform_time=T4), action="shipped"
    )
    reused_id = event_by_key(engine, world, reused)["event_row_id"]
    assert process(engine, world, order_id) == RoundResult(1)
    assert result_of(engine, world, reused) == ("attention", "BUSINESS_REF_CONFLICT")
    assert attention_of(engine, world, order_id) == (
        "committed",
        "defect",
        "BUSINESS_REF_CONFLICT",
    )
    assert (balance(engine, world, sku_id), documents_of(engine, world)) == before
    # 命令登记的安全事件随拒绝保留，在事务提交之后写。
    [event] = security_events_of(engine, world)
    assert event["event_type"] == "business_ref_conflict"
    assert (event["request_id"], event["credential_id"]) == (reused_id, world.system_credential_id)
    ref = order_ref("shopee", world.platform_shop_id, order_id)
    assert event["details"] == {"ref_type": "shipment", "ref": f"{ref}:d:PKG-A"}
