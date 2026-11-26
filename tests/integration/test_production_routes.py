"""生产实例的 /v1 只挂不变量自查（契约 8.6 第 1 条、4.4 的 0.22 说明）。

在真 MySQL 上跑；生产应用在 conftest 的 instance_marked 里以 CORE_ENV=production 建，
建好即退出（标记改回 demo，同 test_demo_resets_api 生产实例的测试）。
Demo 应用的每个 /v1 路由与方法（不变量自查除外）在生产应用上都同不存在的路径一样 404、不鉴权。
"""

import re
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.config import Settings
from app.domain.environment import PRODUCTION
from app.domain.requests import STOCK_OPERATOR
from app.domain.service_auth import OPS_READ
from app.main import create_app
from tests.integration.conftest import TEST_DATABASE_URL, instance_marked
from tests.integration.test_api_auth import Caller, error_of, events, issue
from tests.integration.test_reservations_api import data_of, make_client
from tests.integration.test_stock_imports_api import credential_of

INVARIANTS = "/v1/ops/invariants"
_PATH_PARAMETER = re.compile(r"\{[^}]+\}")

# 改动前 Demo 应用挂的 /v1 路由：生产只去掉它们，Demo 照旧。
DEMO_ROUTES = {
    ("GET", "/v1/products/{product_id}"),
    ("GET", "/v1/skus/{sku_id}"),
    ("GET", "/v1/skus"),
    ("POST", "/v1/products"),
    ("POST", "/v1/products/{product_id}/skus"),
    ("PATCH", "/v1/products/{product_id}"),
    ("PATCH", "/v1/skus/{sku_id}"),
    ("POST", "/v1/products/{product_id}/deactivate"),
    ("POST", "/v1/products/{product_id}/reactivate"),
    ("POST", "/v1/skus/{sku_id}/deactivate"),
    ("POST", "/v1/skus/{sku_id}/reactivate"),
    ("GET", "/v1/catalog-imports/{import_id}"),
    ("POST", "/v1/catalog-imports"),
    ("POST", "/v1/catalog-imports/{import_id}/confirm"),
    ("GET", "/v1/catalog-changes"),
    ("GET", "/v1/pools/{pool_id}"),
    ("GET", "/v1/balances"),
    ("GET", "/v1/availability"),
    ("GET", "/v1/ledger"),
    ("GET", "/v1/stock-imports/{import_id}"),
    ("POST", "/v1/stock-imports"),
    ("POST", "/v1/stock-imports/{import_id}/confirm"),
    ("POST", "/v1/pools/{pool_id}/go-live"),
    ("POST", "/v1/receipts"),
    ("POST", "/v1/unavailable-adjustments"),
    ("POST", "/v1/corrections"),
    ("POST", "/v1/counts"),
    ("POST", "/v1/counts/{count_id}/submit"),
    ("POST", "/v1/counts/{count_id}/confirm"),
    ("POST", "/v1/counts/{count_id}/cancel"),
    ("GET", "/v1/counts/{count_id}"),
    ("GET", "/v1/counts"),
    ("POST", "/v1/sku-merges"),
    ("POST", "/v1/sku-merges/{merge_id}/count"),
    ("POST", "/v1/sku-merges/{merge_id}/execute"),
    ("POST", "/v1/sku-merges/{merge_id}/complete"),
    ("POST", "/v1/sku-merges/{merge_id}/cancel"),
    ("GET", "/v1/sku-merges/{merge_id}"),
    ("POST", "/v1/reservations"),
    ("POST", "/v1/reservations/{reservation_id}/release"),
    ("GET", "/v1/reservations/{reservation_id}"),
    ("GET", "/v1/reservations"),
    ("POST", "/v1/orders/{order_ref}/sale-commits"),
    ("POST", "/v1/orders/{order_ref}/late-payment-review/close"),
    ("POST", "/v1/orders/{order_ref}/dispatches"),
    ("POST", "/v1/orders/{order_ref}/refund-restocks"),
    ("POST", "/v1/orders/{order_ref}/return-receipts"),
    ("GET", "/v1/orders/{order_ref}/stock"),
    ("GET", "/v1/reconciliation/orders"),
    ("GET", INVARIANTS),
    ("POST", "/v1/demo/resets"),
}


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


@pytest.fixture
def demo(migrated_engine: Engine) -> Iterator[TestClient]:
    test_client = make_client()
    try:
        yield test_client
    finally:
        test_client.app.state.engine.dispose()


@pytest.fixture
def production(migrated_engine: Engine) -> Iterator[TestClient]:
    with instance_marked(migrated_engine, PRODUCTION):
        app = create_app(Settings(env=PRODUCTION, git_sha="d" * 40, database_url=TEST_DATABASE_URL))
    try:
        yield TestClient(app)
    finally:
        app.state.engine.dispose()


def v1_routes(app: FastAPI) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/v1/")
        for method in route.methods
    }


def request_id() -> str:
    return f"production-routes-{uuid4().hex}"


def concrete(path: str) -> str:
    """路径参数换成合格的值。"""
    return _PATH_PARAMETER.sub("x-1", path)


def test_the_demo_instance_keeps_all_routes(demo):
    assert v1_routes(demo.app) == DEMO_ROUTES


def test_a_production_instance_mounts_only_the_invariant_check(production):
    assert v1_routes(production.app) == {("GET", INVARIANTS)}


def test_other_v1_routes_do_not_exist_on_a_production_instance(demo, production, engine):
    caller = issue(engine)
    routes = sorted(v1_routes(demo.app) - {("GET", INVARIANTS)})
    assert routes
    for method, path in routes:
        url = concrete(path)
        body = None if method == "GET" else {}
        # 合格的凭据（带写命令要的头）与没有凭据。
        valid = caller.auth | {
            "X-Request-ID": request_id(),
            "Idempotency-Key": f"production:routes:{uuid4().hex}",
            "X-Actor-Ref": "ops-7",
            "X-Actor-Role": STOCK_OPERATOR,
        }
        for headers in (valid, {"X-Request-ID": request_id()}):
            rid = headers["X-Request-ID"]
            response = production.request(method, url, json=body, headers=headers)
            error = error_of(response, 404)
            assert error["code"] == "NOT_FOUND", (method, path)
            assert response.headers["X-Request-ID"] == rid
            # 不鉴权，所以没有安全事件。
            assert events(engine, rid) == [], (method, path)


def test_the_invariant_check_and_health_checks_still_work(production, engine):
    caller = issue(engine)
    ops: Caller = credential_of(engine, caller, [OPS_READ])
    rid = request_id()
    response = production.get(INVARIANTS, headers=ops.auth | {"X-Request-ID": rid})
    data = data_of(response, 200)
    assert set(data) == {"checked_at", "ok", "violation_count", "violations"}
    assert response.headers["X-Request-ID"] == rid

    # 不带凭据照常鉴权：401。
    assert error_of(production.get(INVARIANTS), 401)["code"] == "UNAUTHORIZED"

    health = production.get("/healthz")
    assert health.status_code == 200
    assert health.json()["env"] == PRODUCTION
    ready = production.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"
