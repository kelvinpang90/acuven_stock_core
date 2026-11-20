"""SKU 合并的表：合并单与录入实数时的快照（契约 2.4、2.8 的存放）。

租户隔离见第 10 节第 1 条。只有表结构与数据库约束。状态取自 app/domain/sku_merges.py，
merge_id 的前缀 smg_（MERGE_ID_PREFIX）由应用生成。
“合并单互不重叠”不在库里约束（MySQL 没有部分唯一索引），由新建锁两个 SKU 行后检查保证；
快照的 pool_id 与所属合并单的相同，由同一事务的服务层保证（契约 2.8）。
"""

from datetime import datetime

from sqlalchemy import (
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
from app.domain.sku_merges import (
    CANCELLED,
    COMPLETED,
    COUNTED,
    RECONCILE_PENDING,
    SELLING_PAUSED,
    STATUSES,
)
from app.models.catalog import ID, NOW, TABLE_OPTIONS, TIMESTAMP
from app.models.inventory import CODE32, REF, _quantity_invariants

# 录入实数后才有的列，同有同无；录入的操作者两列另行约束。
COUNT_COLUMNS = (
    "pool_id",
    "shelf_count",
    "unsellable_count",
    "counted_credential_id",
    "counted_request_id",
    "counted_at",
)
# 录入过实数的状态；cancelled 可能录入过也可能没有，不限。
COUNTED_STATUSES = (COUNTED, RECONCILE_PENDING, COMPLETED)

# 执行后才有、且必有的列；adjustment_entry_id 在两个差额都为 0 时为空，执行的操作者两列另行约束。
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

# 操作者两列同有同无的各组（新建的两列必有）。
ACTOR_GROUPS = ("counted", "executed", "completed", "cancelled")


def _in(column: str, values: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def _same_nullness(columns: tuple[str, ...]) -> str:
    first, *rest = columns
    return " AND ".join(f"({first} IS NULL) = ({column} IS NULL)" for column in rest)


def _present_exactly_in(statuses: tuple[str, ...], columns: tuple[str, ...]) -> str:
    # status 在 statuses 里时各列都非空，否则都空。
    in_statuses = _in("status", statuses)
    return " AND ".join(f"({in_statuses}) = ({column} IS NOT NULL)" for column in columns)


def _credential_fk(column: str) -> ForeignKeyConstraint:
    # 各条复合外键都以 tenant_id 开头，按命名约定会同名，显式命名，名字里带上第二列。
    return ForeignKeyConstraint(
        ["tenant_id", column],
        ["credentials.tenant_id", "credentials.credential_id"],
        name=f"fk_sku_merges_{column}_credentials",
    )


def _entry_fk(column: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", column],
        ["ledger_entries.tenant_id", "ledger_entries.entry_id"],
        name=f"fk_sku_merges_{column}_ledger_entries",
    )


class SkuMerge(Base):
    """一张合并单一行，不删除（契约 2.8）。

    status 用 VARCHAR(32)：reconcile_pending 有 17 个字符，同 reservations 的例外。
    """

    __tablename__ = "sku_merges"
    __table_args__ = (
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
        # pool_id 为空时不检查。
        ForeignKeyConstraint(
            ["tenant_id", "pool_id"],
            ["stock_pools.tenant_id", "stock_pools.pool_id"],
            name="fk_sku_merges_pool_id_stock_pools",
        ),
        _credential_fk("credential_id"),
        _credential_fk("counted_credential_id"),
        _credential_fk("executed_credential_id"),
        _credential_fk("completed_credential_id"),
        _credential_fk("cancelled_credential_id"),
        _entry_fk("transfer_out_entry_id"),
        _entry_fk("transfer_in_entry_id"),
        _entry_fk("adjustment_entry_id"),
        # 复合外键 (tenant_id, merge_id) 的引用目标。
        UniqueConstraint("tenant_id", "merge_id"),
        # 重叠检查与停售判定按 SKU 查；也支撑指向两个 SKU 的外键。
        Index("ix_sku_merges_tenant_id_source_sku_id", "tenant_id", "source_sku_id"),
        Index("ix_sku_merges_tenant_id_canonical_sku_id", "tenant_id", "canonical_sku_id"),
        CheckConstraint("source_sku_id <> canonical_sku_id", name="source_not_canonical"),
        CheckConstraint(_in("status", STATUSES), name="status"),
        # status 为未知值时以下各条不拦，只由 status 那条拒绝。
        CheckConstraint(
            _same_nullness(COUNT_COLUMNS)
            + f" AND (NOT ({_in('status', COUNTED_STATUSES)}) OR pool_id IS NOT NULL)"
            + f" AND (status <> '{SELLING_PAUSED}' OR pool_id IS NULL)",
            name="count_columns",
        ),
        CheckConstraint(
            _present_exactly_in(EXECUTED_STATUSES, EXECUTION_COLUMNS)
            + f" AND ({_in('status', EXECUTED_STATUSES)} OR adjustment_entry_id IS NULL)",
            name="execution_columns",
        ),
        CheckConstraint(
            _present_exactly_in((COMPLETED,), COMPLETION_COLUMNS), name="completion_columns"
        ),
        CheckConstraint(
            _present_exactly_in((CANCELLED,), CANCELLATION_COLUMNS), name="cancellation_columns"
        ),
        *[
            CheckConstraint(
                f"({group}_actor_ref IS NULL) = ({group}_actor_role IS NULL)",
                name=f"{group}_actor",
            )
            for group in ACTOR_GROUPS
        ],
        TABLE_OPTIONS,
    )

    merge_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    source_sku_id: Mapped[str] = mapped_column(ID)
    canonical_sku_id: Mapped[str] = mapped_column(ID)
    status: Mapped[str] = mapped_column(CODE32)
    # 执行时写进来源 SKU 的 sku_merged 变更记录（契约 2.8 新建）。
    reason: Mapped[str | None] = mapped_column(String(500))
    # 新建要求 stock_admin 角色，凭据与操作者必有。
    credential_id: Mapped[str] = mapped_column(ID)
    actor_ref: Mapped[str] = mapped_column(REF)
    actor_role: Mapped[str] = mapped_column(CODE32)
    request_id: Mapped[str] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    # 录入实数：池与两项合计实数，重新录入时整份替换。
    pool_id: Mapped[str | None] = mapped_column(ID)
    shelf_count: Mapped[int | None] = mapped_column(BigInteger)
    unsellable_count: Mapped[int | None] = mapped_column(BigInteger)
    counted_credential_id: Mapped[str | None] = mapped_column(ID)
    counted_actor_ref: Mapped[str | None] = mapped_column(REF)
    counted_actor_role: Mapped[str | None] = mapped_column(CODE32)
    counted_request_id: Mapped[str | None] = mapped_column(REF)
    counted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    # 执行：三条流水与改挂的订单行数（契约 2.8 执行第 9 步）。
    executed_credential_id: Mapped[str | None] = mapped_column(ID)
    executed_actor_ref: Mapped[str | None] = mapped_column(REF)
    executed_actor_role: Mapped[str | None] = mapped_column(CODE32)
    executed_request_id: Mapped[str | None] = mapped_column(REF)
    executed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    transfer_out_entry_id: Mapped[str | None] = mapped_column(ID)
    transfer_in_entry_id: Mapped[str | None] = mapped_column(ID)
    adjustment_entry_id: Mapped[str | None] = mapped_column(ID)
    repointed_line_count: Mapped[int | None] = mapped_column(BigInteger)
    completed_credential_id: Mapped[str | None] = mapped_column(ID)
    completed_actor_ref: Mapped[str | None] = mapped_column(REF)
    completed_actor_role: Mapped[str | None] = mapped_column(CODE32)
    completed_request_id: Mapped[str | None] = mapped_column(REF)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    cancelled_credential_id: Mapped[str | None] = mapped_column(ID)
    cancelled_actor_ref: Mapped[str | None] = mapped_column(REF)
    cancelled_actor_role: Mapped[str | None] = mapped_column(CODE32)
    cancelled_request_id: Mapped[str | None] = mapped_column(REF)
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class SkuMergeSnapshot(Base):
    """录入实数时一个 SKU 余额行的四个账面量与 balance_version（契约 2.4、2.8）。

    重新录入时整份替换（还没落账，不留历史）。
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
        # 快照取自余额行，照余额行的约束。
        *_quantity_invariants(),
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
