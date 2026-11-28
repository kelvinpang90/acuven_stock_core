"""admin_auth: admin accounts, admin sessions and admin login attempts; four admin account
security event types

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-24

docs/ADMIN_AUTH.md 0.3 第 3 节（登录一）与契约 7.2 的 0.22 一条（Q-91、Q-92）。
建表 admin_accounts、admin_sessions、admin_login_attempts；时间都由应用写，不设默认值。
指向账号的外键 ON DELETE RESTRICT。两种实例的库都建这三张表，不按 core_instance 区分实例。
security_events.event_type 的检查约束加四种账号操作类型。
MySQL 的检查约束不能同名并存，先删原约束，新约束沿用 ck_security_events_event_type；
已有的行都在原取值里，新约束对它们照样成立。

降级只用于 CI 与开发库：库里有这四种事件时恢复不了原约束，在改动任何东西之前失败。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/admin_auth.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
TOKEN_HASH = sa.CHAR(64, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}
OUTCOMES = ("success", "bad_password", "unknown_user", "disabled", "locked")
# 0003 的十种。
OLD_EVENT_TYPES = (
    "invalid_credential",
    "forbidden",
    "cross_tenant",
    "idempotency_conflict",
    "business_ref_conflict",
    "rate_limited",
    "credential_issued",
    "token_issued",
    "token_revoked",
    "credential_revoked",
)
ADMIN_EVENT_TYPES = (
    "admin_account_created",
    "admin_password_reset",
    "admin_account_disabled",
    "admin_account_enabled",
)
EVENT_TYPE_CHECK = "ck_security_events_event_type"


def _in(column: str, values: Sequence[str]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def _check(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], name=op.f(f"fk_{table}_tenant_id_tenants")
    )


def _account_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "admin_account_id"],
        ["admin_accounts.tenant_id", "admin_accounts.admin_account_id"],
        name=op.f(f"fk_{table}_tenant_id_admin_accounts"),
        ondelete="RESTRICT",
    )


def _replace_event_type_check(values: Sequence[str]) -> None:
    condition = _in("event_type", values)
    op.drop_constraint(op.f(EVENT_TYPE_CHECK), "security_events", type_="check")
    op.create_check_constraint(op.f(EVENT_TYPE_CHECK), "security_events", condition)


def upgrade() -> None:
    op.create_table(
        "admin_accounts",
        sa.Column("admin_account_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("password_updated_at", TIMESTAMP, nullable=False),
        sa.Column("disabled_at", TIMESTAMP, nullable=True),
        sa.PrimaryKeyConstraint("admin_account_id", name=op.f("pk_admin_accounts")),
        _tenant_fk("admin_accounts"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name=op.f("fk_admin_accounts_tenant_id_credentials"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "admin_account_id",
            name=op.f("uq_admin_accounts_tenant_id_admin_account_id"),
        ),
        sa.UniqueConstraint("credential_id", name=op.f("uq_admin_accounts_credential_id")),
        sa.UniqueConstraint("username", name=op.f("uq_admin_accounts_username")),
        _check(
            "admin_accounts",
            "username_length",
            "LENGTH(username) >= 3 AND LENGTH(username) <= 32",
        ),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("admin_account_id", ID, nullable=False),
        sa.Column("token_hash", TOKEN_HASH, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=False),
        sa.Column("revoked_at", TIMESTAMP, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_sessions")),
        _tenant_fk("admin_sessions"),
        _account_fk("admin_sessions"),
        sa.UniqueConstraint("token_hash", name=op.f("uq_admin_sessions_token_hash")),
        _check("admin_sessions", "token_hash", "REGEXP_LIKE(token_hash, '^[0-9a-f]{64}$', 'c')"),
        _check("admin_sessions", "expires_after_created", "expires_at > created_at"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_admin_sessions_admin_account_id"),
        "admin_sessions",
        ["admin_account_id"],
        unique=False,
    )
    op.create_table(
        "admin_login_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", TIMESTAMP, nullable=False),
        sa.Column("throttle_key", sa.String(64), nullable=False),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("tenant_id", ID, nullable=True),
        sa.Column("admin_account_id", ID, nullable=True),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_login_attempts")),
        _tenant_fk("admin_login_attempts"),
        _account_fk("admin_login_attempts"),
        _check("admin_login_attempts", "throttle_key_length", "LENGTH(throttle_key) = 64"),
        _check("admin_login_attempts", "source_key_length", "LENGTH(source_key) = 64"),
        _check(
            "admin_login_attempts",
            "account_tenant",
            "admin_account_id IS NULL OR tenant_id IS NOT NULL",
        ),
        _check("admin_login_attempts", "outcome_known", _in("outcome", OUTCOMES)),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_admin_login_attempts_throttle_key_occurred_at"),
        "admin_login_attempts",
        ["throttle_key", "occurred_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_login_attempts_source_key_occurred_at"),
        "admin_login_attempts",
        ["source_key", "occurred_at"],
        unique=False,
    )
    _replace_event_type_check(OLD_EVENT_TYPES + ADMIN_EVENT_TYPES)


def downgrade() -> None:
    # MySQL 的 DDL 不在事务里：先查，免得删了约束之后才在恢复原约束时失败、库停在半路。
    condition = _in("event_type", ADMIN_EVENT_TYPES)
    count = sa.text(f"SELECT COUNT(*) FROM security_events WHERE {condition}")
    if op.get_bind().execute(count).scalar_one():
        raise RuntimeError("security_events has admin account events; cannot downgrade below 0018")
    _replace_event_type_check(OLD_EVENT_TYPES)
    # 先删引用别表的；索引随表一并删掉。
    op.drop_table("admin_login_attempts")
    op.drop_table("admin_sessions")
    op.drop_table("admin_accounts")
