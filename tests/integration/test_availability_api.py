"""可售量查询接口（契约 §5.6 可售量、§6.2 第 1 条、§10 第 4 条）。

在真 MySQL 上跑；测试直接在库里造 SKU、商品与余额行（用自己提交的连接，应用用另一个连接读），
每个测试建自己的租户与凭据，互不干扰；安全事件按每个请求自己的 X-Request-ID 查。
"""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update

from app.config import Settings
from app.domain.requests import STOCK_OPERATOR
from app.domain.service_auth import CATALOG_READ, STOCK_READ, STOCK_WRITE
from app.main import create_app
from app.models.inventory import StockPool
from tests.integration.conftest import (
    TEST_DATABASE_URL,
    add_pool,
    balance_insert,
    new_id,
    went_live,
)
from tests.integration.test_api_auth import error_of, events, issue
from tests.integration.test_catalog_queries_api import add_product, add_sku, ordered_sku_ids

URL = "/v1/availability"


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


def request_id() -> str:
    return f"availability-test-{uuid4().hex}"


def data_of(response) -> dict:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    return body["data"]


def availability(client: TestClient, caller, sku_ids: list[str], rid: str | None = None) -> dict:
    headers = caller.auth if rid is None else caller.auth | {"X-Request-ID": rid}
    response = client.get(URL, params={"sku_ids": ",".join(sku_ids)}, headers=headers)
    data = data_of(response)
    assert set(data) == {"pool_id", "pool_status", "skus"}
    return data


def sku(sku_id: str, available: int, status: str = "sellable", merged_into=None) -> dict:
    return {
        "sku_id": sku_id,
        "available": available,
        "status": status,
        "merged_into_sku_id": merged_into,
    }


# --- 返回 ---


def test_available_comes_from_the_channels_pool_in_sku_id_order(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    first, second, third, foreign_sku = ordered_sku_ids(4)
    with engine.begin() as db:
        tenant_id, pool_id = caller.tenant_id, caller.pool_id
        product_id = add_product(db, tenant_id)
        for sku_id in (first, second, third):
            add_sku(db, tenant_id, product_id, sku_id=sku_id)
        add_sku(db, foreign.tenant_id, add_product(db, foreign.tenant_id), sku_id=foreign_sku)
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
        db.execute(balance_insert(tenant_id, pool_id, third, stock_balance=4, reserved=4))
        # second 只在同租户的另一个池有余额行：凭据的池里它还没有余额行。
        other_pool = add_pool(db, tenant_id)
        db.execute(balance_insert(tenant_id, other_pool, second, stock_balance=9))
        foreign_balance = balance_insert(foreign.tenant_id, foreign.pool_id, foreign_sku)
        db.execute(foreign_balance.values(stock_balance=8))

    rid = request_id()
    requested = [third, foreign_sku, new_id("sku"), first, "not-an-id", second]
    assert availability(client, caller, requested, rid) == {
        "pool_id": caller.pool_id,
        "pool_status": "initializing",
        "skus": [sku(first, 5), sku(second, 0), sku(third, 0)],
    }
    # 别的租户的 SKU 只是不出现：不报错，也不记事件。
    assert events(engine, rid) == []


def test_a_live_pool_reports_live(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        sku_id = add_sku(db, caller.tenant_id, add_product(db, caller.tenant_id))
        statement = update(StockPool).where(StockPool.pool_id == caller.pool_id)
        db.execute(statement.values(status="live", **went_live(caller.credential_id)))
        db.execute(balance_insert(caller.tenant_id, caller.pool_id, sku_id, stock_balance=3))

    data = availability(client, caller, [sku_id])
    assert data == {"pool_id": caller.pool_id, "pool_status": "live", "skus": [sku(sku_id, 3)]}


def test_only_unknown_skus_give_an_empty_list(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    with engine.begin() as db:
        foreign_sku = add_sku(db, foreign.tenant_id, add_product(db, foreign.tenant_id))
    data = availability(client, caller, [foreign_sku, new_id("sku")])
    assert data == {"pool_id": caller.pool_id, "pool_status": "initializing", "skus": []}


def test_up_to_100_sku_ids_are_accepted(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        sku_id = add_sku(db, caller.tenant_id, add_product(db, caller.tenant_id))
    data = availability(client, caller, [sku_id] + [new_id("sku") for _ in range(99)])
    assert data["skus"] == [sku(sku_id, 0)]


# --- 状态 ---


def test_status_is_merged_then_inactive_then_sellable(client, engine):
    caller = issue(engine)
    # merged_off 已合并、所属商品也停用；sku_off 是 SKU 停用；product_off 只有所属商品停用。
    canonical, merged, merged_off, sku_off, product_off = ordered_sku_ids(5)
    with engine.begin() as db:
        tenant_id, pool_id = caller.tenant_id, caller.pool_id
        active_product = add_product(db, tenant_id)
        inactive_product = add_product(db, tenant_id, status="inactive")
        add_sku(db, tenant_id, active_product, sku_id=canonical)
        # 已合并的 SKU 也是停用的，报 merged。
        add_sku(
            db,
            tenant_id,
            active_product,
            sku_id=merged,
            status="inactive",
            merged_into_sku_id=canonical,
        )
        add_sku(
            db,
            tenant_id,
            inactive_product,
            sku_id=merged_off,
            status="inactive",
            merged_into_sku_id=canonical,
        )
        add_sku(db, tenant_id, active_product, sku_id=sku_off, status="inactive")
        add_sku(db, tenant_id, inactive_product, sku_id=product_off)
        db.execute(balance_insert(tenant_id, pool_id, canonical, stock_balance=6))
        db.execute(balance_insert(tenant_id, pool_id, merged, stock_balance=2, unavailable=1))
        db.execute(balance_insert(tenant_id, pool_id, sku_off, stock_balance=4))
        db.execute(balance_insert(tenant_id, pool_id, product_off, stock_balance=5))

    data = availability(client, caller, [product_off, sku_off, merged_off, merged, canonical])
    assert data["skus"] == [
        sku(canonical, 6),
        sku(merged, 1, "merged", canonical),
        sku(merged_off, 0, "merged", canonical),
        sku(sku_off, 4, "inactive"),
        sku(product_off, 5, "inactive"),
    ]


# --- 拒绝 ---


@pytest.mark.parametrize("channel_status", [None, "inactive"])
def test_a_credential_without_a_sales_channel_is_forbidden(client, engine, channel_status):
    caller = issue(engine, channel_status=channel_status)
    with engine.begin() as db:
        sku_id = add_sku(db, caller.tenant_id, add_product(db, caller.tenant_id))
    rid = request_id()
    headers = caller.auth | {"X-Request-ID": rid}
    response = client.get(URL, params={"sku_ids": sku_id}, headers=headers)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["event_type"] == "forbidden"
    assert event["http_method"] == "GET"
    assert event["path"] == URL
    assert event["tenant_id"] == caller.tenant_id
    assert event["credential_id"] == caller.credential_id
    assert event["details"] == {"category": "pool", "pool_id": None}


def test_availability_needs_stock_read(client, engine):
    caller = issue(engine, scopes=[CATALOG_READ, STOCK_WRITE])
    with engine.begin() as db:
        sku_id = add_sku(db, caller.tenant_id, add_product(db, caller.tenant_id))
    rid = request_id()
    headers = caller.auth | {"X-Request-ID": rid}
    response = client.get(URL, params={"sku_ids": sku_id}, headers=headers)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["event_type"] == "forbidden"
    assert event["details"] == {"category": "scope", "missing_scopes": [STOCK_READ]}


def test_a_missing_scope_comes_before_bad_parameters(client, engine):
    caller = issue(engine, scopes=[CATALOG_READ])
    response = client.get(URL, params={"limit": "1"}, headers=caller.auth)
    assert error_of(response, 403)["code"] == "FORBIDDEN"


def test_availability_needs_no_role(client, engine):
    caller = issue(engine, scopes=[STOCK_READ])
    with engine.begin() as db:
        sku_id = add_sku(db, caller.tenant_id, add_product(db, caller.tenant_id))
    actor = {"X-Actor-Ref": "op-7", "X-Actor-Role": STOCK_OPERATOR}
    params = {"sku_ids": sku_id}
    assert client.get(URL, params=params, headers=caller.auth).status_code == 200
    assert client.get(URL, params=params, headers=caller.auth | actor).status_code == 200


def availability_params(case: str) -> list[tuple[str, str]]:
    return {
        "none": [],
        "sku_ids_twice": [("sku_ids", "sku_1"), ("sku_ids", "sku_2")],
        "unknown": [("sku_ids", "sku_1"), ("pool_id", "pool_1")],
        "name_is_case_sensitive": [("SKU_IDS", "sku_1")],
        "sku_ids_empty": [("sku_ids", "")],
        "sku_ids_empty_item": [("sku_ids", "sku_1,,sku_2")],
        "sku_ids_trailing_comma": [("sku_ids", "sku_1,")],
        "sku_ids_duplicate": [("sku_ids", "sku_1,sku_2,sku_1")],
        "sku_ids_over_100": [("sku_ids", ",".join(f"sku_{i}" for i in range(101)))],
    }[case]


@pytest.mark.parametrize(
    "case",
    [
        "none",
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
def test_bad_availability_parameters_are_422(client, engine, case):
    caller = issue(engine)
    rid = request_id()
    headers = caller.auth | {"X-Request-ID": rid}
    error = error_of(client.get(URL, params=availability_params(case), headers=headers), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert events(engine, rid) == []
