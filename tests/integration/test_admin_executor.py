"""后台接口的公共检查与后台执行器（契约 §8.6 第 2 条、§5.10“命令体共用”，Q-84、Q-86）。

在真 MySQL 上跑；测试给后台开关为真的生产应用挂两条测试路由（照 test_admin_session_api，
在 instance_marked 里以 CORE_ENV=production 建好即退出）：
一个带 pool_id 正文的写命令，命令给管理员的租户插一个名为 marker 的地点、记下收到的命令上下文；
一个按路径 pool_id 读后台入口的读命令。写命令的操作类型可以按 marker 换成订单命令。
管理员真实登录取得会话 cookie 与 CSRF 令牌；来源用随机的 10.x.y.z，各测试的登录尝试互不计数。
提交的数据在 finally 里按外键先后删掉（安全事件、幂等记录、入口、池、地点，
再照 test_admin_auth_service 删登录尝试、会话、账号、主体、租户），
会话结束时 migrated_engine 才能照常降到 base。
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, insert, select

from app.api import admin_executor, fulfilment, reservations, sale_commits
from app.api.admin_executor import (
    ADMIN_ROLES,
    ORDER_OPERATIONS,
    AdminWrite,
    admin_context,
    admin_write,
    assert_role,
    backoffice_entry,
    run_admin,
)
from app.api.admin_session import AdminPrincipal, require_admin
from app.api.errors import ok
from app.api.requests import RequestBody, json_body
from app.domain.requests import CATALOG_ADMIN, STOCK_ADMIN, STOCK_OPERATOR, Actor
from app.models.inventory import IdempotencyRecord, LedgerEntry, Location, SalesChannel, StockPool
from app.models.service_auth import SecurityEvent
from app.services.command_context import CommandContext
from app.services.idempotency import CommandResult, Execution
from tests.integration.conftest import add_pool, channel_insert, new_id
from tests.integration.test_admin_auth_service import Account, World, cleanup
from tests.integration.test_admin_session_api import (
    BASE_URL,
    COOKIE,
    detail_of,
    logged_in,
    login,
    production_app,
)
from tests.integration.test_api_auth import error_of, events

NOTES = "test-admin-notes.create"
NOTES_URL = "/api/v1/admin/test/notes"
POOLS_URL = "/api/v1/admin/test/pools"
MARKER_PREFIX = "admin-note-"
USER_AGENT = "testclient"

# 命令按 marker 记下收到的命令上下文；写路由按 marker 取操作类型（没登记时为 NOTES）。
CONTEXTS: dict[str, CommandContext] = {}
OPERATIONS: dict[str, str] = {}


# --- 测试专用路由 ---


class AdminNote(RequestBody):
    pool_id: str
    marker: str


note_write = admin_write(json_body(AdminNote), STOCK_OPERATOR, STOCK_ADMIN)


def write_note(execution: Execution, note: AdminNote) -> CommandResult:
    context = execution.context
    CONTEXTS[note.marker] = context
    location_id = new_id("loc")
    values = {
        "location_id": location_id,
        "tenant_id": context.tenant_id,
        "name": note.marker,
        "status": "active",
    }
    execution.connection.execute(insert(Location).values(values))
    return CommandResult({"marker": note.marker, "location_id": location_id}, status=201)


def add_test_routes(app) -> None:
    @app.post(NOTES_URL)
    def create_note(
        request: Request, write: Annotated[AdminWrite[AdminNote], Depends(note_write)]
    ) -> Response:
        note = write.body.model
        channel_id = backoffice_entry(request, write.principal, note.pool_id)
        context = admin_context(
            request,
            write.principal,
            role=write.role,
            idempotency_key=write.idempotency_key,
            channel_id=channel_id,
            pool_id=note.pool_id,
        )
        return run_admin(
            request,
            context,
            operation=OPERATIONS.get(note.marker, NOTES),
            body=write.body,
            command=lambda execution: write_note(execution, note),
        )

    @app.get(POOLS_URL + "/{pool_id}")
    def read_pool(
        request: Request,
        pool_id: str,
        principal: Annotated[AdminPrincipal, Depends(require_admin)],
    ) -> Response:
        channel_id = backoffice_entry(request, principal, pool_id)
        return ok(request, {"pool_id": pool_id, "channel_id": channel_id})


# --- 夹具 ---


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
        CONTEXTS.clear()
        OPERATIONS.clear()


def cleanup_admin(engine: Engine, world: World) -> None:
    tenant_ids = world.tenant_ids
    with engine.begin() as db:
        for model in (SecurityEvent, IdempotencyRecord, SalesChannel, StockPool, Location):
            db.execute(delete(model).where(model.tenant_id.in_(tenant_ids)))
    cleanup(engine, world)


@pytest.fixture
def world(engine: Engine) -> Iterator[World]:
    built = World(engine)
    try:
        built.tenant_id = built.tenant()
        yield built
    finally:
        cleanup_admin(engine, built)


# --- 造数据 ---


@dataclass(frozen=True)
class Session:
    account: Account
    token: str
    csrf: str


def sign_in(client: TestClient, world: World) -> Session:
    account = world.account()
    body, token = logged_in(login(client, world.source(), account.username))
    return Session(account, token, body["csrf_token"])


def add_admin_pool(engine: Engine, tenant_id: str, *channels: tuple[str, str]) -> tuple:
    """一个池及给定 (kind, status) 的入口；返回池 ID 与各入口 ID。"""
    with engine.begin() as db:
        pool_id = add_pool(db, tenant_id)
        channel_ids = []
        for kind, status in channels:
            channel_id = new_id("chn")
            db.execute(
                channel_insert(tenant_id, pool_id, channel_id=channel_id, kind=kind, status=status)
            )
            channel_ids.append(channel_id)
    return pool_id, channel_ids


def backoffice_pool(engine: Engine, tenant_id: str) -> tuple[str, str]:
    pool_id, [channel_id] = add_admin_pool(engine, tenant_id, ("backoffice", "active"))
    return pool_id, channel_id


def new_marker() -> str:
    return f"{MARKER_PREFIX}{uuid4().hex}"


def new_key() -> str:
    return f"admin:note:{uuid4().hex}"


def note(pool_id: str, marker: str | None = None) -> dict:
    return {"pool_id": pool_id, "marker": marker or new_marker()}


def write_headers(
    session: Session | None, key: str | None, csrf: str | None = None
) -> dict[str, str]:
    """会话 cookie 与 CSRF 令牌（csrf 不给时用会话的）；key 为 None 时不带 Idempotency-Key。"""
    headers = {}
    if session is not None:
        headers["Cookie"] = f"{COOKIE}={session.token}"
        headers["X-CSRF-Token"] = session.csrf if csrf is None else csrf
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def post_note(client: TestClient, headers: dict[str, str], body: object = None, **kwargs):
    if body is not None:
        kwargs["json"] = body
    response = client.post(NOTES_URL, headers=headers, **kwargs)
    client.cookies.clear()
    return response


def get_pool(client: TestClient, session: Session | None, pool_id: str):
    headers = {} if session is None else {"Cookie": f"{COOKIE}={session.token}"}
    response = client.get(f"{POOLS_URL}/{pool_id}", headers=headers)
    client.cookies.clear()
    return response


# --- 观察 ---


def rid_of(response) -> str:
    return response.headers["X-Request-ID"]


def data_of(response, status: int) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"data", "request_id"}
    assert body["request_id"] == rid_of(response)
    return body["data"]


def records_of(engine: Engine, world: World) -> list[dict]:
    statement = (
        select(*IdempotencyRecord.__table__.columns)
        .where(IdempotencyRecord.tenant_id.in_(world.tenant_ids))
        .order_by(IdempotencyRecord.created_at, IdempotencyRecord.idempotency_record_id)
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def notes_of(engine: Engine, world: World) -> list[tuple[str, str]]:
    """命令写下的地点：(地点名即 marker, location_id)。"""
    statement = (
        select(Location.name, Location.location_id)
        .where(Location.tenant_id.in_(world.tenant_ids), Location.name.like(f"{MARKER_PREFIX}%"))
        .order_by(Location.name, Location.location_id)
    )
    with engine.connect() as db:
        return [tuple(row) for row in db.execute(statement)]


def entry_count(engine: Engine, world: World) -> int:
    statement = (
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.tenant_id.in_(world.tenant_ids))
    )
    with engine.connect() as db:
        return db.execute(statement).scalar_one()


def writes(engine: Engine, world: World) -> tuple:
    """拒绝前后要相同的：幂等记录、流水与命令写下的地点。"""
    return records_of(engine, world), entry_count(engine, world), notes_of(engine, world)


def assert_admin_event(event: dict, session: Session, event_type: str, details: dict) -> None:
    assert event | {"event_id": None, "occurred_at": None} == {
        "event_id": None,
        "occurred_at": None,
        "event_type": event_type,
        "tenant_id": session.account.tenant_id,
        "credential_id": session.account.credential_id,
        "request_id": event["request_id"],
        "actor_ref": session.account.account_id,
        "http_method": "POST",
        "path": NOTES_URL,
        "user_agent": USER_AGENT,
        "details": details,
    }


def without_request_id(response) -> dict:
    body = response.json()
    del body["request_id"]
    return body


# --- 检查顺序：每一步各一条，请求里同时带着之后各步的错 ---


def test_a_body_over_128_kib_is_413_before_the_session(client, engine, world):
    pool_id, _ = backoffice_pool(engine, world.tenant_id)
    before = writes(engine, world)
    content = b'{"pool_id": "' + pool_id.encode() + b'", "marker": "' + b"x" * 131072 + b'"}'
    # 没有会话、没有提交键：大小先报。
    response = post_note(client, {"Content-Type": "application/json"}, content=content)
    assert error_of(response, 413)["code"] == "REQUEST_TOO_LARGE"
    assert writes(engine, world) == before


def test_no_session_is_401_not_authenticated(client, engine, world):
    before = writes(engine, world)
    for headers in (
        {},
        {"Cookie": f"{COOKIE}=no-such-token", "X-CSRF-Token": "x"},
    ):
        # 没有提交键、正文也不合格：会话先报。
        response = post_note(client, headers, {})
        assert detail_of(response, 401) == "not_authenticated"
    assert writes(engine, world) == before


def test_a_wrong_csrf_token_is_403_csrf_failed(client, engine, world):
    session = sign_in(client, world)
    before = writes(engine, world)
    for csrf in ("", session.csrf.upper(), "x" * 64):
        response = post_note(client, write_headers(session, None, csrf), {})
        assert detail_of(response, 403) == "csrf_failed"
    missing = {"Cookie": f"{COOKIE}={session.token}"}
    assert detail_of(post_note(client, missing, {}), 403) == "csrf_failed"
    assert writes(engine, world) == before


@pytest.mark.parametrize("key", [None, "", "has space", "k" * 129])
def test_a_missing_or_malformed_idempotency_key_is_400(client, engine, world, key):
    session = sign_in(client, world)
    before = writes(engine, world)
    # 正文不合格：提交键先报。
    response = post_note(client, write_headers(session, key), {"pool_id": 1})
    error = error_of(response, 400)
    assert error["code"] == "INVALID_REQUEST"
    assert error["message"] == "invalid Idempotency-Key header"
    assert writes(engine, world) == before


def test_an_invalid_body_is_422_before_the_role_and_the_tenant(client, engine, world, monkeypatch):
    session = sign_in(client, world)
    other_pool, _ = backoffice_pool(engine, world.tenant())
    # 角色不被接受、池属于别的租户：正文先报。
    monkeypatch.setattr(admin_executor, "ADMIN_ROLES", frozenset({CATALOG_ADMIN}))
    before = writes(engine, world)
    json_type = {"Content-Type": "application/json"}
    for extra_headers, kwargs in (
        ({}, {"body": {"pool_id": other_pool}}),
        ({}, {"body": note(other_pool) | {"extra": 1}}),
        ({}, {"body": {"pool_id": other_pool, "marker": 7}}),
        (json_type, {"content": b"{"}),
    ):
        headers = write_headers(session, new_key()) | extra_headers
        response = post_note(client, headers, **kwargs)
        assert error_of(response, 422)["code"] == "INVALID_REQUEST"
        assert events(engine, rid_of(response)) == []
    assert writes(engine, world) == before


def test_a_role_the_admin_does_not_hold_is_403_with_a_forbidden_event(
    client, engine, world, monkeypatch
):
    session = sign_in(client, world)
    # 别的租户的池：角色先于本租户报。
    other_pool, _ = backoffice_pool(engine, world.tenant())
    monkeypatch.setattr(admin_executor, "ADMIN_ROLES", frozenset({STOCK_OPERATOR, CATALOG_ADMIN}))
    before = writes(engine, world)
    response = post_note(client, write_headers(session, new_key()), note(other_pool))
    error = error_of(response, 403)
    assert error["code"] == "FORBIDDEN"
    [event] = events(engine, rid_of(response))
    assert_admin_event(event, session, "forbidden", {"category": "role", "role": STOCK_ADMIN})
    assert writes(engine, world) == before


def test_another_tenants_pool_and_a_missing_pool_are_the_same_404(client, engine, world):
    session = sign_in(client, world)
    other_pool, _ = backoffice_pool(engine, world.tenant())
    before = writes(engine, world)

    cross = post_note(client, write_headers(session, new_key()), note(other_pool))
    missing = post_note(client, write_headers(session, new_key()), note(new_id("pool")))
    assert error_of(cross, 404)["code"] == "POOL_NOT_FOUND"
    assert error_of(missing, 404)["code"] == "POOL_NOT_FOUND"
    assert without_request_id(cross) == without_request_id(missing)
    assert rid_of(cross) != rid_of(missing)

    [event] = events(engine, rid_of(cross))
    details = {"object_type": "pool", "id": other_pool}
    assert_admin_event(event, session, "cross_tenant", details)
    assert events(engine, rid_of(missing)) == []
    assert writes(engine, world) == before


def test_a_pool_without_an_active_backoffice_entry_is_422(client, engine, world):
    session = sign_in(client, world)
    bare, _ = add_admin_pool(engine, world.tenant_id)
    inactive, _ = add_admin_pool(engine, world.tenant_id, ("backoffice", "inactive"))
    api, _ = add_admin_pool(engine, world.tenant_id, ("api", "active"))
    before = writes(engine, world)
    for pool_id in (bare, inactive, api):
        response = post_note(client, write_headers(session, new_key()), note(pool_id))
        error = error_of(response, 422)
        assert error["code"] == "INVALID_REQUEST"
        assert error["message"] == "the pool has no active backoffice entry"
        assert events(engine, rid_of(response)) == []
    assert writes(engine, world) == before


# --- 成功、重放与冲突 ---


def test_a_successful_write_runs_with_the_admin_context(client, engine, world):
    session = sign_in(client, world)
    pool_id, channel_id = backoffice_pool(engine, world.tenant_id)
    key = new_key()
    body = note(pool_id)
    # 调用方给的 X-Request-ID 不用：中间件总是生成。
    headers = write_headers(session, key) | {"X-Request-ID": "caller-chosen-id"}
    first = post_note(client, headers, body)
    data = data_of(first, 201)
    rid = rid_of(first)
    assert rid.startswith("req_")
    assert data["marker"] == body["marker"]

    account = session.account
    assert account.account_id.startswith("adm_")
    assert CONTEXTS[body["marker"]] == CommandContext(
        tenant_id=world.tenant_id,
        channel_id=channel_id,
        pool_id=pool_id,
        credential_id=account.credential_id,
        actor=Actor(account.account_id, STOCK_ADMIN),
        request_id=rid,
        idempotency_key=key,
        reservation_ttl_seconds=None,
        http_method="POST",
        path=NOTES_URL,
        user_agent=USER_AGENT,
    )
    [record] = records_of(engine, world)
    assert record["tenant_id"] == world.tenant_id
    assert record["credential_id"] == account.credential_id
    assert record["operation"] == NOTES
    assert record["idempotency_key"] == key
    assert record["request_id"] == rid
    assert record["response_status"] == 201
    assert record["response_body"] == first.json()
    assert notes_of(engine, world) == [(body["marker"], data["location_id"])]

    # 同键同内容：原响应（含当时的 request_id），带 Idempotency-Replayed。
    CONTEXTS.clear()
    replay = post_note(client, write_headers(session, key), body)
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert rid_of(replay) != rid
    assert CONTEXTS == {}

    # 同键换内容：409，记一条以管理员为主体的 idempotency_conflict。
    conflict = post_note(client, write_headers(session, key), note(pool_id))
    assert error_of(conflict, 409)["code"] == "IDEMPOTENCY_CONFLICT"
    [event] = events(engine, rid_of(conflict))
    assert_admin_event(event, session, "idempotency_conflict", {"operation": NOTES})
    assert len(records_of(engine, world)) == 1
    assert notes_of(engine, world) == [(body["marker"], data["location_id"])]
    assert entry_count(engine, world) == 0


def test_two_admins_with_the_same_key_do_not_conflict(client, engine, world):
    first_admin, second_admin = sign_in(client, world), sign_in(client, world)
    pool_id, _ = backoffice_pool(engine, world.tenant_id)
    key = new_key()
    body = note(pool_id)
    first = data_of(post_note(client, write_headers(first_admin, key), body), 201)
    second = data_of(post_note(client, write_headers(second_admin, key), body), 201)
    assert first["location_id"] != second["location_id"]

    records = records_of(engine, world)
    assert {record["credential_id"] for record in records} == {
        first_admin.account.credential_id,
        second_admin.account.credential_id,
    }
    assert {record["idempotency_key"] for record in records} == {key}


# --- 读接口 ---


def test_a_read_needs_a_session_and_the_tenants_pool_but_no_csrf(client, engine, world):
    session = sign_in(client, world)
    pool_id, channel_id = backoffice_pool(engine, world.tenant_id)
    other_pool, _ = backoffice_pool(engine, world.tenant())

    assert detail_of(get_pool(client, None, pool_id), 401) == "not_authenticated"
    assert data_of(get_pool(client, session, pool_id), 200) == {
        "pool_id": pool_id,
        "channel_id": channel_id,
    }
    cross = get_pool(client, session, other_pool)
    missing = get_pool(client, session, new_id("pool"))
    assert error_of(cross, 404)["code"] == "POOL_NOT_FOUND"
    assert without_request_id(cross) == without_request_id(missing)
    [event] = events(engine, rid_of(cross))
    assert event["event_type"] == "cross_tenant"
    assert (event["credential_id"], event["actor_ref"]) == (
        session.account.credential_id,
        session.account.account_id,
    )
    assert (event["http_method"], event["details"]) == (
        "GET",
        {"object_type": "pool", "id": other_pool},
    )
    assert events(engine, rid_of(missing)) == []


# --- 订单命令不走后台执行器 ---


def test_the_order_operations_are_the_seven_order_commands():
    assert ORDER_OPERATIONS == {
        reservations.RESERVATIONS_CREATE,
        reservations.RESERVATIONS_RELEASE,
        sale_commits.SALE_COMMITS_CREATE,
        sale_commits.LATE_PAYMENT_REVIEWS_CLOSE,
        fulfilment.DISPATCHES_CREATE,
        fulfilment.REFUND_RESTOCKS_CREATE,
        fulfilment.RETURN_RECEIPTS_CREATE,
    }
    assert len(ORDER_OPERATIONS) == 7


def spy_core(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把 run_admin 调的执行器核心换成记下操作类型、再调原核心的函数。"""
    calls: list[str] = []
    original = admin_executor.run_with_context

    def spy(request, context, **kwargs):
        calls.append(kwargs["operation"])
        return original(request, context, **kwargs)

    monkeypatch.setattr(admin_executor, "run_with_context", spy)
    return calls


@pytest.mark.parametrize("operation", sorted(ORDER_OPERATIONS))
def test_an_order_command_on_the_backoffice_entry_is_403_before_the_core(
    client, engine, world, monkeypatch, operation
):
    session = sign_in(client, world)
    pool_id, _ = backoffice_pool(engine, world.tenant_id)
    calls = spy_core(monkeypatch)
    body = note(pool_id)
    OPERATIONS[body["marker"]] = operation
    before = writes(engine, world)

    response = post_note(client, write_headers(session, new_key()), body)
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid_of(response))
    details = {"category": "entry_kind", "operation": operation}
    assert_admin_event(event, session, "forbidden", details)
    assert calls == []
    assert body["marker"] not in CONTEXTS
    assert writes(engine, world) == before


def test_other_operations_go_through_the_core(client, engine, world, monkeypatch):
    session = sign_in(client, world)
    pool_id, _ = backoffice_pool(engine, world.tenant_id)
    calls = spy_core(monkeypatch)
    data_of(post_note(client, write_headers(session, new_key()), note(pool_id)), 201)
    assert calls == [NOTES]


# --- 断言的角色（Q-86） ---

PRINCIPAL = AdminPrincipal(
    account_id="adm_TEST",
    username="kelvin",
    session_id=1,
    tenant_id="ten_TEST",
    credential_id="cred_TEST",
)


def asserted(*accepted: str) -> str:
    # 角色被接受时依赖不碰请求。
    return assert_role(*accepted)(None, PRINCIPAL)


def test_admins_hold_the_three_roles():
    assert ADMIN_ROLES == {CATALOG_ADMIN, STOCK_ADMIN, STOCK_OPERATOR}


def test_a_command_accepting_one_role_asserts_it():
    for role in (CATALOG_ADMIN, STOCK_ADMIN, STOCK_OPERATOR):
        assert asserted(role) == role


def test_a_command_accepting_operator_and_admin_asserts_stock_admin():
    assert asserted(STOCK_OPERATOR, STOCK_ADMIN) == STOCK_ADMIN
    assert asserted(STOCK_ADMIN, STOCK_OPERATOR) == STOCK_ADMIN


def test_an_order_command_requiring_no_role_asserts_stock_admin():
    assert asserted() == STOCK_ADMIN


def test_roles_without_a_fixed_rule_are_rejected_when_building():
    with pytest.raises(ValueError):
        assert_role(CATALOG_ADMIN, STOCK_ADMIN)
    with pytest.raises(ValueError):
        assert_role("owner")
