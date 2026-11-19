"""reservation_last_changed_at: last change time of a reservation and its order lines

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-19

契约 5.9 的存放。只加列、加索引（契约 2.2），不动已有的列与约束，所以不按 core_instance 区分实例。
加列时以 CURRENT_TIMESTAMP(6) 为默认值填已有的行，再在同一迁移里回填：
取 created_at、closed_at、committed_at、review_closed_at 与该预留 order_documents.created_at
中最大的一个（空值不计）。转入待审的时刻没有单独记，回填值可能早于真实的最后变更时间。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/orders.py 相同的列约定（契约 2.2）。
TIMESTAMP = mysql.DATETIME(fsp=6)
NOW = sa.text("CURRENT_TIMESTAMP(6)")

# 按命名约定的名字超过 64 字符，模型里显式取短名。
INDEXES = {
    "ix_reservations_channel_last_changed_at": [
        "tenant_id",
        "channel_id",
        "last_changed_at",
        "reservation_id",
    ],
    "ix_reservations_channel_status": ["tenant_id", "channel_id", "status", "reservation_id"],
}

# GREATEST 遇到 NULL 返回 NULL：可空的各项以非空的 created_at 代替，等于不计。
BACKFILL = """
UPDATE reservations AS r
SET r.last_changed_at = GREATEST(
    r.created_at,
    COALESCE(r.closed_at, r.created_at),
    COALESCE(r.committed_at, r.created_at),
    COALESCE(r.review_closed_at, r.created_at),
    COALESCE(
        (
            SELECT MAX(d.created_at)
            FROM order_documents AS d
            WHERE d.tenant_id = r.tenant_id AND d.reservation_id = r.reservation_id
        ),
        r.created_at
    )
)
"""


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("last_changed_at", TIMESTAMP, server_default=NOW, nullable=False),
    )
    op.execute(BACKFILL)
    for name, columns in INDEXES.items():
        op.create_index(op.f(name), "reservations", columns, unique=False)


def downgrade() -> None:
    # 两条索引都不支撑外键：(tenant_id, channel_id) 的外键由 0007 的 order_ref 唯一约束支撑。
    for name in reversed(INDEXES):
        op.drop_index(op.f(name), table_name="reservations")
    op.drop_column("reservations", "last_changed_at")
