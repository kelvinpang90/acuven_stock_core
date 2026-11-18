"""预留的到期关单（契约 §5.2 第 4 条、§5.6 到期关闭与隔离级别）。

在真 MySQL 上跑；每个测试建自己的租户、池、销售入口与凭据，池先切到 live，
预留经接口建立，到期由测试把时间往前挪（自己提交的连接）。
直接调用关单与扫描函数的测试用单独的引擎，会话设置不会带进共用的连接池；
定时任务的扫描查的是所有租户，断言只看本测试自己的预留。
"""

import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, event, select, text, update

from app.config import Settings
from app.db import make_engine
from app.main import RESERVATION_EXPIRY, create_app
from app.models.inventory import IdempotencyRecord
from app.models.orders import Reservation
from app.services import reservations as reservation_service
from app.services.periodic import PeriodicTask
from app.services.reservations import ClosingStopped, sweep_expired_reservations
from tests.integration.conftest import TEST_DATABASE_URL
from tests.integration.test_api_auth import Caller, error_of
from tests.integration.test_idempotency import add_skus
from tests.integration.test_reservation_release_api import (
    commit_columns,
    release,
    releases,
    reserved_order,
    stored_reservation,
)
from tests.integration.test_reservations_api import (
    data_of,
    line,
    live_caller,
    make_client,
    new_key,
    order,
    reserve,
    rows_of,
    stock,
)
from tests.integration.test_stock_service import entries, quantities_of

ISOLATION = "SELECT @@SESSION.transaction_isolation"


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    test_client = make_client()
    try:
        yield test_client
    finally:
        test_client.app.state.engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


@pytest.fixture
def caller(engine: Engine) -> Caller:
    """池已 live、有销售入口的调用方。"""
    return live_caller(engine)


@pytest.fixture
def own_engine(migrated_engine: Engine) -> Iterator[Engine]:
    """单独的引擎与连接池：测试里设的会话隔离级别不会带进共用的连接池。"""
    engine = make_engine(TEST_DATABASE_URL)
    try:
        yield engine
    finally:
        engine.dispose()


# --- 造数据与观察 ---


def shift(engine: Engine, reservation_id: str, hours: int = 1) -> None:
    """把预留的创建与到期时间都往前挪 hours 小时：已到期、还没被关单。"""
    statement = text(
        "UPDATE reservations SET created_at = created_at - INTERVAL :hours HOUR,"
        " expires_at = expires_at - INTERVAL :hours HOUR WHERE reservation_id = :id"
    )
    with engine.begin() as db:
        db.execute(statement, {"id": reservation_id, "hours": hours})


def status_of(engine: Engine, reservation_id: str) -> str:
    statement = select(Reservation.status).where(Reservation.reservation_id == reservation_id)
    with engine.connect() as db:
        return db.execute(statement).scalar_one()


def stored_channel(engine: Engine, reservation_id: str) -> str:
    statement = select(Reservation.channel_id).where(Reservation.reservation_id == reservation_id)
    with engine.connect() as db:
        return db.execute(statement).scalar_one()


def read_committed(connection: Connection) -> None:
    connection.execute(text("SET SESSION transaction_isolation = 'READ-COMMITTED'"))
    connection.commit()


def expiry_releases(engine: Engine, caller: Caller, sku_id: str) -> list[dict]:
    return [
        entry
        for entry in entries(engine, caller)
        if entry["sku_id"] == sku_id and entry["entry_type"] == "RELEASE"
    ]


def assert_closed_by_core(entry: dict, channel_id: str) -> None:
    """关单的流水只带预留的销售入口，原因是 expired。"""
    assert entry["reason_code"] == "expired"
    assert entry["channel_id"] == channel_id
    for column in ("idempotency_key", "credential_id", "actor_ref", "actor_role", "request_id"):
        assert entry[column] is None


# --- 一次关单 ---


def test_closing_a_due_reservation_releases_it_once(client, engine, caller, own_engine):
    created, (first_sku, second_sku) = reserved_order(client, engine, caller, [3, 2])
    reservation_id = created["reservation_id"]
    shift(engine, reservation_id)

    with own_engine.connect() as conn:
        read_committed(conn)
        # 在调用方的事务里运行、自己不提交：回滚之后什么也没留下。
        conn.begin()
        assert reservation_service.close_if_due(conn, caller.tenant_id, reservation_id) is True
        conn.rollback()
        assert status_of(engine, reservation_id) == "active"
        assert releases(engine, caller) == []

        with conn.begin():
            assert reservation_service.close_if_due(conn, caller.tenant_id, reservation_id)

    stored = stored_reservation(engine, caller)
    assert stored["status"] == "expired"
    assert stored["closed_at"] is not None
    assert stored["release_reason"] is None
    assert stored["released_request_id"] is None
    assert quantities_of(engine, caller, first_sku) == (5, 0, 0, 0)
    assert quantities_of(engine, caller, second_sku) == (5, 0, 0, 0)

    written = releases(engine, caller)
    assert [(e["line_ref"], e["sku_id"], e["reserved_delta"]) for e in written] == [
        ("l1", first_sku, -3),
        ("l2", second_sku, -2),
    ]
    for entry in written:
        assert_closed_by_core(entry, stored["channel_id"])
        assert entry["source_type"] == "reservation"
        assert entry["source_ref"] == created["order_ref"]
        assert entry["occurred_at"] == stored["closed_at"]
    assert written[0]["command_id"] == written[1]["command_id"]

    # 再关一次：什么也不写。
    with own_engine.connect() as conn, conn.begin():
        assert reservation_service.close_if_due(conn, caller.tenant_id, reservation_id) is False
    assert releases(engine, caller) == written
    assert stored_reservation(engine, caller) == stored


def test_closing_leaves_a_reservation_that_is_not_due(client, engine, caller, own_engine):
    created, (sku,) = reserved_order(client, engine, caller, [2])
    reservation_id = created["reservation_id"]
    with own_engine.connect() as conn, conn.begin():
        assert reservation_service.close_if_due(conn, caller.tenant_id, reservation_id) is False
    # 到期之后，别的租户也关不到它。
    other = live_caller(engine)
    shift(engine, reservation_id)
    with own_engine.connect() as conn, conn.begin():
        assert reservation_service.close_if_due(conn, other.tenant_id, reservation_id) is False
    assert status_of(engine, reservation_id) == "active"
    assert quantities_of(engine, caller, sku) == (5, 2, 0, 0)
    assert releases(engine, caller) == []


def test_two_connections_closing_the_same_reservation_write_one_set_of_releases(
    client, engine, caller, own_engine
):
    created, (first_sku, second_sku) = reserved_order(client, engine, caller, [2, 1])
    reservation_id = created["reservation_id"]
    shift(engine, reservation_id)

    with own_engine.connect() as first, own_engine.connect() as second:
        read_committed(first)
        read_committed(second)
        transaction = first.begin()
        assert reservation_service.close_if_due(first, caller.tenant_id, reservation_id)

        def close_second() -> bool:
            with second.begin():
                return reservation_service.close_if_due(second, caller.tenant_id, reservation_id)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(close_second)
            time.sleep(0.5)
            # 第二个连接等在预留行的锁上；第一个提交之后它在锁内复核，已不是 active。
            assert not future.done()
            transaction.commit()
            assert future.result(timeout=20) is False

    assert status_of(engine, reservation_id) == "expired"
    assert len(releases(engine, caller)) == 2
    assert quantities_of(engine, caller, first_sku) == (5, 0, 0, 0)
    assert quantities_of(engine, caller, second_sku) == (5, 0, 0, 0)


# --- 新预留之前的关单 ---


def test_a_new_reservation_first_closes_the_due_reservation_holding_the_stock(
    client, engine, caller
):
    sku, other_sku = add_skus(engine, caller.tenant_id, 2)
    stock(engine, caller, sku, 2)
    stock(engine, caller, other_sku, 5)
    old = data_of(reserve(client, caller, order([line("l1", sku, 2)])), 201)
    unrelated = data_of(reserve(client, caller, order([line("l1", other_sku, 1)])), 201)
    shift(engine, old["reservation_id"])
    shift(engine, unrelated["reservation_id"])

    new = data_of(reserve(client, caller, order([line("l1", sku, 2)])), 201)
    assert new["status"] == "active"
    assert status_of(engine, old["reservation_id"]) == "expired"
    # 只关所列 SKU 上的，别的留给定时任务。
    assert status_of(engine, unrelated["reservation_id"]) == "active"
    assert quantities_of(engine, caller, sku) == (2, 2, 0, 0)
    assert quantities_of(engine, caller, other_sku) == (5, 1, 0, 0)

    written = [e for e in entries(engine, caller) if e["sku_id"] == sku]
    assert [(e["entry_type"], e["reserved_delta"]) for e in written] == [
        ("RESERVE", 2),
        ("RELEASE", -2),
        ("RESERVE", 2),
    ]
    assert_closed_by_core(written[1], stored_channel(engine, old["reservation_id"]))


def test_a_failed_closing_is_skipped_and_the_next_one_is_closed(
    client, engine, caller, monkeypatch
):
    [sku] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, sku, 2)
    first = data_of(reserve(client, caller, order([line("l1", sku, 1)])), 201)
    second = data_of(reserve(client, caller, order([line("l1", sku, 1)])), 201)
    # first 到期更早，先关；它出错时回滚它、继续关 second。
    shift(engine, first["reservation_id"], hours=2)
    shift(engine, second["reservation_id"], hours=1)
    real_close = reservation_service.close_if_due
    calls: list[str] = []

    def failing_first(connection, tenant_id, reservation_id):
        calls.append(reservation_id)
        if reservation_id == first["reservation_id"]:
            raise RuntimeError("closing failed")
        return real_close(connection, tenant_id, reservation_id)

    monkeypatch.setattr(reservation_service, "close_if_due", failing_first)
    data_of(reserve(client, caller, order([line("l1", sku, 1)])), 201)

    assert calls == [first["reservation_id"], second["reservation_id"]]
    assert status_of(engine, first["reservation_id"]) == "active"
    assert status_of(engine, second["reservation_id"]) == "expired"
    assert quantities_of(engine, caller, sku) == (2, 2, 0, 0)


def test_a_broken_connection_while_closing_fails_the_request_with_nothing_saved(
    client, engine, caller, monkeypatch
):
    [sku] = add_skus(engine, caller.tenant_id, 1)
    stock(engine, caller, sku, 2)
    old = data_of(reserve(client, caller, order([line("l1", sku, 2)])), 201)
    shift(engine, old["reservation_id"])

    def broken(connection, tenant_id, reservation_id):
        # 钩子用的连接失效：幂等命名锁与 READ COMMITTED 都随之不在。
        connection.invalidate()
        raise RuntimeError("the connection went away")

    monkeypatch.setattr(reservation_service, "close_if_due", broken)
    key = new_key()
    body = order([line("l1", sku, 2)])
    response = reserve(client, caller, body, key)
    error = error_of(response, 503)
    assert error["code"] == "UNAVAILABLE"

    [stored] = rows_of(engine, Reservation, caller)
    assert stored["reservation_id"] == old["reservation_id"]
    assert stored["status"] == "active"
    assert quantities_of(engine, caller, sku) == (2, 2, 0, 0)
    assert len(entries(engine, caller)) == 1
    records = rows_of(engine, IdempotencyRecord, caller)
    assert [row for row in records if row["idempotency_key"] == key] == []

    # 同键重试：这次关单成功，新预留建立。
    monkeypatch.undo()
    data_of(reserve(client, caller, body, key), 201)
    assert status_of(engine, old["reservation_id"]) == "expired"


# --- 定时任务的扫描 ---


def test_a_sweep_closes_every_due_active_reservation_and_nothing_else(
    client, engine, caller, own_engine
):
    due_a, a_skus = reserved_order(client, engine, caller, [1, 2])
    due_b, b_skus = reserved_order(client, engine, caller, [3])
    fresh, fresh_skus = reserved_order(client, engine, caller, [1])
    released, released_skus = reserved_order(client, engine, caller, [1])
    committed, committed_skus = reserved_order(client, engine, caller, [1])
    data_of(release(client, caller, released["reservation_id"], {"reason": "cancelled"}), 200)
    with engine.begin() as db:
        statement = update(Reservation).where(
            Reservation.reservation_id == committed["reservation_id"]
        )
        db.execute(statement.values(status="committed", **commit_columns(caller)))
    for reservation in (due_a, due_b, released, committed):
        shift(engine, reservation["reservation_id"])
    before = {sku: expiry_releases(engine, caller, sku) for sku in released_skus}

    assert sweep_expired_reservations(own_engine) >= 2

    assert status_of(engine, due_a["reservation_id"]) == "expired"
    assert status_of(engine, due_b["reservation_id"]) == "expired"
    assert status_of(engine, fresh["reservation_id"]) == "active"
    assert status_of(engine, released["reservation_id"]) == "released"
    assert status_of(engine, committed["reservation_id"]) == "committed"
    for sku in (*a_skus, *b_skus):
        assert quantities_of(engine, caller, sku)[1] == 0
        [entry] = expiry_releases(engine, caller, sku)
        assert entry["reason_code"] == "expired"
        assert entry["credential_id"] is None
    assert quantities_of(engine, caller, fresh_skus[0]) == (5, 1, 0, 0)
    assert quantities_of(engine, caller, committed_skus[0]) == (5, 1, 0, 0)
    assert expiry_releases(engine, caller, fresh_skus[0]) == []
    assert expiry_releases(engine, caller, committed_skus[0]) == []
    assert {sku: expiry_releases(engine, caller, sku) for sku in released_skus} == before


def test_a_sweep_runs_read_committed_and_returns_a_default_connection_to_the_pool(
    client, engine, caller, own_engine, monkeypatch
):
    created, _ = reserved_order(client, engine, caller, [1])
    shift(engine, created["reservation_id"])
    checked_out: list[object] = []
    event.listen(own_engine, "checkout", lambda dbapi, record, proxy: checked_out.append(dbapi))
    levels: list[str] = []
    real_close = reservation_service.close_if_due

    def observed(connection, tenant_id, reservation_id):
        levels.append(connection.execute(text(ISOLATION)).scalar_one())
        return real_close(connection, tenant_id, reservation_id)

    monkeypatch.setattr(reservation_service, "close_if_due", observed)
    assert sweep_expired_reservations(own_engine) >= 1
    assert levels and set(levels) == {"READ-COMMITTED"}
    assert status_of(engine, created["reservation_id"]) == "expired"

    [used] = checked_out
    with own_engine.connect() as conn:
        # 放回池里的就是扫描用过的那个连接，隔离级别已是默认值。
        assert conn.connection.dbapi_connection is used
        query = "SELECT @@SESSION.transaction_isolation, @@GLOBAL.transaction_isolation"
        session_level, default_level = conn.execute(text(query)).one()
        assert session_level == default_level


def test_a_sweep_discards_the_connection_when_resetting_the_isolation_fails(
    own_engine, monkeypatch
):
    checked_out: list[object] = []
    invalidated: list[object] = []
    event.listen(own_engine, "checkout", lambda dbapi, record, proxy: checked_out.append(dbapi))
    event.listen(own_engine, "invalidate", lambda dbapi, record, exc: invalidated.append(dbapi))
    bad_reset = "SET SESSION transaction_isolation = 'NO-SUCH-LEVEL'"
    monkeypatch.setattr(reservation_service, "_DEFAULT_ISOLATION", bad_reset)

    sweep_expired_reservations(own_engine)

    [used] = checked_out
    assert invalidated == [used]
    with own_engine.connect() as conn:
        # 作废的连接没有放回池里：再取到的是新连接，隔离级别是默认值。
        assert conn.connection.dbapi_connection is not used
        query = "SELECT @@SESSION.transaction_isolation, @@GLOBAL.transaction_isolation"
        session_level, default_level = conn.execute(text(query)).one()
        assert session_level == default_level


def test_a_sweep_logs_a_failed_closing_and_continues(
    client, engine, caller, own_engine, monkeypatch, caplog
):
    first, _ = reserved_order(client, engine, caller, [1])
    second, _ = reserved_order(client, engine, caller, [1])
    shift(engine, first["reservation_id"])
    shift(engine, second["reservation_id"])
    real_close = reservation_service.close_if_due

    def failing_first(connection, tenant_id, reservation_id):
        if reservation_id == first["reservation_id"]:
            raise RuntimeError("closing failed")
        return real_close(connection, tenant_id, reservation_id)

    monkeypatch.setattr(reservation_service, "close_if_due", failing_first)
    caplog.set_level(logging.ERROR, logger="app.api")
    sweep_expired_reservations(own_engine)

    assert status_of(engine, first["reservation_id"]) == "active"
    assert status_of(engine, second["reservation_id"]) == "expired"
    [record] = [r for r in caplog.records if first["reservation_id"] in r.getMessage()]
    assert record.name.startswith("app.api.")
    assert record.exc_info is not None


def test_a_sweep_stops_when_the_connection_breaks(client, engine, caller, own_engine, monkeypatch):
    first, _ = reserved_order(client, engine, caller, [1])
    second, _ = reserved_order(client, engine, caller, [1])
    shift(engine, first["reservation_id"])
    shift(engine, second["reservation_id"])
    calls: list[str] = []

    def broken(connection, tenant_id, reservation_id):
        calls.append(reservation_id)
        connection.invalidate()
        raise RuntimeError("the connection went away")

    monkeypatch.setattr(reservation_service, "close_if_due", broken)
    with pytest.raises(ClosingStopped):
        sweep_expired_reservations(own_engine)

    # 停止本轮，不在新连接上接着关。
    assert len(calls) == 1
    assert status_of(engine, first["reservation_id"]) == "active"
    assert status_of(engine, second["reservation_id"]) == "active"
    assert releases(engine, caller) == []


# --- 接入 ---


def expiry_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == RESERVATION_EXPIRY]


def test_lifespan_starts_and_stops_the_expiry_sweep_every_30_seconds(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
):
    app = create_app(Settings(env="demo", git_sha="d" * 40, database_url=TEST_DATABASE_URL))
    try:
        with TestClient(app) as test_client:
            task = app.state.reservation_expiry
            assert isinstance(task, PeriodicTask)
            assert task.name == RESERVATION_EXPIRY
            assert task.is_alive()
            assert task.first_delay == 30
            assert task.interval == 30
            # 与安全事件清理并列。
            assert app.state.security_event_purge.is_alive()
            assert test_client.get("/healthz").status_code == 200

            # 每次运行把应用的引擎交给扫描函数。
            seen: list[Engine] = []

            def fake_sweep(engine: Engine) -> int:
                seen.append(engine)
                return 0

            monkeypatch.setattr("app.main.sweep_expired_reservations", fake_sweep)
            task.function()
            assert len(seen) == 1
            assert seen[0] is app.state.engine
            purge = app.state.security_event_purge
        assert not task.is_alive()
        assert not purge.is_alive()
        assert expiry_threads() == []
    finally:
        app.state.engine.dispose()
