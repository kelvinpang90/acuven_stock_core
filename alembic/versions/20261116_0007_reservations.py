"""reservations: reservations and order_lines

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16

契约 5.1、5.6 的存放。只建两张新表，不动流水类型约束，所以不按 core_instance 区分实例。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op
from app.domain.reservations import (
    ACTIVE,
    EXPIRED,
    LINE_QUANTITY_MAX,
    LINE_QUANTITY_MIN,
    RELEASE_REASONS,
    RELEASED,
    STATUSES,
    TTL_MAX,
    TTL_MIN,
)

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/orders.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
HASH = sa.CHAR(64, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
ZERO = sa.text("0")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}
RELEASE_COLUMNS = ("release_reason", "released_credential_id", "released_request_id")
LINE_ACCUMULATORS = (
    "committed_qty",
    "dispatched_qty",
    "refund_restocked_qty",
    "returned_sellable_qty",
    "returned_unsellable_qty",
)


def _in(column: str, values: Sequence[str]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def upgrade() -> None:
    op.create_table(
        "reservations",
        sa.Column("reservation_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("channel_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("order_ref", REF, nullable=False),
        sa.Column("status", CODE32, nullable=False),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=False),
        sa.Column("request_hash", HASH, nullable=False),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=True),
        sa.Column("actor_role", CODE32, nullable=True),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("release_reason", CODE32, nullable=True),
        sa.Column("released_credential_id", ID, nullable=True),
        sa.Column("released_actor_ref", REF, nullable=True),
        sa.Column("released_actor_role", CODE32, nullable=True),
        sa.Column("released_request_id", REF, nullable=True),
        sa.Column("closed_at", TIMESTAMP, nullable=True),
        sa.PrimaryKeyConstraint("reservation_id", name=op.f("pk_reservations")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], name=op.f("fk_reservations_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pool_id"],
            ["stock_pools.tenant_id", "stock_pools.pool_id"],
            name=op.f("fk_reservations_tenant_id_stock_pools"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["sales_channels.tenant_id", "sales_channels.channel_id"],
            name=op.f("fk_reservations_tenant_id_sales_channels"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name=op.f("fk_reservations_tenant_id_credentials"),
        ),
        # 与上一条按命名约定会同名，模型里显式命名。
        sa.ForeignKeyConstraint(
            ["tenant_id", "released_credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name=op.f("fk_reservations_released_credential_id_credentials"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_id",
            "order_ref",
            name=op.f("uq_reservations_tenant_id_channel_id_order_ref"),
        ),
        sa.UniqueConstraint(
            "tenant_id", "reservation_id", name=op.f("uq_reservations_tenant_id_reservation_id")
        ),
        sa.CheckConstraint(_in("status", STATUSES), name=op.f("ck_reservations_status")),
        sa.CheckConstraint(
            _in("release_reason", RELEASE_REASONS), name=op.f("ck_reservations_release_reason")
        ),
        sa.CheckConstraint(
            f"ttl_seconds BETWEEN {TTL_MIN} AND {TTL_MAX}", name=op.f("ck_reservations_ttl_seconds")
        ),
        sa.CheckConstraint(
            " AND ".join(
                f"(status = '{RELEASED}') = ({column} IS NOT NULL)" for column in RELEASE_COLUMNS
            ),
            name=op.f("ck_reservations_released_columns"),
        ),
        sa.CheckConstraint(
            f"(status <> '{ACTIVE}' OR closed_at IS NULL)"
            f" AND (status NOT IN ('{RELEASED}', '{EXPIRED}') OR closed_at IS NOT NULL)",
            name=op.f("ck_reservations_closed_at"),
        ),
        sa.CheckConstraint(
            "(actor_ref IS NULL) = (actor_role IS NULL)", name=op.f("ck_reservations_actor")
        ),
        sa.CheckConstraint(
            "(released_actor_ref IS NULL) = (released_actor_role IS NULL)",
            name=op.f("ck_reservations_released_actor"),
        ),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_reservations_expires_at")),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_reservations_status_expires_at"),
        "reservations",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_table(
        "order_lines",
        sa.Column("order_line_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("reservation_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("sku_id", ID, nullable=False),
        sa.Column("line_ref", REF, nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        *[
            sa.Column(column, sa.BigInteger(), server_default=ZERO, nullable=False)
            for column in LINE_ACCUMULATORS
        ],
        sa.PrimaryKeyConstraint("order_line_id", name=op.f("pk_order_lines")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], name=op.f("fk_order_lines_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "reservation_id"],
            ["reservations.tenant_id", "reservations.reservation_id"],
            name=op.f("fk_order_lines_tenant_id_reservations"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pool_id", "sku_id"],
            ["stock_balances.tenant_id", "stock_balances.pool_id", "stock_balances.sku_id"],
            name=op.f("fk_order_lines_tenant_id_stock_balances"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "reservation_id",
            "line_ref",
            name=op.f("uq_order_lines_tenant_id_reservation_id_line_ref"),
        ),
        sa.CheckConstraint(
            f"quantity BETWEEN {LINE_QUANTITY_MIN} AND {LINE_QUANTITY_MAX}",
            name=op.f("ck_order_lines_quantity"),
        ),
        *[
            sa.CheckConstraint(f"{column} >= 0", name=op.f(f"ck_order_lines_{column}_nonnegative"))
            for column in LINE_ACCUMULATORS
        ],
        sa.CheckConstraint(
            "committed_qty = 0 OR committed_qty = quantity",
            name=op.f("ck_order_lines_whole_commit"),
        ),
        sa.CheckConstraint(
            "dispatched_qty + refund_restocked_qty <= committed_qty",
            name=op.f("ck_order_lines_dispatched_restocked_within_committed"),
        ),
        sa.CheckConstraint(
            "returned_sellable_qty + returned_unsellable_qty <= dispatched_qty",
            name=op.f("ck_order_lines_returned_within_dispatched"),
        ),
        **TABLE_OPTIONS,
    )


def downgrade() -> None:
    op.drop_table("order_lines")
    op.drop_index(op.f("ix_reservations_status_expires_at"), table_name="reservations")
    op.drop_table("reservations")
