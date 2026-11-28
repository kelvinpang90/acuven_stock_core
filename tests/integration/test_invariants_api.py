"""不变量自查：正常的账、六项检查、汇总、鉴权与参数（契约 §4.4，§10 第 4 条）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，期初、收货与订单命令经接口建立，
账才对得上；违反由测试直接改库造出（自己提交的连接）。自查用同租户另一个只有 ops.read、
不带销售入口的凭据。
第 6 项的测试用 platform_fixtures 建带 platform 入口的租户，平台订单与配额行直接插入，
结束时在 finally 里 purge_tenant 删掉它的全部行。
"""

import logging
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, update

from app.domain.requests import STOCK_OPERATOR
from app.domain.service_auth import OPS_READ
from app.models.catalog import Sku
from app.models.inventory import IdempotencyRecord, LedgerEntry, StockBalance, StockDocument
from app.models.orders import OrderDocument, OrderLine, Reservation
from app.models.platform import ChannelAllocation, PlatformOrder, PlatformOrderLine
from app.services.reservations import sweep_expired_reservations
from tests.integration import platform_fixtures
from tests.integration.conftest import (
    REFUND_RESTOCKS,
    add_sku,
    balance_insert,
    body_of,
    new_id,
    post,
    qline,
)
from tests.integration.platform_fixtures import PlatformWorld
from tests.integration.test_api_auth import Caller, add_other_pool, error_of, events, issue
from tests.integration.test_catalog_updates_api import db_now
from tests.integration.test_idempotency import add_skus, rid_of
from tests.integration.test_reservation_release_api import release, reserved_order
from tests.integration.test_reservations_api import (
    ACTOR,
    data_of,
    line,
    live_caller,
    make_client,
    order,
    reserve,
    rows_of,
    shift_into_the_past,
    stock,
)
from tests.integration.test_return_receipts_api import dispatch, receive, return_body, rline
from tests.integration.test_sale_commits_api import commit, commit_body
from tests.integration.test_stock_documents_api import RECEIPTS, receipt_body
from tests.integration.test_stock_documents_api import line as receipt_line
from tests.integration.test_stock_documents_api import write_headers as document_headers
from tests.integration.test_stock_imports_api import (
    confirm,
    credential_of,
    parse_timestamp,
    post_go_live,
    preview,
)
from tests.integration.test_stock_imports_api import line as opening_line
from tests.integration.test_stock_service import entries

INVARIANTS = "/v1/ops/invariants"
REPORT_KEYS = {"checked_at", "ok", "violation_count", "violations"}
# 自查前后要相同的表。
BUSINESS_TABLES = (
    StockBalance,
    LedgerEntry,
    StockDocument,
    IdempotencyRecord,
    Reservation,
    OrderLine,
    OrderDocument,
)
OPENING = 10


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    test_client = make_client()
    try:
        yield test_client
    finally:
        test_client.app.state.engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


# --- 请求与准备 ---


def check(client: TestClient, caller: Caller, params=None):
    headers = caller.auth | {"X-Request-ID": f"invariants-{uuid4().hex}"}
    return client.get(INVARIANTS, params=params, headers=headers)


def report_of(client: TestClient, caller: Caller) -> dict:
    data = data_of(check(client, caller), 200)
    assert set(data) == REPORT_KEYS
    return data


def ops_of(engine: Engine, caller: Caller) -> Caller:
    """同租户的运维凭据：只有 ops.read，不带销售入口。"""
    return credential_of(engine, caller, [OPS_READ])


def opened(client: TestClient, engine: Engine, count: int) -> tuple[Caller, list[str]]:
    """新租户与池：count 个 SKU 经期初导入各 OPENING 件，再切 live；账与流水对得上。"""
    caller = issue(engine)
    skus = add_skus(engine, caller.tenant_id, count)
    import_id = preview(client, caller, [opening_line(sku, OPENING) for sku in skus])["import_id"]
    confirm(client, caller, import_id)
    data_of(post_go_live(client, caller, caller.pool_id), 200)
    return caller, skus


def received(client: TestClient, caller: Caller, skus: list[str], quantity: int) -> None:
    lines = [receipt_line(f"r{index + 1}", sku, quantity) for index, sku in enumerate(skus)]
    headers = document_headers(caller, role=STOCK_OPERATOR)
    data_of(client.post(RECEIPTS, json=receipt_body(caller, lines), headers=headers), 201)


def placed(client: TestClient, caller: Caller, skus: list[str], quantities: list[int]) -> dict:
    """预留一单：第 i 行 l{i} 是 skus[i] 的 quantities[i] 件；返回预留的表示。"""
    lines = [
        line(f"l{index + 1}", sku, quantity)
        for index, (sku, quantity) in enumerate(zip(skus, quantities, strict=True))
    ]
    return data_of(reserve(client, caller, order(lines), actor=ACTOR), 201)


def paid(client: TestClient, caller: Caller, skus: list[str], quantities: list[int]) -> dict:
    created = placed(client, caller, skus, quantities)
    data_of(commit(client, caller, created["order_ref"], commit_body(created)), 200)
    return created


def set_balance(engine: Engine, caller: Caller, sku_id: str, **values: object) -> None:
    statement = update(StockBalance).where(
        StockBalance.tenant_id == caller.tenant_id,
        StockBalance.pool_id == caller.pool_id,
        StockBalance.sku_id == sku_id,
    )
    with engine.begin() as db:
        db.execute(statement.values(**values))


def set_line(engine: Engine, reservation_id: str, line_ref: str, **values: object) -> None:
    statement = update(OrderLine).where(
        OrderLine.reservation_id == reservation_id, OrderLine.line_ref == line_ref
    )
    with engine.begin() as db:
        db.execute(statement.values(**values))


def merge_sku(engine: Engine, caller: Caller, sku_id: str, into: str) -> None:
    """绕过合并单直接把 SKU 设为已合并（库约束要求同时 inactive）。"""
    statement = update(Sku).where(Sku.tenant_id == caller.tenant_id, Sku.sku_id == sku_id)
    with engine.begin() as db:
        db.execute(statement.values(status="inactive", merged_into_sku_id=into))


def status_of(engine: Engine, caller: Caller, reservation_id: str) -> str:
    stored = {row["reservation_id"]: row for row in rows_of(engine, Reservation, caller)}
    return stored[reservation_id]["status"]


def ledger_expected(stock_balance=0, reserved=0, unavailable=0, pending=0, count=0) -> dict:
    """ledger_totals 的 expected；版本连续，所以最大的 balance_version_after 等于条数。"""
    return {
        "stock_balance": stock_balance,
        "reserved": reserved,
        "unavailable": unavailable,
        "paid_pending_dispatch": pending,
        "entry_count": count,
        "max_balance_version_after": count,
    }


def balance_actual(stock_balance=0, reserved=0, unavailable=0, pending=0, version=0) -> dict:
    return {
        "stock_balance": stock_balance,
        "reserved": reserved,
        "unavailable": unavailable,
        "paid_pending_dispatch": pending,
        "balance_version": version,
    }


def balance_violation(name: str, caller: Caller, sku_id: str, expected, actual) -> dict:
    return {
        "check": name,
        "pool_id": caller.pool_id,
        "sku_id": sku_id,
        "expected": expected,
        "actual": actual,
    }


def tables_of(engine: Engine, caller: Caller) -> dict:
    return {model.__tablename__: rows_of(engine, model, caller) for model in BUSINESS_TABLES}


def world_ops(engine: Engine, world: PlatformWorld) -> Caller:
    """platform_fixtures 建的租户的运维凭据：只有 ops.read，不带销售入口。"""
    owner = Caller(world.tenant_id, world.pool_id, world.system_credential_id, "")
    return ops_of(engine, owner)


def add_platform_order(
    engine: Engine,
    world: PlatformWorld,
    stage: str,
    lines: list[tuple[str, str | None, int, int]],
    *,
    channel_id: str | None = None,
) -> dict:
    """一个平台订单连同它的行，提交；各行为 (platform_line_id, sku_id, quantity, cancelled_qty)。

    held 的订单带 attention（库约束）。返回订单行插入的值（platform_order_row_id 等）。
    """
    platform_order_id = f"po-{uuid4().hex[:12]}"
    order = {
        "platform_order_row_id": new_id("por"),
        "tenant_id": world.tenant_id,
        "channel_id": channel_id or world.channel_id,
        "platform_order_id": platform_order_id,
        "order_ref": platform_order_id,
        "stage": stage,
        "attention": "unmapped" if stage == "held" else None,
    }
    with engine.begin() as db:
        db.execute(insert(PlatformOrder).values(order))
        for platform_line_id, sku_id, quantity, cancelled_qty in lines:
            db.execute(
                insert(PlatformOrderLine).values(
                    tenant_id=world.tenant_id,
                    platform_order_row_id=order["platform_order_row_id"],
                    platform_line_id=platform_line_id,
                    platform_item_id=f"item-{platform_line_id}",
                    platform_model_id="-",
                    sku_id=sku_id,
                    quantity=quantity,
                    cancelled_qty=cancelled_qty,
                )
            )
    return order


def set_pending(
    engine: Engine, world: PlatformWorld, channel_id: str, sku_id: str, pending: int
) -> None:
    statement = update(ChannelAllocation).where(
        ChannelAllocation.tenant_id == world.tenant_id,
        ChannelAllocation.channel_id == channel_id,
        ChannelAllocation.sku_id == sku_id,
    )
    with engine.begin() as db:
        db.execute(statement.values(pending=pending))


def channel_violation(
    world: PlatformWorld, channel_id: str, sku_id: str, expected: int, actual: int
) -> dict:
    return {
        "check": "channel_pending_vs_orders",
        "pool_id": world.pool_id,
        "sku_id": sku_id,
        "expected": expected,
        "actual": actual,
        "channel_id": channel_id,
    }


# --- 正常的账 ---


def test_books_kept_by_the_commands_have_no_violations(client, engine, caplog):
    caller, (first, second, third) = opened(client, engine, 3)
    received(client, caller, [first, second], 4)

    # 确认销售之后部分发货、部分退款回库，发出的件再退货验收。
    sold = paid(client, caller, [first, second], [3, 2])
    dispatch(client, caller, sold["order_ref"], [qline("l1", 2)])
    refund = body_of(REFUND_RESTOCKS, [qline("l2", 1)])
    data_of(post(client, caller, REFUND_RESTOCKS, sold["order_ref"], refund), 201)
    returned = return_body([rline("l1", 1, 1)])
    data_of(receive(client, caller, sold["order_ref"], returned), 201)
    # 付款前取消。
    cancelled = placed(client, caller, [first], [1])
    data_of(release(client, caller, cancelled["reservation_id"], {"reason": "cancelled"}), 200)
    # 到期关单。
    lapsed = placed(client, caller, [third], [2])
    shift_into_the_past(engine, lapsed["reservation_id"])
    sweep_expired_reservations(engine)
    assert status_of(engine, caller, lapsed["reservation_id"]) == "expired"
    # 一单还在有效期内；另一单已到期还没关掉，仍是 active、仍算在 reserved 里。
    # 新预留会先关掉同 SKU 上已到期的，所以有效期内的那单先下。
    placed(client, caller, [third], [1])
    due = placed(client, caller, [second, third], [1, 1])
    shift_into_the_past(engine, due["reservation_id"])
    assert status_of(engine, caller, due["reservation_id"]) == "active"

    assert {entry["entry_type"] for entry in entries(engine, caller)} == {
        "OPENING_BALANCE",
        "RECEIPT",
        "RESERVE",
        "SALE_COMMIT",
        "DISPATCH",
        "REFUND_RESTOCK",
        "RETURN_SELLABLE",
        "RETURN_UNSELLABLE",
        "RELEASE",
    }

    # 另一个租户的账不对，不影响本租户的结果。
    other = live_caller(engine)
    [foreign] = add_skus(engine, other.tenant_id, 1)
    stock(engine, other, foreign, 5)

    ops = ops_of(engine, caller)
    caplog.set_level(logging.INFO, logger="app.api")
    before = db_now(engine)
    response = check(client, ops)
    after = db_now(engine)
    data = data_of(response, 200)
    assert set(data) == REPORT_KEYS
    assert (data["ok"], data["violation_count"], data["violations"]) == (True, 0, [])
    assert before <= parse_timestamp(data["checked_at"]) <= after
    assert events(engine, rid_of(response)) == []

    # 进程日志一行：租户、用时与违反项数。
    [logged] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("invariant")]
    assert f"tenant_id={caller.tenant_id}" in logged
    assert "duration_ms=" in logged
    assert logged.endswith("violation_count=0")

    # 另一个租户自己查，看得到自己的违反。
    theirs = report_of(client, ops_of(engine, other))
    assert (theirs["ok"], theirs["violation_count"]) == (False, 1)
    assert [v["sku_id"] for v in theirs["violations"]] == [foreign]


# --- ledger_totals ---


def test_a_balance_row_that_disagrees_with_its_entries_is_reported(client, engine, caplog):
    caller, (changed, versioned, untouched) = opened(client, engine, 3)
    ops = ops_of(engine, caller)
    assert report_of(client, ops)["ok"] is True

    set_balance(engine, caller, changed, unavailable=2)
    set_balance(engine, caller, versioned, balance_version=2)
    # 余额行有量却没有流水：expected 全 0。
    [bare] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, bare, 3)

    caplog.set_level(logging.INFO, logger="app.api")
    caplog.clear()
    data = report_of(client, ops)
    opening = ledger_expected(stock_balance=OPENING, count=1)
    expected = [
        balance_violation(
            "ledger_totals",
            caller,
            changed,
            opening,
            balance_actual(stock_balance=OPENING, unavailable=2, version=1),
        ),
        balance_violation(
            "ledger_totals",
            caller,
            versioned,
            opening,
            balance_actual(stock_balance=OPENING, version=2),
        ),
        balance_violation(
            "ledger_totals", caller, bare, ledger_expected(), balance_actual(stock_balance=3)
        ),
    ]
    assert data["ok"] is False
    assert data["violation_count"] == 3
    assert data["violations"] == sorted(expected, key=lambda violation: violation["sku_id"])
    assert untouched not in {violation["sku_id"] for violation in data["violations"]}

    # 日志只有违反项数，不写违反项的内容。
    [logged] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("invariant")]
    assert logged.endswith("violation_count=3")
    assert all(sku not in logged for sku in (changed, versioned, bare))


# --- reserved_vs_orders 与 pending_dispatch_vs_orders ---


def test_reserved_and_paid_pending_dispatch_are_checked_against_the_order_lines(client, engine):
    caller, (held, sold_sku, due_sku, quiet) = opened(client, engine, 4)
    placed(client, caller, [held], [2])
    sold = paid(client, caller, [sold_sku], [3])
    due = placed(client, caller, [due_sku, quiet], [2, 1])
    shift_into_the_past(engine, due["reservation_id"])
    ops = ops_of(engine, caller)
    # 已到期还没关掉的 active 预留不误报。
    assert report_of(client, ops)["ok"] is True
    assert status_of(engine, caller, due["reservation_id"]) == "active"

    # 余额行的 reserved 少了 1 件（与流水也不符）；订单行多记了 1 件发货。
    set_balance(engine, caller, held, reserved=1)
    set_line(engine, sold["reservation_id"], "l1", dispatched_qty=1)

    data = report_of(client, ops)
    assert data["ok"] is False
    assert data["violation_count"] == 3
    # 按检查的编号排。
    assert data["violations"] == [
        balance_violation(
            "ledger_totals",
            caller,
            held,
            ledger_expected(stock_balance=OPENING, reserved=2, count=2),
            balance_actual(stock_balance=OPENING, reserved=1, version=2),
        ),
        balance_violation("reserved_vs_orders", caller, held, 2, 1),
        balance_violation("pending_dispatch_vs_orders", caller, sold_sku, 2, 3),
    ]


# --- order_commit_state ---


def test_a_committed_reservation_with_an_uncommitted_line_is_reported(client, engine):
    caller, (first, second) = opened(client, engine, 2)
    sold = paid(client, caller, [first, second], [2, 1])
    ops = ops_of(engine, caller)
    assert report_of(client, ops)["ok"] is True

    set_line(engine, sold["reservation_id"], "l2", committed_qty=0)

    data = report_of(client, ops)
    assert data["ok"] is False
    assert data["violation_count"] == 2
    # 那一行不再计入未发件数，第 3 项也随之报出；第 4 项排在后面。
    assert data["violations"] == [
        balance_violation("pending_dispatch_vs_orders", caller, second, 0, 1),
        {
            "check": "order_commit_state",
            "reservation_id": sold["reservation_id"],
            "order_ref": sold["order_ref"],
            "status": "committed",
            "line_refs": ["l2"],
        },
    ]


# --- merged_sku_lines ---


def test_an_open_reservation_line_on_a_merged_sku_is_reported(client, engine):
    caller, (held, unmerged, cancelled_sku, canonical) = opened(client, engine, 4)
    held_order = placed(client, caller, [held, unmerged], [2, 1])
    cancelled = placed(client, caller, [cancelled_sku], [1])
    data_of(release(client, caller, cancelled["reservation_id"], {"reason": "cancelled"}), 200)
    ops = ops_of(engine, caller)
    assert report_of(client, ops)["ok"] is True

    # released 预留的订单行合并时不改挂，指向已合并的 SKU 不报。
    merge_sku(engine, caller, cancelled_sku, canonical)
    assert report_of(client, ops)["ok"] is True

    # active 预留的 l1 漏了改挂；余额行与订单行没动，前四项不报。
    merge_sku(engine, caller, held, canonical)
    data = report_of(client, ops)
    assert data["ok"] is False
    assert data["violation_count"] == 1
    assert data["violations"] == [
        {
            "check": "merged_sku_lines",
            "reservation_id": held_order["reservation_id"],
            "order_ref": held_order["order_ref"],
            "status": "active",
            "line_refs": ["l1"],
        }
    ]


# --- channel_pending_vs_orders ---


def test_channel_pending_is_checked_against_unpaid_and_held_platform_orders(client, engine):
    world = platform_fixtures.create_world(engine)
    try:
        channel = world.channel_id
        second = platform_fixtures.add_platform_channel(engine, world)
        first_sku = platform_fixtures.add_sku(engine, world.tenant_id)
        second_sku = platform_fixtures.add_sku(engine, world.tenant_id)
        # 计入的：unpaid 两件剩余（3 − 1）与一件，held 两件；sku_id 为空的行不计。
        add_platform_order(
            engine,
            world,
            "unpaid",
            [("1", first_sku, 3, 1), ("2", second_sku, 1, 0), ("3", None, 4, 0)],
        )
        add_platform_order(engine, world, "held", [("1", first_sku, 2, 0), ("2", None, 1, 0)])
        # 不计的：committed 与 cancelled 阶段的订单，不论剩余件数。
        add_platform_order(engine, world, "committed", [("1", first_sku, 5, 0)])
        add_platform_order(engine, world, "cancelled", [("1", first_sku, 4, 1)])
        # 另一个入口上的订单只计入那个入口。
        add_platform_order(engine, world, "unpaid", [("1", first_sku, 1, 0)], channel_id=second)
        platform_fixtures.set_allocation(engine, world, first_sku, quota=5, pending=4)
        platform_fixtures.set_allocation(engine, world, second_sku, quota=5, pending=1)
        platform_fixtures.set_allocation(
            engine, world, first_sku, quota=5, pending=1, channel_id=second
        )
        # 没有平台订单的配额行按 0 比对。
        platform_fixtures.set_allocation(engine, world, second_sku, channel_id=second)

        ops = world_ops(engine, world)
        data = report_of(client, ops)
        assert (data["ok"], data["violation_count"], data["violations"]) == (True, 0, [])

        set_pending(engine, world, channel, first_sku, 6)
        set_pending(engine, world, channel, second_sku, 0)
        set_pending(engine, world, second, first_sku, 0)
        set_pending(engine, world, second, second_sku, 2)
        data = report_of(client, ops)
        assert data["ok"] is False
        assert data["violation_count"] == 4
        expected = [
            channel_violation(world, channel, first_sku, 4, 6),
            channel_violation(world, channel, second_sku, 1, 0),
            channel_violation(world, second, first_sku, 1, 0),
            channel_violation(world, second, second_sku, 0, 2),
        ]
        # 按 channel_id、sku_id 升序；余额行与预留都没动，前五项不报。
        assert data["violations"] == sorted(
            expected, key=lambda violation: (violation["channel_id"], violation["sku_id"])
        )
    finally:
        platform_fixtures.purge_tenant(engine, world.tenant_id)


def test_channel_pending_violations_come_after_the_first_five(client, engine):
    world = platform_fixtures.create_world(engine)
    try:
        sku = platform_fixtures.add_sku(engine, world.tenant_id)
        add_platform_order(engine, world, "unpaid", [("1", sku, 2, 0)])
        platform_fixtures.set_allocation(engine, world, sku, quota=5, pending=2)
        ops = world_ops(engine, world)
        assert report_of(client, ops)["ok"] is True

        # 余额行有量却没有流水（第 1 项），pending 与平台订单对不上（第 6 项）。
        platform_fixtures.set_allocation(engine, world, sku, quota=5, pending=3, stock_balance=7)
        data = report_of(client, ops)
        assert data["violation_count"] == 2
        assert data["violations"] == [
            {
                "check": "ledger_totals",
                "pool_id": world.pool_id,
                "sku_id": sku,
                "expected": ledger_expected(),
                "actual": balance_actual(stock_balance=7),
            },
            channel_violation(world, world.channel_id, sku, 2, 3),
        ]
    finally:
        platform_fixtures.purge_tenant(engine, world.tenant_id)


def test_demo_books_have_no_channel_allocations_to_check(client, engine):
    caller, (sku,) = opened(client, engine, 1)
    paid(client, caller, [sku], [2])
    placed(client, caller, [sku], [1])
    assert rows_of(engine, ChannelAllocation, caller) == []
    data = report_of(client, ops_of(engine, caller))
    assert (data["ok"], data["violation_count"], data["violations"]) == (True, 0, [])


# --- 汇总 ---


def test_more_than_100_violations_return_the_first_100_in_contract_order(client, engine):
    caller = live_caller(engine)
    other_pool = add_other_pool(engine, caller.tenant_id)
    pools = (caller.pool_id, other_pool)
    # 两个池各 55 个有量却没有流水的余额行：110 项 ledger_totals。
    with engine.begin() as db:
        bare = [add_sku(db, caller.tenant_id) for _ in range(55)]
        for pool_id in pools:
            for sku_id in bare:
                db.execute(balance_insert(caller.tenant_id, pool_id, sku_id, stock_balance=1))
    # 一单预留，订单行在 active 时就记成已确认：第 1、3、4 项各一项（余额行同样没有期初流水）。
    created, [reserved_sku] = reserved_order(client, engine, caller, [1])
    set_line(engine, created["reservation_id"], "l1", committed_qty=1)

    data = report_of(client, ops_of(engine, caller))
    assert data["ok"] is False
    assert data["violation_count"] == 113
    assert len(data["violations"]) == 100
    # 前 100 项都是第 1 项，按 pool_id、sku_id 升序；后面的第 1 项与第 3、4 项截掉。
    ledger_keys = sorted(
        [(pool_id, sku_id) for pool_id in pools for sku_id in bare]
        + [(caller.pool_id, reserved_sku)]
    )
    assert {violation["check"] for violation in data["violations"]} == {"ledger_totals"}
    shown = [(violation["pool_id"], violation["sku_id"]) for violation in data["violations"]]
    assert shown == ledger_keys[:100]


# --- 鉴权与参数 ---


def test_the_check_needs_ops_read(client, engine):
    caller, _ = opened(client, engine, 1)
    response = check(client, caller)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == "forbidden"
    assert event["details"] == {"category": "scope", "missing_scopes": [OPS_READ]}


@pytest.mark.parametrize(
    "params",
    [
        {"pool_id": "pool_1"},
        {"limit": "10"},
        {"anything": ""},
        [("after", "a"), ("after", "b")],
    ],
)
def test_any_query_parameter_is_rejected(client, engine, params):
    caller = live_caller(engine)
    response = check(client, ops_of(engine, caller), params)
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"


def test_the_check_changes_nothing(client, engine):
    caller, (first, second) = opened(client, engine, 2)
    sold = paid(client, caller, [first, second], [2, 1])
    dispatch(client, caller, sold["order_ref"], [qline("l1", 1)])
    placed(client, caller, [second], [1])
    # 有违反时也一样只读。
    set_balance(engine, caller, first, unavailable=1)
    ops = ops_of(engine, caller)
    before = tables_of(engine, caller)

    assert report_of(client, ops)["violation_count"] == 1
    assert error_of(check(client, ops, {"x": "1"}), 422)["code"] == "INVALID_REQUEST"
    assert error_of(check(client, caller), 403)["code"] == "FORBIDDEN"

    assert tables_of(engine, caller) == before
