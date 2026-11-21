import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.main import RESERVATION_EXPIRY, SECURITY_EVENT_PURGE, create_app
from tests.integration.conftest import TEST_DATABASE_URL

UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")


def make_client(**overrides: str) -> TestClient:
    values = {"env": "demo", "git_sha": "b" * 40, "database_url": TEST_DATABASE_URL} | overrides
    return TestClient(create_app(Settings(**values)))


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    """连测试库创建的应用（启动核对要求库里的标记与 CORE_ENV 一致）。"""
    test_client = make_client()
    try:
        yield test_client
    finally:
        test_client.app.state.engine.dispose()


def refuse_connections(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """应用已经启动之后，让它的引擎再也连不上数据库。"""

    def connect():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(client.app.state.engine, "connect", connect)


def test_healthz_reports_commit_and_environment_without_touching_the_database(client, monkeypatch):
    refuse_connections(monkeypatch, client)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "commit": "b" * 40, "env": "demo"}


def test_the_app_refuses_to_start_when_no_database_is_configured():
    with pytest.raises(SystemExit) as exited:
        make_client(database_url="")
    assert exited.value.code == 1


def test_readyz_is_unavailable_when_the_database_cannot_be_reached(client, monkeypatch):
    refuse_connections(monkeypatch, client)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["reason"] == "database_unreachable"


def test_readyz_is_ok_against_a_real_database(client):
    response = client.get("/readyz")
    assert response.status_code == 200


# --- 定时任务的状态（契约 8.5） ---


def task_entries(body: dict) -> dict[str, dict]:
    """periodic.tasks 按名字升序；返回名字到该项的映射。"""
    tasks = body["periodic"]["tasks"]
    names = [task["name"] for task in tasks]
    assert names == sorted(names) == [RESERVATION_EXPIRY, SECURITY_EVENT_PURGE]
    for task in tasks:
        assert set(task) == {"name", "alive", "last_success_at", "last_failure_at", "stale"}
    return {task["name"]: task for task in tasks}


@pytest.fixture
def started_client(migrated_engine: Engine) -> Iterator[TestClient]:
    """进入 lifespan 的应用：两个定时任务已启动（首次等待 30、60 秒，测试里不会自己跑）。"""
    test_client = make_client()
    try:
        with test_client:
            yield test_client
    finally:
        test_client.app.state.engine.dispose()


def test_readyz_reports_both_tasks_running_once_the_app_has_started(started_client):
    response = started_client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["periodic"]["status"] == "ok"
    for task in task_entries(body).values():
        assert task["alive"] is True
        assert task["last_success_at"] is None
        assert task["last_failure_at"] is None
        assert task["stale"] is False


def test_readyz_lists_tasks_not_started_without_lifespan_and_stays_200(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["periodic"]["status"] == "stale"
    for task in task_entries(body).values():
        assert task["alive"] is False
        assert task["last_success_at"] is None
        assert task["last_failure_at"] is None
        assert task["stale"] is True


def test_readyz_503_body_also_carries_periodic(started_client, monkeypatch):
    refuse_connections(monkeypatch, started_client)
    response = started_client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "database_unreachable"
    # 判定不读数据库，连不上时照样判得出。
    assert body["periodic"]["status"] == "ok"
    for task in task_entries(body).values():
        assert task["alive"] is True
        assert task["stale"] is False


def test_readyz_reports_a_task_whose_latest_run_failed_as_stale(started_client):
    app = started_client.app

    def boom() -> None:
        raise RuntimeError("database went away")

    expiry = app.state.reservation_expiry
    expiry.function = boom
    expiry.run_once()

    response = started_client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["periodic"]["status"] == "stale"
    tasks = task_entries(body)
    failed = tasks[RESERVATION_EXPIRY]
    assert failed["alive"] is True
    assert failed["stale"] is True
    assert failed["last_success_at"] is None
    assert UTC_TIMESTAMP.fullmatch(failed["last_failure_at"])
    recorded = datetime.fromisoformat(failed["last_failure_at"].removesuffix("Z"))
    assert abs(datetime.now(UTC).replace(tzinfo=None) - recorded) < timedelta(minutes=1)
    assert tasks[SECURITY_EVENT_PURGE]["stale"] is False


def test_readyz_503_reports_a_failed_task_with_the_same_status_code(started_client, monkeypatch):
    def boom() -> None:
        raise RuntimeError("database went away")

    purge = started_client.app.state.security_event_purge
    purge.function = boom
    purge.run_once()
    refuse_connections(monkeypatch, started_client)

    response = started_client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["reason"] == "database_unreachable"
    assert body["periodic"]["status"] == "stale"
    failed = task_entries(body)[SECURITY_EVENT_PURGE]
    assert failed["stale"] is True
    assert UTC_TIMESTAMP.fullmatch(failed["last_failure_at"])


def test_settings_refuse_an_unknown_environment():
    with pytest.raises(ValueError):
        Settings(env="staging")
