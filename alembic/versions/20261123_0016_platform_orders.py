"""platform_orders: platform orders, their lines and the platform event inbox

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-23

契约 5.10 的存放，以及 3.2 中落在 platform_order_lines 上的已知差异（0.22）。
建表 platform_orders、platform_order_lines、platform_events。
平台给的 ID 与键、由它们派生的 order_ref 用 utf8mb4_bin，不做字符集检查。
只建新表（契约 2.2），不动已有的表与约束，所以不按 core_instance 区分实例。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/platform.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
STATUS = sa.String(16, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
CODE64 = sa.String(64, collation="utf8mb4_bin")
PLATFORM_KEY = sa.String(128, collation="utf8mb4_bin")
LINE_REF = sa.String(32, collation="utf8mb4_bin")
HASH = sa.CHAR(64, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")
ZERO = sa.text("0")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}

STAGES = ("unpaid", "held", "committed", "cancelled")
EVENT_ACTIONS = (
    "created",
    "ready_to_ship",
    "shipped",
    "cancelled",
    "return_requested",
    "return_received",
)
EVENT_SOURCES = ("webhook", "pull", "manual")
EVENT_RESULTS = ("applied", "ignored", "held", "attention")
MANUAL_COLUMNS = ("manual_key", "request_hash")


def _in(column: str, values: Sequence[str]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def _not_empty(table: str, column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"CHAR_LENGTH({column}) > 0", name=op.f(f"ck_{table}_{column}_not_empty")
    )


def _channel_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "channel_id"],
        ["sales_channels.tenant_id", "sales_channels.channel_id"],
        name=op.f(f"fk_{table}_tenant_id_sales_channels"),
    )


def upgrade() -> None:
    op.create_table(
        "platform_orders",
        sa.Column("platform_order_row_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("channel_id", ID, nullable=False),
        sa.Column("platform_order_id", PLATFORM_KEY, nullable=False),
        sa.Column("order_ref", PLATFORM_KEY, nullable=False),
        sa.Column("stage", STATUS, server_default="unpaid", nullable=False),
        sa.Column("attention", CODE64, nullable=True),
        sa.Column("attention_code", CODE64, nullable=True),
        sa.Column("ready_at", TIMESTAMP, nullable=True),
        sa.Column("next_pull_at", TIMESTAMP, nullable=True),
        sa.Column("pull_attempts", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("platform_order_row_id", name=op.f("pk_platform_orders")),
        _channel_fk("platform_orders"),
        sa.UniqueConstraint(
            "tenant_id",
            "platform_order_row_id",
            name=op.f("uq_platform_orders_tenant_id_platform_order_row_id"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_id",
            "platform_order_id",
            name=op.f("uq_platform_orders_tenant_id_channel_id_platform_order_id"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_id",
            "order_ref",
            name=op.f("uq_platform_orders_tenant_id_channel_id_order_ref"),
        ),
        _not_empty("platform_orders", "platform_order_id"),
        sa.CheckConstraint(
            "CHAR_LENGTH(order_ref) BETWEEN 1 AND 112",
            name=op.f("ck_platform_orders_order_ref_length"),
        ),
        sa.CheckConstraint(_in("stage", STAGES), name=op.f("ck_platform_orders_stage")),
        _not_empty("platform_orders", "attention"),
        _not_empty("platform_orders", "attention_code"),
        sa.CheckConstraint(
            "stage <> 'held' OR attention IS NOT NULL",
            name=op.f("ck_platform_orders_held_attention"),
        ),
        sa.CheckConstraint(
            "attention_code IS NULL OR attention IS NOT NULL",
            name=op.f("ck_platform_orders_attention_code_needs_attention"),
        ),
        sa.CheckConstraint("pull_attempts >= 0", name=op.f("ck_platform_orders_pull_attempts")),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "platform_order_lines",
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("platform_order_row_id", ID, nullable=False),
        sa.Column("platform_line_id", PLATFORM_KEY, nullable=False),
        sa.Column("platform_item_id", PLATFORM_KEY, nullable=False),
        sa.Column("platform_model_id", PLATFORM_KEY, nullable=False),
        sa.Column("sku_id", ID, nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("cancelled_qty", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("quota_taken", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("quota_returned", sa.Integer(), server_default=ZERO, nullable=False),
        sa.Column("line_ref", LINE_REF, nullable=True),
        sa.Column("known_discrepancy", sa.Boolean(), server_default=ZERO, nullable=False),
        sa.Column("known_discrepancy_note", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "platform_order_row_id",
            "platform_line_id",
            name=op.f("pk_platform_order_lines"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "platform_order_row_id"],
            ["platform_orders.tenant_id", "platform_orders.platform_order_row_id"],
            name=op.f("fk_platform_order_lines_tenant_id_platform_orders"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "sku_id"],
            ["skus.tenant_id", "skus.sku_id"],
            name=op.f("fk_platform_order_lines_tenant_id_skus"),
        ),
        _not_empty("platform_order_lines", "platform_line_id"),
        _not_empty("platform_order_lines", "platform_item_id"),
        _not_empty("platform_order_lines", "platform_model_id"),
        sa.CheckConstraint(
            "quantity BETWEEN 1 AND 1000000", name=op.f("ck_platform_order_lines_quantity")
        ),
        sa.CheckConstraint(
            "cancelled_qty >= 0 AND cancelled_qty <= quantity",
            name=op.f("ck_platform_order_lines_cancelled_qty"),
        ),
        sa.CheckConstraint(
            "quota_returned >= 0 AND quota_returned <= quota_taken AND quota_taken <= quantity",
            name=op.f("ck_platform_order_lines_quota_taken_returned"),
        ),
        sa.CheckConstraint(
            "(known_discrepancy <> 0) = (known_discrepancy_note IS NOT NULL"
            " AND CHAR_LENGTH(known_discrepancy_note) > 0)",
            name=op.f("ck_platform_order_lines_known_discrepancy"),
        ),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "platform_events",
        sa.Column("event_row_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("channel_id", ID, nullable=False),
        sa.Column("platform_order_id", PLATFORM_KEY, nullable=False),
        sa.Column("platform_line_id", PLATFORM_KEY, nullable=False),
        sa.Column("action", CODE32, nullable=False),
        sa.Column("event_key", PLATFORM_KEY, nullable=False),
        sa.Column("source", STATUS, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("manual_key", REF, nullable=True),
        sa.Column("request_hash", HASH, nullable=True),
        sa.Column("received_at", TIMESTAMP, nullable=False),
        sa.Column("applied_at", TIMESTAMP, nullable=True),
        sa.Column("result", STATUS, nullable=True),
        sa.Column("result_code", CODE64, nullable=True),
        sa.Column("credential_id", ID, nullable=False),
        sa.PrimaryKeyConstraint("event_row_id", name=op.f("pk_platform_events")),
        _channel_fk("platform_events"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name=op.f("fk_platform_events_tenant_id_credentials"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_id",
            "platform_order_id",
            "platform_line_id",
            "action",
            "event_key",
            name=op.f("uq_platform_events_dedupe_key"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "credential_id",
            "manual_key",
            name=op.f("uq_platform_events_tenant_id_credential_id_manual_key"),
        ),
        _not_empty("platform_events", "platform_order_id"),
        sa.CheckConstraint(_in("action", EVENT_ACTIONS), name=op.f("ck_platform_events_action")),
        _not_empty("platform_events", "event_key"),
        sa.CheckConstraint(_in("source", EVENT_SOURCES), name=op.f("ck_platform_events_source")),
        sa.CheckConstraint(
            "JSON_TYPE(payload) = 'OBJECT'", name=op.f("ck_platform_events_payload")
        ),
        sa.CheckConstraint(
            " AND ".join(
                f"(source = 'manual') = ({column} IS NOT NULL)" for column in MANUAL_COLUMNS
            ),
            name=op.f("ck_platform_events_manual_columns"),
        ),
        *(_not_empty("platform_events", column) for column in MANUAL_COLUMNS),
        sa.CheckConstraint(
            _in("result", EVENT_RESULTS), name=op.f("ck_platform_events_result_value")
        ),
        sa.CheckConstraint(
            "(applied_at IS NULL) = (result IS NULL)",
            name=op.f("ck_platform_events_applied_result"),
        ),
        _not_empty("platform_events", "result_code"),
        sa.CheckConstraint(
            "CASE WHEN result = 'applied' THEN TRUE"
            " WHEN result IN ('held', 'attention') THEN result_code IS NOT NULL"
            " ELSE result_code IS NULL END",
            name=op.f("ck_platform_events_result_code_by_result"),
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_platform_events_order_applied_at"),
        "platform_events",
        ["tenant_id", "channel_id", "platform_order_id", "applied_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_platform_events_applied_at_tenant_id_channel_id"),
        "platform_events",
        ["applied_at", "tenant_id", "channel_id"],
        unique=False,
    )


def downgrade() -> None:
    # 先删引用别表的；索引随表一并删掉。
    op.drop_table("platform_events")
    op.drop_table("platform_order_lines")
    op.drop_table("platform_orders")
