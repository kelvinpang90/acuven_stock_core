"""catalog: catalog_imports, catalog_changes

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/catalog.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
STATUS = sa.String(16, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}
CONFIRMATION_COLUMNS = (
    "result",
    "confirmed_credential_id",
    "confirmed_actor_ref",
    "confirmed_request_id",
    "confirmed_at",
)


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], name=op.f(f"fk_{table}_tenant_id_tenants")
    )


def upgrade() -> None:
    op.create_table(
        "catalog_imports",
        sa.Column("import_id", ID, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=False),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.Column("confirmed_credential_id", ID, nullable=True),
        sa.Column("confirmed_actor_ref", REF, nullable=True),
        sa.Column("confirmed_request_id", REF, nullable=True),
        sa.Column("confirmed_at", TIMESTAMP, nullable=True),
        sa.PrimaryKeyConstraint("import_id", name=op.f("pk_catalog_imports")),
        _tenant_fk("catalog_imports"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name=op.f("fk_catalog_imports_tenant_id_credentials"),
        ),
        # 与上一条按命名约定会同名，模型里显式命名。
        sa.ForeignKeyConstraint(
            ["tenant_id", "confirmed_credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name=op.f("fk_catalog_imports_confirmed_credential_id_credentials"),
        ),
        sa.UniqueConstraint(
            "tenant_id", "import_id", name=op.f("uq_catalog_imports_tenant_id_import_id")
        ),
        sa.CheckConstraint(
            "status IN ('previewed', 'confirmed')", name=op.f("ck_catalog_imports_status")
        ),
        # rows 是 MySQL 8 的保留字。
        sa.CheckConstraint("JSON_TYPE(`rows`) = 'ARRAY'", name=op.f("ck_catalog_imports_rows")),
        sa.CheckConstraint(
            " AND ".join(
                f"(status = 'confirmed') = ({column} IS NOT NULL)"
                for column in CONFIRMATION_COLUMNS
            ),
            name=op.f("ck_catalog_imports_confirmation"),
        ),
        **TABLE_OPTIONS,
    )
    op.create_table(
        "catalog_changes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", ID, nullable=False),
        sa.Column("event_type", CODE32, nullable=False),
        sa.Column("object_type", STATUS, nullable=False),
        sa.Column("object_id", ID, nullable=False),
        sa.Column("product_id", ID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("credential_id", ID, nullable=False),
        sa.Column("actor_ref", REF, nullable=False),
        sa.Column("actor_role", CODE32, nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("import_id", ID, nullable=True),
        sa.Column("request_id", REF, nullable=False),
        sa.Column("idempotency_key", REF, nullable=False),
        sa.Column("created_at", TIMESTAMP, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_changes")),
        _tenant_fk("catalog_changes"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["products.tenant_id", "products.product_id"],
            name=op.f("fk_catalog_changes_tenant_id_products"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name=op.f("fk_catalog_changes_tenant_id_credentials"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "import_id"],
            ["catalog_imports.tenant_id", "catalog_imports.import_id"],
            name=op.f("fk_catalog_changes_tenant_id_catalog_imports"),
        ),
        sa.CheckConstraint(
            "object_type IN ('product', 'sku')", name=op.f("ck_catalog_changes_object_type")
        ),
        sa.CheckConstraint(
            "object_type <> 'product' OR object_id = product_id",
            name=op.f("ck_catalog_changes_product_object"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_catalog_changes_version")),
        sa.CheckConstraint(
            "JSON_TYPE(changes) = 'OBJECT'", name=op.f("ck_catalog_changes_changes")
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        op.f("ix_catalog_changes_tenant_id_id"),
        "catalog_changes",
        ["tenant_id", "id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_catalog_changes_tenant_id_object_id_id"),
        "catalog_changes",
        ["tenant_id", "object_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    # 两个索引都以 tenant_id 开头，指向 tenants 的外键要用其中一个，不能先删索引；
    # 删表时一并删掉。
    op.drop_table("catalog_changes")
    op.drop_table("catalog_imports")
