"""order_documents: dispatch, refund restock and return receipt documents

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-18

契约 5.8 的存放。只建一张新表（契约 2.2），不动已有的表与约束，所以不按 core_instance 区分实例。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op
from app.domain.fulfilment import DOCUMENT_TYPES

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/orders.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
HASH = sa.CHAR(64, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}


def _in(column: str, values: Sequence[str]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def upgrade() -> None:
    op.create_table(
        "order_documents",
        # 主键上的 BIGINT 自增（契约 5.8）。
        sa.Column("document_seq", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("document_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("channel_id", ID, nullable=False),
        sa.Column("pool_id", ID, nullable=False),
        sa.Column("reservation_id", ID, nullable=False),
        sa.Column("document_type", CODE32, nullable=False),
        sa.Column("document_ref", REF, nullable=False),
        sa.Column("request_hash", HASH, nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=True),
        sa.Column("actor_role", CODE32, nullable=True),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("document_seq", name=op.f("pk_order_documents")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], name=op.f("fk_order_documents_tenant_id_tenants")
        ),
        # 四条复合外键在模型里显式命名。
        sa.ForeignKeyConstraint(
            ["tenant_id", "reservation_id"],
            ["reservations.tenant_id", "reservations.reservation_id"],
            name=op.f("fk_order_documents_reservation_id_reservations"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pool_id"],
            ["stock_pools.tenant_id", "stock_pools.pool_id"],
            name=op.f("fk_order_documents_pool_id_stock_pools"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["sales_channels.tenant_id", "sales_channels.channel_id"],
            name=op.f("fk_order_documents_channel_id_sales_channels"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name=op.f("fk_order_documents_credential_id_credentials"),
        ),
        sa.UniqueConstraint("document_id", name=op.f("uq_order_documents_document_id")),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_id",
            "document_type",
            "document_ref",
            name=op.f("uq_order_documents_tenant_id_channel_id_document_type_document_ref"),
        ),
        sa.CheckConstraint(
            _in("document_type", DOCUMENT_TYPES), name=op.f("ck_order_documents_document_type")
        ),
        sa.CheckConstraint("JSON_TYPE(result) = 'OBJECT'", name=op.f("ck_order_documents_result")),
        sa.CheckConstraint(
            "(actor_ref IS NULL) = (actor_role IS NULL)", name=op.f("ck_order_documents_actor")
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_order_documents_tenant_id_reservation_id_document_seq"),
        "order_documents",
        ["tenant_id", "reservation_id", "document_seq"],
        unique=False,
    )


def downgrade() -> None:
    # 这条索引支撑指向预留的外键，不能先单独删；删表时一并删掉。
    op.drop_table("order_documents")
