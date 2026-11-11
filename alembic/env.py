"""Alembic environment.

连接串只从环境变量 CORE_DATABASE_URL 读，不写进 alembic.ini（配置不落盘）。
这里不用 app.config.Settings：它还要求 CORE_ENV，而迁移与运行环境无关。
target_metadata 是模型的 metadata，CI 的 `alembic check` 据此比对模型与迁移。
新增模型模块时在这里 import 它，把模型登记到 Base.metadata。
"""

from __future__ import annotations

import os

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.db import Base
from app.models import catalog  # noqa: F401

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("CORE_DATABASE_URL", "")
    if not url:
        raise RuntimeError("CORE_DATABASE_URL is not set; alembic cannot run without a target.")
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (`alembic upgrade head --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    # 与 app/db.py 相同：会话时区固定为 UTC，迁移里的默认值与 NOW() 按 UTC 记。
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"init_command": "SET time_zone = '+00:00'"},
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
