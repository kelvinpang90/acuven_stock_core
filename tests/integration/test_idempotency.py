"""幂等执行器（契约 §7.1、§7.2、§8.3）。

在真 MySQL 上跑；测试给应用挂测试专用的写路由。命令在事务里给调用方的租户插一个地点，
地点名就是请求体里的 marker，用来观察命令的写入有没有生效、有没有重复。
按 marker 挂的钩子在命令插完地点之后运行：业务拒绝、抛异常、绕过锁先提交一条同键记录。
声明了目录写锁的测试路由有两个：一个同上插地点；一个是计数命令，marker 是计数地点的 location_id，
命令用不加锁的普通 SELECT 读出地点名里的计数、运行钩子、再写回加一。
造数据与观察都用测试自己的连接；应用用自己的连接池。
"""

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, func, insert, make_url, select, text, update

from app.api.auth import authorize
from app.api.errors import ApiError
from app.api.requests import ParsedBody, RequestBody, json_body
from app.config import Settings
from app.domain.catalog import catalog_lock_name
from app.domain.idempotency import lock_name, request_digest
from app.domain.service_auth import STOCK_WRITE, generate_token, token_digest
from app.main import create_app
from app.models.inventory import IdempotencyRecord, Location
from app.services import idempotency
from app.services.idempotency import CommandResult, Execution, run_idempotent
from tests.integration.conftest import TEST_DATABASE_URL, add_credential, new_id
from tests.integration.test_api_auth import Caller, add_token, error_of, events, issue
from tests.integration.test_inventory_schema import idempotency_insert
from tests.integration.test_service_auth_schema import token_insert

NOTES = "test-notes.create"
FOLDER_NOTES = "test-folder-notes.create"
CATALOG_NOTES = "test-catalog-notes.create"
CATALOG_COUNT = "test-catalog-count.increment"
CATALOG_NOTES_URL = "/test/catalog/notes"
CATALOG_COUNT_URL = "/test/catalog/count"
KEY = "shop:note:1"
REJECTED = "NOTE_REJECTED"
COMMAND_EVENT = "business_ref_conflict"

# 按 marker 挂的钩子：命令插完地点、登记完事件之后运行。每个测试结束时清空。
HOOKS: dict[str, Callable[[Execution], None]] = {}


# --- 测试专用路由 ---


class Note(RequestBody):
    marker: str
    event: bool = False  # 为真时命令登记一条安全事件


note_body = json_body(Note)
NoteBody = Annotated[ParsedBody[Note], Depends(note_body)]
WRITE = Depends(authorize(STOCK_WRITE, write=True))


def write_note(execution: Execution, note: Note) -> CommandResult:
    tenant_id = execution.access.principal.tenant_id
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
