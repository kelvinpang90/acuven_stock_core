"""库存侧的表：地点、库存池、销售入口、余额、流水、幂等记录、期初导入、库存单据与盘点单
（契约第 1 节、第 3 节、4.1、4.3、6.2 第 7 条、6.3、6.6、6.7、7.1；租户隔离见第 10 节第 1 条）。

只有表结构与数据库约束。锁行顺序、balance_version 与两个判定列的更新、幂等重放等
由服务层在同一事务里做（契约 4.1、7.1、8.1）。
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
from app.models.catalog import (
    CONFIRMATION_COLUMNS,
    EXACT_COLLATION,
    ID,
    NOW,
    STATUS,
    TABLE_OPTIONS,
    TIMESTAMP,
)

REF = String(128, collation=EXACT_COLLATION)
CODE32 = String(32, collation=EXACT_COLLATION)
# 规范化请求摘要 SHA-256 的小写十六进制。
HASH = CHAR(64, collation=EXACT_COLLATION)
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


def _quantity_invariants(suffix: str = "", prefix: str = "") -> list[CheckConstraint]:
    # 契约 4.1 的不变量：四个账面量非负，reserved + unavailable ≤ stock_balance。
    quantities = [f"{prefix}{column}{suffix}" for column in QUANTITIES]
    stock_balance, reserved, unavailable, _ = quantities
    checks = [
        CheckConstraint(f"{column} >= 0", name=f"{column}_nonnegative") for column in quantities
    ]
    checks.append(
        CheckConstraint(
            f"{reserved} + {unavailable} <= {stock_balance}",
            name=f"{prefix}reserved_unavailable{suffix}_within_balance",
        )
    )
    return checks


def _all_or_none(name: str, columns: tuple[str, ...]) -> CheckConstraint:
    first, *rest = columns
    return CheckConstraint(
        " AND ".join(f"({first} IS NULL) = ({column} IS NULL)" for column in rest), name=name
    )


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
    """期初只在 initializing 可用，切到 live 后一律拒绝（契约第 6 节）。

    切 live 不可逆，记下切换的时间与操作人（契约 6.3）。
    """

    __tablename__ = "stock_pools"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "location_id"], ["locations.tenant_id", "locations.location_id"]
        ),
        # 凭据经销售入口指向池，这条与之成环；use_alter 让 metadata 排表时不算它。
        ForeignKeyConstraint(
            ["tenant_id", "went_live_credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            use_alter=True,
        ),
        # 复合外键 (tenant_id, pool_id) 的引用目标。
        UniqueConstraint("tenant_id", "pool_id"),
        _one_of("status", ("initializing", "live")),
        _all_or_none(
            "went_live_columns",
            (
                "went_live_at",
                "went_live_credential_id",
                "went_live_actor_ref",
                "went_live_request_id",
            ),
        ),
        CheckConstraint("(status = 'live') = (went_live_at IS NOT NULL)", name="went_live_status"),
        TABLE_OPTIONS,
    )

    pool_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    location_id: Mapped[str] = mapped_column(ID)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(STATUS)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    went_live_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    went_live_credential_id: Mapped[str | None] = mapped_column(ID)
    went_live_actor_ref: Mapped[str | None] = mapped_column(REF)
    went_live_request_id: Mapped[str | None] = mapped_column(REF)


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
    last_book_only_version、last_reconciled_version 与 opening_entry_id 是 §6 的判定列，
    由写流水的服务层在同一事务里更新（契约 4.1）。
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
        CheckConstraint(
            "last_book_only_version >= 0 AND last_book_only_version <= balance_version",
            name="last_book_only_version_range",
        ),
        CheckConstraint(
            "last_reconciled_version >= 0 AND last_reconciled_version <= last_book_only_version",
            name="last_reconciled_version_range",
        ),
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
    last_book_only_version: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
    last_reconciled_version: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
    # 不加外键：流水表已有外键指向余额行，反向引用会成环。
    opening_entry_id: Mapped[str | None] = mapped_column(ID)


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
        # Core 自己的到期关闭没有凭据，credential_id 为空时不检查。
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        # 复合外键 (tenant_id, entry_id) 的引用目标。
        UniqueConstraint("tenant_id", "entry_id"),
        UniqueConstraint("tenant_id", "pool_id", "sku_id", "balance_version_after"),
        # 一条流水最多被更正一次（契约 6.7）；MySQL 唯一索引允许多个 NULL，不影响其他流水。
        UniqueConstraint("tenant_id", "corrects_entry_id"),
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
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
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
    credential_id: Mapped[str] = mapped_column(ID)
    operation: Mapped[str] = mapped_column(String(64, collation=EXACT_COLLATION))
    idempotency_key: Mapped[str] = mapped_column(REF)
    # 规范化请求摘要 SHA-256 的小写十六进制。
    request_hash: Mapped[str] = mapped_column(CHAR(64, collation=EXACT_COLLATION))
    response_status: Mapped[int] = mapped_column(SmallInteger)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_id: Mapped[str | None] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class StockImport(Base):
    """一次期初导入一行，永久保留（契约 6.3）。

    列同 catalog_imports（契约 2.3），另加 pool_id。过期由 expires_at 推出，不存。
    """

    __tablename__ = "stock_imports"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "pool_id"], ["stock_pools.tenant_id", "stock_pools.pool_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        # 与上一条都以 tenant_id 开头、指向 credentials，按命名约定会同名，这里显式命名。
        ForeignKeyConstraint(
            ["tenant_id", "confirmed_credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name="fk_stock_imports_confirmed_credential_id_credentials",
        ),
        # 复合外键 (tenant_id, import_id) 的引用目标。
        UniqueConstraint("tenant_id", "import_id"),
        _one_of("status", ("previewed", "confirmed")),
        # rows 是 MySQL 8 的保留字，手写的 SQL 里要加反引号。
        CheckConstraint("JSON_TYPE(`rows`) = 'ARRAY'", name="rows"),
        # status 为未知值时这条不拦，只由 status 那条拒绝。
        CheckConstraint(
            " AND ".join(
                f"(status = 'confirmed') = ({column} IS NOT NULL)"
                for column in CONFIRMATION_COLUMNS
            ),
            name="confirmation",
        ),
        TABLE_OPTIONS,
    )

    import_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    pool_id: Mapped[str] = mapped_column(ID)
    status: Mapped[str] = mapped_column(STATUS)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    # 各行原文与预览结果。
    rows: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    # Python 的 None 存成 SQL NULL 而不是 JSON 的 null，否则预览行过不了 confirmation 检查。
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    # 预览要求 stock_admin 角色，必有凭据与操作者。
    credential_id: Mapped[str] = mapped_column(ID)
    actor_ref: Mapped[str] = mapped_column(REF)
    request_id: Mapped[str] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    confirmed_credential_id: Mapped[str | None] = mapped_column(ID)
    confirmed_actor_ref: Mapped[str | None] = mapped_column(REF)
    confirmed_request_id: Mapped[str | None] = mapped_column(REF)
    confirmed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class StockDocument(Base):
    """收货、不可售调整与更正的单据：只追加，永久保留（契约 6.2 第 7 条）。

    每条已生效的命令一行，与流水、余额、幂等记录在同一事务提交。
    """

    __tablename__ = "stock_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["sales_channels.tenant_id", "sales_channels.channel_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "pool_id"], ["stock_pools.tenant_id", "stock_pools.pool_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        # 业务引用的作用域。约束名超过 64 字符，建表时按命名约定截断加摘要。
        UniqueConstraint("tenant_id", "channel_id", "document_type", "document_ref"),
        _one_of("document_type", ("receipt", "unavailable_adjustment", "correction")),
        CheckConstraint("JSON_TYPE(result) = 'OBJECT'", name="result"),
        TABLE_OPTIONS,
    )

    # 前缀按类型分别是 rcp_、adj_、cor_。
    document_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    channel_id: Mapped[str] = mapped_column(ID)
    pool_id: Mapped[str] = mapped_column(ID)
    document_type: Mapped[str] = mapped_column(CODE32)
    # 收货是 receipt_ref，不可售调整是 adjustment_ref，更正是原流水的 entry_id。
    document_ref: Mapped[str] = mapped_column(REF)
    request_hash: Mapped[str] = mapped_column(HASH)
    # 首次 201 响应的 data。
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    # 三种命令都要求角色，凭据、操作者与角色必有。
    credential_id: Mapped[str] = mapped_column(ID)
    actor_ref: Mapped[str] = mapped_column(REF)
    actor_role: Mapped[str] = mapped_column(CODE32)
    request_id: Mapped[str] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


# 盘点单确认后才有的列（confirm_note 可空，另算）与取消后才有的列（契约 6.6）。
COUNT_CONFIRMATION_COLUMNS = (
    "confirmed_credential_id",
    "confirmed_actor_ref",
    "confirmed_request_id",
    "confirmed_at",
    "confirmed_reason_code",
)
COUNT_CANCELLATION_COLUMNS = (
    "cancelled_credential_id",
    "cancelled_actor_ref",
    "cancelled_request_id",
    "cancelled_at",
)


class StockCount(Base):
    """一张盘点单一行（契约 6.6）。确认或取消之后不再改。"""

    __tablename__ = "stock_counts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["sales_channels.tenant_id", "sales_channels.channel_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "pool_id"], ["stock_pools.tenant_id", "stock_pools.pool_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        # 与上一条都以 tenant_id 开头、指向 credentials，按命名约定会同名，这里显式命名。
        ForeignKeyConstraint(
            ["tenant_id", "confirmed_credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name="fk_stock_counts_confirmed_credential_id_credentials",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cancelled_credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name="fk_stock_counts_cancelled_credential_id_credentials",
        ),
        UniqueConstraint("tenant_id", "channel_id", "count_ref"),
        # 复合外键 (tenant_id, count_id) 的引用目标。
        UniqueConstraint("tenant_id", "count_id"),
        _one_of("status", ("open", "confirmed", "cancelled")),
        # status 为未知值时这两条不拦，只由 status 那条拒绝。
        CheckConstraint(
            " AND ".join(
                f"(status = 'confirmed') = ({column} IS NOT NULL)"
                for column in COUNT_CONFIRMATION_COLUMNS
            )
            + " AND (status = 'confirmed' OR confirm_note IS NULL)",
            name="confirmation",
        ),
        CheckConstraint(
            " AND ".join(
                f"(status = 'cancelled') = ({column} IS NOT NULL)"
                for column in COUNT_CANCELLATION_COLUMNS
            ),
            name="cancellation",
        ),
        TABLE_OPTIONS,
    )

    count_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    channel_id: Mapped[str] = mapped_column(ID)
    pool_id: Mapped[str] = mapped_column(ID)
    count_ref: Mapped[str] = mapped_column(REF)
    status: Mapped[str] = mapped_column(STATUS)
    note: Mapped[str | None] = mapped_column(String(500))
    # 开单的规范化请求摘要，换键重报开单时比对（契约 6.6）。
    request_hash: Mapped[str] = mapped_column(HASH)
    credential_id: Mapped[str] = mapped_column(ID)
    actor_ref: Mapped[str] = mapped_column(REF)
    request_id: Mapped[str] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    confirmed_credential_id: Mapped[str | None] = mapped_column(ID)
    confirmed_actor_ref: Mapped[str | None] = mapped_column(REF)
    confirmed_request_id: Mapped[str | None] = mapped_column(REF)
    confirmed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    confirmed_reason_code: Mapped[str | None] = mapped_column(CODE32)
    confirm_note: Mapped[str | None] = mapped_column(String(500))
    cancelled_credential_id: Mapped[str | None] = mapped_column(ID)
    cancelled_actor_ref: Mapped[str | None] = mapped_column(REF)
    cancelled_request_id: Mapped[str | None] = mapped_column(REF)
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class StockCountLine(Base):
    """盘点单的一个 SKU（契约 6.6）。重新提交整行替换，确认后不再改。

    pool_id 与所属盘点单的相同由同一事务的服务层保证。
    """

    __tablename__ = "stock_count_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "count_id"], ["stock_counts.tenant_id", "stock_counts.count_id"]
        ),
        # 提交时已预建余额行。
        ForeignKeyConstraint(
            ["tenant_id", "pool_id", "sku_id"],
            ["stock_balances.tenant_id", "stock_balances.pool_id", "stock_balances.sku_id"],
        ),
        # 确认后才有；两个差额都为 0 时不写流水，为空。
        ForeignKeyConstraint(
            ["tenant_id", "entry_id"], ["ledger_entries.tenant_id", "ledger_entries.entry_id"]
        ),
        CheckConstraint("shelf_count BETWEEN 0 AND 1000000", name="shelf_count_range"),
        CheckConstraint("unsellable_count BETWEEN 0 AND 1000000", name="unsellable_count_range"),
        # 快照取自余额行，满足同样的不变量。约束名超过 64 字符的按命名约定截断加摘要。
        *_quantity_invariants(prefix="snapshot_"),
        CheckConstraint(
            "snapshot_balance_version >= 0", name="snapshot_balance_version_nonnegative"
        ),
        # 契约第 6 节的公式。
        CheckConstraint(
            "d_shelf = shelf_count - (snapshot_stock_balance - snapshot_unavailable"
            " + snapshot_paid_pending_dispatch)",
            name="d_shelf",
        ),
        CheckConstraint("d_u = unsellable_count - snapshot_unavailable", name="d_u"),
        TABLE_OPTIONS,
    )

    count_id: Mapped[str] = mapped_column(ID, primary_key=True)
    sku_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    pool_id: Mapped[str] = mapped_column(ID)
    shelf_count: Mapped[int] = mapped_column(BigInteger)
    unsellable_count: Mapped[int] = mapped_column(BigInteger)
    snapshot_stock_balance: Mapped[int] = mapped_column(BigInteger)
    snapshot_reserved: Mapped[int] = mapped_column(BigInteger)
    snapshot_unavailable: Mapped[int] = mapped_column(BigInteger)
    snapshot_paid_pending_dispatch: Mapped[int] = mapped_column(BigInteger)
    snapshot_balance_version: Mapped[int] = mapped_column(BigInteger)
    d_shelf: Mapped[int] = mapped_column(BigInteger)
    d_u: Mapped[int] = mapped_column(BigInteger)
    actor_ref: Mapped[str] = mapped_column(REF)
    request_id: Mapped[str] = mapped_column(REF)
    submitted_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    entry_id: Mapped[str | None] = mapped_column(ID)
