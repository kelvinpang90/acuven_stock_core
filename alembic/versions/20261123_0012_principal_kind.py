"""principal_kind: credentials become principals of kind service, admin or system

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-23

契约第 10 节第 4 条的操作主体。credentials 加列 kind，默认 service，已有的行都是 service；
原来的 scopes 检查约束换成按种类的：service 的 scopes 是非空数组，admin 与 system 的是空数组、
不带销售入口。MySQL 的检查约束不能同名并存，先删原约束，新约束沿用 ck_credentials_scopes。
已有的行都是 service、scopes 非空，新约束对它们照样成立，所以不按 core_instance 区分实例。
credential_tokens 不加约束：令牌只由命令行签给 service，鉴权也只认 service。

降级只用于 CI 与开发库：库里有 admin 或 system 主体时恢复不了原约束，在改动任何东西之前失败。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.domain.service_auth import ADMIN, PRINCIPAL_KINDS, SERVICE, SYSTEM

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/models/service_auth.py 相同的列约定（契约 2.2）。
STATUS = sa.String(16, collation="utf8mb4_bin")
NO_SCOPE_KINDS = (ADMIN, SYSTEM)
OLD_SCOPES = "JSON_TYPE(scopes) = 'ARRAY' AND JSON_LENGTH(scopes) > 0"


def _in(column: str, values: Sequence[str]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


# kind 为未知值时下面两条不拦，只由 kind 那条拒绝。
SCOPES_BY_KIND = (
    "JSON_TYPE(scopes) = 'ARRAY'"
    f" AND (kind <> '{SERVICE}' OR JSON_LENGTH(scopes) > 0)"
    f" AND (NOT ({_in('kind', NO_SCOPE_KINDS)}) OR JSON_LENGTH(scopes) = 0)"
)
CHANNEL_BY_KIND = f"NOT ({_in('kind', NO_SCOPE_KINDS)}) OR channel_id IS NULL"


def upgrade() -> None:
    op.add_column(
        "credentials",
        sa.Column("kind", STATUS, server_default=SERVICE, nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_credentials_kind"), "credentials", _in("kind", PRINCIPAL_KINDS)
    )
    op.drop_constraint(op.f("ck_credentials_scopes"), "credentials", type_="check")
    op.create_check_constraint(op.f("ck_credentials_scopes"), "credentials", SCOPES_BY_KIND)
    op.create_check_constraint(op.f("ck_credentials_channel"), "credentials", CHANNEL_BY_KIND)


def downgrade() -> None:
    # MySQL 的 DDL 不在事务里：先查，免得删了列与约束之后才在恢复原约束时失败、库停在半路。
    others = op.get_bind().execute(
        sa.text(f"SELECT COUNT(*) FROM credentials WHERE kind <> '{SERVICE}'")
    )
    if others.scalar_one():
        raise RuntimeError(
            "credentials has admin or system principals; cannot downgrade below 0012"
        )
    op.drop_constraint(op.f("ck_credentials_channel"), "credentials", type_="check")
    op.drop_constraint(op.f("ck_credentials_scopes"), "credentials", type_="check")
    op.drop_constraint(op.f("ck_credentials_kind"), "credentials", type_="check")
    op.drop_column("credentials", "kind")
    op.create_check_constraint(op.f("ck_credentials_scopes"), "credentials", OLD_SCOPES)
