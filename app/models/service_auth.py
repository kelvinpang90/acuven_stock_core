"""服务凭据、令牌与安全事件的表（契约 7.2、第 10 节第 1 条与第 4 条）。

只有表结构与数据库约束。scope 的取值、令牌是否可用、池授权等由应用判断
（app/domain/service_auth.py）；签发、轮换、吊销与写安全事件由命令行与服务层做。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.domain.service_auth import ADMIN, PRINCIPAL_KINDS, SERVICE, SYSTEM
from app.models.catalog import EXACT_COLLATION, ID, NOW, STATUS, TABLE_OPTIONS, TIMESTAMP
from app.models.inventory import CODE32, REF

# 契约 7.2 的十种事件类型；轮换记为 token_issued，不另设类型。
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


def _in(column: str, values: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def _one_of(column: str, values: tuple[str, ...]) -> CheckConstraint:
    return CheckConstraint(_in(column, values), name=column)


# kind 为未知值时下面两条不拦，只由 kind 那条拒绝。
_NO_SCOPE_KINDS = (ADMIN, SYSTEM)
_SCOPES_BY_KIND = (
    "JSON_TYPE(scopes) = 'ARRAY'"
    f" AND (kind <> '{SERVICE}' OR JSON_LENGTH(scopes) > 0)"
    f" AND (NOT ({_in('kind', _NO_SCOPE_KINDS)}) OR JSON_LENGTH(scopes) = 0)"
)
_CHANNEL_BY_KIND = f"NOT ({_in('kind', _NO_SCOPE_KINDS)}) OR channel_id IS NULL"


class Credential(Base):
    """一个操作主体（契约第 10 节第 4 条）；scope 签发后不改。

    service 是调用方服务身份，scope 非空；admin 与 system 不带 scope、不带销售入口。
    令牌只签给 service（命令行与鉴权两处保证），credential_tokens 不按种类加约束。
    """

    __tablename__ = "credentials"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["sales_channels.tenant_id", "sales_channels.channel_id"],
        ),
        # 复合外键 (tenant_id, credential_id) 的引用目标。
        UniqueConstraint("tenant_id", "credential_id"),
        _one_of("kind", PRINCIPAL_KINDS),
        # scope 的取值由应用校验，这里只要求是数组：service 非空，admin 与 system 为空。
        CheckConstraint(_SCOPES_BY_KIND, name="scopes"),
        CheckConstraint(_CHANNEL_BY_KIND, name="channel"),
        _one_of("status", ("active", "revoked")),
        CheckConstraint("(status = 'revoked') = (revoked_at IS NOT NULL)", name="revoked_at"),
        TABLE_OPTIONS,
    )

    credential_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    channel_id: Mapped[str | None] = mapped_column(ID)
    name: Mapped[str] = mapped_column(String(200))
    scopes: Mapped[list[str]] = mapped_column(JSON)
    kind: Mapped[str] = mapped_column(STATUS, server_default=SERVICE)
    status: Mapped[str] = mapped_column(STATUS)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class CredentialToken(Base):
    """不存明文令牌，只存摘要（契约第 10 节第 4 条）。"""

    __tablename__ = "credential_tokens"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        # 鉴权时还不知道租户，摘要全库唯一、不含 tenant_id（契约第 10 节第 1 条的例外）。
        UniqueConstraint("token_digest"),
        CheckConstraint("REGEXP_LIKE(token_digest, '^[0-9a-f]{64}$', 'c')", name="token_digest"),
        TABLE_OPTIONS,
    )

    token_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    credential_id: Mapped[str] = mapped_column(ID)
    # 令牌全文 UTF-8 的 SHA-256 小写十六进制。
    token_digest: Mapped[str] = mapped_column(CHAR(64, collation=EXACT_COLLATION))
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class SecurityEvent(Base):
    """只追加；满 1 年按 occurred_at 整批清理（契约 7.2）。

    凭据无效时认不出租户，tenant_id 可空（契约第 10 节第 1 条的例外）。
    不记 Authorization 头、令牌与请求体。
    """

    __tablename__ = "security_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        _one_of("event_type", EVENT_TYPES),
        # 认出了凭据就认出了租户；没有这条，复合外键在 tenant_id 为空时不检查。
        CheckConstraint("credential_id IS NULL OR tenant_id IS NOT NULL", name="credential_tenant"),
        TABLE_OPTIONS,
    )

    event_id: Mapped[str] = mapped_column(ID, primary_key=True)
    event_type: Mapped[str] = mapped_column(CODE32)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW, index=True)
    tenant_id: Mapped[str | None] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    credential_id: Mapped[str | None] = mapped_column(ID)
    # 命令行操作没有请求。
    request_id: Mapped[str | None] = mapped_column(REF)
    actor_ref: Mapped[str | None] = mapped_column(REF)
    http_method: Mapped[str | None] = mapped_column(String(10))
    # 不含查询串。
    path: Mapped[str | None] = mapped_column(String(512))
    user_agent: Mapped[str | None] = mapped_column(String(200))
    # 原因、目标令牌、旧令牌截止时间等，不含令牌与请求体。
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
