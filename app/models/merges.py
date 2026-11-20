"""SKU 合并的表：合并单与录入实数时的快照（契约 2.4、2.8 的存放）。

租户隔离见第 10 节第 1 条。只有表结构与数据库约束。状态取值取自 app/domain/sku_merges.py。
“合并单互不重叠”不在库里约束（MySQL 没有部分唯一索引），由新建锁两个 SKU 行后检查保证；
快照的 pool_id 与所属合并单相同、重新录入时整份替换，由同一事务的服务层保证（契约 2.8）。
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.domain.sku_merges import (
    CANCELLED,
    COMPLETED,
    COUNTED,
    RECONCILE_PENDING,
    SELLING_PAUSED,
    STATUSES,
)
from app.models.catalog import ID, NOW, TABLE_OPTIONS, TIMESTAMP
from app.models.inventory import CODE32, QUANTITIES, REF

# 录入实数之后才有的列，同有同无；录入的操作者两列另行约束。
COUNT_COLUMNS = (
    "pool_id",
    "shelf_count",
    "unsellable_count",
    "counted_credential_id",
    "counted_request_id",
    "counted_at",
)
# 录入实数之后的三种状态这些列非空；cancelled 不限（取消前可能录入过，也可能没有）。
COUNTED_STATUSES = (COUNTED, RECONCILE_PENDING, COMPLETED)

# 执行之后才有、且必有的列；adjustment_entry_id 与执行的操作者两列另行约束。
EXECUTION_COLUMNS = (
    "executed_credential_id",
    "executed_request_id",
    "executed_at",
    "transfer_out_entry_id",
    "transfer_in_entry_id",
    "repointed_line_count",
)
EXECUTED_STATUSES = (RECONCILE_PENDING, COMPLETED)

COMPLETION_COLUMNS = ("completed_credential_id", "completed_request_id", "completed_at")
CANCELLATION_COLUMNS = ("cancelled_credential_id", "cancelled_request_id", "cancelled_at")

# 各组的操作者两列（actor_ref、actor_role）的列名前缀；新建一组没有前缀。
ACTOR_PREFIXES = ("", "counted_", "executed_", "completed_", "cancelled_")


def _in(column: str, values: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def _together(columns: tuple[str, ...]) -> str:
    # 各列同有同无。
    first = columns[0]
    return " AND ".join(f"({first} IS NULL) = ({column} IS NULL)" for column in columns[1:])


def _set_exactly_when(condition: str, columns: tuple[str, ...]) -> str:
    # condition 成立时各列都非空，否则都空。
    return " AND ".join(f"({condition}) = ({column} IS NOT NULL)" for column in columns)


def _actor(prefix: str) -> CheckConstraint:
    return CheckConstraint(
        f"({prefix}actor_ref IS NULL) = ({prefix}actor_role IS NULL)", name=f"{prefix}actor"
    )


def _credential(column: str) -> ForeignKeyConstraint:
    # 各条都以 tenant_id 开头、指向 credentials，按命名约定会同名，显式命名。
    return ForeignKeyConstraint(
        ["tenant_id", column],
        ["credentials.tenant_id", "credentials.credential_id"],
        name=f"fk_sku_merges_{column}_credentials",
    )


def _entry(column: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column],
        ["ledger_entries.tenant_id", "ledger_entries.entry_id"],
        name=f"fk_sku_merges_{column}_ledger_entries",
    )


class SkuMerge(Base):
    """一张合并单一行（契约 2.8）。merge_id 的前缀 smg_ 由应用生成；合并单不删除。

    status 用 VARCHAR(32)，是契约 2.2 状态列 VARCHAR(16) 的例外：
    reconcile_pending 有 17 个字符。
    """

    __tablename__ = "sku_merges"
    __table_args__ = (
        # 两条指向 skus 的复合外键都以 tenant_id 开头，显式命名，名字里带上第二列。
        ForeignKeyConstraint(
            ["tenant_id", "source_sku_id"],
            ["skus.tenant_id", "skus.sku_id"],
            name="fk_sku_merges_source_sku_id_skus",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "canonical_sku_id"],
            ["skus.tenant_id", "skus.sku_id"],
            name="fk_sku_merges_canonical_sku_id_skus",
        ),
        # 录入实数前为空，为空时不检查。
        ForeignKeyConstraint(
            ["tenant_id", "pool_id"],
            ["stock_pools.tenant_id", "stock_pools.pool_id"],
            name="fk_sku_merges_pool_id_stock_pools",
        ),
        _credential("credential_id"),
        _credential("counted_credential_id"),
        _credential("executed_credential_id"),
        _credential("completed_credential_id"),
        _credential("cancelled_credential_id"),
        _entry("transfer_out_entry_id"),
        _entry("transfer_in_entry_id"),
        _entry("adjustment_entry_id"),
        # 复合外键 (tenant_id, merge_id) 的引用目标。
        UniqueConstraint("tenant_id", "merge_id"),
        # 新建的重叠检查与停售判定按 SKU 查合并单；也支撑指向 skus 的两条外键。
        Index("ix_sku_merges_tenant_id_source_sku_id", "tenant_id", "source_sku_id"),
        Index("ix_sku_merges_tenant_id_canonical_sku_id", "tenant_id", "canonical_sku_id"),
        CheckConstraint("source_sku_id <> canonical_sku_id", name="source_not_canonical"),
        CheckConstraint(_in("status", STATUSES), name="status"),
        # status 为未知值时下面几条不一定拦，只由 status 那条拒绝。
        CheckConstraint(
            _together(COUNT_COLUMNS)
            + f" AND (NOT ({_in('status', COUNTED_STATUSES)}) OR pool_id IS NOT NULL)"
            + f" AND (status <> '{SELLING_PAUSED}' OR pool_id IS NULL)",
            name="count_columns",
        ),
        CheckConstraint(
            _set_exactly_when(_in("status", EXECUTED_STATUSES), EXECUTION_COLUMNS)
            + f" AND ({_in('status', EXECUTED_STATUSES)} OR adjustment_entry_id IS NULL)",
            name="execution_columns",
        ),
        CheckConstraint(
            _set_exactly_when(f"status = '{COMPLETED}'", COMPLETION_COLUMNS),
            name="completion_columns",
        ),
        CheckConstraint(
            _set_exactly_when(f"status = '{CANCELLED}'", CANCELLATION_COLUMNS),
            name="cancellation_columns",
        ),
        *[_actor(prefix) for prefix in ACTOR_PREFIXES],
        TABLE_OPTIONS,
    )

    merge_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    source_sku_id: Mapped[str] = mapped_column(ID)
    canonical_sku_id: Mapped[str] = mapped_column(ID)
    status: Mapped[str] = mapped_column(CODE32)
    # 执行时写进来源 SKU 的 sku_merged 变更记录（契约 2.8 新建）。
    reason: Mapped[str | None] = mapped_column(String(500))
    # 新建。
    credential_id: Mapped[str] = mapped_column(ID)
    actor_ref: Mapped[str | None] = mapped_column(REF)
    actor_role: Mapped[str | None] = mapped_column(CODE32)
    request_id: Mapped[str] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    # 录入实数：两个 SKU 合计的在架件数（含已付款待发的件）与不可售件数；重新录入时整份替换。
    pool_id: Mapped[str | None] = mapped_column(ID)
    shelf_count: Mapped[int | None] = mapped_column(BigInteger)
    unsellable_count: Mapped[int | None] = mapped_column(BigInteger)
    counted_credential_id: Mapped[str | None] = mapped_column(ID)
    counted_actor_ref: Mapped[str | None] = mapped_column(REF)
    counted_actor_role: Mapped[str | None] = mapped_column(CODE32)
    counted_request_id: Mapped[str | None] = mapped_column(REF)
    counted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    # 执行：两条迁移流水必有，两个差额都为 0 时不写定数流水。
    executed_credential_id: Mapped[str | None] = mapped_column(ID)
    executed_actor_ref: Mapped[str | None] = mapped_column(REF)
    executed_actor_role: Mapped[str | None] = mapped_column(CODE32)
    executed_request_id: Mapped[str | None] = mapped_column(REF)
    executed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    transfer_out_entry_id: Mapped[str | None] = mapped_column(ID)
    transfer_in_entry_id: Mapped[str | None] = mapped_column(ID)
    adjustment_entry_id: Mapped[str | None] = mapped_column(ID)
    repointed_line_count: Mapped[int | None] = mapped_column(Integer)
    # 完成。
    completed_credential_id: Mapped[str | None] = mapped_column(ID)
    completed_actor_ref: Mapped[str | None] = mapped_column(REF)
    completed_actor_role: Mapped[str | None] = mapped_column(CODE32)
    completed_request_id: Mapped[str | None] = mapped_column(REF)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    # 取消。
    cancelled_credential_id: Mapped[str | None] = mapped_column(ID)
    cancelled_actor_ref: Mapped[str | None] = mapped_column(REF)
    cancelled_actor_role: Mapped[str | None] = mapped_column(CODE32)
    cancelled_request_id: Mapped[str | None] = mapped_column(REF)
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class SkuMergeSnapshot(Base):
    """录入实数时一个 SKU 的余额行快照：四个账面量与 balance_version（契约 2.4、2.8）。

    一张合并单两行（来源与规范）；重新录入时整份替换，不留历史。
    """

    __tablename__ = "sku_merge_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "merge_id"], ["sku_merges.tenant_id", "sku_merges.merge_id"]
        ),
        # 余额行在录入实数时已预建（契约 4.1）。
        ForeignKeyConstraint(
            ["tenant_id", "pool_id", "sku_id"],
            ["stock_balances.tenant_id", "stock_balances.pool_id", "stock_balances.sku_id"],
        ),
        # 快照取自余额行，照余额行的写法。
        *[CheckConstraint(f"{column} >= 0", name=f"{column}_nonnegative") for column in QUANTITIES],
        CheckConstraint("balance_version >= 0", name="balance_version_nonnegative"),
        TABLE_OPTIONS,
    )

    merge_id: Mapped[str] = mapped_column(ID, primary_key=True)
    sku_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    pool_id: Mapped[str] = mapped_column(ID)
    stock_balance: Mapped[int] = mapped_column(BigInteger)
    reserved: Mapped[int] = mapped_column(BigInteger)
    unavailable: Mapped[int] = mapped_column(BigInteger)
    paid_pending_dispatch: Mapped[int] = mapped_column(BigInteger)
    balance_version: Mapped[int] = mapped_column(BigInteger)
