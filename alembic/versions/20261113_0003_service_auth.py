"""service auth: credentials, credential_tokens, security_events; credential foreign keys on
idempotency_records and ledger_entries

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/service_auth.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
STATUS = sa.String(16, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}
EVENT_TYPES = (
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


def _one_of(table: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=op.f(f"ck_{table}_{column}"))


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], name=op.f(f"fk_{table}_tenant_id_tenants")
    )


def _credential_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "credential_id"],
        ["credentials.tenant_id", "credentials.credential_id"],
        name=op.f(f"fk_{table}_tenant_id_credentials"),
    )


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("channel_id", ID, nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("revoked_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("credential_id", name=op.f("pk_credentials")),
        _tenant_fk("credentials"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["sales_channels.tenant_id", "sales_channels.channel_id"],
            name=op.f("fk_credentials_tenant_id_sales_channels"),
        ),
        sa.UniqueConstraint(
            "tenant_id", "credential_id", name=op.f("uq_credentials_tenant_id_credential_id")
        ),
        sa.CheckConstraint(
            "JSON_TYPE(scopes) = 'ARRAY' AND JSON_LENGTH(scopes) > 0",
            name=op.f("ck_credentials_scopes"),
        ),
        _one_of("credentials", "status", ("active", "revoked")),
        sa.CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL)",
            name=op.f("ck_credentials_revoked_at"),
        ),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "credential_tokens",
        sa.Column("token_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("token_digest", sa.CHAR(64, collation="utf8mb4_bin"), nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=True),
        sa.Column("revoked_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("token_id", name=op.f("pk_credential_tokens")),
        _tenant_fk("credential_tokens"),
        _credential_fk("credential_tokens"),
        # 摘要全库唯一、不含 tenant_id（契约第 10 节第 1 条的例外）。
        sa.UniqueConstraint("token_digest", name=op.f("uq_credential_tokens_token_digest")),
        sa.CheckConstraint(
            "REGEXP_LIKE(token_digest, '^[0-9a-f]{64}$', 'c')",
            name=op.f("ck_credential_tokens_token_digest"),
        ),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "security_events",
        sa.Column("event_id", ID, nullable=False),
        sa.Column("event_type", CODE32, nullable=False),
        sa.Column("occurred_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("tenant_id", ID, nullable=True),
        sa.Column("credential_id", ID, nullable=True),
        sa.Column("request_id", REF, nullable=True),
        sa.Column("actor_ref", REF, nullable=True),
        sa.Column("http_method", sa.String(10), nullable=True),
        sa.Column("path", sa.String(512), nullable=True),
        sa.Column("user_agent", sa.String(200), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_security_events")),
        _tenant_fk("security_events"),
        _credential_fk("security_events"),
        _one_of("security_events", "event_type", EVENT_TYPES),
        sa.CheckConstraint(
            "credential_id IS NULL OR tenant_id IS NOT NULL",
            name=op.f("ck_security_events_credential_tenant"),
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_security_events_occurred_at"), "security_events", ["occurred_at"], unique=False
    )
    # 两张表都还没有写入代码，库里没有指不到凭据的行。
    op.create_foreign_key(
        op.f("fk_idempotency_records_tenant_id_credentials"),
        "idempotency_records",
        "credentials",
        ["tenant_id", "credential_id"],
        ["tenant_id", "credential_id"],
    )
    op.create_foreign_key(
        op.f("fk_ledger_entries_tenant_id_credentials"),
        "ledger_entries",
        "credentials",
        ["tenant_id", "credential_id"],
        ["tenant_id", "credential_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_ledger_entries_tenant_id_credentials"), "ledger_entries", type_="foreignkey"
    )
    # ledger_entries 没有以 (tenant_id, credential_id) 开头的索引，
    # 加外键时 MySQL 按外键名隐式建了一个；删外键不会删它，这里删掉，回到 0002 的表结构。
    op.drop_index(op.f("fk_ledger_entries_tenant_id_credentials"), table_name="ledger_entries")
    # idempotency_records 的作用域唯一约束以 (tenant_id, credential_id) 开头，
    # 外键用它，没有隐式索引。
    op.drop_constraint(
        op.f("fk_idempotency_records_tenant_id_credentials"),
        "idempotency_records",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_security_events_occurred_at"), table_name="security_events")
    op.drop_table("security_events")
    op.drop_table("credential_tokens")
    op.drop_table("credentials")
