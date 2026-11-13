"""鉴权、scope、角色、池授权与安全事件（契约 §7.2、§8.1 检查顺序、§10 第 3、4 条）。

在真 MySQL 上跑；测试给 create_app 返回的应用挂测试专用路由。
造数据用自己提交的连接（应用用另一个连接读），每个测试建自己的租户与凭据，互不干扰；
安全事件按每个请求自己的 X-Request-ID 查。
"""

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import uuid4

import pytest
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, insert, literal_column, make_url, select, update
from sqlalchemy.exc import OperationalError

from app.api.auth import (
    POOL_NOT_FOUND,
    ObjectPool,
    authorize,
    cross_tenant_not_found,
    get_access,
    require_object_pool,
    require_pool,
    require_role,
    require_role_by_body,
    require_sales_pool,
)
from app.api.errors import ok
from app.api.requests import ParsedBody, RequestBody, json_body, no_body, request_connection
from app.config import Settings
from app.domain.requests import CATALOG_ADMIN, STOCK_ADMIN, STOCK_OPERATOR, Actor
from app.domain.service_auth import (
    CATALOG_READ,
    CATALOG_WRITE,
    ORDERS_WRITE,
    STOCK_READ,
    STOCK_WRITE,
    generate_token,
    token_digest,
)
from app.main import create_app
from app.models.catalog import Tenant
from app.models.inventory import StockBalance
from app.models.service_auth import CredentialToken, SecurityEvent
from app.services import security_events
from tests.integration.conftest import TEST_DATABASE_URL, add_credential, new_id
from tests.integration.test_inventory_schema import (
    add_pool,
    add_sku,
    add_tenant,
    balance_insert,
    channel_insert,
)
from tests.integration.test_service_auth_schema import token_insert

# 截止时间与吊销时间都按数据库时间写：鉴权也按数据库时间比较。
NOW = literal_column("UTC_TIMESTAMP(6)")
IN_AN_HOUR = literal_column("UTC_TIMESTAMP(6) + INTERVAL 1 HOUR")
A_SECOND_AGO = literal_column("UTC_TIMESTAMP(6) - INTERVAL 1 SECOND")
REVOKED_CREDENTIAL = {"status": "revoked", "revoked_at": NOW}

ALL_SCOPES = [CATALOG_READ, CATALOG_WRITE, STOCK_READ, STOCK_WRITE, ORDERS_WRITE]
KEY = {"Idempotency-Key": "test:key:1"}
EXPECTED_401 = {
    "code": "UNAUTHORIZED",
    "message": "invalid credentials",
    "retryable": False,
    "details": {},
}
OVER_LIMIT = 128 * 1024 + 1


# --- 测试专用路由 ---


class Line(RequestBody):
    line_ref: str
    quantity: int


class Reservation(RequestBody):
    batch_fields = ("lines",)

    lines: list[Line]


class Adjustment(RequestBody):
    pool_id: str
    action: Literal["mark_unavailable", "write_off"]
    quantity: int
    note: str | None = None


reservation_body = json_body(Reservation)
adjustment_body = json_body(Adjustment)
ReservationBody = Annotated[ParsedBody[Reservation], Depends(reservation_body)]
AdjustmentBody = Annotated[ParsedBody[Adjustment], Depends(adjustment_body)]


def adjustment_roles(body: ParsedBody[Adjustment]) -> set[str]:
    """像报损：普通操作员能标不可售，报损只接受库存管理员。"""
    if body.model.action == "write_off":
        return {STOCK_ADMIN}
    return {STOCK_OPERATOR, STOCK_ADMIN}


def adjustment_pool(body: AdjustmentBody) -> str:
    return body.model.pool_id


def path_balance_id(balance_id: str) -> str:
    return balance_id


def find_balance_pool(conn: Connection, balance_id: str) -> ObjectPool | None:
    """余额行当作“池内对象”：按 ID 查它所属的租户与池，不按租户过滤。"""
    columns = (StockBalance.tenant_id, StockBalance.pool_id)
    statement = select(*columns).where(StockBalance.balance_id == balance_id)
    row = conn.execute(statement).first()
    return None if row is None else ObjectPool(*row)


balance_pool = require_object_pool(
    path_balance_id, find_balance_pool, object_type="balance", not_found_code="BALANCE_NOT_FOUND"
)
AdjustmentRole = Annotated[
    Actor | None, Depends(require_role_by_body(adjustment_body, adjustment_roles))
]
AdjustmentPool = Annotated[str, Depends(require_pool(adjustment_pool))]
BalancePool = Annotated[str, Depends(balance_pool)]
StockRole = Annotated[Actor, Depends(require_role(STOCK_OPERATOR, STOCK_ADMIN))]
NO_BODY = Depends(no_body)


def add_test_routes(app) -> None:
    @app.get("/test/items/{item_id}", dependencies=[Depends(authorize(CATALOG_READ))])
    def read_item(request: Request, item_id: str) -> JSONResponse:
        principal = get_access(request).principal
        return ok(request, {"credential_id": principal.credential_id})

    # 合并类：两个 scope 都要，只接受 stock_admin，没有请求体。
    merge_access = Depends(authorize(CATALOG_WRITE, STOCK_WRITE, write=True))

    @app.post("/test/merges", dependencies=[merge_access, NO_BODY])
    def create_merge(
        request: Request, actor: Annotated[Actor, Depends(require_role(STOCK_ADMIN))]
    ) -> JSONResponse:
        return ok(request, {"role": actor.role})

    stock_write = Depends(authorize(STOCK_WRITE, write=True))

    @app.post("/test/adjustments", dependencies=[stock_write])
    def adjust(
        request: Request, body: AdjustmentBody, actor: AdjustmentRole, pool_id: AdjustmentPool
    ) -> JSONResponse:
        return ok(request, {"pool_id": pool_id, "role": actor.role})

    @app.post("/test/reservations", dependencies=[Depends(authorize(ORDERS_WRITE, write=True))])
    def reserve(
        request: Request,
        body: ReservationBody,
        pool_id: Annotated[str, Depends(require_sales_pool)],
    ) -> JSONResponse:
        return ok(request, {"pool_id": pool_id})

    @app.post("/test/balances/{balance_id}/touch", dependencies=[stock_write, NO_BODY])
    def touch(request: Request, actor: StockRole, pool_id: BalancePool) -> JSONResponse:
        return ok(request, {"pool_id": pool_id})

    @app.post("/test/rollbacks/{marker}", dependencies=[stock_write, NO_BODY])
    def roll_back(request: Request, marker: str) -> JSONResponse:
        """业务事务里写了东西、遇到别的租户的对象：先回滚，再记 cross_tenant。"""
        conn = request_connection(request)
        transaction = conn.begin()
        conn.execute(insert(Tenant).values(tenant_id=marker, name="Marker", status="active"))
        transaction.rollback()
        raise cross_tenant_not_found(
            request, object_type="pool", object_id=marker, code=POOL_NOT_FOUND
        )


def make_client(database_url: str) -> TestClient:
    app = create_app(Settings(env="demo", git_sha="c" * 40, database_url=database_url))
    add_test_routes(app)
    return TestClient(app)


def dispose(client: TestClient) -> None:
    engine = client.app.state.engine
    if engine is not None:
        engine.dispose()


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    test_client = make_client(TEST_DATABASE_URL)
    try:
        yield test_client
    finally:
        dispose(test_client)


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


# --- 造数据 ---


@dataclass(frozen=True)
class Caller:
    tenant_id: str
    pool_id: str
    credential_id: str
    token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def issue(
    engine: Engine,
    *,
    scopes: list[str] = ALL_SCOPES,
    channel_status: str | None = "active",
    tenant_status: str = "active",
    credential: dict | None = None,
    token: dict | None = None,
) -> Caller:
    """新租户、它的池、绑定该池的销售入口（channel_status 为 None 时不建）、凭据与一个令牌。"""
    plain = generate_token()
    with engine.begin() as db:
        tenant_id = add_tenant(db)
        pool_id = add_pool(db, tenant_id)
        channel_id = None
        if channel_status is not None:
            channel_id = new_id("chn")
            db.execute(
                channel_insert(tenant_id, pool_id, channel_id=channel_id, status=channel_status)
            )
        credential_id = add_credential(
            db, tenant_id, channel_id=channel_id, scopes=scopes, **(credential or {})
        )
        overrides = {"token_digest": token_digest(plain)} | (token or {})
        db.execute(token_insert(tenant_id, credential_id, **overrides))
        if tenant_status != "active":
            statement = update(Tenant).where(Tenant.tenant_id == tenant_id)
            db.execute(statement.values(status=tenant_status))
    return Caller(tenant_id, pool_id, credential_id, plain)


def add_token(engine: Engine, caller: Caller) -> str:
    """同一个凭据再签一个令牌（轮换）。"""
    plain = generate_token()
    with engine.begin() as db:
        overrides = {"token_digest": token_digest(plain)}
        db.execute(token_insert(caller.tenant_id, caller.credential_id, **overrides))
    return plain


def set_token(engine: Engine, token: str, **values: object) -> None:
    digest = token_digest(token)
    with engine.begin() as db:
        statement = update(CredentialToken).where(CredentialToken.token_digest == digest)
        db.execute(statement.values(**values))


def add_other_pool(engine: Engine, tenant_id: str) -> str:
    """同租户的第二个池，不绑凭据的销售入口。"""
    with engine.begin() as db:
        return add_pool(db, tenant_id)


def add_balance(engine: Engine, tenant_id: str, pool_id: str) -> str:
    balance_id = new_id("bal")
    with engine.begin() as db:
        sku_id = add_sku(db, tenant_id)
        db.execute(balance_insert(tenant_id, pool_id, sku_id, balance_id=balance_id))
    return balance_id


def request_id() -> str:
    return f"auth-test-{uuid4().hex}"


def actor(role: str) -> dict[str, str]:
    return {"X-Actor-Ref": "admin-7", "X-Actor-Role": role}


def adjustment(pool_id: str, action: str = "mark_unavailable", **extra: object) -> dict:
    return {"pool_id": pool_id, "action": action, "quantity": 1} | extra


def events(engine: Engine, rid: str) -> list[dict]:
    columns = SecurityEvent.__table__.columns
    with engine.connect() as db:
        statement = select(*columns).where(SecurityEvent.request_id == rid)
        return [dict(row) for row in db.execute(statement).mappings()]


def error_of(response, status: int) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error", "request_id"}
    assert body["request_id"] == response.headers["X-Request-ID"]
    return body["error"]


# --- 鉴权（第 4 步） ---


def test_healthz_and_readyz_need_no_credentials(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_a_valid_token_is_accepted(client, engine):
    caller = issue(engine)
    response = client.get("/test/items/item-1", headers=caller.auth)
    assert response.status_code == 200
    assert response.json()["data"] == {"credential_id": caller.credential_id}


def bad_authorization(engine: Engine, case: str) -> str | None:
    if case == "missing":
        return None
    if case == "wrong_scheme":
        return "Basic " + issue(engine).token
    if case == "bad_format":
        return "Bearer csk_short"
    if case == "unknown_token":
        return "Bearer " + generate_token()
    if case == "token_revoked":
        caller = issue(engine, token={"revoked_at": NOW})
    elif case == "token_expired":
        # 截止时间就是现在：到点即失效。
        caller = issue(engine, token={"expires_at": NOW})
    elif case == "credential_revoked":
        caller = issue(engine, credential=REVOKED_CREDENTIAL)
    else:
        assert case == "tenant_inactive"
        caller = issue(engine, tenant_status="inactive")
    return caller.auth["Authorization"]


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("missing", "missing_header"),
        ("wrong_scheme", "malformed_header"),
        ("bad_format", "malformed_header"),
        ("unknown_token", "token_not_found"),
        ("token_revoked", "token_revoked"),
        ("token_expired", "token_expired"),
        ("credential_revoked", "credential_revoked"),
        ("tenant_inactive", "tenant_inactive"),
    ],
)
def test_every_bad_credential_gets_the_same_401_and_its_reason_is_only_in_the_event(
    client, engine, case, reason
):
    authorization = bad_authorization(engine, case)
    rid = request_id()
    headers = {"X-Request-ID": rid}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = client.get("/test/items/item-1", headers=headers)
    assert error_of(response, 401) == EXPECTED_401
    assert reason not in response.text

    [event] = events(engine, rid)
    assert event["event_type"] == "invalid_credential"
    assert event["details"] == {"reason": reason}
    # 凭据无效时认不出租户与凭据。
    assert event["tenant_id"] is None
    assert event["credential_id"] is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {
                "token": {"revoked_at": NOW, "expires_at": NOW},
                "credential": REVOKED_CREDENTIAL,
                "tenant_status": "inactive",
            },
            "token_revoked",
        ),
        (
            {
                "token": {"expires_at": NOW},
                "credential": REVOKED_CREDENTIAL,
                "tenant_status": "inactive",
            },
            "token_expired",
        ),
        ({"credential": REVOKED_CREDENTIAL, "tenant_status": "inactive"}, "credential_revoked"),
    ],
)
def test_with_several_reasons_the_first_in_order_is_recorded(client, engine, overrides, reason):
    caller = issue(engine, **overrides)
    rid = request_id()
    response = client.get("/test/items/item-1", headers=caller.auth | {"X-Request-ID": rid})
    assert error_of(response, 401) == EXPECTED_401
    [event] = events(engine, rid)
    assert event["details"] == {"reason": reason}


def test_both_tokens_work_during_rotation_and_the_old_one_stops_at_its_deadline(client, engine):
    caller = issue(engine)
    new_token = add_token(engine, caller)
    set_token(engine, caller.token, expires_at=IN_AN_HOUR)
    for token in (caller.token, new_token):
        response = client.get("/test/items/item-1", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["data"] == {"credential_id": caller.credential_id}

    set_token(engine, caller.token, expires_at=A_SECOND_AGO)
    rid = request_id()
    old = client.get("/test/items/item-1", headers=caller.auth | {"X-Request-ID": rid})
    assert error_of(old, 401) == EXPECTED_401
    assert events(engine, rid)[0]["details"] == {"reason": "token_expired"}
    new = client.get("/test/items/item-1", headers={"Authorization": f"Bearer {new_token}"})
    assert new.status_code == 200


def test_revocation_takes_effect_on_the_next_request(client, engine):
    caller = issue(engine)
    assert client.get("/test/items/item-1", headers=caller.auth).status_code == 200
    set_token(engine, caller.token, revoked_at=NOW)
    assert error_of(client.get("/test/items/item-1", headers=caller.auth), 401) == EXPECTED_401


@pytest.fixture
def unreachable_client() -> Iterator[TestClient]:
    """连一个连不上的库：同一台 MySQL 的一个没人监听的端口。"""
    if not TEST_DATABASE_URL:
        pytest.skip("CORE_TEST_DATABASE_URL not set (CI only)")
    url = make_url(TEST_DATABASE_URL).set(port=1)
    test_client = make_client(url.render_as_string(hide_password=False))
    try:
        yield test_client
    finally:
        dispose(test_client)


def test_authentication_is_503_when_the_database_is_unreachable(unreachable_client):
    headers = {"Authorization": f"Bearer {generate_token()}"}
    error = error_of(unreachable_client.get("/test/items/item-1", headers=headers), 503)
    assert error["code"] == "UNAVAILABLE"
    assert error["retryable"] is True


def test_a_missing_header_is_still_401_when_the_event_cannot_be_written(unreachable_client):
    response = unreachable_client.get("/test/items/item-1")
    assert error_of(response, 401) == EXPECTED_401


def test_authentication_is_503_without_a_configured_database():
    test_client = make_client("")
    headers = {"Authorization": f"Bearer {generate_token()}"}
    error = error_of(test_client.get("/test/items/item-1", headers=headers), 503)
    assert error["code"] == "UNAVAILABLE"


# --- scope（第 5 步） ---


def test_a_missing_scope_is_403_and_recorded(client, engine):
    caller = issue(engine, scopes=[STOCK_WRITE])
    rid = request_id()
    response = client.get("/test/items/item-1", headers=caller.auth | {"X-Request-ID": rid})
    assert error_of(response, 403)["code"] == "FORBIDDEN"

    [event] = events(engine, rid)
    assert event["event_type"] == "forbidden"
    assert event["tenant_id"] == caller.tenant_id
    assert event["credential_id"] == caller.credential_id
    assert event["details"] == {"category": "scope", "missing_scopes": [CATALOG_READ]}


@pytest.mark.parametrize(
    ("scopes", "missing"),
    [
        ([STOCK_WRITE], [CATALOG_WRITE]),
        ([CATALOG_WRITE], [STOCK_WRITE]),
        ([CATALOG_READ, STOCK_READ], [CATALOG_WRITE, STOCK_WRITE]),
    ],
)
def test_merge_endpoints_need_both_scopes(client, engine, scopes, missing):
    caller = issue(engine, scopes=scopes)
    rid = request_id()
    headers = caller.auth | KEY | actor(STOCK_ADMIN) | {"X-Request-ID": rid}
    assert error_of(client.post("/test/merges", headers=headers), 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "scope", "missing_scopes": missing}


def test_merge_endpoints_pass_with_both_scopes(client, engine):
    caller = issue(engine, scopes=[CATALOG_WRITE, STOCK_WRITE])
    headers = caller.auth | KEY | actor(STOCK_ADMIN)
    response = client.post("/test/merges", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"] == {"role": STOCK_ADMIN}


# --- 其他请求头（第 6 步）与角色（第 8 步） ---


@pytest.mark.parametrize("key", [None, "", "has space", "k" * 129])
def test_write_endpoints_need_a_valid_idempotency_key(client, engine, key):
    caller = issue(engine)
    rid = request_id()
    headers = caller.auth | actor(STOCK_ADMIN) | {"X-Request-ID": rid}
    if key is not None:
        headers["Idempotency-Key"] = key
    error = error_of(client.post("/test/merges", headers=headers), 400)
    assert error["code"] == "INVALID_REQUEST"
    assert events(engine, rid) == []


@pytest.mark.parametrize(
    "actor_headers",
    [
        {},
        {"X-Actor-Ref": "admin-7"},
        {"X-Actor-Role": STOCK_ADMIN},
        {"X-Actor-Ref": "admin 7", "X-Actor-Role": STOCK_ADMIN},
        {"X-Actor-Ref": "admin-7", "X-Actor-Role": "Stock_Admin"},
    ],
)
def test_a_role_endpoint_without_valid_actor_headers_is_400(client, engine, actor_headers):
    caller = issue(engine)
    rid = request_id()
    headers = caller.auth | KEY | actor_headers | {"X-Request-ID": rid}
    error = error_of(client.post("/test/merges", headers=headers), 400)
    assert error["code"] == "INVALID_REQUEST"
    assert events(engine, rid) == []


def test_actor_headers_are_checked_even_where_no_role_is_required(client, engine):
    caller = issue(engine)
    headers = caller.auth | {"X-Actor-Ref": "admin-7"}
    error = error_of(client.get("/test/items/item-1", headers=headers), 400)
    assert error["code"] == "INVALID_REQUEST"
    headers = caller.auth | actor(CATALOG_ADMIN)
    assert client.get("/test/items/item-1", headers=headers).status_code == 200


def test_an_unaccepted_role_is_403_and_recorded(client, engine):
    caller = issue(engine)
    rid = request_id()
    headers = caller.auth | KEY | actor(CATALOG_ADMIN) | {"X-Request-ID": rid}
    assert error_of(client.post("/test/merges", headers=headers), 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["event_type"] == "forbidden"
    assert event["actor_ref"] == "admin-7"
    assert event["details"] == {"category": "role", "role": CATALOG_ADMIN}


def test_the_accepted_roles_can_depend_on_the_action_in_the_body(client, engine):
    caller = issue(engine)
    operator = caller.auth | KEY | actor(STOCK_OPERATOR)
    marked = client.post("/test/adjustments", json=adjustment(caller.pool_id), headers=operator)
    assert marked.status_code == 200
    assert marked.json()["data"] == {"pool_id": caller.pool_id, "role": STOCK_OPERATOR}

    rid = request_id()
    write_off = adjustment(caller.pool_id, "write_off")
    refused = client.post(
        "/test/adjustments", json=write_off, headers=operator | {"X-Request-ID": rid}
    )
    assert error_of(refused, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "role", "role": STOCK_OPERATOR}

    admin = caller.auth | KEY | actor(STOCK_ADMIN)
    assert client.post("/test/adjustments", json=write_off, headers=admin).status_code == 200


# --- 池授权（第 9 步） ---


def test_pool_authorization_by_pool_id(client, engine):
    caller = issue(engine)
    other_pool = add_other_pool(engine, caller.tenant_id)
    foreign = issue(engine)
    headers = caller.auth | KEY | actor(STOCK_OPERATOR)

    def post(pool_id: str, rid: str):
        return client.post(
            "/test/adjustments", json=adjustment(pool_id), headers=headers | {"X-Request-ID": rid}
        )

    assert post(caller.pool_id, request_id()).status_code == 200

    # 本租户但不是获准的池：403。
    rid = request_id()
    assert error_of(post(other_pool, rid), 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["event_type"] == "forbidden"
    assert event["details"] == {"category": "pool", "pool_id": other_pool}

    # 别的租户的池与不存在的池：同一个 404，只有前者记 cross_tenant。
    rid = request_id()
    cross = post(foreign.pool_id, rid)
    assert error_of(cross, 404)["code"] == POOL_NOT_FOUND
    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert event["tenant_id"] == caller.tenant_id
    assert event["credential_id"] == caller.credential_id
    assert event["details"] == {"object_type": "pool", "id": foreign.pool_id}

    rid = request_id()
    missing = post(new_id("pool"), rid)
    assert error_of(missing, 404)["code"] == POOL_NOT_FOUND
    assert events(engine, rid) == []


def test_sales_commands_use_the_channel_pool(client, engine):
    caller = issue(engine)
    body = {"lines": [{"line_ref": "line-1", "quantity": 1}]}
    response = client.post("/test/reservations", json=body, headers=caller.auth | KEY)
    assert response.status_code == 200
    assert response.json()["data"] == {"pool_id": caller.pool_id}


def test_pool_authorization_by_object(client, engine):
    caller = issue(engine)
    other_pool = add_other_pool(engine, caller.tenant_id)
    foreign = issue(engine)
    own = add_balance(engine, caller.tenant_id, caller.pool_id)
    in_other_pool = add_balance(engine, caller.tenant_id, other_pool)
    foreign_balance = add_balance(engine, foreign.tenant_id, foreign.pool_id)
    headers = caller.auth | KEY | actor(STOCK_OPERATOR)

    def touch(balance_id: str, rid: str):
        url = f"/test/balances/{balance_id}/touch"
        return client.post(url, headers=headers | {"X-Request-ID": rid})

    response = touch(own, request_id())
    assert response.status_code == 200
    assert response.json()["data"] == {"pool_id": caller.pool_id}

    rid = request_id()
    assert error_of(touch(in_other_pool, rid), 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {
        "category": "pool",
        "pool_id": other_pool,
        "object_type": "balance",
        "object_id": in_other_pool,
    }

    rid = request_id()
    assert error_of(touch(foreign_balance, rid), 404)["code"] == "BALANCE_NOT_FOUND"
    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "balance", "id": foreign_balance}

    rid = request_id()
    assert error_of(touch(new_id("bal"), rid), 404)["code"] == "BALANCE_NOT_FOUND"
    assert events(engine, rid) == []


@pytest.mark.parametrize("channel_status", [None, "inactive"])
def test_without_an_active_channel_every_pool_check_is_403(client, engine, channel_status):
    caller = issue(engine, channel_status=channel_status)
    own = add_balance(engine, caller.tenant_id, caller.pool_id)
    headers = caller.auth | KEY | actor(STOCK_OPERATOR)
    requests = [
        ("/test/adjustments", {"json": adjustment(caller.pool_id)}),
        ("/test/reservations", {"json": {"lines": [{"line_ref": "line-1", "quantity": 1}]}}),
        (f"/test/balances/{own}/touch", {}),
    ]
    for url, kwargs in requests:
        rid = request_id()
        response = client.post(url, headers=headers | {"X-Request-ID": rid}, **kwargs)
        assert error_of(response, 403)["code"] == "FORBIDDEN"
        [event] = events(engine, rid)
        assert event["event_type"] == "forbidden"
        assert event["details"]["category"] == "pool"


def test_a_second_tenant_sees_the_first_tenants_pool_and_objects_as_missing(client, engine):
    first = issue(engine)
    second = issue(engine)
    balance = add_balance(engine, first.tenant_id, first.pool_id)
    missing_balance = new_id("bal")
    headers = second.auth | KEY | actor(STOCK_OPERATOR)

    # 不存在与跨租户：同一个请求 ID 发两次，响应完全相同；只有跨租户那次记事件。
    for url, missing_url, kwargs, missing_kwargs in [
        (
            "/test/adjustments",
            "/test/adjustments",
            {"json": adjustment(first.pool_id)},
            {"json": adjustment(new_id("pool"))},
        ),
        (f"/test/balances/{balance}/touch", f"/test/balances/{missing_balance}/touch", {}, {}),
    ]:
        rid = request_id()
        cross = client.post(url, headers=headers | {"X-Request-ID": rid}, **kwargs)
        missing = client.post(
            missing_url, headers=headers | {"X-Request-ID": rid}, **missing_kwargs
        )
        assert cross.status_code == missing.status_code == 404
        assert cross.json() == missing.json()
        [event] = events(engine, rid)
        assert event["event_type"] == "cross_tenant"
        assert event["tenant_id"] == second.tenant_id


# --- 检查顺序 ---


def test_bad_credentials_come_before_a_bad_body(client):
    headers = {"Content-Type": "application/json", "Authorization": "Bearer nope"}
    response = client.post("/test/adjustments", content=b'{"pool_id": ', headers=headers)
    assert error_of(response, 401) == EXPECTED_401


def test_a_missing_scope_comes_before_a_foreign_pool(client, engine):
    caller = issue(engine, scopes=[CATALOG_READ])
    foreign = issue(engine)
    rid = request_id()
    headers = caller.auth | KEY | actor(STOCK_ADMIN) | {"X-Request-ID": rid}
    response = client.post("/test/adjustments", json=adjustment(foreign.pool_id), headers=headers)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "scope", "missing_scopes": [STOCK_WRITE]}


def test_a_missing_idempotency_key_comes_before_a_bad_body(client, engine):
    caller = issue(engine)
    headers = caller.auth | actor(STOCK_ADMIN) | {"Content-Type": "application/json"}
    response = client.post("/test/adjustments", content=b"[]", headers=headers)
    assert error_of(response, 400)["code"] == "INVALID_REQUEST"


def test_a_bad_body_comes_before_an_unaccepted_role(client, engine):
    caller = issue(engine)
    rid = request_id()
    headers = caller.auth | KEY | actor(CATALOG_ADMIN) | {"X-Request-ID": rid}
    body = {"pool_id": caller.pool_id, "action": "write_off"}  # 少了 quantity
    response = client.post("/test/adjustments", json=body, headers=headers)
    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    assert events(engine, rid) == []


def test_an_unaccepted_role_comes_before_a_foreign_pool(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    rid = request_id()
    headers = caller.auth | KEY | actor(STOCK_OPERATOR) | {"X-Request-ID": rid}
    body = adjustment(foreign.pool_id, "write_off")
    response = client.post("/test/adjustments", json=body, headers=headers)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "role", "role": STOCK_OPERATOR}


def test_an_unauthenticated_oversized_request_is_413(client, engine):
    rid = request_id()
    headers = {"Content-Type": "application/json", "X-Request-ID": rid}
    response = client.post("/test/adjustments", content=b"x" * OVER_LIMIT, headers=headers)
    assert error_of(response, 413)["code"] == "REQUEST_TOO_LARGE"
    assert events(engine, rid) == []


# --- 安全事件的内容与写入 ---


def test_an_event_records_the_request_but_not_the_token_body_or_query(client, engine):
    caller = issue(engine)
    rid = request_id()
    extra = {"X-Request-ID": rid, "User-Agent": "u" * 300}
    headers = caller.auth | KEY | actor(STOCK_OPERATOR) | extra
    body = adjustment(caller.pool_id, "write_off", note="body-marker")
    response = client.post("/test/adjustments?trace=query-marker", json=body, headers=headers)
    assert response.status_code == 403

    [event] = events(engine, rid)
    assert event["event_type"] == "forbidden"
    assert event["request_id"] == rid
    assert event["http_method"] == "POST"
    assert event["path"] == "/test/adjustments"
    assert event["user_agent"] == "u" * 200
    assert event["actor_ref"] == "admin-7"
    assert event["occurred_at"] is not None
    recorded = json.dumps(event, default=str)
    secrets = (caller.token, token_digest(caller.token), "Bearer", "body-marker", "query-marker")
    for marker in secrets:
        assert marker not in recorded


@pytest.mark.parametrize(
    ("actor_headers", "expected"),
    [
        ({"X-Actor-Ref": "admin-7", "X-Actor-Role": STOCK_ADMIN}, "admin-7"),
        ({"X-Actor-Ref": "admin-7"}, None),
        ({"X-Actor-Role": STOCK_ADMIN}, None),
        ({"X-Actor-Ref": "admin-7", "X-Actor-Role": "Stock_Admin"}, None),
        ({"X-Actor-Ref": "admin 7", "X-Actor-Role": STOCK_ADMIN}, None),
    ],
)
def test_the_actor_is_recorded_only_when_both_headers_are_valid(
    client, engine, actor_headers, expected
):
    rid = request_id()
    response = client.get("/test/items/item-1", headers=actor_headers | {"X-Request-ID": rid})
    assert error_of(response, 401) == EXPECTED_401
    [event] = events(engine, rid)
    assert event["actor_ref"] == expected


def test_a_long_path_is_truncated_to_512_characters(client, engine):
    rid = request_id()
    path = "/test/items/" + "x" * 600
    assert client.get(path, headers={"X-Request-ID": rid}).status_code == 401
    [event] = events(engine, rid)
    assert event["path"] == path[:512]


def test_a_security_event_survives_the_business_transaction_rollback(client, engine):
    caller = issue(engine)
    marker = new_id("ten")
    rid = request_id()
    headers = caller.auth | KEY | {"X-Request-ID": rid}
    response = client.post(f"/test/rollbacks/{marker}", headers=headers)
    assert error_of(response, 404)["code"] == POOL_NOT_FOUND

    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "pool", "id": marker}
    with engine.connect() as db:
        statement = select(Tenant.tenant_id).where(Tenant.tenant_id == marker)
        assert db.execute(statement).first() is None


def test_a_failed_event_write_does_not_change_the_response(client, engine, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.services.security_events")
    rid = request_id()
    headers = {"X-Request-ID": rid}

    def broken(connection, values):
        raise OperationalError("INSERT INTO security_events", {}, Exception("disk full"))

    with monkeypatch.context() as patch:
        patch.setattr(security_events, "_insert", broken)
        failed = client.get("/test/items/item-1", headers=headers)
    assert events(engine, rid) == []
    assert any("security event not written" in r.getMessage() for r in caplog.records)

    written = client.get("/test/items/item-1", headers=headers)
    assert len(events(engine, rid)) == 1
    assert failed.status_code == written.status_code == 401
    assert failed.json() == written.json()
