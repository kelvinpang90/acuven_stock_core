"""期初导入的预览、确认与查询，以及池切 live（契约 §6.2、§6.3、§7.2、§10 第 3、4 条）。

在真 MySQL 上跑；每个测试建自己的租户、池与凭据，SKU 与余额行由测试直接在库里造（自己提交的连接）。
并发的测试在服务层的 lock_balances 前后放 Gate，让两笔确认在同一余额行的锁上交错。
"""

import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update

from app.config import Settings
from app.domain.requests import STOCK_ADMIN, STOCK_OPERATOR
from app.domain.service_auth import STOCK_READ, STOCK_WRITE, generate_token, token_digest
from app.main import create_app
from app.models.catalog import Sku
from app.models.inventory import StockImport, StockPool
from app.services import stock_imports as stock_import_service
from app.services.stock import format_timestamp
from tests.integration.conftest import (
    TEST_DATABASE_URL,
    add_credential,
    balance_insert,
    new_id,
    went_live,
)
from tests.integration.test_api_auth import A_SECOND_AGO, Caller, error_of, events, issue
from tests.integration.test_catalog_create_api import created
from tests.integration.test_catalog_queries_api import TIMESTAMP, data_of
from tests.integration.test_catalog_updates_api import db_now
from tests.integration.test_idempotency import add_skus, balance_of
from tests.integration.test_service_auth_schema import token_insert
from tests.integration.test_stock_service import Gate, entries, judgement, quantities_of

IMPORTS = "/v1/stock-imports"
ACTOR_REF = "admin-7"
REPRESENTATION = {
    "import_id",
    "pool_id",
    "status",
    "expires_at",
    "created_at",
    "confirmed_at",
    "rows",
    "summary",
    "result",
}
GO_LIVE_COLUMNS = {
    "status",
    "went_live_at",
    "went_live_credential_id",
    "went_live_actor_ref",
    "went_live_request_id",
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


# --- 请求 ---


def new_key() -> str:
    return f"shop:stock-import:{uuid4().hex}"


def request_id() -> str:
    return f"stock-import-test-{uuid4().hex}"


def write_headers(
    caller: Caller, key: str | None = None, rid: str | None = None, role: str = STOCK_ADMIN
) -> dict:
    extra = {"Idempotency-Key": key or new_key(), "X-Request-ID": rid or request_id()}
    return caller.auth | {"X-Actor-Ref": ACTOR_REF, "X-Actor-Role": role} | extra


def line(sku_id: object, quantity: object) -> dict:
    return {"sku_id": sku_id, "quantity": quantity}


def post_preview(client: TestClient, caller: Caller, sent: list, **headers):
    body = {"pool_id": caller.pool_id, "rows": sent}
    return client.post(IMPORTS, json=body, headers=write_headers(caller, **headers))


def preview(client: TestClient, caller: Caller, sent: list) -> dict:
    return created(post_preview(client, caller, sent))


def post_confirm(client: TestClient, caller: Caller, import_id: str, **headers):
    url = f"{IMPORTS}/{import_id}/confirm"
    return client.post(url, headers=write_headers(caller, **headers))


def confirm(client: TestClient, caller: Caller, import_id: str) -> dict:
    return data_of(post_confirm(client, caller, import_id))


def get_import(client: TestClient, caller: Caller, import_id: str) -> dict:
    return data_of(client.get(f"{IMPORTS}/{import_id}", headers=caller.auth))


def post_go_live(client: TestClient, caller: Caller, pool_id: str, **headers):
    return client.post(f"/v1/pools/{pool_id}/go-live", headers=write_headers(caller, **headers))


def outcomes(data: dict) -> list[tuple]:
    return [(row["outcome"], row["preview_result"], row["failure"]) for row in data["result"]]


def parse_timestamp(value: str) -> datetime:
    assert TIMESTAMP.fullmatch(value), value
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")


# --- 造数据与观察 ---


def credential_of(engine: Engine, caller: Caller, scopes: list[str]) -> Caller:
    """同租户另一个凭据，只有给定的 scope、没有销售入口。"""
    plain = generate_token()
    with engine.begin() as db:
        credential_id = add_credential(db, caller.tenant_id, scopes=scopes)
        db.execute(token_insert(caller.tenant_id, credential_id, token_digest=token_digest(plain)))
    return Caller(caller.tenant_id, caller.pool_id, credential_id, plain)


def merge(engine: Engine, sku_id: str, into: str) -> None:
    with engine.begin() as db:
        statement = update(Sku).where(Sku.sku_id == sku_id)
        db.execute(statement.values(status="inactive", merged_into_sku_id=into))


def set_live(engine: Engine, caller: Caller) -> None:
    with engine.begin() as db:
        statement = update(StockPool).where(StockPool.pool_id == caller.pool_id)
        db.execute(statement.values(status="live", **went_live(caller.credential_id)))


def expire(engine: Engine, import_id: str) -> None:
    """把有效期改到数据库时间之前。"""
    with engine.begin() as db:
        statement = update(StockImport).where(StockImport.import_id == import_id)
        db.execute(statement.values(expires_at=A_SECOND_AGO))


def imports_of(engine: Engine, tenant_id: str) -> list[dict]:
    statement = select(*StockImport.__table__.columns).where(StockImport.tenant_id == tenant_id)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def stored_import(engine: Engine, import_id: str) -> dict:
    statement = select(*StockImport.__table__.columns).where(StockImport.import_id == import_id)
    with engine.connect() as db:
        return dict(db.execute(statement).mappings().one())


def stored_pool(engine: Engine, pool_id: str) -> dict:
    statement = select(*StockPool.__table__.columns).where(StockPool.pool_id == pool_id)
    with engine.connect() as db:
        return dict(db.execute(statement).mappings().one())


# --- 预览 ---


def test_a_preview_judges_each_row_and_stores_the_import(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    fresh, zero, dup, merged, canonical, opened, inactive, bad = add_skus(
        engine, caller.tenant_id, 8
    )
    [foreign_sku] = add_skus(engine, foreign.tenant_id, 1)
    merge(engine, merged, canonical)
    with engine.begin() as db:
        db.execute(update(Sku).where(Sku.sku_id == inactive).values(status="inactive"))
        values = {"opening_entry_id": new_id("ent"), "stock_balance": 4, "balance_version": 1}
        db.execute(balance_insert(caller.tenant_id, caller.pool_id, opened, **values))
    opened_before = balance_of(engine, caller, caller.pool_id, opened)
    sent = [
        line(fresh, 5),
        line(zero, 0),
        line(dup, 1),
        line(dup, 2),
        line(dup, -1),  # 格式不合格的行不参加重复比较
        line(foreign_sku, 1),  # 别的租户的 SKU 同不存在
        line(new_id("sku"), 1),
        line(merged, 1),
        line(opened, 1),
        line(7, "5"),
        line(bad, 1_000_001),
        line(bad, True),
        {"sku_id": bad, "quantity": 1, "note": "extra"},
        {"quantity": 3},
        line(inactive, 1_000_000),  # 停用的 SKU 可以导期初
    ]
    key, rid = new_key(), request_id()
    data = created(post_preview(client, caller, sent, key=key, rid=rid))
    now = db_now(engine)

    assert set(data) == REPRESENTATION
    assert data["import_id"].startswith("simp_")
    assert data["pool_id"] == caller.pool_id
    assert (data["status"], data["confirmed_at"], data["result"]) == ("previewed", None, None)
    expected = [
        (fresh, 5, "ok"),
        (zero, 0, "ok"),
        (dup, 1, "duplicate_sku"),
        (dup, 2, "duplicate_sku"),
        (dup, -1, "invalid_format"),
        (foreign_sku, 1, "sku_not_found"),
        (sent[6]["sku_id"], 1, "sku_not_found"),
        (merged, 1, "sku_merged"),
        (opened, 1, "already_opened"),
        (None, None, "invalid_format"),  # sku_id 不是字符串、quantity 不是整数：都回显 null
        (bad, 1_000_001, "invalid_format"),
        (bad, None, "invalid_format"),  # true 不是整数
        (bad, 1, "invalid_format"),
        (None, 3, "invalid_format"),
        (inactive, 1_000_000, "ok"),
    ]
    assert data["rows"] == [
        {"index": index, "sku_id": sku_id, "quantity": quantity, "result": result}
        for index, (sku_id, quantity, result) in enumerate(expected)
    ]
    assert data["summary"] == {
        "counts": {
            "invalid_format": 6,
            "duplicate_sku": 2,
            "sku_not_found": 2,
            "sku_merged": 1,
            "already_opened": 1,
            "ok": 3,
        },
        "executable_rows": 3,
        "executable_quantity": 1_000_005,
    }

    # 有效期在插入的同一条语句里取数据库时间：与创建时间正好差 24 小时。
    expires_at = parse_timestamp(data["expires_at"])
    created_at = parse_timestamp(data["created_at"])
    assert expires_at - created_at == timedelta(hours=24)
    assert abs(expires_at - (now + timedelta(hours=24))) < timedelta(seconds=10)

    stored = stored_import(engine, data["import_id"])
    assert (stored["tenant_id"], stored["pool_id"]) == (caller.tenant_id, caller.pool_id)
    assert stored["status"] == "previewed"
    assert stored["credential_id"] == caller.credential_id
    assert (stored["actor_ref"], stored["request_id"]) == (ACTOR_REF, rid)
    assert stored["result"] is None and stored["confirmed_at"] is None
    # 每行原文序列化成字符串存放，读回来与发来的完全相同。
    by_index = sorted(stored["rows"], key=lambda row: row["index"])
    assert [json.loads(row["source"]) for row in by_index] == sent
    assert get_import(client, caller, data["import_id"]) == data

    # 预览不写流水、不建也不改余额行；别的租户的 SKU 不记安全事件。
    assert entries(engine, caller) == []
    assert balance_of(engine, caller, caller.pool_id, fresh) is None
    assert balance_of(engine, caller, caller.pool_id, opened) == opened_before
    assert events(engine, rid) == []


def top_level_body(case: str, pool_id: str) -> dict:
    rows = [line(new_id("sku"), 1)]
    return {
        "extra_field": {"pool_id": pool_id, "rows": rows, "note": "x"},
        "no_pool_id": {"rows": rows},
        "pool_id_not_a_string": {"pool_id": 1, "rows": rows},
        "no_rows": {"pool_id": pool_id},
        "rows_not_an_array": {"pool_id": pool_id, "rows": rows[0]},
        "rows_empty": {"pool_id": pool_id, "rows": []},
        "row_not_an_object": {"pool_id": pool_id, "rows": [rows[0], ["sku", 1]]},
    }[case]


@pytest.mark.parametrize(
    "case",
    [
        "extra_field",
        "no_pool_id",
        "pool_id_not_a_string",
        "no_rows",
        "rows_not_an_array",
        "rows_empty",
        "row_not_an_object",
    ],
)
def test_a_bad_top_level_structure_is_422(client, engine, case):
    caller = issue(engine)
    rid = request_id()
    body = top_level_body(case, caller.pool_id)
    response = client.post(IMPORTS, json=body, headers=write_headers(caller, rid=rid))
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert imports_of(engine, caller.tenant_id) == []
    assert events(engine, rid) == []


def test_100_rows_are_accepted_and_101_rows_are_413(client, engine):
    caller = issue(engine)
    sent = [line(new_id("sku"), 1) for _ in range(100)]
    data = preview(client, caller, sent)
    assert data["summary"]["counts"]["sku_not_found"] == 100

    response = post_preview(client, caller, [*sent, line(new_id("sku"), 1)])
    assert error_of(response, 413)["code"] == "REQUEST_TOO_LARGE"
    stored = [row["import_id"] for row in imports_of(engine, caller.tenant_id)]
    assert stored == [data["import_id"]]


def test_a_preview_on_a_live_pool_is_409_and_saved(client, engine):
    caller = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    set_live(engine, caller)
    key = new_key()
    first = post_preview(client, caller, [line(sku_id, 1)], key=key)
    error = error_of(first, 409)
    assert error["code"] == "POOL_ALREADY_LIVE"
    assert error["details"] == {"pool_status": "live"}
    # 409 是确定结果：同键原样重放。
    replay = post_preview(client, caller, [line(sku_id, 1)], key=key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert imports_of(engine, caller.tenant_id) == []


# --- 确认 ---


def test_confirm_opens_ok_rows_and_skips_the_rest(client, engine):
    caller = issue(engine)
    counted, empty = add_skus(engine, caller.tenant_id, 2)
    sent = [line(counted, 5), line(empty, 0), line(counted, "5")]
    import_id = preview(client, caller, sent)["import_id"]
    key, rid = new_key(), request_id()
    data = data_of(post_confirm(client, caller, import_id, key=key, rid=rid))

    assert set(data) == REPRESENTATION
    assert data["status"] == "confirmed"
    [entry] = entries(engine, caller)
    assert data["result"] == [
        {
            "index": 0,
            "sku_id": counted,
            "quantity": 5,
            "outcome": "created",
            "entry_id": entry["entry_id"],
            "preview_result": "ok",
            "failure": None,
        },
        {
            "index": 1,
            "sku_id": empty,
            "quantity": 0,
            "outcome": "created",
            "entry_id": None,
            "preview_result": "ok",
            "failure": None,
        },
        {
            "index": 2,
            "sku_id": counted,
            "quantity": None,
            "outcome": "skipped",
            "entry_id": None,
            "preview_result": "invalid_format",
            "failure": None,
        },
    ]

    # 一条 OPENING_BALANCE：来源是这个导入，带本次请求的凭据、操作者与幂等键。
    assert (entry["sku_id"], entry["entry_type"]) == (counted, "OPENING_BALANCE")
    assert (entry["stock_balance_delta"], entry["stock_balance_after"]) == (5, 5)
    assert (entry["source_type"], entry["source_ref"]) == ("stock_import", import_id)
    assert (entry["actor_ref"], entry["actor_role"]) == (ACTOR_REF, STOCK_ADMIN)
    assert (entry["idempotency_key"], entry["request_id"]) == (key, rid)
    assert entry["credential_id"] == caller.credential_id
    assert (entry["reason_code"], entry["note"], entry["line_ref"]) == (None, None, None)
    assert entry["occurred_at"] == entry["recorded_at"]
    assert data["confirmed_at"] == format_timestamp(entry["recorded_at"])

    # 余额行的有效期初与两个判定列随之更新；数量为 0 的行已预建，但不写流水、不算有效期初。
    assert quantities_of(engine, caller, counted) == (5, 0, 0, 0)
    assert judgement(engine, caller, counted) == (1, 1, 1, entry["entry_id"])
    assert judgement(engine, caller, empty) == (0, 0, 0, None)

    stored = stored_import(engine, import_id)
    assert stored["status"] == "confirmed"
    assert stored["confirmed_credential_id"] == caller.credential_id
    assert (stored["confirmed_actor_ref"], stored["confirmed_request_id"]) == (ACTOR_REF, rid)
    assert stored["result"] == data["result"]
    assert get_import(client, caller, import_id) == data


def test_rows_that_changed_after_the_preview_fail_and_the_others_are_created(client, engine):
    caller = issue(engine)
    opened_meanwhile, merged_meanwhile, canonical, untouched = add_skus(engine, caller.tenant_id, 4)
    sent = [line(opened_meanwhile, 3), line(merged_meanwhile, 4), line(untouched, 6)]
    import_id = preview(client, caller, sent)["import_id"]

    # 预览之后：另一个导入先确认了同一个 SKU，另一个 SKU 被合并。
    other = preview(client, caller, [line(opened_meanwhile, 2)])["import_id"]
    assert outcomes(confirm(client, caller, other)) == [("created", "ok", None)]
    merge(engine, merged_meanwhile, canonical)

    data = confirm(client, caller, import_id)
    assert outcomes(data) == [
        ("failed", "ok", "already_opened"),
        ("failed", "ok", "sku_merged"),
        ("created", "ok", None),
    ]
    assert [row["entry_id"] for row in data["result"][:2]] == [None, None]
    assert quantities_of(engine, caller, opened_meanwhile) == (2, 0, 0, 0)
    assert quantities_of(engine, caller, merged_meanwhile) == (0, 0, 0, 0)
    assert quantities_of(engine, caller, untouched) == (6, 0, 0, 0)
    booked = [(entry["sku_id"], entry["source_ref"]) for entry in entries(engine, caller)]
    assert booked == sorted([(opened_meanwhile, other), (untouched, import_id)])


def test_confirming_again_returns_the_first_result_without_booking_twice(client, engine):
    caller = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    import_id = preview(client, caller, [line(sku_id, 8)])["import_id"]
    key = new_key()
    first = post_confirm(client, caller, import_id, key=key)
    data = data_of(first)
    booked = entries(engine, caller)
    stored = stored_import(engine, import_id)

    # 同键：原样重放首次的响应。
    replay = post_confirm(client, caller, import_id, key=key)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()

    # 换键：已确认过，200 与当前表示，不再执行。
    again = post_confirm(client, caller, import_id)
    assert "Idempotency-Replayed" not in again.headers
    assert data_of(again) == data

    assert entries(engine, caller) == booked
    assert quantities_of(engine, caller, sku_id) == (8, 0, 0, 0)
    assert stored_import(engine, import_id) == stored


def test_an_expired_import_cannot_be_confirmed_and_shows_as_expired(client, engine):
    caller = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    previewed = preview(client, caller, [line(sku_id, 2)])
    import_id = previewed["import_id"]
    # 只有 stock.read、不带操作者头的凭据也能查。
    reader = credential_of(engine, caller, [STOCK_READ])
    assert get_import(client, reader, import_id) == previewed

    expire(engine, import_id)
    error = error_of(post_confirm(client, caller, import_id), 409)
    assert error["code"] == "IMPORT_EXPIRED"
    assert entries(engine, caller) == []
    stored = stored_import(engine, import_id)
    assert (stored["status"], stored["result"], stored["confirmed_at"]) == ("previewed", None, None)

    shown = get_import(client, reader, import_id)
    assert shown["status"] == "expired"
    assert parse_timestamp(shown["expires_at"]) <= db_now(engine)
    unchanged = REPRESENTATION - {"status", "expires_at"}
    shown_rest = {name: shown[name] for name in unchanged}
    assert shown_rest == {name: previewed[name] for name in unchanged}


def test_confirm_takes_no_body(client, engine):
    caller = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    import_id = preview(client, caller, [line(sku_id, 2)])["import_id"]
    url = f"{IMPORTS}/{import_id}/confirm"
    response = client.post(url, json={}, headers=write_headers(caller))
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert stored_import(engine, import_id)["status"] == "previewed"
    assert entries(engine, caller) == []


def test_concurrent_confirms_of_the_same_sku_open_it_once(client, engine, monkeypatch):
    caller = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    late = preview(client, caller, [line(sku_id, 3)])["import_id"]
    early = preview(client, caller, [line(sku_id, 7)])["import_id"]
    late_rid, early_rid = request_id(), request_id()

    # late 在锁余额行之前停住（读快照已建立）；early 锁住余额行后停住。
    # 放行 late，它在 early 持有的行锁上等待；再放行 early 提交。
    before_lock, holding_lock = Gate(), Gate()
    lock_balances = stock_import_service.lock_balances

    def gated(execution, pool_id, sku_ids):
        rid = execution.context.request_id
        if rid == late_rid:
            before_lock.pass_through()
        balances = lock_balances(execution, pool_id, sku_ids)
        if rid == early_rid:
            holding_lock.pass_through()
        return balances

    monkeypatch.setattr(stock_import_service, "lock_balances", gated)
    other_client = TestClient(client.app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            late_response = pool.submit(post_confirm, client, caller, late, rid=late_rid)
            assert before_lock.reached.wait(10)
            early_response = pool.submit(post_confirm, other_client, caller, early, rid=early_rid)
            assert holding_lock.reached.wait(10)
            before_lock.proceed.set()
            time.sleep(0.5)
        finally:
            before_lock.proceed.set()
            holding_lock.proceed.set()
        late_data = data_of(late_response.result())
        early_data = data_of(early_response.result())

    # 两笔都完成（没有死锁）；只有一笔期初生效，另一笔看到它、记为 already_opened。
    [entry] = entries(engine, caller)
    assert (entry["source_ref"], entry["stock_balance_delta"]) == (early, 7)
    assert outcomes(early_data) == [("created", "ok", None)]
    assert early_data["result"][0]["entry_id"] == entry["entry_id"]
    assert outcomes(late_data) == [("failed", "ok", "already_opened")]
    assert quantities_of(engine, caller, sku_id) == (7, 0, 0, 0)
    assert judgement(engine, caller, sku_id) == (1, 1, 1, entry["entry_id"])


# --- 池切 live ---


def test_go_live_switches_the_pool_once_and_closes_the_openings(client, engine):
    caller = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    import_id = preview(client, caller, [line(sku_id, 4)])["import_id"]
    pool_url = f"/v1/pools/{caller.pool_id}"
    assert data_of(client.get(pool_url, headers=caller.auth))["status"] == "initializing"
    before = stored_pool(engine, caller.pool_id)

    rid = request_id()
    data = data_of(post_go_live(client, caller, caller.pool_id, rid=rid))
    assert data["status"] == "live"
    stored = stored_pool(engine, caller.pool_id)
    assert stored["status"] == "live"
    assert stored["went_live_at"] == parse_timestamp(data["went_live_at"])
    assert stored["went_live_credential_id"] == caller.credential_id
    assert (stored["went_live_actor_ref"], stored["went_live_request_id"]) == (ACTOR_REF, rid)
    # 池的其他列不变；表示与池的查询相同。
    for column in set(before) - GO_LIVE_COLUMNS:
        assert stored[column] == before[column], column
    assert data_of(client.get(pool_url, headers=caller.auth)) == data

    # 再调（换键）：200 与当前表示，不再改。
    again = post_go_live(client, caller, caller.pool_id)
    assert "Idempotency-Replayed" not in again.headers
    assert data_of(again) == data
    assert stored_pool(engine, caller.pool_id) == stored

    # 切换之后，预览与确认都 409。
    error = error_of(post_preview(client, caller, [line(sku_id, 4)]), 409)
    assert error["code"] == "POOL_ALREADY_LIVE"
    error = error_of(post_confirm(client, caller, import_id), 409)
    assert error["code"] == "POOL_ALREADY_LIVE"
    assert entries(engine, caller) == []
    assert stored_import(engine, import_id)["status"] == "previewed"
    assert len(imports_of(engine, caller.tenant_id)) == 1


def test_go_live_takes_no_body(client, engine):
    caller = issue(engine)
    url = f"/v1/pools/{caller.pool_id}/go-live"
    response = client.post(url, json={}, headers=write_headers(caller))
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert stored_pool(engine, caller.pool_id)["status"] == "initializing"


# --- 授权与跨租户 ---


def test_writes_need_stock_write_actor_headers_and_stock_admin(client, engine):
    caller = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    import_id = preview(client, caller, [line(sku_id, 1)])["import_id"]
    reader = credential_of(engine, caller, [STOCK_READ])
    requests = [
        (IMPORTS, {"pool_id": caller.pool_id, "rows": [line(sku_id, 1)]}),
        (f"{IMPORTS}/{import_id}/confirm", None),
        (f"/v1/pools/{caller.pool_id}/go-live", None),
    ]
    for url, body in requests:
        rid = request_id()
        response = client.post(url, json=body, headers=write_headers(reader, rid=rid))
        assert error_of(response, 403)["code"] == "FORBIDDEN"
        [event] = events(engine, rid)
        assert event["details"] == {"category": "scope", "missing_scopes": [STOCK_WRITE]}

        headers = write_headers(caller)
        del headers["X-Actor-Ref"], headers["X-Actor-Role"]
        response = client.post(url, json=body, headers=headers)
        assert error_of(response, 400)["code"] == "INVALID_REQUEST"

        rid = request_id()
        headers = write_headers(caller, rid=rid, role=STOCK_OPERATOR)
        response = client.post(url, json=body, headers=headers)
        assert error_of(response, 403)["code"] == "FORBIDDEN"
        [event] = events(engine, rid)
        assert event["details"] == {"category": "role", "role": STOCK_OPERATOR}

    assert [row["import_id"] for row in imports_of(engine, caller.tenant_id)] == [import_id]
    assert stored_import(engine, import_id)["status"] == "previewed"
    assert stored_pool(engine, caller.pool_id)["status"] == "initializing"
    assert entries(engine, caller) == []


def test_get_needs_stock_read(client, engine):
    caller = issue(engine)
    [sku_id] = add_skus(engine, caller.tenant_id, 1)
    import_id = preview(client, caller, [line(sku_id, 1)])["import_id"]
    writer = credential_of(engine, caller, [STOCK_WRITE])
    rid = request_id()
    headers = writer.auth | {"X-Request-ID": rid}
    response = client.get(f"{IMPORTS}/{import_id}", headers=headers)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "scope", "missing_scopes": [STOCK_READ]}


def send(client: TestClient, caller: Caller, method: str, url: str, body, rid: str):
    if method == "GET":
        return client.get(url, headers=caller.auth | {"X-Request-ID": rid})
    return client.post(url, json=body, headers=write_headers(caller, rid=rid))


def test_another_tenants_import_and_pool_look_missing_and_are_recorded(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    [foreign_sku] = add_skus(engine, foreign.tenant_id, 1)
    foreign_import = preview(client, foreign, [line(foreign_sku, 1)])["import_id"]
    missing_import, missing_pool = new_id("simp"), new_id("pool")
    rows = [line(foreign_sku, 1)]
    import_event = {"object_type": "stock_import", "id": foreign_import}
    pool_event = {"object_type": "pool", "id": foreign.pool_id}
    cases = [
        (
            "POST",
            f"{IMPORTS}/{foreign_import}/confirm",
            f"{IMPORTS}/{missing_import}/confirm",
            None,
            None,
            "STOCK_IMPORT_NOT_FOUND",
            import_event,
        ),
        (
            "GET",
            f"{IMPORTS}/{foreign_import}",
            f"{IMPORTS}/{missing_import}",
            None,
            None,
            "STOCK_IMPORT_NOT_FOUND",
            import_event,
        ),
        (
            "POST",
            f"/v1/pools/{foreign.pool_id}/go-live",
            f"/v1/pools/{missing_pool}/go-live",
            None,
            None,
            "POOL_NOT_FOUND",
            pool_event,
        ),
        (
            "POST",
            IMPORTS,
            IMPORTS,
            {"pool_id": foreign.pool_id, "rows": rows},
            {"pool_id": missing_pool, "rows": rows},
            "POOL_NOT_FOUND",
            pool_event,
        ),
    ]
    for method, cross_url, missing_url, cross_body, missing_body, code, details in cases:
        # 同一个请求 ID：响应完全相同，只有跨租户那次记事件。
        rid = request_id()
        cross = send(client, caller, method, cross_url, cross_body, rid)
        missing = send(client, caller, method, missing_url, missing_body, rid)
        assert error_of(cross, 404)["code"] == code
        assert cross.json() == missing.json()
        [event] = events(engine, rid)
        assert event["event_type"] == "cross_tenant"
        assert event["tenant_id"] == caller.tenant_id
        assert event["credential_id"] == caller.credential_id
        assert (event["http_method"], event["path"]) == (method, cross_url)
        assert event["details"] == details

    assert stored_import(engine, foreign_import)["status"] == "previewed"
    assert stored_pool(engine, foreign.pool_id)["status"] == "initializing"
    assert imports_of(engine, caller.tenant_id) == []
    assert entries(engine, foreign) == []
