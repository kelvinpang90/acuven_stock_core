"""Alembic environment.

连接串只从环境变量 CORE_DATABASE_URL 读，不写进 alembic.ini（配置不落盘）。
这里不用 app.config.Settings：迁移只要连接串与 CORE_ENV，不要 API 的其他配置。
从 0006（标记迁移）起迁移要求 CORE_ENV（契约 6.1）：0006 按它写入库里的实例标记 core_instance，
并在 production 时收窄流水类型约束。
迁移前核对（契约 6.1）：在线模式下，库里已有 core_instance 时，执行任何迁移之前先比对它的
core_env 与执行进程的 CORE_ENV；不一致、表在而没有行、CORE_ENV 没有设或不合法都失败，不改库。
还没有这张表（0006 之前）时跳过。离线模式（--sql）不连库，不做这一步。
target_metadata 是模型的 metadata，CI 的 `alembic check` 据此比对模型与迁移。
新增模型模块时在这里 import 它，把模型登记到 Base.metadata。
"""

from __future__ import annotations

import os

from sqlalchemy import Connection, engine_from_config, inspect, pool, text

from alembic import context
from app.db import Base
from app.domain.environment import check_instance_marker, parse_core_env
from app.models import catalog, instance, inventory, service_auth  # noqa: F401

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("CORE_DATABASE_URL", "")
    if not url:
        raise RuntimeError("CORE_DATABASE_URL is not set; alembic cannot run without a target.")
    return url


def _check_instance_marker(connection: Connection) -> None:
    # 在自己的事务里读完就结束：留着连接上自动开启的事务，alembic 会沿用它而不在结束时提交。
    with connection.begin():
        if not inspect(connection).has_table("core_instance"):
            return
        rows = connection.execute(text("SELECT core_env FROM core_instance"))
        marker = rows.scalar_one_or_none()
    check_instance_marker(marker, parse_core_env(os.environ.get("CORE_ENV", "")))


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
        _check_instance_marker(connection)
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
