"""Demo 每日重置（契约 §6.1、§6.2、§7.1、§8.1、§10 第 3、4 条）。

在真 MySQL 上跑；每个测试建自己的租户、池与带 demo.reset 的凭据，
SKU 与余额行由测试直接在库里造。池默认先切到 live。
并发的测试在服务层的 lock_balances 前后放 Gate，让先到的请求持有余额行锁时后到的请求在锁上等待。
生产实例的测试在 conftest 的 instance_marked 里以 CORE_ENV=production 建应用，
建好即退出（标记改回 demo）。
"""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.config import Settings
from app.domain.environment import DEMO, PRODUCTION
from app.domain.requests import CATALOG_ADMIN, STOCK_OPERATOR
from app.domain.service_auth import DEMO_RESET
from app.main import create_app
from app.models.inventory import DemoReset, SalesChannel
from app.services import demo_resets as demo_reset_service
from app.services.stock import format_timestamp
from tests.integration.conftest import TEST_DATABASE_URL, instance_marked, new_id
from tests.integration.test_api_auth import Caller, error_of, events, issue
from tests.integration.test_catalog_create_api import created
from tests.integration.test_catalog_queries_api import data_of
from tests.integration.test_idempotency import add_skus, balance_of
from tests.integration.test_stock_documents_api import (
    deactivate,
    idempotency_records,
    preset_balance,
)
from tests.integration.test_stock_imports_api import merge, set_live
from tests.integration.test_stock_service import Gate, entries, judgement, quantities_of

RESETS = "/v1/demo/resets"
BUSINESS_DATE = "2026-11-16"
ACTOR_REF = "scheduler-1"
RESET_KEYS = {"reset_id", "pool_id", "business_date", "created_at", "skus"}
SKU_KEYS = {
    "sku_id",
    "target",
    "delta",
    "entry_id",
    "stock_balance",
    "reserved",
    "unavailable",
    "paid_pending_dispatch",
    "available",
    "balance_version",
}


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    app = create_app(Settings(env=DEMO, git_sha="d" * 40, database_url=TEST_DATABASE_URL))
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.state.engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


@pytest.fixture
def caller(engine: Engine) -> Caller:
    """池已 live、只带 demo.reset 的调用方。"""
    issued = issue(engine, scopes=[DEMO_RESET])
    set_live(engine, issued)
    return issued


# --- 请求 ---


def new_key() -> str:
    return f"shop:demo-reset:{uuid4().hex}"


def request_id() -> str:
    return f"demo-reset-test-{uuid4().hex}"


def write_headers(
    caller: Caller, key: str | None = None, rid: str | None = None, role: str | None = None
) -> dict:
    """role 为 None 时不带操作者头（重置不要求角色）。"""
    extra = {"Idempotency-Key": key or new_key(), "X-Request-ID": rid or request_id()}
    headers = caller.auth | extra
    if role is not None:
        headers |= {"X-Actor-Ref": ACTOR_REF, "X-Actor-Role": role}
    return headers


def target(sku_id: str, value: object) -> dict:
    return {"sku_id": sku_id, "target": value}


def reset_body(
    caller: Caller, skus: list, business_date: object = BUSINESS_DATE, **extra: object
) -> dict:
    return {"pool_id": caller.pool_id, "business_date": business_date, "skus": skus} | extra


def post(client: TestClient, caller: Caller, body: dict, **headers):
    return client.post(RESETS, json=body, headers=write_headers(caller, **headers))


# --- 观察 ---


def resets(engine: Engine, caller: Caller) -> list[dict]:
    statement = select(*DemoReset.__table__.columns).where(DemoReset.tenant_id == caller.tenant_id)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def channel_of(engine: Engine, caller: Caller) -> str:
    statement = select(SalesChannel.channel_id).where(SalesChannel.tenant_id == caller.tenant_id)
    with engine.connect() as db:
        return db.execute(statement).scalar_one()


def snapshot(engine: Engine, caller: Caller, sku_ids: list[str]) -> tuple:
    """流水、重置行与各余额行的四个量和判定列。"""
    balances = [
        (quantities_of(engine, caller, sku_id), judgement(engine, caller, sku_id))
        for sku_id in sku_ids
    ]
    return entries(engine, caller), resets(engine, caller), balances


# --- 数量规则与表示 ---


def test_a_reset_books_each_sku_and_returns_the_reset(client, engine, caller):
    raised, floored, unchanged, fresh, unlisted = add_skus(engine, caller.tenant_id, 5)
    preset_balance(
        engine,
        caller,
        raised,
        stock_balance=10,
        reserved=3,
        unavailable=2,
        paid_pending_dispatch=1,
        balance_version=4,
        last_book_only_version=1,
        last_reconciled_version=1,
    )
    # target 5 低于 reserved + unavailable = 7：new_balance 取 7。
    preset_balance(
        engine,
        caller,
        floored,
        stock_balance=8,
        reserved=4,
        unavailable=3,
        paid_pending_dispatch=2,
        balance_version=2,
    )
    preset_balance(engine, caller, unchanged, stock_balance=6, reserved=1, balance_version=3)
    preset_balance(engine, caller, unlisted, stock_balance=9, reserved=2, balance_version=5)
    unlisted_before = balance_of(engine, caller, caller.pool_id, unlisted)

    key, rid = new_key(), request_id()
    skus = [target(floored, 5), target(unchanged, 6), target(raised, 20), target(fresh, 0)]
    body = reset_body(caller, skus)
    data = created(post(client, caller, body, key=key, rid=rid, role=STOCK_OPERATOR))

    assert set(data) == RESET_KEYS
    assert data["reset_id"].startswith("drs_")
    assert (data["pool_id"], data["business_date"]) == (caller.pool_id, BUSINESS_DATE)

    by_sku = {entry["sku_id"]: entry for entry in entries(engine, caller)}
    # 增量为 0 的两个 SKU 不写流水。
    assert set(by_sku) == {raised, floored}
    assert [set(row) for row in data["skus"]] == [SKU_KEYS] * 4
    # 按请求顺序；available = max(target − reserved − unavailable, 0)。
    assert data["skus"] == [
        {
            "sku_id": floored,
            "target": 5,
            "delta": -1,
            "entry_id": by_sku[floored]["entry_id"],
            "stock_balance": 7,
            "reserved": 4,
            "unavailable": 3,
            "paid_pending_dispatch": 2,
            "available": 0,
            "balance_version": 3,
        },
        {
            "sku_id": unchanged,
            "target": 6,
            "delta": 0,
            "entry_id": None,
            "stock_balance": 6,
            "reserved": 1,
            "unavailable": 0,
            "paid_pending_dispatch": 0,
            "available": 5,
            "balance_version": 3,
        },
        {
            "sku_id": raised,
            "target": 20,
            "delta": 10,
            "entry_id": by_sku[raised]["entry_id"],
            "stock_balance": 20,
            "reserved": 3,
            "unavailable": 2,
            "paid_pending_dispatch": 1,
            "available": 15,
            "balance_version": 5,
        },
        {
            "sku_id": fresh,
            "target": 0,
            "delta": 0,
            "entry_id": None,
            "stock_balance": 0,
            "reserved": 0,
            "unavailable": 0,
            "paid_pending_dispatch": 0,
            "available": 0,
            "balance_version": 0,
        },
    ]

    # reserved、unavailable、paid_pending_dispatch 不变。
    assert quantities_of(engine, caller, raised) == (20, 3, 2, 1)
    assert quantities_of(engine, caller, floored) == (7, 4, 3, 2)
    assert quantities_of(engine, caller, unchanged) == (6, 1, 0, 0)
    assert quantities_of(engine, caller, fresh) == (0, 0, 0, 0)
    # 两个判定列在写与不写流水时都等于写后的 balance_version。
    assert judgement(engine, caller, raised) == (5, 5, 5, None)
    assert judgement(engine, caller, floored) == (3, 3, 3, None)
    assert judgement(engine, caller, unchanged) == (3, 3, 3, None)
    assert judgement(engine, caller, fresh) == (0, 0, 0, None)
    # 没列的 SKU 不动。
    assert balance_of(engine, caller, caller.pool_id, unlisted) == unlisted_before

    channel_id = channel_of(engine, caller)
    assert len({entry["command_id"] for entry in by_sku.values()}) == 1
    for sku_id, delta in ((raised, 10), (floored, -1)):
        entry = by_sku[sku_id]
        assert entry["entry_type"] == "DEMO_RESET"
        deltas = (
            entry["stock_balance_delta"],
            entry["reserved_delta"],
            entry["unavailable_delta"],
            entry["paid_pending_dispatch_delta"],
        )
        assert deltas == (delta, 0, 0, 0)
        assert (entry["source_type"], entry["source_ref"]) == ("demo_reset", data["reset_id"])
        assert entry["channel_id"] == channel_id
        assert entry["line_ref"] is None
        assert (entry["reason_code"], entry["note"]) == (None, None)
        assert entry["corrects_entry_id"] is None
        assert entry["occurred_at"] == entry["recorded_at"]
        assert format_timestamp(entry["recorded_at"]) == data["created_at"]
        assert (entry["actor_ref"], entry["actor_role"]) == (ACTOR_REF, STOCK_OPERATOR)
        assert (entry["idempotency_key"], entry["request_id"]) == (key, rid)
        assert entry["credential_id"] == caller.credential_id

    # demo_resets 一行，result 就是响应的 data。
    [row] = resets(engine, caller)
    assert row["reset_id"] == data["reset_id"]
    assert (row["pool_id"], row["channel_id"]) == (caller.pool_id, channel_id)
    assert row["business_date"] == date(2026, 11, 16)
    assert row["result"] == data
    assert row["credential_id"] == caller.credential_id
    assert (row["actor_ref"], row["actor_role"]) == (ACTOR_REF, STOCK_OPERATOR)
    assert row["request_id"] == rid
    assert format_timestamp(row["created_at"]) == data["created_at"]
    [record] = idempotency_records(engine, caller)
    assert record["request_hash"] == row["request_hash"]


def test_no_role_is_required_and_any_role_is_recorded(client, engine, caller):
    first, second = add_skus(engine, caller.tenant_id, 2)
    created(post(client, caller, reset_body(caller, [target(first, 3)], "2026-11-01")))
    body = reset_body(caller, [target(second, 4)], "2026-11-02")
    created(post(client, caller, body, role=CATALOG_ADMIN))

    actors = {
        row["business_date"]: (row["actor_ref"], row["actor_role"])
        for row in resets(engine, caller)
    }
    assert actors == {
        date(2026, 11, 1): (None, None),
        date(2026, 11, 2): (ACTOR_REF, CATALOG_ADMIN),
    }
    by_sku = {entry["sku_id"]: entry for entry in entries(engine, caller)}
    assert (by_sku[first]["actor_ref"], by_sku[first]["actor_role"]) == (None, None)
    assert (by_sku[second]["actor_ref"], by_sku[second]["actor_role"]) == (ACTOR_REF, CATALOG_ADMIN)

    # 操作者头只带一个：第 6 步 400。
    headers = write_headers(caller) | {"X-Actor-Ref": ACTOR_REF}
    body = reset_body(caller, [target(first, 1)], "2026-11-03")
    response = client.post(RESETS, json=body, headers=headers)
    assert error_of(response, 400)["code"] == "INVALID_REQUEST"
    assert len(resets(engine, caller)) == 2


def test_an_inactive_sku_is_reset(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    deactivate(engine, sku_id)
    data = created(post(client, caller, reset_body(caller, [target(sku_id, 12)])))
    assert data["skus"][0]["stock_balance"] == 12
    assert quantities_of(engine, caller, sku_id) == (12, 0, 0, 0)


# --- 业务引用与幂等 ---


def test_a_reset_takes_effect_once_per_pool_and_day(client, engine, caller):
    first_sku, second_sku = add_skus(engine, caller.tenant_id, 2)
    skus = [target(first_sku, 5), target(second_sku, 7)]
    body = reset_body(caller, skus)
    key = new_key()
    first = post(client, caller, body, key=key)
    data = created(first)
    applied = snapshot(engine, caller, [first_sku, second_sku])

    # 同键：原样重放。
    replay = post(client, caller, body, key=key)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()

    # 换键同内容（换个操作者也算同内容）：200 与原结果。
    again = post(client, caller, body, role=STOCK_OPERATOR)
    assert "Idempotency-Replayed" not in again.headers
    assert data_of(again) == data

    # 换键改 target、换键改 SKU 顺序：409 BUSINESS_REF_CONFLICT，记事件。
    expected = {"ref_type": "demo_reset", "ref": BUSINESS_DATE, "pool_id": caller.pool_id}
    for changed in (
        reset_body(caller, [target(first_sku, 6), target(second_sku, 7)]),
        reset_body(caller, skus[::-1]),
    ):
        rid = request_id()
        error = error_of(post(client, caller, changed, rid=rid), 409)
        assert error["code"] == "BUSINESS_REF_CONFLICT"
        assert error["details"] == expected
        [event] = events(engine, rid)
        assert event["event_type"] == "business_ref_conflict"
        assert event["details"] == expected

    assert snapshot(engine, caller, [first_sku, second_sku]) == applied
    assert quantities_of(engine, caller, first_sku) == (5, 0, 0, 0)

    # 换一天可以再次重置。
    next_day = reset_body(caller, [target(first_sku, 2), target(second_sku, 7)], "2026-11-17")
    later = created(post(client, caller, next_day))
    assert later["reset_id"] != data["reset_id"]
    assert later["business_date"] == "2026-11-17"
    assert [(row["delta"], row["entry_id"] is None) for row in later["skus"]] == [
        (-3, False),
        (0, True),
    ]
    assert quantities_of(engine, caller, first_sku) == (2, 0, 0, 0)
    assert len(resets(engine, caller)) == 2


def test_another_pool_has_its_own_day(client, engine, caller):
    other = issue(engine, scopes=[DEMO_RESET])
    set_live(engine, other)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    [other_sku] = add_skus(engine, other.tenant_id, 1)
    created(post(client, caller, reset_body(caller, [target(sku_id, 3)])))
    created(post(client, other, reset_body(other, [target(other_sku, 4)])))
    assert quantities_of(engine, caller, sku_id) == (3, 0, 0, 0)
    assert quantities_of(engine, other, other_sku) == (4, 0, 0, 0)


@pytest.mark.parametrize("same_content", [True, False])
def test_concurrent_resets_of_one_pool_and_day_take_effect_once(
    client, engine, caller, monkeypatch, same_content
):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    early_body = reset_body(caller, [target(sku_id, 4)])
    late_body = early_body if same_content else reset_body(caller, [target(sku_id, 6)])
    late_rid = request_id()

    # late 在锁余额行之前停住（读快照已建立，里面还没有重置）；early 锁住余额行后停住。
    # 放行 late，它在 early 持有的行锁上等待；再放行 early 提交。
    # late 拿到锁后写 demo_resets 撞上唯一约束，按 early 的重置重新判定。
    before_lock, holding_lock = Gate(), Gate()
    lock_balances = demo_reset_service.lock_balances

    def gated(execution, pool_id, sku_ids):
        late = execution.context.request_id == late_rid
        if late:
            before_lock.pass_through()
        balances = lock_balances(execution, pool_id, sku_ids)
        if not late:
            holding_lock.pass_through()
        return balances

    monkeypatch.setattr(demo_reset_service, "lock_balances", gated)
    other_client = TestClient(client.app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            late = pool.submit(post, client, caller, late_body, rid=late_rid)
            assert before_lock.reached.wait(10)
            early = pool.submit(post, other_client, caller, early_body)
            assert holding_lock.reached.wait(10)
            before_lock.proceed.set()
            time.sleep(0.5)
        finally:
            before_lock.proceed.set()
            holding_lock.proceed.set()
        early_data = created(early.result())
        late_response = late.result()

    if same_content:
        assert data_of(late_response) == early_data
        assert events(engine, late_rid) == []
    else:
        error = error_of(late_response, 409)
        assert error["code"] == "BUSINESS_REF_CONFLICT"
        [event] = events(engine, late_rid)
        assert event["event_type"] == "business_ref_conflict"

    # 只生效一次；late 的结果也作为它那个键的确定结果保存。
    [entry] = entries(engine, caller)
    assert entry["entry_id"] == early_data["skus"][0]["entry_id"]
    assert quantities_of(engine, caller, sku_id) == (4, 0, 0, 0)
    assert judgement(engine, caller, sku_id) == (1, 1, 1, None)
    [row] = resets(engine, caller)
    assert row["reset_id"] == early_data["reset_id"]
    assert len(idempotency_records(engine, caller)) == 2


# --- 拒绝 ---


def test_a_reset_on_an_initializing_pool_is_409(client, engine):
    caller = issue(engine, scopes=[DEMO_RESET])
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    error = error_of(post(client, caller, reset_body(caller, [target(sku_id, 5)])), 409)
    assert error["code"] == "POOL_NOT_LIVE"
    assert error["details"] == {"pool_status": "initializing"}
    assert entries(engine, caller) == [] and resets(engine, caller) == []


def test_missing_and_foreign_skus_are_404(client, engine, caller):
    foreign = issue(engine)
    [known] = add_skus(engine, caller.tenant_id, 1)
    [foreign_sku] = add_skus(engine, foreign.tenant_id, 1)
    missing = new_id("sku")
    skus = [target(foreign_sku, 1), target(known, 1), target(missing, 1)]
    rid = request_id()
    error = error_of(post(client, caller, reset_body(caller, skus), rid=rid), 404)
    assert error["code"] == "SKU_NOT_FOUND"
    assert error["details"] == {"sku_ids": [foreign_sku, missing]}
    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "sku", "id": foreign_sku}
    assert entries(engine, caller) == [] and resets(engine, caller) == []
    assert entries(engine, foreign) == []


def test_a_merged_sku_is_409_and_nothing_is_reset(client, engine, caller):
    merged, canonical = add_skus(engine, caller.tenant_id, 2)
    preset_balance(engine, caller, canonical, stock_balance=2, balance_version=1)
    merge(engine, merged, canonical)
    before = snapshot(engine, caller, [canonical])
    skus = [target(canonical, 9), target(merged, 1)]
    error = error_of(post(client, caller, reset_body(caller, skus)), 409)
    assert error["code"] == "SKU_MERGED"
    assert error["details"] == {"sku_id": merged, "merged_into_sku_id": canonical}
    assert snapshot(engine, caller, [canonical]) == before
    assert resets(engine, caller) == []


def bad_reset(case: str, caller: Caller, sku_id: str) -> dict:
    good = [target(sku_id, 1)]
    return {
        "negative_target": reset_body(caller, [target(sku_id, -1)]),
        "string_target": reset_body(caller, [target(sku_id, "5")]),
        "fractional_target": reset_body(caller, [target(sku_id, 5.5)]),
        "bool_target": reset_body(caller, [target(sku_id, True)]),
        "null_target": reset_body(caller, [target(sku_id, None)]),
        "over_a_million": reset_body(caller, [target(sku_id, 1_000_001)]),
        "duplicate_sku_id": reset_body(caller, [target(sku_id, 1), target(sku_id, 2)]),
        "unknown_field": reset_body(caller, good, note="x"),
        "reason_code": reset_body(caller, good, reason_code="daily"),
        "unknown_sku_field": reset_body(caller, [target(sku_id, 1) | {"line_ref": "A"}]),
        "missing_target": reset_body(caller, [{"sku_id": sku_id}]),
        "no_skus": reset_body(caller, []),
        "skus_not_a_list": reset_body(caller, {"sku_id": sku_id, "target": 1}),
        "impossible_date": reset_body(caller, good, "2026-02-30"),
        "month_13": reset_body(caller, good, "2026-13-01"),
        "short_date": reset_body(caller, good, "2026-1-16"),
        "date_with_time": reset_body(caller, good, "2026-11-16T00:00:00Z"),
        "non_ascii_date": reset_body(caller, good, "２０２６-11-16"),
        "numeric_date": reset_body(caller, good, 20261116),
        "year_out_of_range": reset_body(caller, good, "0999-12-31"),
        "missing_date": {"pool_id": caller.pool_id, "skus": good},
    }[case]


@pytest.mark.parametrize(
    "case",
    [
        "negative_target",
        "string_target",
        "fractional_target",
        "bool_target",
        "null_target",
        "over_a_million",
        "duplicate_sku_id",
        "unknown_field",
        "reason_code",
        "unknown_sku_field",
        "missing_target",
        "no_skus",
        "skus_not_a_list",
        "impossible_date",
        "month_13",
        "short_date",
        "date_with_time",
        "non_ascii_date",
        "numeric_date",
        "year_out_of_range",
        "missing_date",
    ],
)
def test_a_bad_reset_body_is_422(client, engine, caller, case):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    response = post(client, caller, bad_reset(case, caller, sku_id))
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    # 第 7 步的拒绝不进幂等、不保存。
    assert idempotency_records(engine, caller) == []
    assert entries(engine, caller) == [] and resets(engine, caller) == []


def test_a_million_is_accepted_and_101_skus_are_413(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    data = created(post(client, caller, reset_body(caller, [target(sku_id, 1_000_000)])))
    assert data["skus"][0]["stock_balance"] == 1_000_000

    skus = [target(new_id("sku"), 1) for _ in range(101)]
    response = post(client, caller, reset_body(caller, skus, "2026-11-17"))
    assert error_of(response, 413)["code"] == "REQUEST_TOO_LARGE"
    assert len(entries(engine, caller)) == 1
    assert len(resets(engine, caller)) == 1


def test_a_credential_without_demo_reset_is_403(client, engine):
    caller = issue(engine)  # stock.write 等都有，唯独没有 demo.reset
    set_live(engine, caller)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    rid = request_id()
    response = post(client, caller, reset_body(caller, [target(sku_id, 5)]), rid=rid)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["event_type"] == "forbidden"
    assert event["details"] == {"category": "scope", "missing_scopes": [DEMO_RESET]}
    assert entries(engine, caller) == [] and resets(engine, caller) == []
    assert balance_of(engine, caller, caller.pool_id, sku_id) is None


def test_another_tenants_pool_looks_missing(client, engine, caller):
    foreign = issue(engine, scopes=[DEMO_RESET])
    set_live(engine, foreign)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    rid = request_id()
    error = error_of(post(client, caller, reset_body(foreign, [target(sku_id, 1)]), rid=rid), 404)
    assert error["code"] == "POOL_NOT_FOUND"
    [event] = events(engine, rid)
    assert event["details"] == {"object_type": "pool", "id": foreign.pool_id}
    assert resets(engine, caller) == [] and resets(engine, foreign) == []


# --- 生产实例 ---


def test_the_route_does_not_exist_on_a_production_instance(migrated_engine, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    with instance_marked(migrated_engine, PRODUCTION):
        app = create_app(Settings(env=PRODUCTION, git_sha="d" * 40, database_url=TEST_DATABASE_URL))
    try:
        production = TestClient(app)
        assert production.get("/healthz").json()["env"] == PRODUCTION
        body = reset_body(caller, [target(sku_id, 5)])
        for headers in (
            write_headers(caller, role=STOCK_OPERATOR),  # 合格的凭据
            {"X-Request-ID": request_id()},  # 没有凭据
            {"Authorization": "Bearer csk_unknown", "X-Request-ID": request_id()},
        ):
            rid = headers["X-Request-ID"]
            response = production.post(RESETS, json=body, headers=headers)
            error = error_of(response, 404)
            assert error["code"] == "NOT_FOUND"
            assert response.headers["X-Request-ID"] == rid
            # 不鉴权，所以没有 invalid_credential 之类的事件。
            assert events(engine, rid) == []
        # 别的方法同样 404，而不是 405。
        assert error_of(production.get(RESETS, headers=caller.auth), 404)["code"] == "NOT_FOUND"
    finally:
        app.state.engine.dispose()

    # 什么也没写：没有流水、重置行、幂等记录，连余额行也没有预建。
    assert entries(engine, caller) == [] and resets(engine, caller) == []
    assert idempotency_records(engine, caller) == []
    assert balance_of(engine, caller, caller.pool_id, sku_id) is None
