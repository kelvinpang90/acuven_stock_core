"""平台渠道的运营表：平台连接、自动写平台暂停、人工核对、严重告警、对账与只追加的运营日志
（契约 3.2 的存放，Q-99、Q-101；租户隔离见第 10 节第 1 条）。

只有表结构与数据库约束。令牌与连接状态的搭配、checked_until 不晚于数据库当前时间、告警类型的取值、
运营日志前后值的键白名单与 4 KiB 上限，都由写这些表的服务管，不是数据库约束。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    VARBINARY,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.catalog import EXACT_COLLATION, ID, NOW, STATUS, TABLE_OPTIONS, TIMESTAMP
from app.models.inventory import CODE32, REF, ZERO

MANUAL = "manual"
AUTO = "auto"
MODES = (MANUAL, AUTO)
NOT_CONNECTED = "not_connected"
CONNECTION_STATUSES = (NOT_CONNECTED, "active", "auth_failed", "revoked")
SELF_SELLER = "self_seller"
APP_PROFILES = (SELF_SELLER,)
EMPTY_OBJECT = text("(JSON_OBJECT())")
# 加密后的令牌。
CIPHERTEXT = VARBINARY(4096)
NOTE = String(500)

# 设上与解除各一组，每组三列同空同非空（契约 3.2）。
SET_COLUMNS = ("set_credential_id", "set_at", "set_reason")
CLEARED_COLUMNS = ("cleared_credential_id", "cleared_at", "cleared_reason")

SCHEDULED = "scheduled"
TRIGGERS = (SCHEDULED, MANUAL)

# 契约 3.2 表里的十六种运营操作；加值要迁移重建约束。
OPS_ACTIONS = (
    "write_pause_set",
    "write_pause_cleared",
    "review_hold_set",
    "review_hold_cleared",
    "manual_check_recorded",
    "manual_quota_confirmed",
    "known_discrepancy_marked",
    "held_order_closed",
    "attention_cleared",
    "alert_acknowledged",
    "incident_decision",
    "reconciliation_started",
    "connection_authorized",
    "connection_disconnected",
    "connection_mode_changed",
    "connection_status_changed",
)


def _one_of(column: str, values: tuple[str, ...], name: str | None = None) -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name or column)


def _object(column: str, name: str) -> CheckConstraint:
    # 可空的列为 NULL 时 JSON_TYPE 为 NULL、不拦。
    return CheckConstraint(f"JSON_TYPE({column}) = 'OBJECT'", name=name)


def _same_nullness(columns: tuple[str, ...], name: str) -> CheckConstraint:
    first = columns[0]
    return CheckConstraint(
        " AND ".join(f"({first} IS NULL) = ({column} IS NULL)" for column in columns[1:]),
        name=name,
    )


def _credential_fk(column: str, name: str | None = None) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column], ["credentials.tenant_id", "credentials.credential_id"], name=name
    )


def _channel_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "channel_id"], ["sales_channels.tenant_id", "sales_channels.channel_id"]
    )


ACTOR_CHECK = "(actor_ref IS NULL) = (actor_role IS NULL)"


class PlatformConnection(Base):
    """每个 platform 入口一行，只存当前状态（契约 3.2）；每次改动另在运营日志留一条。

    开通 platform 入口时取默认值建（人工模式、未连接）。mode_changed_at 在建行与每次切换模式时
    写数据库时间：人工模式下还没核对过的，逾期从这个时刻算起（契约 3.2，Q-99）。
    """

    __tablename__ = "platform_connections"
    __table_args__ = (
        _channel_fk(),
        # 约束名互不为子串：测试按约束名的子串认定是哪一条拒绝的。
        _one_of("mode", MODES),
        _one_of("status", CONNECTION_STATUSES),
        _one_of("app_profile", APP_PROFILES),
        _object("capabilities", "capabilities"),
        TABLE_OPTIONS,
    )

    # 不另设到 tenants 的外键：到 sales_channels 的复合外键指向同租户的行（同 platform_orders）。
    tenant_id: Mapped[str] = mapped_column(ID, primary_key=True)
    channel_id: Mapped[str] = mapped_column(ID, primary_key=True)
    mode: Mapped[str] = mapped_column(STATUS, server_default=MANUAL)
    mode_changed_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    status: Mapped[str] = mapped_column(STATUS, server_default=NOT_CONNECTED)
    app_profile: Mapped[str] = mapped_column(STATUS, server_default=SELF_SELLER)
    # 能力的名字与可用与否。
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, server_default=EMPTY_OBJECT)
    access_token_ciphertext: Mapped[bytes | None] = mapped_column(CIPHERTEXT)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    refresh_token_ciphertext: Mapped[bytes | None] = mapped_column(CIPHERTEXT)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    # 已补拉到的平台更新时间。
    pull_cursor: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    next_pull_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    last_synced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class PlatformWritePause(Base):
    """“自动写平台暂停”，每个租户一行（契约 3.2，PLATFORM_CHANNELS 第 5 节）。

    开通这个租户的第一个 platform 入口时建，paused 为假；恢复时运行手册的 SQL 只改已有的行。
    """

    __tablename__ = "platform_write_pause"
    __table_args__ = (
        # 两条指向 credentials 的复合外键按约定会同名，这里显式命名。
        _credential_fk("set_credential_id", name="fk_platform_write_pause_set_credential"),
        _credential_fk("cleared_credential_id", name="fk_platform_write_pause_cleared_credential"),
        _same_nullness(SET_COLUMNS, "set_columns"),
        _same_nullness(CLEARED_COLUMNS, "cleared_columns"),
        TABLE_OPTIONS,
    )

    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"), primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, server_default=ZERO)
    set_credential_id: Mapped[str | None] = mapped_column(ID)
    set_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    set_reason: Mapped[str | None] = mapped_column(NOTE)
    cleared_credential_id: Mapped[str | None] = mapped_column(ID)
    cleared_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    cleared_reason: Mapped[str | None] = mapped_column(NOTE)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class ManualCheck(Base):
    """人工核对一次一行（契约 3.2，Q-99）。

    checked_until 不晚于数据库当前时间由写它的服务判：MySQL 的检查约束不能用当前时间。
    """

    __tablename__ = "manual_checks"
    __table_args__ = (
        _channel_fk(),
        _credential_fk("credential_id"),
        # 按入口取最近一次核对；也是到 sales_channels 的外键要用的索引。
        Index(
            "ix_manual_checks_tenant_id_channel_id_checked_until",
            "tenant_id",
            "channel_id",
            "checked_until",
        ),
        CheckConstraint("checked_from < checked_until", name="checked_range"),
        CheckConstraint(ACTOR_CHECK, name="actor"),
        TABLE_OPTIONS,
    )

    manual_check_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID)
    channel_id: Mapped[str] = mapped_column(ID)
    checked_from: Mapped[datetime] = mapped_column(TIMESTAMP)
    checked_until: Mapped[datetime] = mapped_column(TIMESTAMP)
    found_discrepancy: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(NOTE)
    credential_id: Mapped[str] = mapped_column(ID)
    actor_ref: Mapped[str | None] = mapped_column(REF)
    actor_role: Mapped[str | None] = mapped_column(CODE32)
    request_id: Mapped[str] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class PlatformAlert(Base):
    """严重告警一条一行（契约 3.2，PLATFORM_CHANNELS 第 7 节）。

    kind 不加检查约束，由应用校验（同 catalog_changes.event_type）。告警记下了而 Telegram 还没
    发成功时 first_sent_at 与 last_notified_at 为空。
    """

    __tablename__ = "platform_alerts"
    __table_args__ = (
        # 对象列都可空，为空时不检查。
        _channel_fk(),
        ForeignKeyConstraint(["tenant_id", "sku_id"], ["skus.tenant_id", "skus.sku_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "platform_order_row_id"],
            ["platform_orders.tenant_id", "platform_orders.platform_order_row_id"],
        ),
        _credential_fk("acked_credential_id"),
        # 按租户列出未确认的告警。
        Index(
            "ix_platform_alerts_tenant_id_acked_at_created_at",
            "tenant_id",
            "acked_at",
            "created_at",
        ),
        _object("detail", "detail"),
        CheckConstraint("notify_count >= 0", name="notify_count"),
        CheckConstraint("(acked_credential_id IS NULL) = (acked_at IS NULL)", name="acked"),
        TABLE_OPTIONS,
    )

    alert_id: Mapped[str] = mapped_column(ID, primary_key=True)
    # 复合外键都可空，另设到 tenants 的外键。
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    kind: Mapped[str] = mapped_column(CODE32)
    channel_id: Mapped[str | None] = mapped_column(ID)
    sku_id: Mapped[str | None] = mapped_column(ID)
    platform_order_row_id: Mapped[str | None] = mapped_column(ID)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    first_sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    last_notified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    notify_count: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    acked_credential_id: Mapped[str | None] = mapped_column(ID)
    acked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    # 转成超时待办的时间。
    escalated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class ReconciliationRun(Base):
    """每次对账一行（契约 3.2，PLATFORM_CHANNELS 第 8 节）。

    跑完时 finished_at 与差异数一并写上。
    """

    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        _credential_fk("credential_id"),
        # trigger 是 MySQL 的保留字，表达式里要加引号。
        _one_of("`trigger`", TRIGGERS, name="trigger"),
        CheckConstraint(
            f"`trigger` <> '{MANUAL}' OR credential_id IS NOT NULL", name="manual_credential"
        ),
        CheckConstraint("(finished_at IS NULL) = (discrepancy_count IS NULL)", name="finished"),
        CheckConstraint("discrepancy_count >= 0", name="discrepancy_count"),
        TABLE_OPTIONS,
    )

    run_id: Mapped[str] = mapped_column(ID, primary_key=True)
    # 定时对账没有发起的主体，另设到 tenants 的外键。
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    trigger: Mapped[str] = mapped_column(STATUS)
    credential_id: Mapped[str | None] = mapped_column(ID)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    discrepancy_count: Mapped[int | None] = mapped_column(Integer)


class PlatformOpsLog(Base):
    """只追加：每一种后台运营写操作与作业对连接状态的改动一行（契约 3.2，Q-101）。

    与改动本身同一事务写。前后值由服务层拼出，不得含令牌、平台应用凭据、OAuth 的 code 与
    state、顾客资料。
    """

    __tablename__ = "platform_ops_log"
    __table_args__ = (
        _credential_fk("credential_id"),
        # 按租户、按主键倒序列出。
        Index("ix_platform_ops_log_tenant_id_id", "tenant_id", "id"),
        # 约束名互不为子串：测试按约束名的子串认定是哪一条拒绝的。
        _one_of("action", OPS_ACTIONS),
        # before 是 MySQL 的保留字，表达式里要加引号。
        _object("`before`", "before"),
        _object("`after`", "after"),
        CheckConstraint(ACTOR_CHECK, name="actor"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 不另设到 tenants 的外键：到 credentials 的复合外键非空、指向同租户的行。
    tenant_id: Mapped[str] = mapped_column(ID)
    action: Mapped[str] = mapped_column(CODE32)
    object_type: Mapped[str | None] = mapped_column(CODE32)
    object_id: Mapped[str | None] = mapped_column(String(64, collation=EXACT_COLLATION))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    reason: Mapped[str | None] = mapped_column(NOTE)
    # 作业写时用租户的 system 主体（契约第 10 节第 4 条）。
    credential_id: Mapped[str] = mapped_column(ID)
    actor_ref: Mapped[str | None] = mapped_column(REF)
    actor_role: Mapped[str | None] = mapped_column(CODE32)
    request_id: Mapped[str | None] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
