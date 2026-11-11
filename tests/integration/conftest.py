import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine

from alembic import command
from app.db import make_engine

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
