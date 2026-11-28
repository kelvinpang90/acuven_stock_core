"""后台管理员账号、会话与登录尝试的表（docs/ADMIN_AUTH.md 第 3 节）。

只有表结构与数据库约束。用户名字符集、密码哈希、锁定、会话签发与到期由规则层判断
（app/services/admin_auth.py）。时间都是不带时区的 UTC、由应用写（utc_now() 截到整秒），
不用数据库默认值。指向账号的外键一律 ON DELETE RESTRICT：停用账号用 disabled_at，不删行。
"""

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.catalog import EXACT_COLLATION, ID, TABLE_OPTIONS, TIMESTAMP

OUTCOMES = ("success", "bad_password", "unknown_user", "disabled", "locked")
_OUTCOME_VALUES = ", ".join(f"'{outcome}'" for outcome in OUTCOMES)


def _account_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "admin_account_id"],
        ["admin_accounts.tenant_id", "admin_accounts.admin_account_id"],
        ondelete="RESTRICT",
    )


class AdminAccount(Base):
    """一个管理员账号；admin_account_id 就是写进 Core 流水与审计的 actor_ref。

    credential_id 指向这个账号的操作主体（kind 为 admin，契约第 10 节第 4 条），
    主体的 kind 由建号的服务保证。
    """

    __tablename__ = "admin_accounts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        # 复合外键 (tenant_id, admin_account_id) 的引用目标。
        UniqueConstraint("tenant_id", "admin_account_id"),
        UniqueConstraint("credential_id"),
        # 登录时还不知道租户，用户名全库唯一（同令牌摘要的例外，契约第 10 节第 1 条）。
        UniqueConstraint("username"),
        CheckConstraint("LENGTH(username) >= 3 AND LENGTH(username) <= 32", name="username_length"),
        TABLE_OPTIONS,
    )

    admin_account_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    credential_id: Mapped[str] = mapped_column(ID)
    username: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    password_updated_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    # 非空即已停用。
    disabled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class AdminSession(Base):
    """服务端会话；只存令牌的摘要。"""

    __tablename__ = "admin_sessions"
    __table_args__ = (
        _account_fk(),
        # 全库唯一（契约第 10 节第 1 条的例外）。
        UniqueConstraint("token_hash"),
        CheckConstraint("REGEXP_LIKE(token_hash, '^[0-9a-f]{64}$', 'c')", name="token_hash"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    admin_account_id: Mapped[str] = mapped_column(ID, index=True)
    # 会话令牌 UTF-8 的 SHA-256 小写十六进制。
    token_hash: Mapped[str] = mapped_column(CHAR(64, collation=EXACT_COLLATION))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    # 非空即已撤销。
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class AdminLoginAttempt(Base):
    """每次登录尝试一条；不存来源地址与提交的用户名原文，只存两个摘要（设计第 2 节）。

    认不出账号时 tenant_id 与 admin_account_id 为空（同安全事件的例外）。
    """

    __tablename__ = "admin_login_attempts"
    __table_args__ = (
        _account_fk(),
        CheckConstraint("LENGTH(throttle_key) = 64", name="throttle_key_length"),
        CheckConstraint("LENGTH(source_key) = 64", name="source_key_length"),
        # 认出了账号就认出了租户；没有这条，复合外键在 tenant_id 为空时不检查。
        CheckConstraint("admin_account_id IS NULL OR tenant_id IS NOT NULL", name="account_tenant"),
        CheckConstraint(f"outcome IN ({_OUTCOME_VALUES})", name="outcome_known"),
        Index("ix_admin_login_attempts_throttle_key_occurred_at", "throttle_key", "occurred_at"),
        Index("ix_admin_login_attempts_source_key_occurred_at", "source_key", "occurred_at"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    # 来源与提交的用户名的 SHA-256（设计 4.2）。
    throttle_key: Mapped[str] = mapped_column(String(64))
    # 只有来源的 SHA-256（设计 4.2）。
    source_key: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    admin_account_id: Mapped[str | None] = mapped_column(ID)
    outcome: Mapped[str] = mapped_column(String(20))
