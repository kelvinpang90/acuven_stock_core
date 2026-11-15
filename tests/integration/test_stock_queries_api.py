"""池、余额与流水的查询接口（契约 §6.2 第 1 条、§6.3 池的查询、§6.8、§7.2）。

在真 MySQL 上跑；测试直接在库里造池、SKU、余额行与流水（用自己提交的连接，应用用另一个连接读），
每个测试建自己的租户与凭据，互不干扰；安全事件按每个请求自己的 X-Request-ID 查。
"""

import re
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, select

from app.config import Settings
from app.domain.requests import STOCK_OPERATOR
from app.domain.service_auth import CATALOG_READ, STOCK_READ, STOCK_WRITE
from app.domain.stock import format_ledger_cursor
from app.main import create_app
from app.models.inventory import SalesChannel
from tests.integration.conftest import (
    TEST_DATABASE_URL,
    add_entry,
    add_location,
    add_pool,
    balance_insert,
    new_id,
    pool_insert,
    went_live,
)
from tests.integration.test_api_auth import Caller, error_of, events, issue
from tests.integration.test_catalog_queries_api import add_product, add_sku, ordered_sku_ids

CREATED_AT = datetime(2026, 11, 14, 9, 30, 15, 123456)
# 第 v 条流水记在 RECORDED_AT 之后 v 分钟。
RECORDED_AT = datetime(2026, 11, 15, 8, 30)
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
ZERO_DELTAS = {"stock_balance": 0, "reserved": 0, "unavailable": 0, "paid_pending_dispatch": 0}
ENTRY_FIELDS = {
    "entry_id",
    "pool_id",
    "sku_id",
    "entry_type",
    "deltas",
    "after",
    "balance_version_after",
    "channel_id",
    "source_type",
    "source_ref",
    "line_ref",
    "command_id",
    "actor_ref",
    "actor_role",
    "reason_code",
    "note",
    "request_id",
    "corrects_entry_id",
    "occurred_at",
    "recorded_at",
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


# --- 造数据 ---


def request_id() -> str:
    return f"stock-query-test-{uuid4().hex}"


def data_of(response) -> dict:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    return body["data"]


def recorded_at(version: int) -> datetime:
    return RECORDED_AT + timedelta(minutes=version)


def add_tenant_sku(db: Connection, tenant_id: str, **overrides: object) -> str:
    return add_sku(db, tenant_id, add_product(db, tenant_id), **overrides)


def add_receipts(db: Connection, caller: Caller, sku_id: str, versions: list[int]) -> list[str]:
    """按给定的顺序写收货流水：第 v 条收 5 件、写后 5v 件、记在 recorded_at(v)。"""
    balance = (caller.tenant_id, caller.pool_id, sku_id)
    return [
        add_entry(
            db,
            balance,
            version,
            stock_balance_after=5 * version,
            recorded_at=recorded_at(version),
        )
        for version in versions
    ]


def ledger_sku(engine: Engine, caller: Caller, versions: Sequence[int] = ()) -> str:
    """调用方租户里一个 SKU，在调用方的池有余额行，并写好 versions 这些收货流水。"""
    with engine.begin() as db:
        sku_id = add_tenant_sku(db, caller.tenant_id)
        db.execute(balance_insert(caller.tenant_id, caller.pool_id, sku_id))
        add_receipts(db, caller, sku_id, list(versions))
    return sku_id


def ledger(client: TestClient, caller: Caller, params: dict) -> dict:
    data = data_of(client.get("/v1/ledger", params=params, headers=caller.auth))
    assert set(data) == {"entries", "next_cursor"}
    return data


def versions_of(data: dict) -> list[int]:
    return [entry["balance_version_after"] for entry in data["entries"]]


def assert_same_404(engine: Engine, cross, missing, code: str, rid: str, path: str, details):
    """跨租户与不存在的响应完全相同；同一个请求 ID 下只有跨租户那次记了一条 cross_tenant。"""
    assert error_of(cross, 404)["code"] == code
    assert cross.json() == missing.json()
    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert event["http_method"] == "GET"
    assert event["path"] == path
    assert event["details"] == details
    return event


# --- 池的查询 ---


def test_a_pool_has_its_fields(client, engine):
    caller = issue(engine)
    pool_id = new_id("pool")
    with engine.begin() as db:
        location_id = add_location(db, caller.tenant_id)
        db.execute(
            pool_insert(
                caller.tenant_id,
                location_id,
                pool_id=pool_id,
                name="Kuala Lumpur",
                status="live",
                created_at=CREATED_AT,
                **went_live(caller.credential_id),
            )
        )

    data = data_of(client.get(f"/v1/pools/{pool_id}", headers=caller.auth))
    assert data == {
        "pool_id": pool_id,
        "location_id": location_id,
        "name": "Kuala Lumpur",
        "status": "live",
        "went_live_at": "2026-11-15T08:00:00.000000Z",
        "created_at": "2026-11-14T09:30:15.123456Z",
    }


def test_an_initializing_pool_has_no_went_live_at(client, engine):
    caller = issue(engine)
    data = data_of(client.get(f"/v1/pools/{caller.pool_id}", headers=caller.auth))
    assert data["status"] == "initializing"
    assert data["went_live_at"] is None
    assert TIMESTAMP.fullmatch(data["created_at"])


def test_another_tenants_pool_looks_missing_and_is_recorded(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    rid = request_id()
    headers = caller.auth | {"X-Request-ID": rid}
    cross = client.get(f"/v1/pools/{foreign.pool_id}", headers=headers)
    missing = client.get(f"/v1/pools/{new_id('pool')}", headers=headers)
    event = assert_same_404(
        engine,
        cross,
        missing,
        "POOL_NOT_FOUND",
        rid,
        f"/v1/pools/{foreign.pool_id}",
        {"object_type": "pool", "id": foreign.pool_id},
    )
    assert event["tenant_id"] == caller.tenant_id
    assert event["credential_id"] == caller.credential_id


def test_a_missing_pool_is_not_recorded(client, engine):
    caller = issue(engine)
    rid = request_id()
    headers = caller.auth | {"X-Request-ID": rid}
    response = client.get(f"/v1/pools/{new_id('pool')}", headers=headers)
    assert error_of(response, 404)["code"] == "POOL_NOT_FOUND"
    assert events(engine, rid) == []


# --- 余额 ---


def test_balances_mix_rows_missing_rows_merged_and_foreign_skus(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    first, second, third, foreign_sku = ordered_sku_ids(4)
    with engine.begin() as db:
        tenant_id, pool_id = caller.tenant_id, caller.pool_id
        add_tenant_sku(db, tenant_id, sku_id=first)
        add_tenant_sku(db, tenant_id, sku_id=second)
        add_tenant_sku(db, tenant_id, sku_id=third, status="inactive", merged_into_sku_id=first)
        add_tenant_sku(db, foreign.tenant_id, sku_id=foreign_sku)
        db.execute(
            balance_insert(
                tenant_id,
                pool_id,
                first,
                stock_balance=10,
                reserved=2,
                unavailable=3,
                paid_pending_dispatch=1,
                balance_version=7,
            )
        )
        db.execute(
            balance_insert(
                tenant_id, pool_id, third, stock_balance=6, unavailable=6, balance_version=3
            )
        )
        # second 只在同租户的另一个池有余额行：这个池里它还没有余额行。
        other_pool = add_pool(db, tenant_id)
        db.execute(balance_insert(tenant_id, other_pool, second, stock_balance=4))
        db.execute(balance_insert(foreign.tenant_id, foreign.pool_id, foreign_sku))

    rid = request_id()
    sku_ids = ",".join([third, foreign_sku, new_id("sku"), first, "not-an-id", second])
    params = {"pool_id": caller.pool_id, "sku_ids": sku_ids}
    headers = caller.auth | {"X-Request-ID": rid}
    response = client.get("/v1/balances", params=params, headers=headers)
    assert data_of(response) == {
        "balances": [
            {
                "sku_id": first,
                "stock_balance": 10,
                "reserved": 2,
                "unavailable": 3,
                "paid_pending_dispatch": 1,
                "available": 5,
                "balance_version": 7,
                "merged_into_sku_id": None,
            },
            {
                "sku_id": second,
                "stock_balance": 0,
                "reserved": 0,
                "unavailable": 0,
                "paid_pending_dispatch": 0,
                "available": 0,
                "balance_version": 0,
                "merged_into_sku_id": None,
            },
            {
                "sku_id": third,
                "stock_balance": 6,
                "reserved": 0,
                "unavailable": 6,
                "paid_pending_dispatch": 0,
                "available": 0,
                "balance_version": 3,
                "merged_into_sku_id": first,
            },
        ]
    }
    # 别的租户的 SKU 只是不出现：不报错，也不记事件。
    assert events(engine, rid) == []


def test_balances_with_only_unknown_skus_are_empty(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    with engine.begin() as db:
        foreign_sku = add_tenant_sku(db, foreign.tenant_id)
    params = {"pool_id": caller.pool_id, "sku_ids": f"{foreign_sku},{new_id('sku')}"}
    data = data_of(client.get("/v1/balances", params=params, headers=caller.auth))
    assert data == {"balances": []}


def test_balances_accept_up_to_100_sku_ids(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        sku_id = add_tenant_sku(db, caller.tenant_id)
    sku_ids = ",".join([sku_id] + [new_id("sku") for _ in range(99)])
    params = {"pool_id": caller.pool_id, "sku_ids": sku_ids}
    data = data_of(client.get("/v1/balances", params=params, headers=caller.auth))
    assert [balance["sku_id"] for balance in data["balances"]] == [sku_id]


def test_balances_in_another_tenants_pool_look_missing_and_are_recorded(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    with engine.begin() as db:
        sku_id = add_tenant_sku(db, caller.tenant_id)
    rid = request_id()
    headers = caller.auth | {"X-Request-ID": rid}
    cross = client.get(
        "/v1/balances", params={"pool_id": foreign.pool_id, "sku_ids": sku_id}, headers=headers
    )
    missing = client.get(
        "/v1/balances", params={"pool_id": new_id("pool"), "sku_ids": sku_id}, headers=headers
    )
    details = {"object_type": "pool", "id": foreign.pool_id}
    assert_same_404(engine, cross, missing, "POOL_NOT_FOUND", rid, "/v1/balances", details)


def balance_params(case: str, pool_id: str) -> list[tuple[str, str]]:
    sku = "sku_1"
    return {
        "none": [],
        "pool_only": [("pool_id", pool_id)],
        "sku_ids_only": [("sku_ids", sku)],
        "pool_twice": [("pool_id", pool_id), ("pool_id", pool_id), ("sku_ids", sku)],
        "sku_ids_twice": [("pool_id", pool_id), ("sku_ids", sku), ("sku_ids", "sku_2")],
        "unknown": [("pool_id", pool_id), ("sku_ids", sku), ("limit", "1")],
        "name_is_case_sensitive": [("pool_id", pool_id), ("SKU_IDS", sku)],
        "sku_ids_empty": [("pool_id", pool_id), ("sku_ids", "")],
        "sku_ids_empty_item": [("pool_id", pool_id), ("sku_ids", "sku_1,,sku_2")],
        "sku_ids_trailing_comma": [("pool_id", pool_id), ("sku_ids", "sku_1,")],
        "sku_ids_duplicate": [("pool_id", pool_id), ("sku_ids", "sku_1,sku_2,sku_1")],
        "sku_ids_over_100": [
            ("pool_id", pool_id),
            ("sku_ids", ",".join(f"sku_{i}" for i in range(101))),
        ],
    }[case]


@pytest.mark.parametrize(
    "case",
    [
        "none",
        "pool_only",
        "sku_ids_only",
        "pool_twice",
        "sku_ids_twice",
        "unknown",
        "name_is_case_sensitive",
        "sku_ids_empty",
        "sku_ids_empty_item",
        "sku_ids_trailing_comma",
        "sku_ids_duplicate",
        "sku_ids_over_100",
    ],
)
def test_bad_balance_parameters_are_422(client, engine, case):
    caller = issue(engine)
    rid = request_id()
    params = balance_params(case, caller.pool_id)
    headers = caller.auth | {"X-Request-ID": rid}
    error = error_of(client.get("/v1/balances", params=params, headers=headers), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert events(engine, rid) == []


# --- 流水 ---


def test_entries_have_the_contract_fields_in_version_order(client, engine):
    caller = issue(engine)
    tenant_id = caller.tenant_id
    with engine.begin() as db:
        sku_id = add_tenant_sku(db, tenant_id)
        db.execute(balance_insert(tenant_id, caller.pool_id, sku_id))
        statement = select(SalesChannel.channel_id).where(SalesChannel.tenant_id == tenant_id)
        channel_id = db.execute(statement).scalar_one()
        balance = (tenant_id, caller.pool_id, sku_id)
        command_id = new_id("cmd")
        receipt = add_entry(
            db,
            balance,
            1,
            channel_id=channel_id,
            source_type="receipt",
            source_ref="rcp-1",
            line_ref="line-1",
            command_id=command_id,
            idempotency_key="shop:receipt:1",
            credential_id=caller.credential_id,
            actor_ref="op-1",
            actor_role=STOCK_OPERATOR,
            reason_code="supplier_short",
            note="box dented",
            request_id="req-1",
            recorded_at=recorded_at(1),
        )
        correction = add_entry(
            db,
            balance,
            2,
            entry_type="CORRECTION",
            stock_balance_delta=-5,
            stock_balance_after=0,
            corrects_entry_id=receipt,
            channel_id=channel_id,
            source_type="correction",
            source_ref=receipt,
            recorded_at=recorded_at(2),
        )

    data = ledger(client, caller, {"pool_id": caller.pool_id, "sku_id": sku_id})
    assert versions_of(data) == [1, 2]
    for entry in data["entries"]:
        assert set(entry) == ENTRY_FIELDS
    assert data["entries"][0] == {
        "entry_id": receipt,
        "pool_id": caller.pool_id,
        "sku_id": sku_id,
        "entry_type": "RECEIPT",
        "deltas": ZERO_DELTAS | {"stock_balance": 5},
        "after": ZERO_DELTAS | {"stock_balance": 5},
        "balance_version_after": 1,
        "channel_id": channel_id,
        "source_type": "receipt",
        "source_ref": "rcp-1",
        "line_ref": "line-1",
        "command_id": command_id,
        "actor_ref": "op-1",
        "actor_role": STOCK_OPERATOR,
        "reason_code": "supplier_short",
        "note": "box dented",
        "request_id": "req-1",
        "corrects_entry_id": None,
        "occurred_at": "2026-11-12T08:30:00.000000Z",
        "recorded_at": "2026-11-15T08:31:00.000000Z",
    }
    second = data["entries"][1]
    assert second["entry_id"] == correction
    assert second["entry_type"] == "CORRECTION"
    assert second["deltas"] == ZERO_DELTAS | {"stock_balance": -5}
    assert second["after"] == ZERO_DELTAS
    assert second["corrects_entry_id"] == receipt
    assert (second["source_type"], second["source_ref"]) == ("correction", receipt)
    assert second["line_ref"] is None and second["actor_ref"] is None
    assert data["next_cursor"] == format_ledger_cursor(2, caller.pool_id, sku_id, None, None)


def test_limit_pages_through_with_next_cursor_without_gaps_or_repeats(client, engine):
    caller = issue(engine)
    sku_id = ledger_sku(engine, caller, [3, 1, 5, 2, 4])
    base = {"pool_id": caller.pool_id, "sku_id": sku_id, "limit": "2"}

    pages = []
    after = None
    while True:
        params = base if after is None else base | {"after": after}
        data = ledger(client, caller, params)
        if not data["entries"]:
            # 没有新记录：next_cursor 原样返回请求里的 after。
            assert data["next_cursor"] == after
            break
        pages.append(versions_of(data))
        assert data["next_cursor"] != after
        after = data["next_cursor"]
    assert pages == [[1, 2], [3, 4], [5]]

    # 之后写的流水接着从同一个游标取到，前面的不再出现。
    with engine.begin() as db:
        add_receipts(db, caller, sku_id, [6])
    data = ledger(client, caller, base | {"after": after})
    assert versions_of(data) == [6]


def test_limit_defaults_to_100(client, engine):
    caller = issue(engine)
    sku_id = ledger_sku(engine, caller, list(range(1, 102)))
    data = ledger(client, caller, {"pool_id": caller.pool_id, "sku_id": sku_id})
    assert versions_of(data) == list(range(1, 101))
    params = {"pool_id": caller.pool_id, "sku_id": sku_id, "after": data["next_cursor"]}
    data = ledger(client, caller, params)
    assert versions_of(data) == [101]


def test_no_entries_without_after_gives_a_null_cursor(client, engine):
    caller = issue(engine)
    sku_id = ledger_sku(engine, caller)
    data = ledger(client, caller, {"pool_id": caller.pool_id, "sku_id": sku_id})
    assert data == {"entries": [], "next_cursor": None}


def test_a_sku_without_a_balance_row_has_no_entries(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        sku_id = add_tenant_sku(db, caller.tenant_id)
    data = ledger(client, caller, {"pool_id": caller.pool_id, "sku_id": sku_id})
    assert data == {"entries": [], "next_cursor": None}


def test_from_is_inclusive_and_to_is_exclusive_on_recorded_at(client, engine):
    caller = issue(engine)
    sku_id = ledger_sku(engine, caller, [1, 2, 3, 4, 5])
    # from 是 08:32Z（第 2 条）写成 +08:00；to 恰为第 4 条的 recorded_at。
    params = {
        "pool_id": caller.pool_id,
        "sku_id": sku_id,
        "from": "2026-11-15T16:32:00+08:00",
        "to": "2026-11-15T08:34:00Z",
    }
    assert versions_of(ledger(client, caller, params)) == [2, 3]
    only_from = {"pool_id": caller.pool_id, "sku_id": sku_id, "from": "2026-11-15T08:34:00Z"}
    assert versions_of(ledger(client, caller, only_from)) == [4, 5]
    only_to = {"pool_id": caller.pool_id, "sku_id": sku_id, "to": "2026-11-15T08:32:00.000001Z"}
    assert versions_of(ledger(client, caller, only_to)) == [1, 2]


def test_a_cursor_only_works_with_the_filters_it_came_from(client, engine):
    caller = issue(engine)
    sku_id = ledger_sku(engine, caller, [1, 2, 3, 4])
    other_sku = ledger_sku(engine, caller, [1, 2, 3, 4])
    filtered = {
        "pool_id": caller.pool_id,
        "sku_id": sku_id,
        "from": "2026-11-15T08:31:00Z",
        "limit": "1",
    }
    cursor = ledger(client, caller, filtered)["next_cursor"]
    assert versions_of(ledger(client, caller, filtered | {"after": cursor})) == [2]
    # 同一时刻换个写法，规范化后是同一个筛选条件。
    same = filtered | {"from": "2026-11-15T16:31:00.000+08:00", "after": cursor}
    assert versions_of(ledger(client, caller, same)) == [2]

    for changed in [
        {"pool_id": caller.pool_id, "sku_id": sku_id},
        {"pool_id": caller.pool_id, "sku_id": sku_id, "from": "2026-11-15T08:31:00.000001Z"},
        filtered | {"to": "2026-11-16T00:00:00Z"},
        filtered | {"sku_id": other_sku},
    ]:
        rid = request_id()
        params = changed | {"after": cursor}
        headers = caller.auth | {"X-Request-ID": rid}
        error = error_of(client.get("/v1/ledger", params=params, headers=headers), 422)
        assert error["code"] == "INVALID_REQUEST"
        assert error["details"] == {"parameter": "after"}
        assert events(engine, rid) == []


def test_a_merged_sku_returns_its_own_entries(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        canonical = add_tenant_sku(db, caller.tenant_id)
        merged = add_tenant_sku(
            db, caller.tenant_id, status="inactive", merged_into_sku_id=canonical
        )
        for sku_id in (canonical, merged):
            db.execute(balance_insert(caller.tenant_id, caller.pool_id, sku_id))
        add_receipts(db, caller, merged, [1, 2])
        add_receipts(db, caller, canonical, [1])

    data = ledger(client, caller, {"pool_id": caller.pool_id, "sku_id": merged})
    assert versions_of(data) == [1, 2]
    assert {entry["sku_id"] for entry in data["entries"]} == {merged}


def test_ledger_in_another_tenants_pool_looks_missing_and_is_recorded(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    sku_id = ledger_sku(engine, caller)
    rid = request_id()
    headers = caller.auth | {"X-Request-ID": rid}
    cross = client.get(
        "/v1/ledger", params={"pool_id": foreign.pool_id, "sku_id": sku_id}, headers=headers
    )
    missing = client.get(
        "/v1/ledger", params={"pool_id": new_id("pool"), "sku_id": sku_id}, headers=headers
    )
    details = {"object_type": "pool", "id": foreign.pool_id}
    assert_same_404(engine, cross, missing, "POOL_NOT_FOUND", rid, "/v1/ledger", details)


def test_ledger_of_another_tenants_sku_looks_missing_and_is_recorded(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    with engine.begin() as db:
        foreign_sku = add_tenant_sku(db, foreign.tenant_id)
        db.execute(balance_insert(foreign.tenant_id, foreign.pool_id, foreign_sku))
        add_entry(db, (foreign.tenant_id, foreign.pool_id, foreign_sku), 1)
    rid = request_id()
    headers = caller.auth | {"X-Request-ID": rid}
    cross = client.get(
        "/v1/ledger", params={"pool_id": caller.pool_id, "sku_id": foreign_sku}, headers=headers
    )
    missing = client.get(
        "/v1/ledger", params={"pool_id": caller.pool_id, "sku_id": new_id("sku")}, headers=headers
    )
    details = {"object_type": "sku", "id": foreign_sku}
    event = assert_same_404(engine, cross, missing, "SKU_NOT_FOUND", rid, "/v1/ledger", details)
    assert event["tenant_id"] == caller.tenant_id


def ledger_params(case: str, pool_id: str) -> list[tuple[str, str]]:
    required = [("pool_id", pool_id), ("sku_id", "sku_1")]
    eight = "2026-11-15T08:00:00Z"
    nine = "2026-11-15T09:00:00Z"
    cursor = "v1.1.0123456789abcdef"
    return {
        "none": [],
        "pool_only": [("pool_id", pool_id)],
        "sku_only": [("sku_id", "sku_1")],
        "pool_twice": [*required, ("pool_id", pool_id)],
        "sku_twice": [*required, ("sku_id", "sku_2")],
        "from_twice": [*required, ("from", eight), ("from", eight)],
        "after_twice": [*required, ("after", cursor), ("after", cursor)],
        "limit_twice": [*required, ("limit", "1"), ("limit", "2")],
        "unknown": [*required, ("sku_ids", "sku_1")],
        "limit_zero": [*required, ("limit", "0")],
        "limit_over_100": [*required, ("limit", "101")],
        "limit_leading_zero": [*required, ("limit", "01")],
        "from_date_only": [*required, ("from", "2026-11-15")],
        "from_without_zone": [*required, ("from", "2026-11-15T08:00:00")],
        "to_lowercase_z": [*required, ("to", "2026-11-15t08:00:00z")],
        "from_equals_to": [*required, ("from", eight), ("to", "2026-11-15T16:00:00+08:00")],
        "from_after_to": [*required, ("from", nine), ("to", eight)],
        "after_not_a_cursor": [*required, ("after", "1")],
        "after_bad_prefix": [*required, ("after", "v2.1.0123456789abcdef")],
        "after_version_zero": [*required, ("after", "v1.0.0123456789abcdef")],
        "after_bad_digest": [*required, ("after", "v1.1.0123456789ABCDEF")],
    }[case]


@pytest.mark.parametrize(
    "case",
    [
        "none",
        "pool_only",
        "sku_only",
        "pool_twice",
        "sku_twice",
        "from_twice",
        "after_twice",
        "limit_twice",
        "unknown",
        "limit_zero",
        "limit_over_100",
        "limit_leading_zero",
        "from_date_only",
        "from_without_zone",
        "to_lowercase_z",
        "from_equals_to",
        "from_after_to",
        "after_not_a_cursor",
        "after_bad_prefix",
        "after_version_zero",
        "after_bad_digest",
    ],
)
def test_bad_ledger_parameters_are_422(client, engine, case):
    caller = issue(engine)
    rid = request_id()
    params = ledger_params(case, caller.pool_id)
    headers = caller.auth | {"X-Request-ID": rid}
    error = error_of(client.get("/v1/ledger", params=params, headers=headers), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert events(engine, rid) == []


def test_bad_parameters_come_before_a_foreign_pool(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    for url, params in [
        ("/v1/balances", {"pool_id": foreign.pool_id, "sku_ids": "sku_1,sku_1"}),
        ("/v1/ledger", {"pool_id": foreign.pool_id, "sku_id": "sku_1", "limit": "0"}),
    ]:
        rid = request_id()
        headers = caller.auth | {"X-Request-ID": rid}
        error = error_of(client.get(url, params=params, headers=headers), 422)
        assert error["code"] == "INVALID_REQUEST"
        assert events(engine, rid) == []


# --- 授权 ---


def query_urls(engine: Engine, caller: Caller) -> list[str]:
    sku_id = ledger_sku(engine, caller, [1])
    pool_id = caller.pool_id
    return [
        f"/v1/pools/{pool_id}",
        f"/v1/balances?pool_id={pool_id}&sku_ids={sku_id}",
        f"/v1/ledger?pool_id={pool_id}&sku_id={sku_id}",
    ]


def test_stock_queries_need_stock_read(client, engine):
    caller = issue(engine, scopes=[CATALOG_READ, STOCK_WRITE])
    for url in query_urls(engine, caller):
        rid = request_id()
        response = client.get(url, headers=caller.auth | {"X-Request-ID": rid})
        assert error_of(response, 403)["code"] == "FORBIDDEN"
        [event] = events(engine, rid)
        assert event["event_type"] == "forbidden"
        assert event["details"] == {"category": "scope", "missing_scopes": [STOCK_READ]}


def test_a_missing_scope_comes_before_bad_parameters(client, engine):
    caller = issue(engine, scopes=[CATALOG_READ])
    for url in ["/v1/balances?limit=1", "/v1/ledger?sku_ids=", f"/v1/pools/{new_id('pool')}"]:
        assert error_of(client.get(url, headers=caller.auth), 403)["code"] == "FORBIDDEN"


def test_stock_queries_need_no_role(client, engine):
    caller = issue(engine, scopes=[STOCK_READ])
    actor = {"X-Actor-Ref": "op-7", "X-Actor-Role": STOCK_OPERATOR}
    for url in query_urls(engine, caller):
        assert client.get(url, headers=caller.auth).status_code == 200
        assert client.get(url, headers=caller.auth | actor).status_code == 200
