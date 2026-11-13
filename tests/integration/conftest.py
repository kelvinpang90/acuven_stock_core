import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, Engine, insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql.expression import Executable

from alembic import command
from app.db import make_engine
from app.models.service_auth import Credential

# 只指向 CI 的一次性 MySQL 服务容器；本地没设时，用到下面夹具的测试跳过。
TEST_DATABASE_URL = os.environ.get("CORE_TEST_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    """需要表结构的测试共用：会话开始时升级到 head，结束时降到 base。

    结束时降到底，CI 之后的迁移往返才能照常从空库开始。
    """
    if not TEST_DATABASE_URL:
        pytest.skip("CORE_TEST_DATABASE_URL not set (CI only)")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    with pytest.MonkeyPatch.context() as env:
        # alembic/env.py 只从 CORE_DATABASE_URL 读连接串。
        env.setenv("CORE_DATABASE_URL", TEST_DATABASE_URL)
        command.upgrade(config, "head")
        engine = make_engine(TEST_DATABASE_URL)
        try:
            yield engine
        finally:
            engine.dispose()
            command.downgrade(config, "base")


# 下面是 schema 测试共用的：每个测试在一个事务里跑、结束时回滚；
# 被拒的语句包在保存点里，并断言拒绝它的正是预期的那条约束，而不是碰巧别的错误。


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Connection]:
    with migrated_engine.connect() as conn:
        conn.begin()
        yield conn
        conn.rollback()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:26].upper()}"


def assert_rejected_by(db: Connection, statement: Executable, constraint: str) -> None:
    with pytest.raises(DBAPIError) as caught, db.begin_nested():
        db.execute(statement)
    assert constraint in str(caught.value.orig)


def credential_insert(tenant_id: str, **overrides: object) -> Executable:
    values = {
        "credential_id": new_id("cred"),
        "tenant_id": tenant_id,
        "name": "Shop",
        "scopes": ["stock.read", "stock.write"],
        "status": "active",
    }
    return insert(Credential).values(values | overrides)


def add_credential(db: Connection, tenant_id: str, **overrides: object) -> str:
    """幂等记录与流水的 credential_id 要指向同租户的凭据；同一租户可以建多个。"""
    credential_id = new_id("cred")
    db.execute(credential_insert(tenant_id, credential_id=credential_id, **overrides))
    return credential_id
