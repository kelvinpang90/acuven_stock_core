"""盘点单的开单、提交实数、取消与查询（契约第 6 节、§6.2、§6.6、§7.1、§7.2、§10 第 3、4 条）。

在真 MySQL 上跑；每个测试建自己的租户、池与凭据，池默认先切到 live，
SKU 与余额行由测试直接在库里造（自己提交的连接）。
并发的测试在服务层的 require_live 或 lock_count 之后放 Gate，
让先到的请求停住（后者停住时持有盘点单行锁），再发出后到的请求。
"""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.config import Settings
from app.domain.requests import CATALOG_ADMIN, STOCK_ADMIN, STOCK_OPERATOR
from app.domain.service_auth import STOCK_READ
from app.main import create_app
from app.models.inventory import StockBalance, StockCount, StockCountLine
from app.services import counts as count_service
from app.services.stock import format_timestamp
from tests.integration.conftest import TEST_DATABASE_URL, balance_insert, new_id
from tests.integration.test_api_auth import Caller, error_of, events, issue
from tests.integration.test_catalog_create_api import created
from tests.integration.test_catalog_queries_api import data_of
from tests.integration.test_idempotency import add_skus
from tests.integration.test_stock_imports_api import merge, set_live
from tests.integration.test_stock_service import Gate, entries, quantities_of

COUNTS = "/v1/counts"
RECEIPTS = "/v1/receipts"
ACTOR_REF = "clerk-7"
COUNT_KEYS = {
    "count_id",
    "count_ref",
    "pool_id",
    "status",
    "note",
    "created_at",
    "confirmed_at",
    "reason_code",
    "confirm_note",
    "cancelled_at",
    "lines",
}
LINE_KEYS = {
    "sku_id",
    "shelf_count",
    "unsellable_count",
    "snapshot",
    "submitted_at",
    "d_shelf",
    "d_u",
    "entry_id",
}
SNAPSHOT_KEYS = {
    "stock_balance",
    "reserved",
    "unavailable",
    "paid_pending_dispatch",
    "balance_version",
}


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    app = create_app(Settings(env="demo", git_sha="c" * 40, database_url=TEST_DATABASE_URL))
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.state.engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


def live_caller(engine: Engine, **options: object) -> Caller:
    issued = issue(engine, **options)
    set_live(engine, issued)
    return issued


@pytest.fixture
def caller(engine: Engine) -> Caller:
    """池已 live 的调用方。"""
    return live_caller(engine)


# --- 请求 ---


def new_key() -> str:
    return f"shop:count:{uuid4().hex}"


def request_id() -> str:
    return f"count-test-{uuid4().hex}"


def new_ref() -> str:
    return f"cnt-ref-{uuid4().hex}"


def write_headers(
    caller: Caller, key: str | None = None, rid: str | None = None, role: str = STOCK_OPERATOR
) -> dict:
    extra = {"Idempotency-Key": key or new_key(), "X-Request-ID": rid or request_id()}
    return caller.auth | {"X-Actor-Ref": ACTOR_REF, "X-Actor-Role": role} | extra


def read_headers(caller: Caller, rid: str | None = None) -> dict:
    return caller.auth | {"X-Request-ID": rid or request_id()}


def opening(caller: Caller, ref: str | None = None, **extra: object) -> dict:
    return {"pool_id": caller.pool_id, "count_ref": ref or new_ref()} | extra


def counted(sku_id: str, shelf_count: object, unsellable_count: object = 0) -> dict:
    return {"sku_id": sku_id, "shelf_count": shelf_count, "unsellable_count": unsellable_count}


def open_count(client: TestClient, caller: Caller, body: dict | None = None, **headers):
    body = body or opening(caller)
    return client.post(COUNTS, json=body, headers=write_headers(caller, **headers))


def submit(client: TestClient, caller: Caller, count_id: str, skus: list, **headers):
    url = f"{COUNTS}/{count_id}/submit"
    return client.post(url, json={"skus": skus}, headers=write_headers(caller, **headers))


def cancel(client: TestClient, caller: Caller, count_id: str, **headers):
    return client.post(f"{COUNTS}/{count_id}/cancel", headers=write_headers(caller, **headers))


def get_count(client: TestClient, caller: Caller, count_id: str, rid: str | None = None):
    return client.get(f"{COUNTS}/{count_id}", headers=read_headers(caller, rid))


def find_counts(client: TestClient, caller: Caller, params) -> object:
    return client.get(COUNTS, params=params, headers=read_headers(caller))


def new_count(client: TestClient, caller: Caller, **extra: object) -> dict:
    return created(open_count(client, caller, opening(caller, **extra)))


# --- 造数据与观察 ---


def preset_balance(engine: Engine, caller: Caller, sku_id: str, **values: object) -> None:
    with engine.begin() as db:
        db.execute(balance_insert(caller.tenant_id, caller.pool_id, sku_id, **values))


def stored_count(engine: Engine, count_id: str) -> dict:
    statement = select(*StockCount.__table__.columns).where(StockCount.count_id == count_id)
    with engine.connect() as db:
        return dict(db.execute(statement).mappings().one())


def counts_of(engine: Engine, caller: Caller) -> list[dict]:
    statement = select(*StockCount.__table__.columns).where(
        StockCount.tenant_id == caller.tenant_id
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def stored_lines(engine: Engine, count_id: str) -> list[dict]:
    statement = (
        select(*StockCountLine.__table__.columns)
        .where(StockCountLine.count_id == count_id)
        .order_by(StockCountLine.sku_id)
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def balance_row(engine: Engine, caller: Caller, sku_id: str) -> dict | None:
    statement = select(*StockBalance.__table__.columns).where(
        StockBalance.tenant_id == caller.tenant_id,
        StockBalance.pool_id == caller.pool_id,
        StockBalance.sku_id == sku_id,
    )
    with engine.connect() as db:
        row = db.execute(statement).mappings().first()
        return None if row is None else dict(row)


def receive(client: TestClient, caller: Caller, sku_id: str, quantity: int) -> None:
    body = {
        "pool_id": caller.pool_id,
        "receipt_ref": new_ref(),
        "lines": [{"line_ref": "L", "sku_id": sku_id, "quantity": quantity}],
    }
    created(client.post(RECEIPTS, json=body, headers=write_headers(caller)))


def expected_line(
    sku_id: str, shelf: int, unsellable: int, snapshot: tuple, version: int, submitted_at: str
) -> dict:
    stock_balance, reserved, unavailable, paid_pending_dispatch = snapshot
    return {
        "sku_id": sku_id,
        "shelf_count": shelf,
        "unsellable_count": unsellable,
        "snapshot": {
            "stock_balance": stock_balance,
            "reserved": reserved,
            "unavailable": unavailable,
            "paid_pending_dispatch": paid_pending_dispatch,
            "balance_version": version,
        },
        "submitted_at": submitted_at,
        # §6 的公式。
        "d_shelf": shelf - (stock_balance - unavailable + paid_pending_dispatch),
        "d_u": unsellable - unavailable,
        "entry_id": None,
    }


# --- 开单 ---


def test_opening_a_count_returns_201_and_the_representation(client, engine, caller):
    ref, key, rid = new_ref(), new_key(), request_id()
    body = opening(caller, ref, note="  aisle 4  ")
    data = created(open_count(client, caller, body, key=key, rid=rid))

    assert set(data) == COUNT_KEYS
    assert data["count_id"].startswith("cnt_")
    assert (data["count_ref"], data["pool_id"], data["status"]) == (ref, caller.pool_id, "open")
    assert data["note"] == "aisle 4"
    for name in ("confirmed_at", "reason_code", "confirm_note", "cancelled_at"):
        assert data[name] is None
    assert data["lines"] == []

    row = stored_count(engine, data["count_id"])
    assert row["tenant_id"] == caller.tenant_id
    assert (row["count_ref"], row["status"], row["note"]) == (ref, "open", "aisle 4")
    assert row["credential_id"] == caller.credential_id
    assert (row["actor_ref"], row["request_id"]) == (ACTOR_REF, rid)
    assert format_timestamp(row["created_at"]) == data["created_at"]
    assert entries(engine, caller) == []

    # 同键：原样重放。
    replay = open_count(client, caller, body, key=key)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["data"] == data


def test_reopening_a_ref_answers_with_the_current_count(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    ref = new_ref()
    body = opening(caller, ref, note="first")
    data = created(open_count(client, caller, body))
    count_id = data["count_id"]

    # 换键同内容：200 与当前表示（含已提交的盘点行），不开新单。
    current = data_of(submit(client, caller, count_id, [counted(sku_id, 3)]))
    again = open_count(client, caller, body)
    assert "Idempotency-Replayed" not in again.headers
    assert data_of(again) == current
    assert data_of(get_count(client, caller, count_id)) == current

    # 已取消的单同样返回当前表示，状态 cancelled。
    cancelled = data_of(cancel(client, caller, count_id))
    assert data_of(open_count(client, caller, body)) == cancelled
    assert cancelled["status"] == "cancelled"

    # 换键不同内容：409 BUSINESS_REF_CONFLICT，记事件。
    rid = request_id()
    changed = opening(caller, ref, note="second")
    error = error_of(open_count(client, caller, changed, rid=rid), 409)
    assert error["code"] == "BUSINESS_REF_CONFLICT"
    [event] = events(engine, rid)
    assert event["event_type"] == "business_ref_conflict"
    assert event["details"] == {"ref_type": "count", "ref": ref}

    [row] = counts_of(engine, caller)
    assert (row["count_id"], row["note"]) == (count_id, "first")


def test_opening_on_an_initializing_pool_is_409(client, engine):
    caller = issue(engine)
    error = error_of(open_count(client, caller), 409)
    assert error["code"] == "POOL_NOT_LIVE"
    assert error["details"] == {"pool_status": "initializing"}
    assert counts_of(engine, caller) == []


def test_a_bad_opening_body_is_422(client, engine, caller):
    for body in (
        opening(caller, "has space"),
        opening(caller, note="   "),
        opening(caller, extra="x"),
        {"pool_id": caller.pool_id},
    ):
        assert error_of(open_count(client, caller, body), 422)["code"] == "INVALID_REQUEST"
    assert counts_of(engine, caller) == []


@pytest.mark.parametrize("same_content", [True, False])
def test_concurrent_openings_with_one_ref_open_one_count(
    client, engine, caller, monkeypatch, same_content
):
    ref = new_ref()
    early_body = opening(caller, ref, note="early")
    late_body = early_body if same_content else opening(caller, ref, note="late")
    late_rid = request_id()

    # late 查过 count_ref（还没有）之后停住；early 开单并提交；再放行 late，
    # 它写盘点单撞上唯一约束，在新事务里按 early 的单重新判定。
    before_live = Gate()
    require_live = count_service.require_live

    def gated(execution, pool_id):
        if execution.context.request_id == late_rid:
            before_live.pass_through()
        return require_live(execution, pool_id)

    monkeypatch.setattr(count_service, "require_live", gated)
    other_client = TestClient(client.app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            late = pool.submit(open_count, client, caller, late_body, rid=late_rid)
            assert before_live.reached.wait(10)
            early_data = created(open_count(other_client, caller, early_body))
        finally:
            before_live.proceed.set()
        late_response = late.result()

    if same_content:
        assert data_of(late_response) == early_data
        assert events(engine, late_rid) == []
    else:
        assert error_of(late_response, 409)["code"] == "BUSINESS_REF_CONFLICT"
        [event] = events(engine, late_rid)
        assert event["event_type"] == "business_ref_conflict"
    [row] = counts_of(engine, caller)
    assert row["count_id"] == early_data["count_id"]


# --- 提交实数 ---


def test_submitting_records_the_snapshot_and_the_deltas(client, engine, caller):
    stocked, fresh = sorted(add_skus(engine, caller.tenant_id, 2))
    preset_balance(
        engine,
        caller,
        stocked,
        stock_balance=10,
        reserved=2,
        unavailable=3,
        paid_pending_dispatch=4,
        balance_version=7,
    )
    count_id = new_count(client, caller)["count_id"]
    assert balance_row(engine, caller, fresh) is None

    rid = request_id()
    skus = [counted(fresh, 2, 1), counted(stocked, 9, 1)]
    data = data_of(submit(client, caller, count_id, skus, rid=rid))

    assert set(data) == COUNT_KEYS and data["status"] == "open"
    assert [set(line) for line in data["lines"]] == [LINE_KEYS, LINE_KEYS]
    assert all(set(line["snapshot"]) == SNAPSHOT_KEYS for line in data["lines"])
    submitted_at = data["lines"][0]["submitted_at"]
    # lines 按 sku_id 升序；快照等于提交时的账面。
    expected = {
        stocked: expected_line(stocked, 9, 1, (10, 2, 3, 4), 7, submitted_at),
        fresh: expected_line(fresh, 2, 1, (0, 0, 0, 0), 0, submitted_at),
    }
    assert data["lines"] == [expected[sku_id] for sku_id in sorted(expected)]
    assert expected[stocked]["d_shelf"] == -2 and expected[stocked]["d_u"] == -2
    assert expected[fresh]["d_shelf"] == 2 and expected[fresh]["d_u"] == 1

    # 还没有余额行的 SKU 先预建（全零），余额不变，不写流水。
    fresh_balance = balance_row(engine, caller, fresh)
    assert fresh_balance is not None and fresh_balance["balance_version"] == 0
    assert quantities_of(engine, caller, fresh) == (0, 0, 0, 0)
    assert quantities_of(engine, caller, stocked) == (10, 2, 3, 4)
    assert entries(engine, caller) == []

    for line in stored_lines(engine, count_id):
        assert (line["tenant_id"], line["pool_id"]) == (caller.tenant_id, caller.pool_id)
        assert (line["actor_ref"], line["request_id"]) == (ACTOR_REF, rid)
        assert format_timestamp(line["submitted_at"]) == submitted_at
        assert line["entry_id"] is None
    assert data_of(get_count(client, caller, count_id)) == data


def test_resubmitting_replaces_only_the_listed_skus(client, engine, caller):
    kept, recounted = sorted(add_skus(engine, caller.tenant_id, 2))
    preset_balance(engine, caller, kept, stock_balance=5, balance_version=1)
    preset_balance(engine, caller, recounted, stock_balance=8, balance_version=1)
    count_id = new_count(client, caller)["count_id"]
    first = data_of(submit(client, caller, count_id, [counted(kept, 5), counted(recounted, 6)]))
    [kept_line, _] = stored_lines(engine, count_id)

    # 收货改了账面之后只重新提交一个 SKU：它重取快照，另一个保留原实数与原快照。
    receive(client, caller, kept, 4)
    receive(client, caller, recounted, 2)
    rid = request_id()
    second = data_of(submit(client, caller, count_id, [counted(recounted, 7, 1)], rid=rid))

    assert second["lines"][0] == first["lines"][0]
    submitted_at = second["lines"][1]["submitted_at"]
    assert second["lines"][1] == expected_line(recounted, 7, 1, (10, 0, 0, 0), 2, submitted_at)
    kept_after, recounted_after = stored_lines(engine, count_id)
    assert kept_after == kept_line
    assert (recounted_after["shelf_count"], recounted_after["request_id"]) == (7, rid)
    assert data_of(get_count(client, caller, count_id)) == second


def test_a_merged_sku_is_409(client, engine, caller):
    merged, canonical = add_skus(engine, caller.tenant_id, 2)
    merge(engine, merged, canonical)
    count_id = new_count(client, caller)["count_id"]
    response = submit(client, caller, count_id, [counted(canonical, 1), counted(merged, 1)])
    error = error_of(response, 409)
    assert error["code"] == "SKU_MERGED"
    assert error["details"] == {"sku_id": merged, "merged_into_sku_id": canonical}
    assert stored_lines(engine, count_id) == []


def test_missing_and_foreign_skus_are_404(client, engine, caller):
    foreign = issue(engine)
    [known] = add_skus(engine, caller.tenant_id, 1)
    [foreign_sku] = add_skus(engine, foreign.tenant_id, 1)
    missing = new_id("sku")
    count_id = new_count(client, caller)["count_id"]
    rid = request_id()
    skus = [counted(foreign_sku, 1), counted(known, 1), counted(missing, 1)]
    error = error_of(submit(client, caller, count_id, skus, rid=rid), 404)
    assert error["code"] == "SKU_NOT_FOUND"
    assert error["details"] == {"sku_ids": [foreign_sku, missing]}
    [event] = events(engine, rid)
    assert event["details"] == {"object_type": "sku", "id": foreign_sku}
    assert stored_lines(engine, count_id) == []


def test_more_than_100_skus_on_a_count_is_422_and_keeps_its_lines(client, engine, caller):
    sku_ids = add_skus(engine, caller.tenant_id, 101)
    count_id = new_count(client, caller)["count_id"]
    data_of(submit(client, caller, count_id, [counted(sku_id, 1) for sku_id in sku_ids[:100]]))
    before = stored_lines(engine, count_id)
    assert len(before) == 100

    key = new_key()
    response = submit(client, caller, count_id, [counted(sku_ids[100], 1)], key=key)
    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"]["reason"] == "count_sku_limit"
    assert stored_lines(engine, count_id) == before
    # 按数据库状态判出，作为确定结果保存。
    replay = submit(client, caller, count_id, [counted(sku_ids[100], 1)], key=key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()

    # 重新提交已有的 SKU 不增加总数。
    data = data_of(submit(client, caller, count_id, [counted(sku_ids[0], 2)]))
    assert len(data["lines"]) == 100


def bad_submission(case: str, sku_id: str) -> list:
    return {
        "duplicate_sku_id": [counted(sku_id, 1), counted(sku_id, 2)],
        "negative_shelf": [counted(sku_id, -1)],
        "negative_unsellable": [counted(sku_id, 1, -1)],
        "too_many": [counted(sku_id, 1_000_001)],
        "string_count": [counted(sku_id, "5")],
        "bool_count": [counted(sku_id, True)],
        "missing_field": [{"sku_id": sku_id, "shelf_count": 1}],
        "unknown_field": [counted(sku_id, 1) | {"note": "x"}],
        "empty": [],
    }[case]


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_sku_id",
        "negative_shelf",
        "negative_unsellable",
        "too_many",
        "string_count",
        "bool_count",
        "missing_field",
        "unknown_field",
        "empty",
    ],
)
def test_a_bad_submission_is_422(client, engine, caller, case):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    count_id = new_count(client, caller)["count_id"]
    response = submit(client, caller, count_id, bad_submission(case, sku_id))
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert stored_lines(engine, count_id) == []


def test_zero_and_a_million_are_accepted_and_101_skus_are_413(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    count_id = new_count(client, caller)["count_id"]
    data = data_of(submit(client, caller, count_id, [counted(sku_id, 1_000_000, 0)]))
    [line] = data["lines"]
    assert (line["shelf_count"], line["unsellable_count"]) == (1_000_000, 0)
    skus = [counted(new_id("sku"), 1) for _ in range(101)]
    assert error_of(submit(client, caller, count_id, skus), 413)["code"] == "REQUEST_TOO_LARGE"


# --- 取消与状态 ---


def test_cancelling_drops_the_lines_and_keeps_the_ref(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    preset_balance(engine, caller, sku_id, stock_balance=6, balance_version=2)
    ref = new_ref()
    body = opening(caller, ref)
    count_id = created(open_count(client, caller, body))["count_id"]
    data_of(submit(client, caller, count_id, [counted(sku_id, 4)]))
    assert len(stored_lines(engine, count_id)) == 1

    rid = request_id()
    data = data_of(cancel(client, caller, count_id, rid=rid))
    assert set(data) == COUNT_KEYS
    assert (data["status"], data["lines"]) == ("cancelled", [])
    row = stored_count(engine, count_id)
    assert row["status"] == "cancelled"
    assert format_timestamp(row["cancelled_at"]) == data["cancelled_at"]
    assert row["cancelled_credential_id"] == caller.credential_id
    assert (row["cancelled_actor_ref"], row["cancelled_request_id"]) == (ACTOR_REF, rid)
    assert stored_lines(engine, count_id) == []
    # 不写流水，账面不变。
    assert entries(engine, caller) == []
    assert quantities_of(engine, caller, sku_id) == (6, 0, 0, 0)
    assert data_of(get_count(client, caller, count_id)) == data

    # 已取消的单再提交、再取消：409 STATE_CONFLICT。
    for response in (
        submit(client, caller, count_id, [counted(sku_id, 1)]),
        cancel(client, caller, count_id),
    ):
        error = error_of(response, 409)
        assert error["code"] == "STATE_CONFLICT"
        assert error["details"] == {"status": "cancelled"}
    assert stored_lines(engine, count_id) == []

    # count_ref 仍被占用：同内容得原单，不同内容 409。
    assert data_of(open_count(client, caller, body)) == data
    changed = opening(caller, ref, note="again")
    assert error_of(open_count(client, caller, changed), 409)["code"] == "BUSINESS_REF_CONFLICT"
    assert len(counts_of(engine, caller)) == 1


def test_cancel_takes_no_body(client, engine, caller):
    count_id = new_count(client, caller)["count_id"]
    url = f"{COUNTS}/{count_id}/cancel"
    response = client.post(url, json={"reason": "x"}, headers=write_headers(caller))
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert stored_count(engine, count_id)["status"] == "open"


@pytest.mark.parametrize("first", ["submit", "cancel"])
def test_submit_and_cancel_on_one_count_run_one_after_the_other(
    client, engine, caller, monkeypatch, first
):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    count_id = new_count(client, caller)["count_id"]

    # 先到者锁住盘点单行后停住；后到者在这把锁上等待，先到者提交后才继续。
    holding = Gate()
    lock_count = count_service.lock_count

    def gated(connection, tenant_id, locked_id):
        record = lock_count(connection, tenant_id, locked_id)
        holding.pass_through()
        return record

    monkeypatch.setattr(count_service, "lock_count", gated)
    other_client = TestClient(client.app)
    commands = {
        "submit": lambda test_client: submit(test_client, caller, count_id, [counted(sku_id, 3)]),
        "cancel": lambda test_client: cancel(test_client, caller, count_id),
    }
    second = "cancel" if first == "submit" else "submit"
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            early = pool.submit(commands[first], client)
            assert holding.reached.wait(10)
            late = pool.submit(commands[second], other_client)
            time.sleep(0.5)
            assert not late.done()
        finally:
            holding.proceed.set()
        responses = {first: early.result(), second: late.result()}

    assert data_of(responses["cancel"])["status"] == "cancelled"
    if first == "submit":
        assert len(data_of(responses["submit"])["lines"]) == 1
    else:
        error = error_of(responses["submit"], 409)
        assert error["code"] == "STATE_CONFLICT"
    # 无论先后，取消之后都没有实数留下。
    assert stored_count(engine, count_id)["status"] == "cancelled"
    assert stored_lines(engine, count_id) == []
    assert data_of(get_count(client, caller, count_id))["lines"] == []


# --- 查询 ---


def test_both_queries_return_the_same_count(client, engine, caller):
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    ref = new_ref()
    count_id = new_count(client, caller, count_ref=ref)["count_id"]
    submitted = data_of(submit(client, caller, count_id, [counted(sku_id, 2)]))

    by_id = data_of(get_count(client, caller, count_id))
    by_ref = data_of(find_counts(client, caller, {"count_ref": ref}))
    assert by_id == submitted
    assert by_ref == {"counts": [by_id]}

    # 查不到：空数组。
    assert data_of(find_counts(client, caller, {"count_ref": new_ref()})) == {"counts": []}


def test_a_foreign_count_looks_exactly_like_a_missing_one(client, engine, caller):
    foreign = live_caller(engine)
    foreign_ref = new_ref()
    foreign_id = new_count(client, foreign, count_ref=foreign_ref)["count_id"]
    missing_id = new_id("cnt")

    missing_rid, foreign_rid = request_id(), request_id()
    missing = error_of(get_count(client, caller, missing_id, missing_rid), 404)
    found = error_of(get_count(client, caller, foreign_id, foreign_rid), 404)
    assert missing == found
    assert missing["code"] == "COUNT_NOT_FOUND"
    assert events(engine, missing_rid) == []
    [event] = events(engine, foreign_rid)
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "count", "id": foreign_id}

    # 按 count_ref 只查本凭据的销售入口。
    assert data_of(find_counts(client, caller, {"count_ref": foreign_ref})) == {"counts": []}

    # 提交与取消在池授权时同样 404。
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    for send in (
        lambda count_id, rid: submit(client, caller, count_id, [counted(sku_id, 1)], rid=rid),
        lambda count_id, rid: cancel(client, caller, count_id, rid=rid),
    ):
        missing_rid, foreign_rid = request_id(), request_id()
        missing = error_of(send(missing_id, missing_rid), 404)
        assert error_of(send(foreign_id, foreign_rid), 404) == missing
        assert missing["code"] == "COUNT_NOT_FOUND"
        assert events(engine, missing_rid) == []
        [event] = events(engine, foreign_rid)
        assert event["details"] == {"object_type": "count", "id": foreign_id}
    assert stored_count(engine, foreign_id)["status"] == "open"


@pytest.mark.parametrize("channel_status", [None, "inactive"])
def test_a_credential_without_a_channel_finds_no_counts(client, engine, channel_status):
    caller = issue(engine, channel_status=channel_status)
    data = data_of(find_counts(client, caller, {"count_ref": new_ref()}))
    assert data == {"counts": []}


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"count_ref": "has space"},
        {"count_ref": ""},
        [("count_ref", "a"), ("count_ref", "b")],
        {"count_ref": "a", "pool_id": "p"},
    ],
)
def test_bad_query_parameters_are_422(client, engine, caller, params):
    error = error_of(find_counts(client, caller, params), 422)
    assert error["code"] == "INVALID_REQUEST"


# --- 授权 ---


def test_writes_need_stock_write_and_a_stock_role(client, engine, caller):
    count_id = new_count(client, caller)["count_id"]
    reader = live_caller(engine, scopes=[STOCK_READ])
    for response in (
        open_count(client, reader, opening(reader)),
        cancel(client, reader, count_id),
    ):
        assert error_of(response, 403)["code"] == "FORBIDDEN"
    assert counts_of(engine, reader) == []

    # 缺操作者头 400。
    headers = write_headers(caller)
    del headers["X-Actor-Ref"], headers["X-Actor-Role"]
    response = client.post(COUNTS, json=opening(caller), headers=headers)
    assert error_of(response, 400)["code"] == "INVALID_REQUEST"
    response = client.post(f"{COUNTS}/{count_id}/cancel", headers=headers)
    assert error_of(response, 400)["code"] == "INVALID_REQUEST"

    # catalog_admin 不被接受；stock_admin 与 stock_operator 都可以。
    rid = request_id()
    response = open_count(client, caller, opening(caller), rid=rid, role=CATALOG_ADMIN)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "role", "role": CATALOG_ADMIN}
    created(open_count(client, caller, opening(caller), role=STOCK_ADMIN))
    assert data_of(cancel(client, caller, count_id, role=STOCK_ADMIN))["status"] == "cancelled"
    assert len(counts_of(engine, caller)) == 2


def test_opening_on_another_tenants_pool_is_404(client, engine, caller):
    foreign = live_caller(engine)
    rid = request_id()
    error = error_of(open_count(client, caller, opening(foreign), rid=rid), 404)
    assert error["code"] == "POOL_NOT_FOUND"
    [event] = events(engine, rid)
    assert event["details"] == {"object_type": "pool", "id": foreign.pool_id}
    assert counts_of(engine, caller) == [] and counts_of(engine, foreign) == []
