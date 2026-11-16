from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.main import create_app
from tests.integration.conftest import TEST_DATABASE_URL


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


def test_settings_refuse_an_unknown_environment():
    with pytest.raises(ValueError):
        Settings(env="staging")
