"""安全事件满 1 年的定时清理（契约 7.1 保留期、7.2）。

清理函数在真 MySQL 上验证：在传给它的连接上用 SET TIMESTAMP 冻结数据库时间，
测试结束时恢复，并删掉自己造的行。定时机制不碰数据库。
"""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, delete, event, func, insert, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db import Base
from app.domain.periodic import FAILED, SUCCEEDED, is_stale
from app.main import RESERVATION_EXPIRY, SECURITY_EVENT_PURGE, create_app
from app.models.catalog import Tenant
from app.models.service_auth import Credential, SecurityEvent
from app.services import security_events
from app.services.periodic import PeriodicTask, TaskState, stop_all
from app.services.security_events import PURGE_BATCH_SIZE, purge_expired_security_events
from tests.integration.conftest import TEST_DATABASE_URL, add_credential, new_id
from tests.integration.test_inventory_schema import add_tenant

# purpose 第一条的例子：数据库时间为 11 月 13 日 12:00:00。
NOW = datetime(2026, 11, 13, 12, 0, 0)
EXACTLY_ONE_YEAR = datetime(2025, 11, 13, 12, 0, 0)
ONE_SECOND_SHORT = datetime(2025, 11, 13, 12, 0, 1)


# --- 夹具 ---


class Created:
    """测试造的行，结束时删掉（先事件，再凭据，再租户）。"""

    def __init__(self) -> None:
        self.event_ids: list[str] = []
        self.credential_ids: list[str] = []
        self.tenant_ids: list[str] = []


@pytest.fixture
def created(migrated_engine: Engine) -> Iterator[Created]:
    rows = Created()
    try:
        yield rows
    finally:
        with migrated_engine.begin() as conn:
            if rows.event_ids:
                conn.execute(
                    delete(SecurityEvent).where(SecurityEvent.event_id.in_(rows.event_ids))
                )
            if rows.credential_ids:
                conn.execute(
                    delete(Credential).where(Credential.credential_id.in_(rows.credential_ids))
                )
            if rows.tenant_ids:
                conn.execute(delete(Tenant).where(Tenant.tenant_id.in_(rows.tenant_ids)))


@pytest.fixture
def connection(migrated_engine: Engine) -> Iterator[Connection]:
    """传给清理函数的连接；结束时恢复数据库时间，恢复不了就作废，不留给复用连接的测试。

    测试参数里写在 created 之后：先回滚这里没结束的事务，再删造的行，不会等它的行锁。
    """
    with migrated_engine.connect() as conn:
        try:
            yield conn
        finally:
            try:
                conn.rollback()
                conn.execute(text("SET TIMESTAMP = DEFAULT"))
                conn.commit()
            except SQLAlchemyError:
                conn.invalidate()


def freeze(conn: Connection, at: datetime) -> None:
    """冻结这个连接上的数据库时间（会话时区是 UTC），并结束这条语句隐式开的事务。"""
    conn.execute(text("SET TIMESTAMP = UNIX_TIMESTAMP(:at)"), {"at": at})
    assert conn.execute(text("SELECT CURRENT_TIMESTAMP(6)")).scalar_one() == at
    conn.commit()


def add_event(
    conn: Connection, created: Created, occurred_at: datetime | None, **values: object
) -> str:
    event_id = new_id("sev")
    row = {"event_id": event_id, "event_type": "invalid_credential"} | values
    if occurred_at is not None:
        row["occurred_at"] = occurred_at
    conn.execute(insert(SecurityEvent).values(row))
    created.event_ids.append(event_id)
    return event_id


def existing_events(engine: Engine, event_ids: list[str]) -> set[str]:
    with engine.connect() as conn:
        statement = select(SecurityEvent.event_id).where(SecurityEvent.event_id.in_(event_ids))
        return set(conn.execute(statement).scalars())


def other_table_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            table.name: conn.execute(select(func.count()).select_from(table)).scalar_one()
            for table in Base.metadata.sorted_tables
            if table.name != SecurityEvent.__tablename__
        }


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


# --- 清理函数 ---


def test_purge_deletes_events_exactly_one_year_old_and_older(
    migrated_engine: Engine, created: Created, connection: Connection
):
    freeze(connection, NOW)
    with connection.begin():
        tenant_id = add_tenant(connection)
        created.tenant_ids.append(tenant_id)
        credential_id = add_credential(connection, tenant_id)
        created.credential_ids.append(credential_id)
        older = add_event(connection, created, datetime(2024, 3, 1, 8, 30))
        older_of_credential = add_event(
            connection,
            created,
            datetime(2025, 6, 1),
            event_type="forbidden",
            tenant_id=tenant_id,
            credential_id=credential_id,
        )
        exactly = add_event(connection, created, EXACTLY_ONE_YEAR)
        one_second_short = add_event(connection, created, ONE_SECOND_SHORT)
        fresh = add_event(connection, created, None)  # 刚写入：取冻结的数据库时间
    others_before = other_table_counts(migrated_engine)

    assert purge_expired_security_events(connection) == 3

    remaining = existing_events(migrated_engine, created.event_ids)
    assert remaining == {one_second_short, fresh}
    assert not remaining & {older, older_of_credential, exactly}
    with migrated_engine.connect() as conn:
        statement = select(SecurityEvent.occurred_at).where(SecurityEvent.event_id == fresh)
        assert conn.execute(statement).scalar_one() == NOW
        # 被删事件指向的凭据不受影响。
        statement = select(Credential.tenant_id).where(Credential.credential_id == credential_id)
        assert conn.execute(statement).scalar_one() == tenant_id
    assert other_table_counts(migrated_engine) == others_before


def test_purge_deletes_in_batches_until_a_short_batch(
    migrated_engine: Engine, created: Created, connection: Connection
):
    freeze(connection, NOW)
    event_ids = [new_id("sev") for _ in range(2500)]
    created.event_ids.extend(event_ids)
    rows = [
        {"event_id": event_id, "event_type": "forbidden", "occurred_at": datetime(2025, 1, 1)}
        for event_id in event_ids
    ]
    with connection.begin():
        connection.execute(insert(SecurityEvent), rows)

    batches: list[int] = []
    commits: list[None] = []

    def after_execute(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.lstrip().upper().startswith("DELETE"):
            batches.append(cursor.rowcount)

    def after_commit(conn) -> None:
        commits.append(None)

    event.listen(connection, "after_cursor_execute", after_execute)
    event.listen(connection, "commit", after_commit)
    try:
        assert purge_expired_security_events(connection) == 2500
    finally:
        event.remove(connection, "after_cursor_execute", after_execute)
        event.remove(connection, "commit", after_commit)

    # 每批一个短事务；满批之后还要再删一批，直到不足一批。
    assert PURGE_BATCH_SIZE == 1000
    assert batches == [1000, 1000, 500]
    assert len(commits) == 3
    assert existing_events(migrated_engine, event_ids) == set()


def test_purge_refuses_a_connection_with_an_open_transaction(
    created: Created, connection: Connection
):
    freeze(connection, NOW)
    connection.begin()
    exactly = add_event(connection, created, EXACTLY_ONE_YEAR)
    with pytest.raises(SQLAlchemyError):
        purge_expired_security_events(connection)
    connection.rollback()
    assert existing_events(connection.engine, [exactly]) == set()


def test_purge_result_is_logged_to_app_api_without_a_handler_on_the_module_logger(
    connection: Connection, caplog: pytest.LogCaptureFixture
):
    # 冻结到很早的时间：没有事件满 1 年，结果行记 0 条。
    freeze(connection, datetime(2000, 1, 1))
    # 不调 caplog.set_level：记录能被捕获，说明日志器本身的有效级别放行 INFO。
    assert purge_expired_security_events(connection) == 0

    [record] = [r for r in caplog.records if "purged" in r.getMessage()]
    assert record.name == "app.api" or record.name.startswith("app.api.")
    assert record.levelno == logging.INFO
    assert record.getMessage() == "purged 0 security events older than 1 year"
    # app.api 挂了处理器（生产上看得到）；本模块的日志器没有另挂处理器（命令行不受影响）。
    assert logging.getLogger("app.api").handlers
    assert logging.getLogger(security_events.__name__).handlers == []


# --- 定时机制 ---


def test_periodic_task_defaults_wait_a_minute_then_run_daily():
    task = PeriodicTask("defaults", lambda: None)
    assert task.first_delay == 60
    assert task.interval == 24 * 60 * 60
    assert not task.is_alive()


def test_periodic_task_runs_repeatedly_and_stops():
    calls: list[None] = []
    task = PeriodicTask("repeat", lambda: calls.append(None), interval=0.01, first_delay=0)
    task.start()
    try:
        wait_until(lambda: len(calls) >= 3)
    finally:
        task.stop()

    assert not task.is_alive()
    stopped_at = len(calls)
    time.sleep(0.1)
    assert len(calls) == stopped_at


def test_periodic_task_waits_for_the_first_delay():
    calls: list[None] = []
    task = PeriodicTask("delayed", lambda: calls.append(None), interval=0.01, first_delay=0.5)
    task.start()
    try:
        time.sleep(0.1)
        assert calls == []
        wait_until(lambda: len(calls) >= 1)
    finally:
        task.stop()
    assert not task.is_alive()


def test_periodic_task_logs_a_failure_and_keeps_running(caplog: pytest.LogCaptureFixture):
    calls: list[None] = []

    def flaky() -> None:
        calls.append(None)
        if len(calls) == 1:
            raise RuntimeError("database went away")

    task = PeriodicTask("flaky", flaky, interval=0.01, first_delay=0)
    task.start()
    try:
        wait_until(lambda: len(calls) >= 3)
        assert task.is_alive()
    finally:
        task.stop()
    assert not task.is_alive()

    [failure] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert failure.name.startswith("app.api.")
    assert "flaky" in failure.getMessage()
    assert failure.exc_info is not None
    assert "database went away" in str(failure.exc_info[1])


def test_periodic_task_stop_before_the_first_run_never_calls():
    calls: list[None] = []
    task = PeriodicTask("never", lambda: calls.append(None), first_delay=60)
    task.start()
    started = time.monotonic()
    task.stop()
    assert time.monotonic() - started < 5
    assert not task.is_alive()
    assert calls == []


def test_stop_all_waits_for_every_task_against_one_deadline(caplog: pytest.LogCaptureFixture):
    release = threading.Event()
    entered = [threading.Event(), threading.Event()]

    def blocking(index: int) -> Callable[[], None]:
        def run() -> None:
            entered[index].set()
            release.wait(10)

        return run

    tasks = [
        PeriodicTask(f"blocked-{index}", blocking(index), interval=60, first_delay=0)
        for index in range(2)
    ]
    for task in tasks:
        task.start()
    try:
        for flag in entered:
            wait_until(flag.is_set)
        started = time.monotonic()
        stop_all(tasks, timeout=0.5)
        elapsed = time.monotonic() - started
        # 共用一个截止时间：不是两个 0.5 秒之和。
        assert elapsed < 0.9
        assert all(task.is_alive() for task in tasks)
    finally:
        release.set()
        stop_all(tasks)
    assert not any(task.is_alive() for task in tasks)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert sorted(r.getMessage() for r in warnings) == [
        "periodic task blocked-0 did not stop within 0.5 seconds",
        "periodic task blocked-1 did not stop within 0.5 seconds",
    ]
    assert all(r.name.startswith("app.api.") for r in warnings)


def test_stop_all_ends_both_threads_when_the_functions_return_quickly(
    caplog: pytest.LogCaptureFixture,
):
    calls: list[str] = []
    tasks = [
        PeriodicTask(name, lambda name=name: calls.append(name), interval=0.01, first_delay=0)
        for name in ("quick-a", "quick-b")
    ]
    for task in tasks:
        task.start()
    wait_until(lambda: {"quick-a", "quick-b"} <= set(calls))
    stop_all(tasks)
    assert not any(task.is_alive() for task in tasks)
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_stop_all_accepts_tasks_that_never_started(caplog: pytest.LogCaptureFixture):
    calls: list[None] = []
    never = PeriodicTask("never-started", lambda: calls.append(None), first_delay=0)
    running = PeriodicTask("started", lambda: None, interval=60, first_delay=60)
    running.start()
    stop_all([never, running])
    assert not never.is_alive()
    assert not running.is_alive()
    assert calls == []
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def assert_state_matches_the_domain_rule(task: PeriodicTask, now: float) -> TaskState:
    state = task.state(now)
    assert state.stale == is_stale(
        alive=state.alive,
        interval=task.interval,
        first_delay=task.first_delay,
        started_at=state.started_at,
        last_success_at=state.last_success_monotonic,
        last_outcome=state.last_outcome,
        now=now,
    )
    return state


def test_periodic_task_state_before_start_is_not_alive_and_stale():
    task = PeriodicTask("unstarted", lambda: None, interval=30, first_delay=30)
    state = assert_state_matches_the_domain_rule(task, time.monotonic())
    assert state.name == "unstarted"
    assert not state.alive
    assert state.started_at is None
    assert state.last_outcome is None
    assert state.last_success_at is None
    assert state.last_failure_at is None
    assert state.stale


def test_periodic_task_records_a_success():
    calls: list[None] = []
    task = PeriodicTask("succeeds", lambda: calls.append(None), interval=60, first_delay=0)
    before = datetime.now(UTC)
    started = time.monotonic()
    task.start()
    try:
        wait_until(lambda: task.state().last_outcome == SUCCEEDED)
        state = assert_state_matches_the_domain_rule(task, time.monotonic())
        assert state.alive
        assert state.started_at is not None and state.started_at >= started
        assert state.last_success_monotonic is not None
        assert state.last_success_monotonic >= state.started_at
        assert state.last_success_at is not None
        assert state.last_success_at.tzinfo is not None
        assert before <= state.last_success_at <= datetime.now(UTC)
        assert state.last_failure_at is None
        assert not state.stale
        # 超过 2 个间隔加 300 秒没再正常结束即 stale，判定只用单调时钟。
        limit = state.last_success_monotonic + 2 * task.interval + 300
        assert not assert_state_matches_the_domain_rule(task, limit - 1).stale
        assert assert_state_matches_the_domain_rule(task, limit + 1).stale
    finally:
        task.stop()

    state = assert_state_matches_the_domain_rule(task, time.monotonic())
    assert not state.alive
    assert state.stale
    # 停止后记录还在。
    assert state.last_outcome == SUCCEEDED
    assert state.last_success_at is not None


def test_periodic_task_records_a_failure_and_keeps_running():
    calls: list[None] = []
    fail = threading.Event()
    fail.set()

    def flaky() -> None:
        calls.append(None)
        if fail.is_set():
            raise RuntimeError("database went away")

    task = PeriodicTask("records-failure", flaky, interval=0.01, first_delay=0)
    task.start()
    try:
        wait_until(lambda: task.state().last_outcome == FAILED)
        state = assert_state_matches_the_domain_rule(task, time.monotonic())
        assert state.alive
        assert state.last_failure_at is not None
        assert state.last_failure_at.tzinfo is not None
        assert state.last_success_at is None
        assert state.stale

        # 线程继续跑；之后正常结束一次就恢复，失败的时间留着作展示。
        failed_calls = len(calls)
        fail.clear()
        wait_until(lambda: task.state().last_outcome == SUCCEEDED)
        state = assert_state_matches_the_domain_rule(task, time.monotonic())
        assert task.is_alive()
        assert len(calls) > failed_calls
        assert state.last_success_at is not None
        assert state.last_failure_at is not None
        assert not state.stale
    finally:
        task.stop()
    assert not task.is_alive()
    assert assert_state_matches_the_domain_rule(task, time.monotonic()).stale


# --- 接入 ---


def purge_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == SECURITY_EVENT_PURGE]


def test_lifespan_starts_and_stops_the_purge_when_the_database_is_configured(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
):
    app = create_app(Settings(env="demo", git_sha="d" * 40, database_url=TEST_DATABASE_URL))
    try:
        with TestClient(app) as client:
            task = app.state.security_event_purge
            assert isinstance(task, PeriodicTask)
            assert task.is_alive()
            assert task.first_delay == 60
            assert task.interval == 24 * 60 * 60
            assert client.get("/healthz").status_code == 200

            # 每次运行从应用的连接池取一个连接交给清理函数，其上没有打开的事务。
            seen: list[Connection] = []

            def fake_purge(conn: Connection) -> int:
                assert conn.engine is app.state.engine
                assert not conn.in_transaction()
                seen.append(conn)
                return 0

            monkeypatch.setattr("app.main.purge_expired_security_events", fake_purge)
            task.function()
            assert len(seen) == 1
        assert not task.is_alive()
        assert purge_threads() == []
    finally:
        app.state.engine.dispose()


def test_lifespan_exit_stops_both_tasks_and_swaps_in_unstarted_ones(migrated_engine: Engine):
    app = create_app(Settings(env="demo", git_sha="d" * 40, database_url=TEST_DATABASE_URL))
    try:
        with TestClient(app) as client:
            purge = app.state.security_event_purge
            expiry = app.state.reservation_expiry
            assert purge.is_alive() and expiry.is_alive()
            # 记一次成功：换上的新任务没有这条记录，就绪检查据此看得出换过。
            purge.function = lambda: None
            purge.run_once()
        assert not purge.is_alive()
        assert not expiry.is_alive()
        # app.state 上的两项不换，仍是停下的那两个任务。
        assert app.state.security_event_purge is purge
        assert app.state.reservation_expiry is expiry

        response = client.get("/readyz")
        assert response.status_code == 200
        tasks = {task["name"]: task for task in response.json()["periodic"]["tasks"]}
        assert set(tasks) == {SECURITY_EVENT_PURGE, RESERVATION_EXPIRY}
        for task in tasks.values():
            assert task["alive"] is False
            assert task["last_success_at"] is None
            assert task["stale"] is True
        assert purge_threads() == []
    finally:
        app.state.engine.dispose()


def test_the_purge_never_starts_without_a_database_because_the_app_refuses_to_start():
    # 连接串必填（契约 6.1 启动核对）：create_app 就失败，不会有应用进入 lifespan。
    with pytest.raises(SystemExit) as exited:
        create_app(Settings(env="demo", git_sha="d" * 40, database_url=""))
    assert exited.value.code == 1
    assert purge_threads() == []
