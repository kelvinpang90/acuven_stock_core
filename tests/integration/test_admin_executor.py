"""后台写接口与读接口的公共检查、后台执行器（契约 §8.6 第 2 条、§5.10“命令体共用”、§7.1）。

在真 MySQL 上跑；照 test_admin_session_api，给后台开关为真的生产应用挂测试路由
（一个带 pool_id 正文的写命令、一个订单类操作类型的写命令与一个读命令），
真实登录取得会话 cookie 与 CSRF 令牌。安全事件按响应头 X-Request-ID 查（后台路径总是生成）。
提交的数据在 finally 里按外键先后删掉，会话结束时 migrated_engine 才能照常降到 base。
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, Request
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select

from app.api import admin_executor
from app.api.admin_executor import (
    ORDER_OPERATIONS,
    AdminWrite,
    admin_context,
    assert_role,
    asserted_role,
    backoffice_entry,
    require_admin_write,
    run_admin,
)
from app.api.admin_session import AdminPrincipal, require_admin
from app.api.errors import ok
from app.api.requests import ParsedBody, RequestBody, json_body
from app.domain.requests import CATALOG_ADMIN, STOCK_ADMIN, STOCK_OPERATOR
from app.models.inventory import IdempotencyRecord, LedgerEntry, Location, SalesChannel, StockPool
from app.models.service_auth import SecurityEvent
from app.services.idempotency import CommandResult, Execution
from tests.integration.conftest import add_pool, channel_insert, new_id
from tests.integration.test_admin_auth_service import Account, World, cleanup
from tests.integration.test_admin_session_api import (
    BASE_URL,
    detail_of,
    logged_in,
    login,
    production_app,
    with_cookie,
)
from tests.integration.test_api_auth import error_of

TEST_OPERATION = "admin_tests.command"
COMMANDS = "/api/v1/admin/test-commands"
ORDER_COMMANDS = "/api/v1/admin/test-order-commands"
POOLS = "/api/v1/admin/test-pools"
OVER_LIMIT = 128 * 1024 + 1
USER_AGENT = "admin-executor-test"


# --- 测试专用路由 ---


class Command(RequestBody):
    pool_id: str
    note: str


command_body = json_body(Command)
Write = Annotated[AdminWrite, Depends(require_admin_write)]
CommandBody = Annotated[ParsedBody[Command], Depends(command_body)]
StockRole = Annotated[str, Depends(assert_role({STOCK_OPERATOR, STOCK_ADMIN}))]
OrderRole = Annotated[str, Depends(assert_role(()))]
CurrentAdmin = Annotated[AdminPrincipal, Depends(require_admin)]


def echo_context(execution: Execution) -> CommandResult:
    """命令体：只经 execution.context 读身份、入口与请求。"""
    context = execution.context
    return CommandResult(
        {
            "tenant_id": context.tenant_id,
            "channel_id": context.channel_id,
            "pool_id": context.pool_id,
            "credential_id": context.credential_id,
            "actor_ref": context.actor.ref,
            "actor_role": context.actor.role,
            "request_id": context.request_id,
            "idempotency_key": context.idempotency_key,
        },
        201,
    )


# 订单类操作的命令体被调用时记在这里（应当始终为空）。
order_calls: list[str] = []


def order_command(execution: Execution) -> CommandResult:
    order_calls.append(execution.operation)
    return CommandResult({})


def _run(
    request: Request, write: AdminWrite, body: ParsedBody[Command], role: str, operation: str
) -> Response:
    pool_id = body.model.pool_id
    channel_id = backoffice_entry(request, write.principal, pool_id)
    context = admin_context(
        request,
        write.principal,
        role=role,
        idempotency_key=write.idempotency_key,
        channel_id=channel_id,
        pool_id=pool_id,
    )
    command = order_command if operation in ORDER_OPERATIONS else echo_context
    return run_admin(request, context, operation, command, body=body)


def add_test_routes(app) -> None:
    @app.post(COMMANDS)
    def command(request: Request, write: Write, body: CommandBody, role: StockRole) -> Response:
        return _run(request, write, body, role, TEST_OPERATION)

    @app.post(ORDER_COMMANDS + "/{operation}")
    def order(
        request: Request, operation: str, write: Write, body: CommandBody, role: OrderRole
    ) -> Response:
        return _run(request, write, body, role, operation)

    @app.get(POOLS + "/{pool_id}")
    def read_pool(request: Request, pool_id: str, principal: CurrentAdmin) -> JSONResponse:
        channel_id = backoffice_entry(request, principal, pool_id)
        return ok(request, {"channel_id": channel_id})


# --- 夹具与造数据 ---


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    app = production_app(engine, admin_enabled=True)
    add_test_routes(app)
    try:
        yield TestClient(app, base_url=BASE_URL)
    finally:
        app.state.engine.dispose()


@dataclass(frozen=True)
class SignedIn:
    """真实登录得到的会话令牌与 CSRF 令牌。"""

    account: Account
    token: str
    csrf: str

    def headers(self, key: str | bytes | None = None, **extra: str) -> dict[str, str | bytes]:
        sent = with_cookie(self.token, **{"X-CSRF-Token": self.csrf, "User-Agent": USER_AGENT})
        if key is not None:
            sent["Idempotency-Key"] = key
        return sent | extra


@dataclass
class Setup:
    world: World
    tenant_id: str
    pool_id: str  # 有 active 的 backoffice 入口
    channel_id: str
    bare_pool_id: str  # 只有一个 inactive 的 backoffice 入口
    foreign_pool_id: str  # 别的租户的池，有 backoffice 入口


def add_backoffice_pool(engine: Engine, tenant_id: str, status: str = "active") -> tuple[str, str]:
    channel_id = new_id("chn")
    with engine.begin() as db:
        pool_id = add_pool(db, tenant_id)
        db.execute(
            channel_insert(
                tenant_id, pool_id, channel_id=channel_id, kind="backoffice", status=status
            )
        )
    return pool_id, channel_id


def remove(engine: Engine, world: World) -> None:
    tenants = world.tenant_ids
    with engine.begin() as db:
        for model in (SecurityEvent, IdempotencyRecord, LedgerEntry):
            db.execute(delete(model).where(model.tenant_id.in_(tenants)))
        for model in (SalesChannel, StockPool, Location):
            db.execute(delete(model).where(model.tenant_id.in_(tenants)))
    cleanup(engine, world)


@pytest.fixture
def setup(engine: Engine) -> Iterator[Setup]:
    world = World(engine)
    try:
        world.tenant_id = world.tenant()
        pool_id, channel_id = add_backoffice_pool(engine, world.tenant_id)
        bare_pool_id, _ = add_backoffice_pool(engine, world.tenant_id, status="inactive")
        foreign_pool_id, _ = add_backoffice_pool(engine, world.tenant())
        yield Setup(world, world.tenant_id, pool_id, channel_id, bare_pool_id, foreign_pool_id)
    finally:
        remove(engine, world)


def sign_in(client: TestClient, setup: Setup) -> SignedIn:
    """本租户的新管理员，经登录接口真实登录。"""
    account = setup.world.account()
    body, token = logged_in(login(client, setup.world.source(), account.username))
    return SignedIn(account, token, body["csrf_token"])


@pytest.fixture
def admin(client: TestClient, setup: Setup) -> SignedIn:
    return sign_in(client, setup)


def new_key() -> str:
    return f"admin:test:{uuid4().hex}"


def command_body_of(pool_id: str, note: str = "first") -> dict:
    return {"pool_id": pool_id, "note": note}


def post(
    client: TestClient,
    admin: SignedIn,
    body: object,
    key: str | bytes | None,
    path: str = COMMANDS,
):
    return client.post(path, json=body, headers=admin.headers(key))


# --- 观察 ---


def events(engine: Engine, response) -> list[dict]:
    rid = response.headers["X-Request-ID"]
    columns = SecurityEvent.__table__.columns
    with engine.connect() as db:
        statement = select(*columns).where(SecurityEvent.request_id == rid)
        return [dict(row) for row in db.execute(statement).mappings()]


def records(engine: Engine, setup: Setup) -> list[dict]:
    columns = IdempotencyRecord.__table__.columns
    statement = (
        select(*columns)
        .where(IdempotencyRecord.tenant_id.in_(setup.world.tenant_ids))
        .order_by(IdempotencyRecord.created_at, IdempotencyRecord.idempotency_record_id)
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def ledger_count(engine: Engine, setup: Setup) -> int:
    statement = (
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.tenant_id.in_(setup.world.tenant_ids))
    )
    with engine.connect() as db:
        return db.execute(statement).scalar_one()


def assert_nothing_saved(engine: Engine, setup: Setup) -> None:
    assert records(engine, setup) == []
    assert ledger_count(engine, setup) == 0


def assert_admin_event(event: dict, admin: SignedIn, event_type: str, details: dict, path: str):
    assert event["event_type"] == event_type
    assert event["tenant_id"] == admin.account.tenant_id
    assert event["credential_id"] == admin.account.credential_id
    assert event["actor_ref"] == admin.account.account_id
    assert event["http_method"] == "POST"
    assert event["path"] == path
    assert event["user_agent"] == USER_AGENT
    assert event["details"] == details


# --- 检查顺序 ---


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "without-length"])
def test_an_oversized_body_is_413_before_the_session(client, engine, setup, chunked):
    content = b"x" * OVER_LIMIT
    response = client.post(
        COMMANDS,
        content=iter([content]) if chunked else content,
        headers={"Content-Type": "application/json"},
    )
    assert error_of(response, 413)["code"] == "REQUEST_TOO_LARGE"
    assert_nothing_saved(engine, setup)


def test_without_a_session_it_is_401(client, engine, setup):
    for headers in ({}, with_cookie("no-such-token", **{"X-CSRF-Token": "x"})):
        response = client.post(
            COMMANDS,
            json={"bad": "body"},
            headers=headers | {"Idempotency-Key": "not valid"},
        )
        assert detail_of(response, 401) == "not_authenticated"
    assert_nothing_saved(engine, setup)


def test_a_wrong_csrf_token_is_403_before_the_key_and_body(client, engine, setup, admin):
    for csrf in (None, "", admin.csrf.upper(), admin.csrf[:-1]):
        headers = with_cookie(admin.token)
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        response = client.post(COMMANDS, json={"bad": "body"}, headers=headers)
        assert detail_of(response, 403) == "csrf_failed"
    assert_nothing_saved(engine, setup)


@pytest.mark.parametrize("key", [None, "", "has space", "x" * 129, "é".encode()])
def test_a_missing_or_malformed_key_is_400_before_the_body(client, engine, setup, admin, key):
    response = post(client, admin, {"bad": "body"}, key)
    error = error_of(response, 400)
    assert error["code"] == "INVALID_REQUEST"
    assert error["message"] == "invalid Idempotency-Key header"
    assert_nothing_saved(engine, setup)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"pool_id": "pool_x"}, id="missing-note"),
        pytest.param({"pool_id": "pool_x", "note": "n", "extra": 1}, id="extra-field"),
        pytest.param({"pool_id": 1, "note": "n"}, id="wrong-type"),
        pytest.param([], id="not-an-object"),
    ],
)
def test_an_invalid_body_is_422_before_the_role(client, engine, setup, admin, monkeypatch, body):
    monkeypatch.setattr(admin_executor, "ADMIN_ROLES", frozenset({CATALOG_ADMIN}))
    response = post(client, admin, body, new_key())
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert events(engine, response) == []
    assert_nothing_saved(engine, setup)


def test_a_role_the_admin_does_not_hold_is_403_and_recorded(
    client, engine, setup, admin, monkeypatch
):
    monkeypatch.setattr(admin_executor, "ADMIN_ROLES", frozenset({CATALOG_ADMIN, STOCK_OPERATOR}))
    # 角色先于本租户：别的租户的池也是 403，且没有 cross_tenant。
    response = post(client, admin, command_body_of(setup.foreign_pool_id), new_key())
    error = error_of(response, 403)
    assert error == {"code": "FORBIDDEN", "message": "forbidden", "retryable": False, "details": {}}
    [event] = events(engine, response)
    details = {"category": "role", "role": STOCK_ADMIN}
    assert_admin_event(event, admin, "forbidden", details, COMMANDS)
    assert event["request_id"] == response.headers["X-Request-ID"]
    assert_nothing_saved(engine, setup)


def test_a_foreign_pool_and_a_missing_pool_are_the_same_404(client, engine, setup, admin):
    foreign = post(client, admin, command_body_of(setup.foreign_pool_id), new_key())
    missing = post(client, admin, command_body_of(new_id("pool")), new_key())

    for response in (foreign, missing):
        error = error_of(response, 404)
        assert error == {
            "code": "POOL_NOT_FOUND",
            "message": "pool not found",
            "retryable": False,
            "details": {},
        }
    # 两个响应只差 request_id。
    assert foreign.json() | {"request_id": None} == missing.json() | {"request_id": None}
    assert foreign.headers["X-Request-ID"] != missing.headers["X-Request-ID"]

    [event] = events(engine, foreign)
    details = {"object_type": "pool", "id": setup.foreign_pool_id}
    assert_admin_event(event, admin, "cross_tenant", details, COMMANDS)
    assert events(engine, missing) == []
    assert_nothing_saved(engine, setup)


def test_a_pool_without_an_active_backoffice_channel_is_422(client, engine, setup, admin):
    response = post(client, admin, command_body_of(setup.bare_pool_id), new_key())
    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"] == {"pool_id": setup.bare_pool_id}
    assert events(engine, response) == []
    assert_nothing_saved(engine, setup)


# --- 成功、重放与冲突 ---


def test_a_successful_command_sees_the_admin_context(client, engine, setup, admin):
    key = new_key()
    body = command_body_of(setup.pool_id)
    response = client.post(
        COMMANDS, json=body, headers=admin.headers(key, **{"X-Request-ID": "caller-chosen"})
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert set(payload) == {"data", "request_id"}
    request_id = response.headers["X-Request-ID"]
    # 后台路径不用调用方给的 X-Request-ID，总是由中间件生成。
    assert request_id != "caller-chosen"
    assert request_id.startswith("req_")
    assert payload["request_id"] == request_id
    assert payload["data"] == {
        "tenant_id": setup.tenant_id,
        "channel_id": setup.channel_id,
        "pool_id": setup.pool_id,
        "credential_id": admin.account.credential_id,
        "actor_ref": admin.account.account_id,
        "actor_role": STOCK_ADMIN,
        "request_id": request_id,
        "idempotency_key": key,
    }
    assert admin.account.account_id.startswith("adm_")
    assert response.headers.get_list("cache-control") == ["no-store"]

    [record] = records(engine, setup)
    assert record["tenant_id"] == setup.tenant_id
    assert record["credential_id"] == admin.account.credential_id
    assert record["operation"] == TEST_OPERATION
    assert record["idempotency_key"] == key
    assert record["request_id"] == request_id
    assert record["response_status"] == 201
    assert record["response_body"] == payload
    assert events(engine, response) == []

    replay = post(client, admin, body, key)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == payload
    assert replay.headers["X-Request-ID"] != request_id

    conflict = post(client, admin, command_body_of(setup.pool_id, "changed"), key)
    assert error_of(conflict, 409)["code"] == "IDEMPOTENCY_CONFLICT"
    [event] = events(engine, conflict)
    assert event["event_type"] == "idempotency_conflict"
    assert event["credential_id"] == admin.account.credential_id
    assert event["actor_ref"] == admin.account.account_id
    assert event["details"] == {"operation": TEST_OPERATION}
    assert len(records(engine, setup)) == 1


def test_two_admins_with_the_same_key_do_not_conflict(client, engine, setup):
    first, second = sign_in(client, setup), sign_in(client, setup)
    key = new_key()
    one = post(client, first, command_body_of(setup.pool_id, "one"), key)
    two = post(client, second, command_body_of(setup.pool_id, "two"), key)
    for response, admin in ((one, first), (two, second)):
        assert response.status_code == 201, response.text
        assert "Idempotency-Replayed" not in response.headers
        assert response.json()["data"]["credential_id"] == admin.account.credential_id
        assert response.json()["data"]["actor_ref"] == admin.account.account_id
    saved = records(engine, setup)
    assert sorted(record["credential_id"] for record in saved) == sorted(
        [first.account.credential_id, second.account.credential_id]
    )
    assert {record["idempotency_key"] for record in saved} == {key}


# --- 订单命令不经后台执行器 ---


def test_the_order_operations_are_the_seven_order_commands():
    assert ORDER_OPERATIONS == {
        "reservations.create",
        "reservations.release",
        "sale_commits.create",
        "dispatches.create",
        "refund_restocks.create",
        "return_receipts.create",
        "late_payment_reviews.close",
    }


@pytest.mark.parametrize("operation", sorted(ORDER_OPERATIONS))
def test_an_order_operation_on_the_backoffice_channel_is_403(
    client, engine, setup, admin, monkeypatch, operation
):
    core_calls = []

    def core(*args, **kwargs):
        core_calls.append(kwargs.get("operation"))
        raise AssertionError("the executor core must not be called")

    monkeypatch.setattr(admin_executor, "run_with_context", core)
    order_calls.clear()
    path = f"{ORDER_COMMANDS}/{operation}"
    response = post(client, admin, command_body_of(setup.pool_id), new_key(), path)

    error = error_of(response, 403)
    assert error == {"code": "FORBIDDEN", "message": "forbidden", "retryable": False, "details": {}}
    [event] = events(engine, response)
    details = {"category": "entry_kind", "operation": operation}
    assert_admin_event(event, admin, "forbidden", details, path)
    assert core_calls == []
    assert order_calls == []
    assert_nothing_saved(engine, setup)


def test_a_non_order_operation_reaches_the_executor_core(client, engine, setup, admin, monkeypatch):
    seen = []
    original = admin_executor.run_with_context

    def core(request, context, **kwargs):
        seen.append((context.channel_id, kwargs["operation"]))
        return original(request, context, **kwargs)

    monkeypatch.setattr(admin_executor, "run_with_context", core)
    response = post(client, admin, command_body_of(setup.pool_id), new_key())
    assert response.status_code == 201, response.text
    assert seen == [(setup.channel_id, TEST_OPERATION)]


# --- 角色的断言规则（Q-86） ---


@pytest.mark.parametrize("role", [CATALOG_ADMIN, STOCK_ADMIN, STOCK_OPERATOR])
def test_a_command_accepting_one_role_asserts_it(role):
    assert asserted_role({role}) == role
    principal = AdminPrincipal("adm_x", "kelvin", 1, "ten_x", "cred_x")
    # 通过时不碰请求。
    assert assert_role([role])(None, principal) == role


def test_a_command_accepting_operator_and_admin_asserts_stock_admin():
    assert asserted_role({STOCK_OPERATOR, STOCK_ADMIN}) == STOCK_ADMIN
    principal = AdminPrincipal("adm_x", "kelvin", 1, "ten_x", "cred_x")
    assert assert_role((STOCK_ADMIN, STOCK_OPERATOR))(None, principal) == STOCK_ADMIN


def test_an_order_command_requiring_no_role_asserts_stock_admin():
    assert asserted_role(()) == STOCK_ADMIN
    principal = AdminPrincipal("adm_x", "kelvin", 1, "ten_x", "cred_x")
    assert assert_role(set())(None, principal) == STOCK_ADMIN


@pytest.mark.parametrize(
    "accepted",
    [{"admin"}, {CATALOG_ADMIN, STOCK_ADMIN}, {CATALOG_ADMIN, STOCK_OPERATOR, STOCK_ADMIN}],
)
def test_roles_without_a_rule_are_refused_when_the_endpoint_is_defined(accepted):
    with pytest.raises(ValueError):
        asserted_role(accepted)
    with pytest.raises(ValueError):
        assert_role(accepted)


# --- 读接口 ---


def test_a_read_needs_only_the_session_and_the_tenant(client, engine, setup, admin):
    # 不带 CSRF 令牌与提交键。
    headers = with_cookie(admin.token, **{"User-Agent": USER_AGENT})
    response = client.get(f"{POOLS}/{setup.pool_id}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"channel_id": setup.channel_id}

    assert detail_of(client.get(f"{POOLS}/{setup.pool_id}"), 401) == "not_authenticated"

    foreign = client.get(f"{POOLS}/{setup.foreign_pool_id}", headers=headers)
    assert error_of(foreign, 404)["code"] == "POOL_NOT_FOUND"
    [event] = events(engine, foreign)
    assert event["event_type"] == "cross_tenant"
    assert event["http_method"] == "GET"
    assert event["actor_ref"] == admin.account.account_id
    assert event["credential_id"] == admin.account.credential_id
    assert event["details"] == {"object_type": "pool", "id": setup.foreign_pool_id}
    assert_nothing_saved(engine, setup)
