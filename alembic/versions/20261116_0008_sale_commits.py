"""sale_commits: reservation columns for sale commits and late payment review

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-16

契约 5.7 的存放。只加列、加约束（契约 2.2），不动已有的列与约束。
还没有写确认销售与待审结案的命令上线，库里的预留只有 active、released、expired，
新列取默认值 false 与空，各条检查约束对现有行都成立。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op
from app.domain.reservations import CLOSED_REFUNDED, COMMITTED, LATE_PAYMENT_REVIEW

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/orders.py 相同的列约定（契约 2.2）。
ID = sa.String(32, collation="utf8mb4_bin")
REF = sa.String(128, collation="utf8mb4_bin")
CODE32 = sa.String(32, collation="utf8mb4_bin")
HASH = sa.CHAR(64, collation="utf8mb4_bin")
TIMESTAMP = mysql.DATETIME(fsp=6)
ZERO = sa.text("0")
PAID_STATUSES = (COMMITTED, LATE_PAYMENT_REVIEW, CLOSED_REFUNDED)
COMMIT_COLUMNS = (
    "payment_ref",
    "paid_at",
    "commit_request_hash",
    "commit_credential_id",
    "commit_request_id",
)
REVIEW_CLOSED_COLUMNS = (
    "review_closed_at",
    "review_closed_credential_id",
    "review_closed_request_id",
)

COLUMNS = (
    ("payment_ref", REF),
    ("paid_at", TIMESTAMP),
    ("commit_request_hash", HASH),
    ("committed_at", TIMESTAMP),
    ("commit_credential_id", ID),
    ("commit_actor_ref", REF),
    ("commit_actor_role", CODE32),
    ("commit_request_id", REF),
    ("review_closed_at", TIMESTAMP),
    ("review_closed_credential_id", ID),
    ("review_closed_actor_ref", REF),
    ("review_closed_actor_role", CODE32),
    ("review_closed_request_id", REF),
)
FLAGS = ("late", "fault_mode")

# 都以 tenant_id 开头、指向 credentials，按命名约定会与已有外键同名，模型里显式命名。
FOREIGN_KEYS = {
    "fk_reservations_commit_credential_id_credentials": "commit_credential_id",
    "fk_reservations_review_closed_credential_id_credentials": "review_closed_credential_id",
}


def _in(column: str, values: Sequence[str]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


CHECKS = {
    "ck_reservations_commit_columns": " AND ".join(
        f"({_in('status', PAID_STATUSES)}) = ({column} IS NOT NULL)" for column in COMMIT_COLUMNS
    ),
    "ck_reservations_committed_at": f"(status = '{COMMITTED}') = (committed_at IS NOT NULL)",
    "ck_reservations_late": f"status = '{COMMITTED}' OR NOT late",
    "ck_reservations_review_closed_columns": " AND ".join(
        f"(status = '{CLOSED_REFUNDED}') = ({column} IS NOT NULL)"
        for column in REVIEW_CLOSED_COLUMNS
    ),
    "ck_reservations_paid_closed_at": (
        f"NOT ({_in('status', PAID_STATUSES)}) OR closed_at IS NOT NULL"
    ),
    "ck_reservations_commit_actor": "(commit_actor_ref IS NULL) = (commit_actor_role IS NULL)",
    "ck_reservations_review_closed_actor": (
        "(review_closed_actor_ref IS NULL) = (review_closed_actor_role IS NULL)"
    ),
}


def upgrade() -> None:
    for column, type_ in COLUMNS:
        op.add_column("reservations", sa.Column(column, type_, nullable=True))
    for column in FLAGS:
        op.add_column(
            "reservations",
            sa.Column(column, sa.Boolean(), server_default=ZERO, nullable=False),
        )
    for name, column in FOREIGN_KEYS.items():
        op.create_foreign_key(
            op.f(name),
            "reservations",
            "credentials",
            ["tenant_id", column],
            ["tenant_id", "credential_id"],
        )
    op.create_unique_constraint(
        op.f("uq_reservations_tenant_id_channel_id_payment_ref"),
        "reservations",
        ["tenant_id", "channel_id", "payment_ref"],
    )
    for name, condition in CHECKS.items():
        op.create_check_constraint(op.f(name), "reservations", condition)


def downgrade() -> None:
    # 检查约束引用的列删不掉，先删约束。
    for name in reversed(CHECKS):
        op.drop_constraint(op.f(name), "reservations", type_="check")
    # 0007 的 (tenant_id, channel_id, order_ref) 唯一约束仍支撑销售入口的外键，这条可以直接删。
    op.drop_constraint(
        op.f("uq_reservations_tenant_id_channel_id_payment_ref"), "reservations", type_="unique"
    )
    for name in reversed(FOREIGN_KEYS):
        op.drop_constraint(op.f(name), "reservations", type_="foreignkey")
        # 加外键时 MySQL 按外键名隐式建了索引；删外键不会删它，这里删掉（同 0005 的降级）。
        op.drop_index(op.f(name), table_name="reservations")
    for column in reversed(FLAGS):
        op.drop_column("reservations", column)
    for column, _ in reversed(COLUMNS):
        op.drop_column("reservations", column)
