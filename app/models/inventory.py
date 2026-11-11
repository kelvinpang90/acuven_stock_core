"""库存侧的表：地点、库存池、销售入口、余额、流水与幂等记录
（契约第 1 节、第 3 节、4.1、4.3、7.1；租户隔离见第 10 节第 1 条）。

只有表结构与数据库约束。锁行顺序、balance_version 递增、幂等重放等
由服务层在同一事务里做（契约 7.1、8.1）。
调用方引用、幂等键、操作人、凭据 ID、原因代码的格式由接口层校验，
这里只按 utf8mb4_bin 精确比较。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.catalog import EXACT_COLLATION, ID, NOW, STATUS, TABLE_OPTIONS, TIMESTAMP

REF = String(128, collation=EXACT_COLLATION)
CODE32 = String(32, collation=EXACT_COLLATION)
ZERO = text("0")

QUANTITIES = ("stock_balance", "reserved", "unavailable", "paid_pending_dispatch")

# 契约 4.3 表的十八种流水类型。
ENTRY_TYPES = (
    "OPENING_BALANCE",
    "RECEIPT",
    "COUNT_ADJUSTMENT",
    "MARK_UNAVAILABLE",
    "RESTORE_AVAILABLE",
    "WRITE_OFF",
    "RESERVE",
    "RELEASE",
    "SALE_COMMIT",
    "LATE_SALE_COMMIT",
    "DISPATCH",
    "REFUND_RESTOCK",
    "RETURN_SELLABLE",
    "RETURN_UNSELLABLE",
    "MERGE_TRANSFER_OUT",
    "MERGE_TRANSFER_IN",
    "DEMO_RESET",
    "CORRECTION",
)


def _one_of(column: str, values: tuple[str, ...]) -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


def _quantity_invariants(suffix: str = "") -> list[CheckConstraint]:
    # 契约 4.1 的不变量：四个账面量非负，reserved + unavailable ≤ stock_balance。
    checks = [
        CheckConstraint(f"{column}{suffix} >= 0", name=f"{column}{suffix}_nonnegative")
        for column in QUANTITIES
    ]
    checks.append(
        CheckConstraint(
            f"reserved{suffix} + unavailable{suffix} <= stock_balance{suffix}",
            name=f"reserved_unavailable{suffix}_within_balance",
        )
    )
    return checks


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = (
        # 复合外键 (tenant_id, location_id) 的引用目标。
        UniqueConstraint("tenant_id", "location_id"),
        _one_of("status", ("active", "inactive")),
        TABLE_OPTIONS,
    )

    location_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(STATUS)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class StockPool(Base):
    """期初只在 initializing 可用，切到 live 后一律拒绝（契约第 6 节）。"""

    __tablename__ = "stock_pools"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "location_id"], ["locations.tenant_id", "locations.location_id"]
        ),
        # 复合外键 (tenant_id, pool_id) 的引用目标。
        UniqueConstraint("tenant_id", "pool_id"),
        _one_of("status", ("initializing", "live")),
        TABLE_OPTIONS,
    )

    pool_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    location_id: Mapped[str] = mapped_column(ID)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(STATUS)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class SalesChannel(Base):
    __tablename__ = "sales_channels"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "pool_id"], ["stock_pools.tenant_id", "stock_pools.pool_id"]
        ),
        # 复合外键 (tenant_id, channel_id) 的引用目标。
        UniqueConstraint("tenant_id", "channel_id"),
        # 一个池只允许一个销售入口（契约第 3 节，C-07）；将来启用共卖时另行迁移放开。
        UniqueConstraint("tenant_id", "pool_id"),
        _one_of("status", ("active", "inactive")),
        TABLE_OPTIONS,
    )

    channel_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    pool_id: Mapped[str] = mapped_column(ID)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(STATUS)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class StockBalance(Base):
    """每个 tenant × pool × sku 一行（契约 4.1）。

    available 是派生量，不存。balance_version 由应用每写一条流水加 1：
    一个命令对同一行写多条流水时逐条加。多 SKU 命令按 (pool_id, sku_id) 升序锁行（契约 8.1）。
    """

    __tablename__ = "stock_balances"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "pool_id"], ["stock_pools.tenant_id", "stock_pools.pool_id"]
        ),
        ForeignKeyConstraint(["tenant_id", "sku_id"], ["skus.tenant_id", "skus.sku_id"]),
        # 每个 tenant × pool × sku 一行；也是流水复合外键的引用目标。
        UniqueConstraint("tenant_id", "pool_id", "sku_id"),
        *_quantity_invariants(),
        CheckConstraint("balance_version >= 0", name="balance_version_nonnegative"),
        TABLE_OPTIONS,
    )

    balance_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    pool_id: Mapped[str] = mapped_column(ID)
    sku_id: Mapped[str] = mapped_column(ID)
    stock_balance: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
    reserved: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
    unavailable: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
    paid_pending_dispatch: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
    balance_version: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class LedgerEntry(Base):
    """只追加（契约 4.3）。

    同一余额行的流水按 balance_version_after 排序，不按 recorded_at 或 entry_id：
    同一命令写的多条流水时间相同。
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "pool_id", "sku_id"],
            ["stock_balances.tenant_id", "stock_balances.pool_id", "stock_balances.sku_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["sales_channels.tenant_id", "sales_channels.channel_id"],
        ),
        # 只能更正同租户的流水（契约第 10 节第 1 条）。
        ForeignKeyConstraint(
            ["tenant_id", "corrects_entry_id"],
            ["ledger_entries.tenant_id", "ledger_entries.entry_id"],
        ),
        # 复合外键 (tenant_id, entry_id) 的引用目标。
        UniqueConstraint("tenant_id", "entry_id"),
        UniqueConstraint("tenant_id", "pool_id", "sku_id", "balance_version_after"),
        _one_of("entry_type", ENTRY_TYPES),
        *_quantity_invariants("_after"),
        CheckConstraint("balance_version_after >= 1", name="balance_version_after_positive"),
        CheckConstraint("(source_type IS NULL) = (source_ref IS NULL)", name="source_pair"),
        # CORRECTION 必须引用原流水，其他类型不得引用（契约 4.3）。
        CheckConstraint(
            "(entry_type = 'CORRECTION') = (corrects_entry_id IS NOT NULL)",
            name="correction_target",
        ),
        CheckConstraint("corrects_entry_id <> entry_id", name="correction_not_self"),
        TABLE_OPTIONS,
    )

    entry_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    pool_id: Mapped[str] = mapped_column(ID)
    sku_id: Mapped[str] = mapped_column(ID)
    entry_type: Mapped[str] = mapped_column(CODE32)
    stock_balance_delta: Mapped[int] = mapped_column(BigInteger)
    reserved_delta: Mapped[int] = mapped_column(BigInteger)
    unavailable_delta: Mapped[int] = mapped_column(BigInteger)
    paid_pending_dispatch_delta: Mapped[int] = mapped_column(BigInteger)
    stock_balance_after: Mapped[int] = mapped_column(BigInteger)
    reserved_after: Mapped[int] = mapped_column(BigInteger)
    unavailable_after: Mapped[int] = mapped_column(BigInteger)
    paid_pending_dispatch_after: Mapped[int] = mapped_column(BigInteger)
    balance_version_after: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[str | None] = mapped_column(ID)
    source_type: Mapped[str | None] = mapped_column(CODE32)
    source_ref: Mapped[str | None] = mapped_column(REF)
    line_ref: Mapped[str | None] = mapped_column(REF)
    # 同一命令写的各条流水相同。
    command_id: Mapped[str] = mapped_column(ID, index=True)
    # Core 自己的到期关闭没有请求，也就没有幂等键。
    idempotency_key: Mapped[str | None] = mapped_column(REF)
    # 服务凭据表随下一任务建，届时再加外键。
    credential_id: Mapped[str | None] = mapped_column(ID)
    actor_ref: Mapped[str | None] = mapped_column(REF)
    actor_role: Mapped[str | None] = mapped_column(CODE32)
    reason_code: Mapped[str | None] = mapped_column(CODE32)
    note: Mapped[str | None] = mapped_column(String(500))
    request_id: Mapped[str | None] = mapped_column(REF)
    corrects_entry_id: Mapped[str | None] = mapped_column(ID)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    recorded_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class IdempotencyRecord(Base):
    """只追加；保存首次确定的结果，与数量变化同一事务提交（契约 7.1）。

    5xx、429 与 REQUEST_IN_PROGRESS 不是确定结果，不保存（契约 7.1、8.3）。
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        # 契约 7.1 的作用域。约束名超过 64 字符，建表时按命名约定截断加摘要。
        UniqueConstraint("tenant_id", "credential_id", "operation", "idempotency_key"),
        CheckConstraint(
            "response_status BETWEEN 200 AND 499 AND response_status <> 429",
            name="response_status",
        ),
        TABLE_OPTIONS,
    )

    idempotency_record_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    # 服务凭据表随下一任务建，届时再加外键。
    credential_id: Mapped[str] = mapped_column(ID)
    operation: Mapped[str] = mapped_column(String(64, collation=EXACT_COLLATION))
    idempotency_key: Mapped[str] = mapped_column(REF)
    # 规范化请求摘要 SHA-256 的小写十六进制。
    request_hash: Mapped[str] = mapped_column(CHAR(64, collation=EXACT_COLLATION))
    response_status: Mapped[int] = mapped_column(SmallInteger)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_id: Mapped[str | None] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
