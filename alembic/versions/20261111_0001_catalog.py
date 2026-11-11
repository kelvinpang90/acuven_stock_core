"""catalog: tenants, products, skus, sku_code_history, sku_code_claims

Revision ID: 0001
Revises:
Create Date: 2026-11-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/catalog.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
STATUS = sa.String(16, collation="utf8mb4_bin")
CODE = sa.String(64, collation="utf8mb4_0900_ai_ci")
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}
STATUS_IN = "status IN ('active', 'inactive')"


def _trimmed(column: str) -> str:
    return f"{column} <> '' AND TRIM({column}) = {column}"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_tenants")),
        sa.CheckConstraint(STATUS_IN, name=op.f("ck_tenants_status")),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "products",
        sa.Column("product_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("product_id", name=op.f("pk_products")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], name=op.f("fk_products_tenant_id_tenants")
        ),
        sa.UniqueConstraint(
            "tenant_id", "product_id", name=op.f("uq_products_tenant_id_product_id")
        ),
        sa.CheckConstraint(STATUS_IN, name=op.f("ck_products_status")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_products_version")),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "skus",
        sa.Column("sku_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("product_id", ID, nullable=False),
        sa.Column("sku_code", CODE, nullable=False),
        sa.Column("barcode", CODE, nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("reference_price_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("reference_price_currency", sa.CHAR(3), nullable=True),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("merged_into_sku_id", ID, nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("updated_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("sku_id", name=op.f("pk_skus")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["products.tenant_id", "products.product_id"],
            name=op.f("fk_skus_tenant_id_products"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merged_into_sku_id"],
            ["skus.tenant_id", "skus.sku_id"],
            name=op.f("fk_skus_tenant_id_skus"),
        ),
        sa.UniqueConstraint("tenant_id", "sku_id", name=op.f("uq_skus_tenant_id_sku_id")),
        sa.UniqueConstraint("tenant_id", "sku_code", name=op.f("uq_skus_tenant_id_sku_code")),
        sa.UniqueConstraint("tenant_id", "barcode", name=op.f("uq_skus_tenant_id_barcode")),
        sa.CheckConstraint(STATUS_IN, name=op.f("ck_skus_status")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_skus_version")),
        sa.CheckConstraint(_trimmed("sku_code"), name=op.f("ck_skus_sku_code_trimmed")),
        sa.CheckConstraint(_trimmed("barcode"), name=op.f("ck_skus_barcode_trimmed")),
        sa.CheckConstraint(
            "(reference_price_amount_minor IS NULL) = (reference_price_currency IS NULL)",
            name=op.f("ck_skus_reference_price_pair"),
        ),
        sa.CheckConstraint(
            "reference_price_amount_minor >= 0", name=op.f("ck_skus_reference_price_nonnegative")
        ),
        sa.CheckConstraint("merged_into_sku_id <> sku_id", name=op.f("ck_skus_merged_not_self")),
        sa.CheckConstraint(
            "merged_into_sku_id IS NULL OR status = 'inactive'",
            name=op.f("ck_skus_merged_inactive"),
        ),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "sku_code_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("sku_id", ID, nullable=False),
        sa.Column("old_code", CODE, nullable=False),
        sa.Column("new_code", CODE, nullable=False),
        sa.Column("actor_ref", sa.String(128, collation="utf8mb4_bin"), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sku_code_history")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "sku_id"],
            ["skus.tenant_id", "skus.sku_id"],
            name=op.f("fk_sku_code_history_tenant_id_skus"),
        ),
        sa.CheckConstraint(_trimmed("old_code"), name=op.f("ck_sku_code_history_old_code_trimmed")),
        sa.CheckConstraint(_trimmed("new_code"), name=op.f("ck_sku_code_history_new_code_trimmed")),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "sku_code_claims",
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("code", CODE, nullable=False),
        sa.Column("sku_id", ID, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "code", name=op.f("pk_sku_code_claims")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "sku_id"],
            ["skus.tenant_id", "skus.sku_id"],
            name=op.f("fk_sku_code_claims_tenant_id_skus"),
        ),
        sa.CheckConstraint(_trimmed("code"), name=op.f("ck_sku_code_claims_code_trimmed")),
        **TABLE_OPTIONS,
    )


def downgrade() -> None:
    op.drop_table("sku_code_claims")
    op.drop_table("sku_code_history")
    op.drop_table("skus")
    op.drop_table("products")
    op.drop_table("tenants")
