"""幂等执行器（契约 §7.1、§7.2、§8.3）。

在真 MySQL 上跑；测试给应用挂测试专用的写路由。命令在事务里给调用方的租户插一个地点，
地点名就是请求体里的 marker，用来观察命令的写入有没有生效、有没有重复。
按 marker 挂的钩子在命令插完地点之后运行：业务拒绝、抛异常、绕过锁先提交一条同键记录。
声明了目录写锁的测试路由有两个：一个同上插地点；一个是计数命令，marker 是计数地点的 location_id，
命令用不加锁的普通 SELECT 读出地点名里的计数、运行钩子、再写回加一。
声明了余额行预建的测试路由按请求体的 rows 预建余额行，命令同上插地点；
声明了 READ COMMITTED 的测试路由命令同上。
业务引用路由的命令先插一个名为 marker 的地点，再插 location_id 为 marker 的引用地点
（地点名记下请求摘要与先插那一行的 ID），撞主键时抛 BusinessRefTaken，按引用地点重新判定。
重做路由声明了余额行预建、READ COMMITTED 与开事务前的钩子，命令同上插地点；
预建函数与钩子可以按 marker 换掉，用来数调用次数、在重做期间停住。
三遍路由声明了 READ COMMITTED 与最多做 3 遍，命令同上插地点。
上下文路由直接调核心 run_with_context，命令上下文按 marker 取测试登记的，命令同上插地点。
造数据与观察都用测试自己的连接；应用用自己的连接池。
"""

import ast
import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, event, func, insert, make_url, select, text, update
from sqlalchemy.exc import IntegrityError

from app.api.auth import authorize
from app.api.errors import ApiError
from app.api.requests import ParsedBody, RequestBody, json_body
from app.config import Settings
from app.domain.catalog import catalog_lock_name
from app.domain.idempotency import lock_name, request_digest
from app.domain.requests import STOCK_ADMIN, STOCK_OPERATOR, Actor
from app.domain.service_auth import STOCK_WRITE, generate_token, token_digest
from app.main import create_app
from app.models.inventory import IdempotencyRecord, Location, StockBalance
from app.services import command_context, idempotency
from app.services.command_context import CommandContext
from app.services.idempotency import (
    BalanceRows,
    BeforeTransaction,
    BusinessRefTaken,
    CommandResult,
    Execution,
    RedoCommand,
    run_idempotent,
)
from tests.integration.conftest import (
    TEST_DATABASE_URL,
    add_credential,
    add_sku,
    add_tenant,
    balance_insert,
    new_id,
)
from tests.integration.test_api_auth import (
    Caller,
    add_other_pool,
    add_token,
    error_of,
    events,
    issue,
)
from tests.integration.test_inventory_schema import idempotency_insert
from tests.integration.test_service_auth_schema import token_insert

NOTES = "test-notes.create"
FOLDER_NOTES = "test-folder-notes.create"
CATALOG_NOTES = "test-catalog-notes.create"
CATALOG_COUNT = "test-catalog-count.increment"
CATALOG_NOTES_URL = "/test/catalog/notes"
CATALOG_COUNT_URL = "/test/catalog/count"
BALANCE_NOTES = "test-balance-notes.create"
BALANCE_NOTES_URL = "/test/balances/notes"
READ_COMMITTED_NOTES = "test-read-committed-notes.create"
READ_COMMITTED_NOTES_URL = "/test/read-committed/notes"
REFS = "test-refs.claim"
REFS_URL = "/test/refs"
REDO_NOTES = "test-redo-notes.create"
REDO_NOTES_URL = "/test/redo/notes"
THREE_ATTEMPTS_NOTES = "test-three-attempts-notes.create"
THREE_ATTEMPTS_NOTES_URL = "/test/three-attempts/notes"
CONTEXT_NOTES = "test-context-notes.create"
CONTEXT_NOTES_URL = "/test/context/notes"
KEY = "shop:note:1"
REJECTED = "NOTE_REJECTED"
REF_CONFLICT = "BUSINESS_REF_CONFLICT"
COMMAND_EVENT = "business_ref_conflict"

# 按 marker 挂的钩子：命令插完地点、登记完事件之后运行。每个测试结束时清空。
HOOKS: dict[str, Callable[[Execution], None]] = {}
# 重做路由按 marker 换的预建函数与开事务前的钩子；没挂时预建请求体的 rows、不调钩子。
BALANCE_ROWS: dict[str, BalanceRows] = {}
BEFORE_TRANSACTION: dict[str, BeforeTransaction] = {}
# 上下文路由按 marker 取的命令上下文，与它收到的请求（看兼容属性 request 是不是它）。
CONTEXTS: dict[str, CommandContext] = {}
ROUTE_REQUESTS: dict[str, Request] = {}


# --- 测试专用路由 ---


class Note(RequestBody):
    marker: str
    event: bool = False  # 为真时命令登记一条安全事件


note_body = json_body(Note)
NoteBody = Annotated[ParsedBody[Note], Depends(note_body)]
WRITE = Depends(authorize(STOCK_WRITE, write=True))


def write_note(execution: Execution, note: Note) -> CommandResult:
    tenant_id = execution.context.tenant_id
    location_id = new_id("loc")
    values = {
        "location_id": location_id,
        "tenant_id": tenant_id,
        "name": note.marker,
        "status": "active",
    }
    execution.connection.execute(insert(Location).values(values))
    if note.event:
        details = {"ref_type": "note", "ref": note.marker}
        execution.record_security_event(COMMAND_EVENT, details)
    hook = HOOKS.get(note.marker)
    if hook is not None:
        hook(execution)
    return CommandResult({"marker": note.marker, "location_id": location_id}, status=201)


def count_note(execution: Execution, note: Note) -> CommandResult:
    """读计数（普通 SELECT，不加锁）、运行钩子、写回加一。"""
    where = Location.location_id == note.marker
    current = int(execution.connection.execute(select(Location.name).where(where)).scalar_one())
    hook = HOOKS.get(note.marker)
    if hook is not None:
        hook(execution)
    execution.connection.execute(update(Location).where(where).values(name=str(current + 1)))
    return CommandResult({"count": current + 1})


class BalanceNote(Note):
    rows: list[list[str]]  # 要预建的 [pool_id, sku_id]，原样交给执行器


balance_note_body = json_body(BalanceNote)


def balance_rows_of(note: BalanceNote) -> Callable[[Connection], list[tuple[str, str]]]:
    def balance_rows(connection: Connection) -> list[tuple[str, str]]:
        # 预建函数可以读库；读完由执行器结束这条读隐式开的事务。
        connection.execute(select(func.count()).select_from(StockBalance)).scalar_one()
        return [(pool_id, sku_id) for pool_id, sku_id in note.rows]

    return balance_rows


def claim_ref(execution: Execution, note: Note) -> CommandResult:
    """先插一个地点，再插业务引用（location_id 为 marker 的地点）。

    撞主键时抛 BusinessRefTaken；插完引用之后运行钩子。
    """
    tenant_id = execution.context.tenant_id
    note_id = new_id("loc")
    values = {"location_id": note_id, "tenant_id": tenant_id, "name": note.marker}
    execution.connection.execute(insert(Location).values(status="active", **values))
    if note.event:
        execution.record_security_event(COMMAND_EVENT, {"ref_type": "note", "ref": note.marker})
    ref = {
        "location_id": note.marker,
        "tenant_id": tenant_id,
        "name": f"{execution.request_digest}:{note_id}",
        "status": "active",
    }
    try:
        execution.connection.execute(insert(Location).values(ref))
    except IntegrityError as exc:
        if exc.orig.args[0] != 1062:
            raise
        raise BusinessRefTaken(lambda rejudging: judge_ref(rejudging, note.marker)) from None
    hook = HOOKS.get(note.marker)
    if hook is not None:
        hook(execution)
    return CommandResult({"ref": note.marker, "note_id": note_id}, status=201)


def judge_ref(execution: Execution, ref: str) -> CommandResult:
    """按已提交的引用重新判定：摘要相同返回它的结果，不同 409。都登记一条事件。"""
    statement = select(Location.name).where(Location.location_id == ref)
    digest, note_id = execution.connection.execute(statement).scalar_one().split(":")
    execution.record_security_event(COMMAND_EVENT, {"ref_type": "rejudge", "ref": ref})
    if digest != execution.request_digest:
        raise ApiError(409, REF_CONFLICT, "the reference was used with different content")
    return CommandResult({"ref": ref, "note_id": note_id})


def add_test_routes(app) -> None:
    @app.post("/test/notes", dependencies=[WRITE])
    def create_note(request: Request, body: NoteBody) -> Response:
        return run_idempotent(
            request,
            operation=NOTES,
            body=body,
            command=lambda execution: write_note(execution, body.model),
        )

    @app.post("/test/folders/{folder_id}/notes", dependencies=[WRITE])
    def create_folder_note(request: Request, folder_id: str, body: NoteBody) -> Response:
        return run_idempotent(
            request,
            operation=FOLDER_NOTES,
            body=body,
            command=lambda execution: write_note(execution, body.model),
        )

    @app.post(CATALOG_NOTES_URL, dependencies=[WRITE])
    def create_catalog_note(request: Request, body: NoteBody) -> Response:
        return run_idempotent(
            request,
            operation=CATALOG_NOTES,
            body=body,
            command=lambda execution: write_note(execution, body.model),
            catalog_lock=True,
        )

    @app.post(CATALOG_COUNT_URL, dependencies=[WRITE])
    def increment_catalog_count(request: Request, body: NoteBody) -> Response:
        return run_idempotent(
            request,
            operation=CATALOG_COUNT,
            body=body,
            command=lambda execution: count_note(execution, body.model),
            catalog_lock=True,
        )

    @app.post(BALANCE_NOTES_URL, dependencies=[WRITE])
    def create_balance_note(
        request: Request, body: Annotated[ParsedBody[BalanceNote], Depends(balance_note_body)]
    ) -> Response:
        return run_idempotent(
            request,
            operation=BALANCE_NOTES,
            body=body,
            command=lambda execution: write_note(execution, body.model),
            balance_rows=balance_rows_of(body.model),
        )

    @app.post(READ_COMMITTED_NOTES_URL, dependencies=[WRITE])
    def create_read_committed_note(request: Request, body: NoteBody) -> Response:
        return run_idempotent(
            request,
            operation=READ_COMMITTED_NOTES,
            body=body,
            command=lambda execution: write_note(execution, body.model),
            read_committed=True,
        )

    @app.post(REFS_URL, dependencies=[WRITE])
    def claim_test_ref(request: Request, body: NoteBody) -> Response:
        return run_idempotent(
            request,
            operation=REFS,
            body=body,
            command=lambda execution: claim_ref(execution, body.model),
        )

    @app.post(REDO_NOTES_URL, dependencies=[WRITE])
    def create_redo_note(
        request: Request, body: Annotated[ParsedBody[BalanceNote], Depends(balance_note_body)]
    ) -> Response:
        note = body.model
        return run_idempotent(
            request,
            operation=REDO_NOTES,
            body=body,
            command=lambda execution: write_note(execution, note),
            balance_rows=BALANCE_ROWS.get(note.marker, balance_rows_of(note)),
            read_committed=True,
            before_transaction=BEFORE_TRANSACTION.get(note.marker),
        )

    @app.post(THREE_ATTEMPTS_NOTES_URL, dependencies=[WRITE])
    def create_three_attempts_note(request: Request, body: NoteBody) -> Response:
        return run_idempotent(
            request,
            operation=THREE_ATTEMPTS_NOTES,
            body=body,
            command=lambda execution: write_note(execution, body.model),
            read_committed=True,
            attempts=3,
        )

    @app.post(CONTEXT_NOTES_URL, dependencies=[WRITE])
    def create_context_note(request: Request, body: NoteBody) -> Response:
        note = body.model
        ROUTE_REQUESTS[note.marker] = request
        return idempotency.run_with_context(
            request,
            CONTEXTS[note.marker],
            operation=CONTEXT_NOTES,
            body=body,
            command=lambda execution: write_note(execution, note),
        )


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    app = create_app(Settings(env="demo", git_sha="c" * 40, database_url=TEST_DATABASE_URL))
    add_test_routes(app)
    try:
        yield TestClient(app)
    finally:
        app.state.engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


@pytest.fixture(autouse=True)
def clear_hooks() -> Iterator[None]:
    yield
    HOOKS.clear()
    BALANCE_ROWS.clear()
    BEFORE_TRANSACTION.clear()


@pytest.fixture(autouse=True)
def clear_contexts() -> Iterator[None]:
    yield
    CONTEXTS.clear()
    ROUTE_REQUESTS.clear()


# --- 请求、造数据与观察 ---


def send(
    client: TestClient,
    caller: Caller,
    key: str = KEY,
    *,
    url: str = "/test/notes",
    token: str | None = None,
    **kwargs: object,
):
    headers = {
        "Authorization": f"Bearer {token or caller.token}",
        "Idempotency-Key": key,
        "X-Request-ID": f"idem-test-{uuid4().hex}",
    }
    headers |= kwargs.pop("headers", {})
    return client.post(url, headers=headers, **kwargs)


def rid_of(response) -> str:
    return response.headers["X-Request-ID"]


def add_caller(engine: Engine, caller: Caller) -> Caller:
    """同一租户的另一个凭据。"""
    plain = generate_token()
    with engine.begin() as db:
        credential_id = add_credential(db, caller.tenant_id, scopes=[STOCK_WRITE])
        db.execute(token_insert(caller.tenant_id, credential_id, token_digest=token_digest(plain)))
    return Caller(caller.tenant_id, caller.pool_id, credential_id, plain)


def notes(engine: Engine, caller: Caller, marker: str) -> int:
    """命令写入的数据：调用方租户里名为 marker 的地点数。"""
    where = (Location.tenant_id == caller.tenant_id, Location.name == marker)
    statement = select(func.count()).select_from(Location).where(*where)
    with engine.connect() as db:
        return db.execute(statement).scalar_one()


def records(engine: Engine, caller: Caller, key: str = KEY, operation: str = NOTES) -> list[dict]:
    statement = select(*IdempotencyRecord.__table__.columns).where(
        IdempotencyRecord.tenant_id == caller.tenant_id,
        IdempotencyRecord.credential_id == caller.credential_id,
        IdempotencyRecord.operation == operation,
        IdempotencyRecord.idempotency_key == key,
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def note_lock(caller: Caller, key: str = KEY, operation: str = NOTES) -> str:
    database = make_url(TEST_DATABASE_URL).database
    return lock_name(database, caller.tenant_id, caller.credential_id, operation, key)


def catalog_lock(caller: Caller) -> str:
    database = make_url(TEST_DATABASE_URL).database
    return catalog_lock_name(database, caller.tenant_id)


def get_lock(db: Connection, name: str) -> int | None:
    return db.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": name}).scalar()


def release_lock(db: Connection, name: str) -> None:
    db.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})


def assert_lock_free(engine: Engine, name: str) -> None:
    """另一个连接能取到同名锁，说明处理请求的连接已经释放了它。"""
    with engine.connect() as other:
        assert get_lock(other, name) == 1
        release_lock(other, name)


def reject(execution: Execution) -> None:
    raise ApiError(409, REJECTED, "note rejected", details={"reason": "test"})


def fail(execution: Execution) -> None:
    raise RuntimeError("boom-marker")


# --- 首次执行与重放 ---


def test_the_first_success_writes_the_command_data_and_the_record(client, engine):
    caller = issue(engine)
    body = {"marker": "m"}
    response = send(client, caller, json=body)
    assert response.status_code == 201
    assert "Idempotency-Replayed" not in response.headers
    first = response.json()
    assert first["request_id"] == rid_of(response)
    assert first["data"]["marker"] == "m"
    assert notes(engine, caller, "m") == 1

    [record] = records(engine, caller)
    assert record["request_hash"] == request_digest({}, body)
    assert record["response_status"] == 201
    assert record["response_body"] == first
    assert record["request_id"] == rid_of(response)


def test_the_same_key_and_content_replays_the_first_response(client, engine):
    caller = issue(engine)
    first = send(client, caller, json={"marker": "m"})
    replay = send(client, caller, json={"marker": "m"})

    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    # 响应体原样是首次的（含首次的 request_id），X-Request-ID 头是本次的。
    assert replay.json() == first.json()
    assert replay.json()["request_id"] == rid_of(first)
    assert rid_of(replay) != rid_of(first)
    assert notes(engine, caller, "m") == 1
    assert len(records(engine, caller)) == 1


def test_a_different_key_order_is_the_same_content(client, engine):
    caller = issue(engine)
    json_type = {"headers": {"Content-Type": "application/json"}}
    first = send(client, caller, content=b'{"marker": "m", "event": false}', **json_type)
    replay = send(client, caller, content=b'{"event":false,"marker":"m"}', **json_type)
    assert first.status_code == replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert notes(engine, caller, "m") == 1


def test_operations_and_credentials_have_their_own_keys(client, engine):
    caller = issue(engine)
    other = add_caller(engine, caller)
    body = {"marker": "m"}
    responses = [
        send(client, caller, json=body),
        send(client, caller, url="/test/folders/f1/notes", json=body),
        send(client, other, json=body),
    ]
    for response in responses:
        assert response.status_code == 201
        assert "Idempotency-Replayed" not in response.headers
    assert notes(engine, caller, "m") == 3
    assert len(records(engine, caller)) == 1
    assert len(records(engine, caller, operation=FOLDER_NOTES)) == 1
    assert len(records(engine, other)) == 1


def test_after_a_token_rotation_the_same_key_replays(client, engine):
    caller = issue(engine)
    new_token = add_token(engine, caller)
    first = send(client, caller, json={"marker": "m"})
    replay = send(client, caller, token=new_token, json={"marker": "m"})
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert notes(engine, caller, "m") == 1


# --- 冲突 ---


def test_the_same_key_with_different_content_is_a_conflict(client, engine):
    caller = issue(engine)
    first = send(client, caller, json={"marker": "m1"})
    conflict = send(client, caller, json={"marker": "m2"})

    error = error_of(conflict, 409)
    assert error["code"] == "IDEMPOTENCY_CONFLICT"
    assert "Idempotency-Replayed" not in conflict.headers
    assert notes(engine, caller, "m1") == 1
    assert notes(engine, caller, "m2") == 0
    [record] = records(engine, caller)
    assert record["response_body"] == first.json()

    [event] = events(engine, rid_of(conflict))
    assert event["event_type"] == "idempotency_conflict"
    assert event["tenant_id"] == caller.tenant_id
    assert event["credential_id"] == caller.credential_id
    assert event["details"] == {"operation": NOTES}


def test_path_parameters_are_part_of_the_content(client, engine):
    caller = issue(engine)
    body = {"marker": "m"}
    first = send(client, caller, url="/test/folders/f1/notes", json=body)
    assert first.status_code == 201
    conflict = send(client, caller, url="/test/folders/f2/notes", json=body)
    assert error_of(conflict, 409)["code"] == "IDEMPOTENCY_CONFLICT"
    assert notes(engine, caller, "m") == 1


# --- 命令登记的安全事件 ---


def test_a_command_event_is_written_on_success_and_not_on_replay(client, engine):
    caller = issue(engine)
    body = {"marker": "m", "event": True}
    first = send(client, caller, json=body)
    assert first.status_code == 201
    [event] = events(engine, rid_of(first))
    assert event["event_type"] == COMMAND_EVENT
    assert event["tenant_id"] == caller.tenant_id
    assert event["credential_id"] == caller.credential_id
    assert event["details"] == {"ref_type": "note", "ref": "m"}

    replay = send(client, caller, json=body)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert events(engine, rid_of(replay)) == []


def test_a_command_event_is_written_when_the_command_is_rejected(client, engine):
    caller = issue(engine)
    HOOKS["m"] = reject
    response = send(client, caller, json={"marker": "m", "event": True})
    assert error_of(response, 409)["code"] == REJECTED
    # 命令的写入撤销了，事件在业务事务之外写，照样留下。
    assert notes(engine, caller, "m") == 0
    [event] = events(engine, rid_of(response))
    assert event["event_type"] == COMMAND_EVENT


# --- 取锁 ---


def test_a_held_lock_is_request_in_progress_and_nothing_is_written(client, engine):
    caller = issue(engine)
    name = note_lock(caller)
    with engine.connect() as holder:
        assert get_lock(holder, name) == 1
        try:
            busy = send(client, caller, json={"marker": "m", "event": True})
            error = error_of(busy, 409)
            assert error["code"] == "REQUEST_IN_PROGRESS"
            assert error["retryable"] is True
            assert busy.headers["Retry-After"] == "1"
            assert records(engine, caller) == []
            assert notes(engine, caller, "m") == 0
            assert events(engine, rid_of(busy)) == []
        finally:
            release_lock(holder, name)

    retry = send(client, caller, json={"marker": "m", "event": True})
    assert retry.status_code == 201
    assert "Idempotency-Replayed" not in retry.headers
    assert notes(engine, caller, "m") == 1
    assert len(records(engine, caller)) == 1


def test_the_lock_is_released_after_every_request(client, engine):
    caller = issue(engine)
    HOOKS["failing"] = fail
    HOOKS["rejected"] = reject
    cases = [
        ("first", KEY, {"marker": "m"}, 201),
        ("replay", KEY, {"marker": "m"}, 201),
        ("conflict", KEY, {"marker": "other"}, 409),
        ("rejected", "shop:note:2", {"marker": "rejected"}, 409),
        ("exception", "shop:note:3", {"marker": "failing"}, 500),
    ]
    for case, key, body, status in cases:
        response = send(client, caller, key, json=body)
        assert response.status_code == status, case
        assert_lock_free(engine, note_lock(caller, key))


# --- 业务拒绝 ---


def test_a_rejection_is_saved_and_replayed_and_a_new_key_runs_again(client, engine):
    caller = issue(engine)
    HOOKS["m"] = reject
    rejected = send(client, caller, json={"marker": "m"})
    assert error_of(rejected, 409)["code"] == REJECTED
    assert notes(engine, caller, "m") == 0
    [record] = records(engine, caller)
    assert record["response_status"] == 409
    assert record["response_body"] == rejected.json()

    # 拒绝的原因消失了（像补了货），同键也只会重放原来的 409。
    del HOOKS["m"]
    replay = send(client, caller, json={"marker": "m"})
    assert replay.status_code == 409
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == rejected.json()
    assert notes(engine, caller, "m") == 0

    fresh = send(client, caller, "shop:note:2", json={"marker": "m"})
    assert fresh.status_code == 201
    assert notes(engine, caller, "m") == 1


def test_writes_declared_to_survive_a_rejection_are_kept_with_the_record(client, engine):
    caller = issue(engine)

    def reject_keeping(execution: Execution) -> None:
        values = {
            "location_id": new_id("loc"),
            "tenant_id": caller.tenant_id,
            "name": "kept",
            "status": "active",
        }
        execution.keep_on_rejection(lambda conn: conn.execute(insert(Location).values(values)))
        reject(execution)

    HOOKS["m"] = reject_keeping
    rejected = send(client, caller, json={"marker": "m"})
    assert error_of(rejected, 409)["code"] == REJECTED
    assert notes(engine, caller, "m") == 0
    assert notes(engine, caller, "kept") == 1
    [record] = records(engine, caller)
    assert record["response_status"] == 409

    replay = send(client, caller, json={"marker": "m"})
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert notes(engine, caller, "kept") == 1


# --- 未处理的异常 ---


def test_an_exception_is_500_saves_nothing_and_the_same_key_runs_again(client, engine):
    caller = issue(engine)
    HOOKS["m"] = fail
    body = {"marker": "m", "event": True}
    failed = send(client, caller, json=body)
    error = error_of(failed, 500)
    assert error["code"] == "INTERNAL_ERROR"
    assert error["retryable"] is True
    assert "boom-marker" not in failed.text
    assert notes(engine, caller, "m") == 0
    assert records(engine, caller) == []
    # 异常之前登记的事件在回滚、释放锁之后照样写。
    [event] = events(engine, rid_of(failed))
    assert event["event_type"] == COMMAND_EVENT

    # 同键重试会重新执行：再登记的事件再写一次。
    again = send(client, caller, json=body)
    assert error_of(again, 500)["code"] == "INTERNAL_ERROR"
    assert len(events(engine, rid_of(again))) == 1

    del HOOKS["m"]
    done = send(client, caller, json=body)
    assert done.status_code == 201
    assert "Idempotency-Replayed" not in done.headers
    assert notes(engine, caller, "m") == 1
    assert len(records(engine, caller)) == 1


# --- 取锁之前的拒绝 ---


def test_requests_rejected_before_the_lock_save_nothing(client, engine):
    caller = issue(engine)
    unauthorized = send(client, caller, token=generate_token(), json={"marker": "m"})
    assert error_of(unauthorized, 401)["code"] == "UNAUTHORIZED"
    invalid = send(client, caller, json={"marker": 5})
    assert error_of(invalid, 422)["code"] == "INVALID_REQUEST"
    assert records(engine, caller) == []
    assert notes(engine, caller, "m") == 0

    corrected = send(client, caller, json={"marker": "m"})
    assert corrected.status_code == 201
    assert "Idempotency-Replayed" not in corrected.headers
    assert notes(engine, caller, "m") == 1


# --- 写记录撞上唯一约束 ---


@pytest.mark.parametrize("same_content", [True, False])
def test_a_record_committed_past_the_lock_turns_into_a_replay_or_conflict(
    client, engine, same_content
):
    caller = issue(engine)
    body = {"marker": "m", "event": True}
    stored_body = {"data": {"from": "elsewhere"}, "request_id": "req-elsewhere"}
    request_hash = request_digest({}, body) if same_content else "0" * 64

    def commit_first(execution: Execution) -> None:
        # 执行器已经查过记录、还没写：另一个连接绕过锁先提交一条同键记录。
        with engine.begin() as other:
            other.execute(
                idempotency_insert(
                    caller.tenant_id,
                    caller.credential_id,
                    operation=NOTES,
                    idempotency_key=KEY,
                    request_hash=request_hash,
                    response_status=201,
                    response_body=stored_body,
                    request_id="req-elsewhere",
                )
            )

    HOOKS["m"] = commit_first
    response = send(client, caller, json=body)
    recorded = events(engine, rid_of(response))
    if same_content:
        assert response.status_code == 201
        assert response.headers["Idempotency-Replayed"] == "true"
        assert response.json() == stored_body
        # 被回滚的命令登记的事件丢弃。
        assert recorded == []
    else:
        assert error_of(response, 409)["code"] == "IDEMPOTENCY_CONFLICT"
        assert [event["event_type"] for event in recorded] == ["idempotency_conflict"]
    assert notes(engine, caller, "m") == 0
    assert [record["request_id"] for record in records(engine, caller)] == ["req-elsewhere"]
    assert_lock_free(engine, note_lock(caller))


# --- 目录写锁 ---


@pytest.fixture
def short_catalog_wait(monkeypatch) -> int:
    monkeypatch.setattr(idempotency, "CATALOG_LOCK_WAIT_SECONDS", 1)
    return 1


def send_catalog(client: TestClient, caller: Caller, key: str = KEY, **kwargs: object):
    return send(client, caller, key, url=CATALOG_NOTES_URL, **kwargs)


def test_a_held_catalog_lock_is_unavailable_after_waiting(client, engine, short_catalog_wait):
    caller = issue(engine)
    with engine.connect() as holder:
        assert get_lock(holder, catalog_lock(caller)) == 1
        try:
            started = time.monotonic()
            busy = send_catalog(client, caller, json={"marker": "m", "event": True})
            elapsed = time.monotonic() - started
            error = error_of(busy, 503)
            assert error["code"] == "UNAVAILABLE"
            assert error["retryable"] is True
            assert elapsed >= short_catalog_wait * 0.9
            assert records(engine, caller, operation=CATALOG_NOTES) == []
            assert notes(engine, caller, "m") == 0
            assert events(engine, rid_of(busy)) == []
        finally:
            release_lock(holder, catalog_lock(caller))

    retry = send_catalog(client, caller, json={"marker": "m", "event": True})
    assert retry.status_code == 201
    assert "Idempotency-Replayed" not in retry.headers
    assert notes(engine, caller, "m") == 1
    assert len(records(engine, caller, operation=CATALOG_NOTES)) == 1


def test_another_tenants_catalog_lock_does_not_matter(client, engine, short_catalog_wait):
    caller = issue(engine)
    other = issue(engine)
    with engine.connect() as holder:
        assert get_lock(holder, catalog_lock(other)) == 1
        try:
            response = send_catalog(client, caller, json={"marker": "m"})
        finally:
            release_lock(holder, catalog_lock(other))
    assert response.status_code == 201
    assert notes(engine, caller, "m") == 1


def test_commands_without_the_catalog_lock_ignore_it(client, engine, short_catalog_wait):
    caller = issue(engine)
    with engine.connect() as holder:
        assert get_lock(holder, catalog_lock(caller)) == 1
        try:
            # 声明了目录锁的路由此时会 503。
            response = send(client, caller, json={"marker": "m"})
        finally:
            release_lock(holder, catalog_lock(caller))
    assert response.status_code == 201
    assert notes(engine, caller, "m") == 1


def test_a_held_idempotency_lock_is_409_without_waiting_for_the_catalog_lock(client, engine):
    caller = issue(engine)
    name = note_lock(caller, operation=CATALOG_NOTES)
    with engine.connect() as holder:
        assert get_lock(holder, name) == 1
        assert get_lock(holder, catalog_lock(caller)) == 1
        try:
            started = time.monotonic()
            busy = send_catalog(client, caller, json={"marker": "m"})
            elapsed = time.monotonic() - started
        finally:
            release_lock(holder, catalog_lock(caller))
            release_lock(holder, name)
    assert error_of(busy, 409)["code"] == "REQUEST_IN_PROGRESS"
    # 等待秒数是默认的 5：没去等目录锁。
    assert elapsed < idempotency.CATALOG_LOCK_WAIT_SECONDS / 2
    assert records(engine, caller, operation=CATALOG_NOTES) == []
    assert notes(engine, caller, "m") == 0


def test_the_catalog_lock_is_released_after_every_request(client, engine, short_catalog_wait):
    caller = issue(engine)
    HOOKS["failing"] = fail
    HOOKS["rejected"] = reject
    cases = [
        ("success", "shop:note:1", {"marker": "m"}, 201),
        ("rejected", "shop:note:2", {"marker": "rejected"}, 409),
        ("exception", "shop:note:3", {"marker": "failing"}, 500),
    ]
    for case, key, body, status in cases:
        response = send_catalog(client, caller, key, json=body)
        assert response.status_code == status, case
        assert_lock_free(engine, catalog_lock(caller))
        assert_lock_free(engine, note_lock(caller, key, CATALOG_NOTES))

    key = "shop:note:4"
    with engine.connect() as holder:
        assert get_lock(holder, catalog_lock(caller)) == 1
        try:
            busy = send_catalog(client, caller, key, json={"marker": "m"})
            assert error_of(busy, 503)["code"] == "UNAVAILABLE"
            # 请求的连接已释放它取到的幂等锁。
            assert_lock_free(engine, note_lock(caller, key, CATALOG_NOTES))
        finally:
            release_lock(holder, catalog_lock(caller))
    assert_lock_free(engine, catalog_lock(caller))


def test_the_read_snapshot_is_taken_after_the_catalog_lock(client, engine):
    caller = issue(engine)
    counter_id = new_id("loc")
    with engine.begin() as db:
        values = {"location_id": counter_id, "tenant_id": caller.tenant_id, "name": "0"}
        db.execute(insert(Location).values(values | {"status": "active"}))

    first_read = threading.Event()
    proceed = threading.Event()

    def pause_first(execution: Execution) -> None:
        # 第一个请求读完计数后停在这里；第二个请求在目录锁上等待。
        if not first_read.is_set():
            first_read.set()
            assert proceed.wait(10)

    HOOKS[counter_id] = pause_first
    body = {"marker": counter_id}
    other_client = TestClient(client.app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first = pool.submit(
                send, client, caller, "shop:count:1", url=CATALOG_COUNT_URL, json=body
            )
            assert first_read.wait(10)
            second = pool.submit(
                send, other_client, caller, "shop:count:2", url=CATALOG_COUNT_URL, json=body
            )
            # 给第二个请求时间走到取目录锁那一步（远小于等待秒数 5）。
            time.sleep(0.5)
        finally:
            proceed.set()
        responses = [first.result(), second.result()]

    assert [response.status_code for response in responses] == [200, 200]
    assert sorted(response.json()["data"]["count"] for response in responses) == [1, 2]
    with engine.connect() as db:
        statement = select(Location.name).where(Location.location_id == counter_id)
        assert db.execute(statement).scalar_one() == "2"


# --- 余额行预建 ---

# 已有余额行的量与版本：预建不能改动它们。
EXISTING_BALANCE = {
    "stock_balance": 7,
    "reserved": 2,
    "unavailable": 1,
    "paid_pending_dispatch": 4,
    "balance_version": 3,
    "last_book_only_version": 2,
    "last_reconciled_version": 1,
}
ZERO_BALANCE = {column: 0 for column in EXISTING_BALANCE}


@pytest.fixture
def balance_inserts(client) -> Iterator[list[tuple[str, str]]]:
    """应用的连接上执行的余额行插入，按执行顺序记下 (pool_id, sku_id)。"""
    inserted: list[tuple[str, str]] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.startswith("INSERT INTO stock_balances"):
            inserted.append((parameters["pool_id"], parameters["sku_id"]))

    app_engine = client.app.state.engine
    event.listen(app_engine, "before_cursor_execute", record)
    try:
        yield inserted
    finally:
        event.remove(app_engine, "before_cursor_execute", record)


def balances(engine: Engine, *where) -> list[dict]:
    statement = select(*StockBalance.__table__.columns).where(*where)
    statement = statement.order_by(StockBalance.pool_id, StockBalance.sku_id)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def balance_of(engine: Engine, caller: Caller, pool_id: str, sku_id: str) -> dict | None:
    where = (
        StockBalance.tenant_id == caller.tenant_id,
        StockBalance.pool_id == pool_id,
        StockBalance.sku_id == sku_id,
    )
    rows = balances(engine, *where)
    return rows[0] if rows else None


def add_skus(engine: Engine, tenant_id: str, count: int) -> list[str]:
    with engine.begin() as db:
        return [add_sku(db, tenant_id) for _ in range(count)]


def send_balances(client: TestClient, caller: Caller, rows, key: str = KEY, **body: object):
    payload = {"marker": "m", "rows": [list(row) for row in rows]} | body
    return send(client, caller, key, url=BALANCE_NOTES_URL, json=payload)


def test_balance_rows_are_committed_before_the_command_runs(client, engine):
    caller = issue(engine)
    new_sku, existing_sku = add_skus(engine, caller.tenant_id, 2)
    with engine.begin() as db:
        foreign_sku = add_sku(db, add_tenant(db))
        values = EXISTING_BALANCE | {"opening_entry_id": new_id("led")}
        db.execute(balance_insert(caller.tenant_id, caller.pool_id, existing_sku, **values))
    existing_before = balance_of(engine, caller, caller.pool_id, existing_sku)
    missing_pool = new_id("pool")

    seen: dict[str, list[dict]] = {}

    def look(execution: Execution) -> None:
        # 命令的业务事务还开着：另一个连接已经看得到预建的行。
        seen["rows"] = balances(engine, StockBalance.tenant_id == caller.tenant_id)

    HOOKS["m"] = look
    rows = [
        (caller.pool_id, new_sku),
        (caller.pool_id, foreign_sku),  # SKU 属于别的租户
        (missing_pool, new_sku),  # 池不存在
        (caller.pool_id, existing_sku),
    ]
    response = send_balances(client, caller, rows)
    assert response.status_code == 201
    assert notes(engine, caller, "m") == 1

    assert sorted(row["sku_id"] for row in seen["rows"]) == sorted([new_sku, existing_sku])
    created = balance_of(engine, caller, caller.pool_id, new_sku)
    assert created is not None
    assert created["balance_id"].startswith("bal_")
    assert {column: created[column] for column in ZERO_BALANCE} == ZERO_BALANCE
    assert created["opening_entry_id"] is None
    # 已有的行一列都没变。
    assert balance_of(engine, caller, caller.pool_id, existing_sku) == existing_before
    # 外键失败的行跳过，不在任何租户下建出来。
    assert balances(engine, StockBalance.sku_id == foreign_sku) == []
    assert balances(engine, StockBalance.pool_id == missing_pool) == []


def test_balance_rows_are_inserted_in_order_and_again_on_replay(client, engine, balance_inserts):
    caller = issue(engine)
    other_pool = add_other_pool(engine, caller.tenant_id)
    skus = add_skus(engine, caller.tenant_id, 3)
    rows = [
        (other_pool, skus[2]),
        (caller.pool_id, skus[1]),
        (other_pool, skus[0]),
        (caller.pool_id, skus[2]),
        (other_pool, skus[2]),  # 重复的只插一次
        (caller.pool_id, skus[0]),
    ]
    expected = sorted(set(rows))
    assert len(expected) == 5

    first = send_balances(client, caller, rows)
    assert first.status_code == 201
    assert balance_inserts == expected
    created = balances(engine, StockBalance.tenant_id == caller.tenant_id)
    assert [(row["pool_id"], row["sku_id"]) for row in created] == expected

    # 同键重放：命令不再执行，预建照样按顺序做一遍，已建的行不变。
    balance_inserts.clear()
    replay = send_balances(client, caller, rows)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert notes(engine, caller, "m") == 1
    assert balance_inserts == expected
    assert balances(engine, StockBalance.tenant_id == caller.tenant_id) == created


def test_a_database_error_while_pre_creating_is_500_and_saves_nothing(client, engine):
    caller = issue(engine)
    [sku] = add_skus(engine, caller.tenant_id, 1)
    # 比 VARCHAR(32) 长：严格模式下是数据错误（1406），不是外键失败。排在合格的那一行之后。
    too_long = "z" * 40
    rows = [(caller.pool_id, too_long), (caller.pool_id, sku)]
    failed = send_balances(client, caller, rows, event=True)
    error = error_of(failed, 500)
    assert error["code"] == "INTERNAL_ERROR"
    assert error["retryable"] is True
    assert records(engine, caller, operation=BALANCE_NOTES) == []
    assert notes(engine, caller, "m") == 0
    assert events(engine, rid_of(failed)) == []
    # 先插的那一行也随短事务回滚。
    assert balances(engine, StockBalance.tenant_id == caller.tenant_id) == []
    assert_lock_free(engine, note_lock(caller, operation=BALANCE_NOTES))

    # 没保存幂等记录：同键重试会重新执行。
    retry = send_balances(client, caller, [(caller.pool_id, sku)], event=True)
    assert retry.status_code == 201
    assert "Idempotency-Replayed" not in retry.headers
    assert notes(engine, caller, "m") == 1
    assert balance_of(engine, caller, caller.pool_id, sku) is not None


# --- 本请求的摘要 ---


def test_the_command_sees_the_request_digest(client, engine):
    caller = issue(engine)
    seen: list[str] = []
    HOOKS["m"] = lambda execution: seen.append(execution.request_digest)
    body = {"marker": "m", "event": False}
    response = send(client, caller, url="/test/folders/f1/notes", json=body)
    assert response.status_code == 201

    # 契约 §7.1：{"path": 路径参数, "body": 请求体} 排序键、不留空白地序列化后取 SHA-256。
    canonical = {"path": {"folder_id": "f1"}, "body": body}
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert seen == [hashlib.sha256(serialized.encode("utf-8")).hexdigest()]
    [record] = records(engine, caller, operation=FOLDER_NOTES)
    assert record["request_hash"] == seen[0]


# --- READ COMMITTED ---


@pytest.mark.parametrize("read_committed", [True, False])
def test_read_committed_applies_to_the_business_transaction_only(client, engine, read_committed):
    caller = issue(engine)
    seen: dict[str, object] = {}
    outside = (Location.tenant_id == caller.tenant_id, Location.name == "outside")

    def look(execution: Execution) -> None:
        db = execution.connection
        seen["isolation"] = db.execute(text("SELECT @@transaction_isolation")).scalar_one()
        seen["connection_id"] = db.execute(text("SELECT CONNECTION_ID()")).scalar_one()
        # 命令运行中另一个连接提交一行，再用普通读去读。
        with engine.begin() as other:
            values = {"location_id": new_id("loc"), "tenant_id": caller.tenant_id}
            other.execute(insert(Location).values(name="outside", status="active", **values))
        statement = select(func.count()).select_from(Location).where(*outside)
        seen["outside"] = db.execute(statement).scalar_one()

    HOOKS["m"] = look
    url = READ_COMMITTED_NOTES_URL if read_committed else "/test/notes"
    response = send(client, caller, url=url, json={"marker": "m"})
    assert response.status_code == 201

    if read_committed:
        assert seen["isolation"] == "READ-COMMITTED"
        assert seen["outside"] == 1
    else:
        # 默认的 REPEATABLE READ：读快照在查幂等记录时已经建立，看不到之后提交的行。
        assert seen["isolation"] == "REPEATABLE-READ"
        assert seen["outside"] == 0

    # 请求结束后同一连接（这个应用的连接池里只有它）再开的事务回到默认隔离级别。
    with client.app.state.engine.connect() as db:
        assert db.execute(text("SELECT CONNECTION_ID()")).scalar_one() == seen["connection_id"]
        isolation = db.execute(text("SELECT @@transaction_isolation")).scalar_one()
        assert isolation == "REPEATABLE-READ"
        statement = select(func.count()).select_from(Location).where(*outside)
        assert db.execute(statement).scalar_one() == 1


# --- 并发撞上业务引用 ---


@pytest.mark.parametrize("same_content", [True, False])
def test_a_business_ref_taken_concurrently_is_rejudged_and_saved(client, engine, same_content):
    caller = issue(engine)
    ref = new_id("loc")
    first_body = {"marker": ref, "event": True}
    second_body = first_body if same_content else {"marker": ref}

    inserted = threading.Event()
    proceed = threading.Event()

    def pause_first(execution: Execution) -> None:
        # 先到者插完引用、提交之前停在这里；后到者写完自己那一行后在引用的主键上等它。
        if not inserted.is_set():
            inserted.set()
            assert proceed.wait(10)

    HOOKS[ref] = pause_first
    other_client = TestClient(client.app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first_future = pool.submit(
                send, client, caller, "shop:ref:1", url=REFS_URL, json=first_body
            )
            assert inserted.wait(10)
            second_future = pool.submit(
                send, other_client, caller, "shop:ref:2", url=REFS_URL, json=second_body
            )
            time.sleep(0.5)
        finally:
            proceed.set()
        first, second = first_future.result(), second_future.result()

    assert first.status_code == 201
    if same_content:
        # 重新判定读到先到者已提交的引用，返回它的结果。
        assert second.status_code == 200
        assert second.json()["data"] == first.json()["data"]
    else:
        assert error_of(second, 409)["code"] == REF_CONFLICT
    # 后到者先写的那一行撤销了。
    assert notes(engine, caller, ref) == 1

    # 重新判定的结果是后到者那个幂等键的确定结果。
    [record] = records(engine, caller, "shop:ref:2", operation=REFS)
    assert record["response_status"] == second.status_code
    assert record["response_body"] == second.json()

    # 后到者命令登记的事件丢弃，重新判定登记的照常写。
    [first_event] = events(engine, rid_of(first))
    assert first_event["details"] == {"ref_type": "note", "ref": ref}
    [second_event] = events(engine, rid_of(second))
    assert second_event["event_type"] == COMMAND_EVENT
    assert second_event["details"] == {"ref_type": "rejudge", "ref": ref}

    replay = send(client, caller, "shop:ref:2", url=REFS_URL, json=second_body)
    assert replay.status_code == second.status_code
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == second.json()
    assert events(engine, rid_of(replay)) == []
    assert notes(engine, caller, ref) == 1


# --- 回滚后整个重做 ---


def redo_first(times: int, seen: list[int]) -> Callable[[Execution], None]:
    """每次运行写一个名为 attempt-<第几次> 的地点、登记一条记下第几次的事件。

    前 times 次运行在写完、登记完之后抛 RedoCommand。
    """

    def hook(execution: Execution) -> None:
        attempt = len(seen) + 1
        seen.append(attempt)
        values = {
            "location_id": new_id("loc"),
            "tenant_id": execution.context.tenant_id,
            "name": f"attempt-{attempt}",
            "status": "active",
        }
        execution.connection.execute(insert(Location).values(values))
        execution.record_security_event(COMMAND_EVENT, {"ref_type": "attempt", "ref": str(attempt)})
        if attempt <= times:
            raise RedoCommand()

    return hook


def test_a_redo_runs_the_command_again_and_saves_the_second_result(client, engine):
    caller = issue(engine)
    seen: list[int] = []
    HOOKS["m"] = redo_first(1, seen)
    body = {"marker": "m", "event": True}
    response = send(client, caller, json=body)
    assert response.status_code == 201
    assert "Idempotency-Replayed" not in response.headers
    assert seen == [1, 2]

    # 第一次的写入回滚了，只留第二次的；响应是第二次的。
    assert notes(engine, caller, "attempt-1") == 0
    assert notes(engine, caller, "attempt-2") == 1
    assert notes(engine, caller, "m") == 1
    where = (Location.tenant_id == caller.tenant_id, Location.name == "m")
    with engine.connect() as db:
        location_id = db.execute(select(Location.location_id).where(*where)).scalar_one()
    assert response.json()["data"]["location_id"] == location_id
    [record] = records(engine, caller)
    assert record["response_status"] == 201
    assert record["response_body"] == response.json()

    # 第一次登记的事件丢弃，第二次的照常写。
    recorded = events(engine, rid_of(response))
    details = sorted((event["details"]["ref_type"], event["details"]["ref"]) for event in recorded)
    assert details == [("attempt", "2"), ("note", "m")]

    replay = send(client, caller, json=body)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()
    assert seen == [1, 2]


def test_a_redo_pre_creates_again_and_calls_the_hook_again_under_the_lock(client, engine):
    caller = issue(engine)
    first_sku, second_sku = add_skus(engine, caller.tenant_id, 2)
    calls: list[str] = []
    seen: dict[str, object] = {}
    redoing = threading.Event()
    proceed = threading.Event()

    def balance_rows(connection: Connection) -> list[tuple[str, str]]:
        calls.append("balance_rows")
        if calls.count("balance_rows") == 1:
            return [(caller.pool_id, first_sku)]
        # 重做：停在这里，让同键的请求来撞幂等锁；这次多返回一行。
        redoing.set()
        assert proceed.wait(10)
        return [(caller.pool_id, first_sku), (caller.pool_id, second_sku)]

    def before_transaction(connection: Connection) -> None:
        calls.append("before_transaction")

    def command(execution: Execution) -> None:
        calls.append("command")
        if calls.count("command") == 1:
            raise RedoCommand()
        # 重做的命令运行时，另一个连接已经看得到第二次预建的行。
        seen["second"] = balance_of(engine, caller, caller.pool_id, second_sku)

    BALANCE_ROWS["m"] = balance_rows
    BEFORE_TRANSACTION["m"] = before_transaction
    HOOKS["m"] = command
    body = {"marker": "m", "rows": []}
    other_client = TestClient(client.app)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            future = pool.submit(send, client, caller, url=REDO_NOTES_URL, json=body)
            assert redoing.wait(10)
            busy = send(other_client, caller, url=REDO_NOTES_URL, json=body)
        finally:
            proceed.set()
        response = future.result()

    error = error_of(busy, 409)
    assert error["code"] == "REQUEST_IN_PROGRESS"
    assert error["retryable"] is True

    assert response.status_code == 201
    assert calls == ["balance_rows", "before_transaction", "command"] * 2
    assert seen["second"] is not None
    assert balance_of(engine, caller, caller.pool_id, first_sku) is not None
    assert notes(engine, caller, "m") == 1
    [record] = records(engine, caller, operation=REDO_NOTES)
    assert record["response_body"] == response.json()
    assert_lock_free(engine, note_lock(caller, operation=REDO_NOTES))


def test_a_write_kept_on_rejection_can_ask_for_a_redo(client, engine):
    caller = issue(engine)
    taken_id = new_id("loc")
    with engine.begin() as db:
        values = {"location_id": taken_id, "tenant_id": caller.tenant_id, "name": "taken"}
        db.execute(insert(Location).values(status="active", **values))
    # 第一次的写入撞上已有的主键，重做时换一个。
    kept_ids = [taken_id, new_id("loc")]
    attempts: list[int] = []

    def reject_keeping(execution: Execution) -> None:
        values = {
            "location_id": kept_ids[len(attempts)],
            "tenant_id": caller.tenant_id,
            "name": "kept",
            "status": "active",
        }
        attempts.append(len(attempts) + 1)

        def keep(connection: Connection) -> None:
            try:
                connection.execute(insert(Location).values(values))
            except IntegrityError as exc:
                if exc.orig.args[0] != 1062:
                    raise
                raise RedoCommand() from None

        execution.keep_on_rejection(keep)
        reject(execution)

    HOOKS["m"] = reject_keeping
    body = {"marker": "m", "event": True}
    rejected = send(client, caller, json=body)
    assert error_of(rejected, 409)["code"] == REJECTED
    assert attempts == [1, 2]

    # 第二次的拒绝与它保留的写入一起保存；已有的那一行没变。
    assert notes(engine, caller, "m") == 0
    assert notes(engine, caller, "kept") == 1
    assert notes(engine, caller, "taken") == 1
    with engine.connect() as db:
        statement = select(Location.name).where(Location.location_id == kept_ids[1])
        assert db.execute(statement).scalar_one() == "kept"
    [record] = records(engine, caller)
    assert record["response_status"] == 409
    assert record["response_body"] == rejected.json()
    # 第一次登记的事件丢弃，第二次的照常写。
    [event] = events(engine, rid_of(rejected))
    assert event["event_type"] == COMMAND_EVENT

    replay = send(client, caller, json=body)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == rejected.json()
    assert attempts == [1, 2]
    assert notes(engine, caller, "kept") == 1


def test_a_second_redo_is_unavailable_and_saves_nothing(client, engine):
    caller = issue(engine)
    seen: list[int] = []
    HOOKS["m"] = redo_first(2, seen)
    body = {"marker": "m", "event": True}
    failed = send(client, caller, json=body)
    error = error_of(failed, 503)
    assert error["code"] == "UNAVAILABLE"
    assert error["retryable"] is True
    assert seen == [1, 2]

    assert records(engine, caller) == []
    for marker in ("m", "attempt-1", "attempt-2"):
        assert notes(engine, caller, marker) == 0
    # 两次登记的事件都丢弃。
    assert events(engine, rid_of(failed)) == []
    assert_lock_free(engine, note_lock(caller))

    # 没保存幂等记录：同键重试会重新执行。
    del HOOKS["m"]
    retry = send(client, caller, json=body)
    assert retry.status_code == 201
    assert "Idempotency-Replayed" not in retry.headers
    assert notes(engine, caller, "m") == 1
    assert len(records(engine, caller)) == 1


def test_a_command_given_three_attempts_saves_the_third_result(client, engine):
    caller = issue(engine)
    seen: list[int] = []
    HOOKS["m"] = redo_first(2, seen)
    body = {"marker": "m", "event": True}
    response = send(client, caller, url=THREE_ATTEMPTS_NOTES_URL, json=body)
    assert response.status_code == 201
    assert seen == [1, 2, 3]

    # 前两遍的写入都回滚了，只留第三遍的；响应是第三遍的。
    assert notes(engine, caller, "attempt-1") == 0
    assert notes(engine, caller, "attempt-2") == 0
    assert notes(engine, caller, "attempt-3") == 1
    assert notes(engine, caller, "m") == 1
    [record] = records(engine, caller, operation=THREE_ATTEMPTS_NOTES)
    assert record["response_status"] == 201
    assert record["response_body"] == response.json()

    # 前两遍登记的事件丢弃，第三遍的照常写。
    recorded = events(engine, rid_of(response))
    details = sorted((event["details"]["ref_type"], event["details"]["ref"]) for event in recorded)
    assert details == [("attempt", "3"), ("note", "m")]

    replay = send(client, caller, url=THREE_ATTEMPTS_NOTES_URL, json=body)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()
    assert seen == [1, 2, 3]


def test_a_redo_on_the_last_of_three_attempts_is_unavailable_and_saves_nothing(client, engine):
    caller = issue(engine)
    seen: list[int] = []
    HOOKS["m"] = redo_first(3, seen)
    body = {"marker": "m", "event": True}
    failed = send(client, caller, url=THREE_ATTEMPTS_NOTES_URL, json=body)
    error = error_of(failed, 503)
    assert error["code"] == "UNAVAILABLE"
    assert error["retryable"] is True
    assert seen == [1, 2, 3]

    assert records(engine, caller, operation=THREE_ATTEMPTS_NOTES) == []
    for marker in ("m", "attempt-1", "attempt-2", "attempt-3"):
        assert notes(engine, caller, marker) == 0
    # 三遍登记的事件都丢弃。
    assert events(engine, rid_of(failed)) == []
    assert_lock_free(engine, note_lock(caller, operation=THREE_ATTEMPTS_NOTES))


def test_the_default_still_allows_one_redo_only(client, engine):
    """不传 attempts 的命令照旧最多做 2 遍：第 2 遍再要重做就 503。"""
    caller = issue(engine)
    seen: list[int] = []
    HOOKS["m"] = redo_first(2, seen)
    failed = send(client, caller, url=READ_COMMITTED_NOTES_URL, json={"marker": "m"})
    assert error_of(failed, 503)["code"] == "UNAVAILABLE"
    assert seen == [1, 2]
    assert records(engine, caller, operation=READ_COMMITTED_NOTES) == []


def test_a_redo_stays_read_committed_and_the_isolation_is_reset_afterwards(client, engine):
    caller = issue(engine)
    seen: list[tuple[str, int]] = []

    def look(execution: Execution) -> None:
        db = execution.connection
        isolation = db.execute(text("SELECT @@transaction_isolation")).scalar_one()
        seen.append((isolation, db.execute(text("SELECT CONNECTION_ID()")).scalar_one()))
        if len(seen) == 1:
            raise RedoCommand()

    HOOKS["m"] = look
    response = send(client, caller, url=REDO_NOTES_URL, json={"marker": "m", "rows": []})
    assert response.status_code == 201
    [(first_isolation, connection_id), (second_isolation, second_id)] = seen
    assert first_isolation == second_isolation == "READ-COMMITTED"
    # 重做在同一连接上。
    assert second_id == connection_id

    # 请求结束后同一连接再开的事务回到默认隔离级别。
    with client.app.state.engine.connect() as db:
        assert db.execute(text("SELECT CONNECTION_ID()")).scalar_one() == connection_id
        isolation = db.execute(text("SELECT @@transaction_isolation")).scalar_one()
        assert isolation == "REPEATABLE-READ"


# --- 命令上下文 ---


def test_the_command_context_does_not_import_fastapi_or_starlette():
    tree = ast.parse(Path(command_context.__file__).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    assert modules
    assert {module.split(".")[0] for module in modules}.isdisjoint({"fastapi", "starlette"})


def context_for(caller: Caller, **values: object) -> CommandContext:
    fields = {
        "tenant_id": caller.tenant_id,
        "channel_id": None,
        "pool_id": caller.pool_id,
        "credential_id": caller.credential_id,
        "actor": None,
        "request_id": f"ctx-{uuid4().hex}",
        "idempotency_key": KEY,
        "reservation_ttl_seconds": 900,
    }
    return CommandContext(**(fields | values))


def test_the_command_context_is_immutable(engine):
    context = context_for(issue(engine))
    with pytest.raises(FrozenInstanceError):
        context.tenant_id = "other"


def test_the_http_wrapper_builds_the_context_from_the_request(client, engine):
    caller = issue(engine)
    seen: dict[str, object] = {}

    def look(execution: Execution) -> None:
        request = execution.request
        seen["context"] = execution.context
        seen["access"] = execution.access
        seen["state_access"] = request.state.access
        seen["request_id"] = request.state.request_id
        seen["ttl"] = request.app.state.reservation_ttl_seconds

    HOOKS["m"] = look
    headers = {
        "X-Actor-Ref": "clerk-1",
        "X-Actor-Role": STOCK_OPERATOR,
        "User-Agent": "context-agent/1",
    }
    url = "/test/folders/f1/notes?view=full"
    response = send(client, caller, url=url, json={"marker": "m"}, headers=headers)
    assert response.status_code == 201

    context = seen["context"]
    access = seen["access"]
    # 兼容属性 access 照旧是第 4–6 步放进 request.state 的那一个。
    assert access is seen["state_access"]
    principal = access.principal
    assert context.tenant_id == principal.tenant_id == caller.tenant_id
    assert context.credential_id == principal.credential_id == caller.credential_id
    assert context.channel_id == principal.channel_id
    assert context.channel_id is not None
    assert context.pool_id == principal.pool_id == caller.pool_id
    assert context.actor == access.actor == Actor("clerk-1", STOCK_OPERATOR)
    assert context.idempotency_key == access.idempotency_key == KEY
    assert context.request_id == seen["request_id"] == rid_of(response)
    assert context.reservation_ttl_seconds == seen["ttl"]
    assert context.reservation_ttl_seconds == client.app.state.reservation_ttl_seconds
    assert context.http_method == "POST"
    assert context.path == "/test/folders/f1/notes"  # 不含查询串
    assert context.user_agent == "context-agent/1"


def test_the_core_takes_identity_from_the_context_not_the_token(client, engine):
    caller = issue(engine)
    # 同租户另一个凭据、另一组操作者与 request_id；请求本身用 caller 的令牌、不带操作者头。
    other = add_caller(engine, caller)
    context = context_for(
        caller,
        credential_id=other.credential_id,
        actor=Actor("context-actor", STOCK_ADMIN),
        http_method="POST",
        path="/context/path",
        user_agent="context-agent/2",
    )
    CONTEXTS["m"] = CONTEXTS["m2"] = context
    seen: dict[str, object] = {}

    def look(execution: Execution) -> None:
        used = text("SELECT IS_USED_LOCK(:name)")
        with engine.connect() as db:
            name = note_lock(other, operation=CONTEXT_NOTES)
            seen["context_lock"] = db.execute(used, {"name": name}).scalar()
            name = note_lock(caller, operation=CONTEXT_NOTES)
            seen["token_lock"] = db.execute(used, {"name": name}).scalar()
        seen["context"] = execution.context
        seen["same_request"] = execution.request is ROUTE_REQUESTS["m"]
        seen["access"] = execution.access
        seen["state_access"] = execution.request.state.access

    HOOKS["m"] = look
    body = {"marker": "m", "event": True}
    first = send(client, caller, url=CONTEXT_NOTES_URL, json=body)
    assert first.status_code == 201
    assert notes(engine, caller, "m") == 1

    # 命名锁按上下文的凭据取，令牌的凭据那一把没人持有。
    assert seen["context_lock"] is not None
    assert seen["token_lock"] is None
    assert seen["context"] is context
    # 兼容属性照旧是请求与令牌的鉴权结果。
    assert seen["same_request"] is True
    assert seen["access"] is seen["state_access"]
    assert seen["access"].principal.credential_id == caller.credential_id

    # 幂等记录的作用域、credential_id 与 request_id 取自上下文；响应按请求写。
    assert records(engine, caller, operation=CONTEXT_NOTES) == []
    [record] = records(engine, other, operation=CONTEXT_NOTES)
    assert record["tenant_id"] == caller.tenant_id
    assert record["credential_id"] == other.credential_id
    assert record["request_id"] == context.request_id
    assert record["response_body"] == first.json()
    assert first.json()["request_id"] == rid_of(first)

    # 安全事件的身份与 request_id 取自上下文。
    assert events(engine, rid_of(first)) == []
    [event] = events(engine, context.request_id)
    assert event["event_type"] == COMMAND_EVENT
    assert event["tenant_id"] == caller.tenant_id
    assert event["credential_id"] == other.credential_id
    assert event["actor_ref"] == "context-actor"
    assert event["request_id"] == context.request_id
    assert event["http_method"] == "POST"
    assert event["path"] == "/context/path"
    assert event["user_agent"] == "context-agent/2"
    assert_lock_free(engine, note_lock(other, operation=CONTEXT_NOTES))

    # 同一上下文同键重放：命令不执行，不写事件。
    replay = send(client, caller, url=CONTEXT_NOTES_URL, json=body)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert notes(engine, caller, "m") == 1
    assert len(events(engine, context.request_id)) == 1

    # 同一上下文同键换内容：冲突，冲突事件同样取自上下文。
    conflict = send(client, caller, url=CONTEXT_NOTES_URL, json={"marker": "m2"})
    assert error_of(conflict, 409)["code"] == "IDEMPOTENCY_CONFLICT"
    assert notes(engine, caller, "m2") == 0
    assert events(engine, rid_of(conflict)) == []
    recorded = events(engine, context.request_id)
    [conflict_event] = [e for e in recorded if e["event_type"] == "idempotency_conflict"]
    assert conflict_event["tenant_id"] == caller.tenant_id
    assert conflict_event["credential_id"] == other.credential_id
    assert conflict_event["actor_ref"] == "context-actor"
    assert conflict_event["details"] == {"operation": CONTEXT_NOTES}
    [kept] = records(engine, other, operation=CONTEXT_NOTES)
    assert kept["request_id"] == context.request_id
    assert_lock_free(engine, note_lock(other, operation=CONTEXT_NOTES))
