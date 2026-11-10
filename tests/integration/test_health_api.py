import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

# 只指向 CI 的一次性 MySQL 服务容器；本地没设时跳过需要数据库的那条。
TEST_DATABASE_URL = os.environ.get("CORE_TEST_DATABASE_URL", "")


def client(**overrides: str) -> TestClient:
    values = {"env": "demo", "git_sha": "b" * 40, "database_url": ""} | overrides
    return TestClient(create_app(Settings(**values)))


def test_healthz_reports_commit_and_environment_without_touching_the_database():
    response = client(database_url="mysql+pymysql://nobody:x@127.0.0.1:1/none").get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "commit": "b" * 40, "env": "demo"}


def test_readyz_is_unavailable_when_no_database_is_configured():
    response = client().get("/readyz")
    assert response.status_code == 503
    assert response.json()["reason"] == "database_not_configured"


def test_readyz_is_unavailable_when_the_database_cannot_be_reached():
    response = client(database_url="mysql+pymysql://nobody:x@127.0.0.1:1/none").get("/readyz")
    assert response.status_code == 503
    assert response.json()["reason"] == "database_unreachable"


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="CORE_TEST_DATABASE_URL not set (CI only)")
def test_readyz_is_ok_against_a_real_database():
    response = client(database_url=TEST_DATABASE_URL).get("/readyz")
    assert response.status_code == 200


def test_settings_refuse_an_unknown_environment():
    with pytest.raises(ValueError):
        Settings(env="staging")
