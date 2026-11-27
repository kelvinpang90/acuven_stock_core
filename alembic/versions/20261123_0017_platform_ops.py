"""platform_ops: platform connections, write pause, manual checks, alerts, reconciliation runs and
the platform operations log

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-23

契约 3.2 的存放（0.22，Q-99、Q-101）。建表 platform_connections、platform_write_pause、
manual_checks、platform_alerts、reconciliation_runs、platform_ops_log；
platform_connections 另有契约列表里没写的 mode_changed_at（人工核对逾期从切换时刻算起要用它）。
库里已有的每个 platform 入口补建它的连接行、有 platform 入口的租户补建写入暂停行
（只在开发库与 CI 上可能有），之后由开通入口的命令行在同一事务里建。
只建新表（契约 2.2），不动已有的表与约束，所以不按 core_instance 区分实例。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/platform_ops.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
STATUS = sa.String(16, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
CODE64 = sa.String(64, collation="utf8mb4_bin")
NOTE = sa.String(500)
CIPHERTEXT = sa.VARBINARY(4096)
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")
ZERO = sa.text("0")
EMPTY_OBJECT = sa.text("(JSON_OBJECT())")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}

MODES = ("manual", "auto")
CONNECTION_STATUSES = ("not_connected", "active", "auth_failed", "revoked")
APP_PROFILES = ("self_seller",)
SET_COLUMNS = ("set_credential_id", "set_at", "set_reason")
CLEARED_COLUMNS = ("cleared_credential_id", "cleared_at", "cleared_reason")
TRIGGERS = ("scheduled", "manual")
OPS_ACTIONS = (
    "write_pause_set",
    "write_pause_cleared",
    "review_hold_set",
    "review_hold_cleared",
    "manual_check_recorded",
    "manual_quota_confirmed",
    "known_discrepancy_marked",
    "held_order_closed",
    "attention_cleared",
    "alert_acknowledged",
    "incident_decision",
    "reconciliation_started",
    "connection_authorized",
    "connection_disconnected",
    "connection_mode_changed",
    "connection_status_changed",
)
ACTOR_CHECK = "(actor_ref IS NULL) = (actor_role IS NULL)"


def _in(column: str, values: Sequence[str]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def _check(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _same_nullness(columns: Sequence[str]) -> str:
    first = columns[0]
    return " AND ".join(f"({first} IS NULL) = ({column} IS NULL)" for column in columns[1:])


def _channel_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "channel_id"],
        ["sales_channels.tenant_id", "sales_channels.channel_id"],
        name=op.f(f"fk_{table}_tenant_id_sales_channels"),
    )


def _credential_fk(table: str, column: str, name: str | None = None) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", column],
        ["credentials.tenant_id", "credentials.credential_id"],
        name=op.f(name or f"fk_{table}_tenant_id_credentials"),
    )


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], name=op.f(f"fk_{table}_tenant_id_tenants")
    )


def upgrade() -> None:
    op.create_table(
        "platform_connections",
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("channel_id", ID, nullable=False),
        sa.Column("mode", STATUS, server_default="manual", nullable=False),
        sa.Column("mode_changed_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("status", STATUS, server_default="not_connected", nullable=False),
        sa.Column("app_profile", STATUS, server_default="self_seller", nullable=False),
        sa.Column("capabilities", sa.JSON(), server_default=EMPTY_OBJECT, nullable=False),
        sa.Column("access_token_ciphertext", CIPHERTEXT, nullable=True),
        sa.Column("access_token_expires_at", TIMESTAMP, nullable=True),
        sa.Column("refresh_token_ciphertext", CIPHERTEXT, nullable=True),
        sa.Column("refresh_token_expires_at", TIMESTAMP, nullable=True),
        sa.Column("pull_cursor", TIMESTAMP, nullable=True),
        sa.Column("next_pull_at", TIMESTAMP, nullable=True),
        sa.Column("last_refreshed_at", TIMESTAMP, nullable=True),
        sa.Column("last_synced_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "channel_id", name=op.f("pk_platform_connections")),
        _channel_fk("platform_connections"),
        _check("platform_connections", "mode", _in("mode", MODES)),
        _check("platform_connections", "status", _in("status", CONNECTION_STATUSES)),
        _check("platform_connections", "app_profile", _in("app_profile", APP_PROFILES)),
        _check("platform_connections", "capabilities", "JSON_TYPE(capabilities) = 'OBJECT'"),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "platform_write_pause",
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("paused", sa.Boolean(), server_default=ZERO, nullable=False),
        sa.Column("set_credential_id", ID, nullable=True),
        sa.Column("set_at", TIMESTAMP, nullable=True),
        sa.Column("set_reason", NOTE, nullable=True),
        sa.Column("cleared_credential_id", ID, nullable=True),
        sa.Column("cleared_at", TIMESTAMP, nullable=True),
        sa.Column("cleared_reason", NOTE, nullable=True),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_platform_write_pause")),
        _tenant_fk("platform_write_pause"),
        _credential_fk(
            "platform_write_pause", "set_credential_id", "fk_platform_write_pause_set_credential"
        ),
        _credential_fk(
            "platform_write_pause",
            "cleared_credential_id",
            "fk_platform_write_pause_cleared_credential",
        ),
        _check("platform_write_pause", "set_columns", _same_nullness(SET_COLUMNS)),
        _check("platform_write_pause", "cleared_columns", _same_nullness(CLEARED_COLUMNS)),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "manual_checks",
        sa.Column("manual_check_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("channel_id", ID, nullable=False),
        sa.Column("checked_from", TIMESTAMP, nullable=False),
        sa.Column("checked_until", TIMESTAMP, nullable=False),
        sa.Column("found_discrepancy", sa.Boolean(), nullable=False),
        sa.Column("note", NOTE, nullable=True),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=True),
        sa.Column("actor_role", CODE32, nullable=True),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("manual_check_id", name=op.f("pk_manual_checks")),
        _channel_fk("manual_checks"),
        _credential_fk("manual_checks", "credential_id"),
        _check("manual_checks", "checked_range", "checked_from < checked_until"),
        _check("manual_checks", "actor", ACTOR_CHECK),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_manual_checks_tenant_id_channel_id_checked_until"),
        "manual_checks",
        ["tenant_id", "channel_id", "checked_until"],
        unique=False,
    )
    op.create_table(
        "platform_alerts",
        sa.Column("alert_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("kind", CODE32, nullable=False),
        sa.Column("channel_id", ID, nullable=True),
        sa.Column("sku_id", ID, nullable=True),
        sa.Column("platform_order_row_id", ID, nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("first_sent_at", TIMESTAMP, nullable=True),
        sa.Column("last_notified_at", TIMESTAMP, nullable=True),
        sa.Column("notify_count", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("acked_credential_id", ID, nullable=True),
        sa.Column("acked_at", TIMESTAMP, nullable=True),
        sa.Column("escalated_at", TIMESTAMP, nullable=True),
        sa.PrimaryKeyConstraint("alert_id", name=op.f("pk_platform_alerts")),
        _tenant_fk("platform_alerts"),
        _channel_fk("platform_alerts"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "sku_id"],
            ["skus.tenant_id", "skus.sku_id"],
            name=op.f("fk_platform_alerts_tenant_id_skus"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "platform_order_row_id"],
            ["platform_orders.tenant_id", "platform_orders.platform_order_row_id"],
            name=op.f("fk_platform_alerts_tenant_id_platform_orders"),
        ),
        _credential_fk("platform_alerts", "acked_credential_id"),
        _check("platform_alerts", "detail", "JSON_TYPE(detail) = 'OBJECT'"),
        _check("platform_alerts", "notify_count", "notify_count >= 0"),
        _check("platform_alerts", "acked", "(acked_credential_id IS NULL) = (acked_at IS NULL)"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_platform_alerts_tenant_id_acked_at_created_at"),
        "platform_alerts",
        ["tenant_id", "acked_at", "created_at"],
        unique=False,
    )
    op.create_table(
        "reconciliation_runs",
        sa.Column("run_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("trigger", STATUS, nullable=False),
        sa.Column("credential_id", ID, nullable=True),
        sa.Column("started_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("finished_at", TIMESTAMP, nullable=True),
        sa.Column("discrepancy_count", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_reconciliation_runs")),
        _tenant_fk("reconciliation_runs"),
        _credential_fk("reconciliation_runs", "credential_id"),
        _check("reconciliation_runs", "trigger", _in("`trigger`", TRIGGERS)),
        _check(
            "reconciliation_runs",
            "manual_credential",
            "`trigger` <> 'manual' OR credential_id IS NOT NULL",
        ),
        _check(
            "reconciliation_runs",
            "finished",
            "(finished_at IS NULL) = (discrepancy_count IS NULL)",
        ),
        _check("reconciliation_runs", "discrepancy_count", "discrepancy_count >= 0"),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "platform_ops_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("action", CODE32, nullable=False),
        sa.Column("object_type", CODE32, nullable=True),
        sa.Column("object_id", CODE64, nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("reason", NOTE, nullable=True),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=True),
        sa.Column("actor_role", CODE32, nullable=True),
        sa.Column("request_id", REF, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_ops_log")),
        _credential_fk("platform_ops_log", "credential_id"),
        _check("platform_ops_log", "action", _in("action", OPS_ACTIONS)),
        _check("platform_ops_log", "before", "JSON_TYPE(`before`) = 'OBJECT'"),
        _check("platform_ops_log", "after", "JSON_TYPE(`after`) = 'OBJECT'"),
        _check("platform_ops_log", "actor", ACTOR_CHECK),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_platform_ops_log_tenant_id_id"),
        "platform_ops_log",
        ["tenant_id", "id"],
        unique=False,
    )
    # 已有的 platform 入口补建连接行（全取默认值），它们的租户补建写入暂停行（paused 为假）。
    op.execute(
        "INSERT INTO platform_connections (tenant_id, channel_id)"
        " SELECT tenant_id, channel_id FROM sales_channels WHERE kind = 'platform'"
    )
    op.execute(
        "INSERT INTO platform_write_pause (tenant_id)"
        " SELECT DISTINCT tenant_id FROM sales_channels WHERE kind = 'platform'"
    )


def downgrade() -> None:
    # 先删引用别表的；索引随表一并删掉。
    op.drop_table("platform_ops_log")
    op.drop_table("reconciliation_runs")
    op.drop_table("platform_alerts")
    op.drop_table("manual_checks")
    op.drop_table("platform_write_pause")
    op.drop_table("platform_connections")
