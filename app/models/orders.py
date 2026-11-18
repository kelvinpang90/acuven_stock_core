"""网店订单的表：预留与订单行（契约 5.1、5.6、5.7 的存放；租户隔离见第 10 节第 1 条）。

只有表结构与数据库约束。状态、释放原因与数量上下限取自 app/domain/reservations.py。
expires_at = created_at + ttl_seconds、订单行的 pool_id 与所属预留相同、
同一预留里 sku_id 不重复，都由服务层在同一事务里保证（契约 5.6）。
"""

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.domain.reservations import (
    ACTIVE,
    CLOSED_REFUNDED,
    COMMITTED,
    EXPIRED,
    LATE_PAYMENT_REVIEW,
    LINE_QUANTITY_MAX,
    LINE_QUANTITY_MIN,
    RELEASE_REASONS,
    RELEASED,
    STATUSES,
    TTL_MAX,
    TTL_MIN,
)
from app.models.catalog import EXACT_COLLATION, ID, TABLE_OPTIONS, TIMESTAMP
from app.models.inventory import CODE32, REF, ZERO

# 释放时才有、且必有的列；释放的操作者两列可选，另行约束。
RELEASE_COLUMNS = ("release_reason", "released_credential_id", "released_request_id")

# 收到付款之后的三种状态（契约 5.7）：确认，或转入待审、再结案。
PAID_STATUSES = (COMMITTED, LATE_PAYMENT_REVIEW, CLOSED_REFUNDED)

# 这三种状态才有、且必有的列；确认的操作者两列可选，另行约束。
COMMIT_COLUMNS = (
    "payment_ref",
    "paid_at",
    "commit_request_hash",
    "commit_credential_id",
    "commit_request_id",
)

# 待审结案时才有、且必有的列；结案的操作者两列可选，另行约束。
REVIEW_CLOSED_COLUMNS = (
    "review_closed_at",
    "review_closed_credential_id",
    "review_closed_request_id",
)

# 订单行的五个累计量（契约 5.1）。
LINE_ACCUMULATORS = (
    "committed_qty",
    "dispatched_qty",
    "refund_restocked_qty",
    "returned_sellable_qty",
    "returned_unsellable_qty",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


class Reservation(Base):
    """一个订单一行（契约 5.6）。reservation_id 的前缀 rsv_ 由应用生成。

    status 与 release_reason 用 VARCHAR(32)，是契约 2.2 状态列 VARCHAR(16) 的例外。
    """

    __tablename__ = "reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "pool_id"], ["stock_pools.tenant_id", "stock_pools.pool_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"], ["sales_channels.tenant_id", "sales_channels.channel_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        # 与上一条都以 tenant_id 开头、指向 credentials，按命名约定会同名，这里显式命名。
        ForeignKeyConstraint(
            ["tenant_id", "released_credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name="fk_reservations_released_credential_id_credentials",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "commit_credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name="fk_reservations_commit_credential_id_credentials",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "review_closed_credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name="fk_reservations_review_closed_credential_id_credentials",
        ),
        # 业务引用：一个订单只有一个预留（契约 5.2 第 2 条）。
        UniqueConstraint("tenant_id", "channel_id", "order_ref"),
        # 业务引用：一笔付款只确认一个订单（契约 5.7）；MySQL 唯一索引允许多个 NULL。
        UniqueConstraint("tenant_id", "channel_id", "payment_ref"),
        # 复合外键 (tenant_id, reservation_id) 的引用目标。
        UniqueConstraint("tenant_id", "reservation_id"),
        # 定时任务按 expires_at 扫描 active 的预留。
        Index("ix_reservations_status_expires_at", "status", "expires_at"),
        CheckConstraint(_in("status", STATUSES), name="status"),
        CheckConstraint(_in("release_reason", RELEASE_REASONS), name="release_reason"),
        CheckConstraint(f"ttl_seconds BETWEEN {TTL_MIN} AND {TTL_MAX}", name="ttl_seconds"),
        # status 为未知值时这条与 closed_at 那条不拦，只由 status 那条拒绝。
        CheckConstraint(
            " AND ".join(
                f"(status = '{RELEASED}') = ({column} IS NOT NULL)" for column in RELEASE_COLUMNS
            ),
            name="released_columns",
        ),
        # committed 及之后的状态见下面的 paid_closed_at。
        CheckConstraint(
            f"(status <> '{ACTIVE}' OR closed_at IS NULL)"
            f" AND (status NOT IN ('{RELEASED}', '{EXPIRED}') OR closed_at IS NOT NULL)",
            name="closed_at",
        ),
        CheckConstraint(
            " AND ".join(
                f"({_in('status', PAID_STATUSES)}) = ({column} IS NOT NULL)"
                for column in COMMIT_COLUMNS
            ),
            name="commit_columns",
        ),
        CheckConstraint(
            f"(status = '{COMMITTED}') = (committed_at IS NOT NULL)", name="committed_at"
        ),
        # 只有经 LATE_SALE_COMMIT 确认的才为真；转入待审时不设（契约 5.7 表示）。
        CheckConstraint(f"status = '{COMMITTED}' OR NOT late", name="late"),
        CheckConstraint(
            " AND ".join(
                f"(status = '{CLOSED_REFUNDED}') = ({column} IS NOT NULL)"
                for column in REVIEW_CLOSED_COLUMNS
            ),
            name="review_closed_columns",
        ),
        # 离开 active 的时间：正常确认为确认时间，经到期再确认的保留到期关单时间，
        # 故障补交新建的为创建时间（契约 5.7 存放）。
        CheckConstraint(
            f"NOT ({_in('status', PAID_STATUSES)}) OR closed_at IS NOT NULL",
            name="paid_closed_at",
        ),
        # 预留与释放都不要求角色（契约第 10 节第 3 条）：有操作者时才有角色，反之亦然。
        CheckConstraint("(actor_ref IS NULL) = (actor_role IS NULL)", name="actor"),
        CheckConstraint(
            "(released_actor_ref IS NULL) = (released_actor_role IS NULL)", name="released_actor"
        ),
        # 确认与结案同样不要求角色，也不按状态约束。
        CheckConstraint(
            "(commit_actor_ref IS NULL) = (commit_actor_role IS NULL)", name="commit_actor"
        ),
        CheckConstraint(
            "(review_closed_actor_ref IS NULL) = (review_closed_actor_role IS NULL)",
            name="review_closed_actor",
        ),
        # expires_at = created_at + ttl_seconds 由服务层保证，这里只查先后。
        CheckConstraint("expires_at > created_at", name="expires_at"),
        TABLE_OPTIONS,
    )

    reservation_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    channel_id: Mapped[str] = mapped_column(ID)
    pool_id: Mapped[str] = mapped_column(ID)
    order_ref: Mapped[str] = mapped_column(REF)
    status: Mapped[str] = mapped_column(CODE32)
    # 创建时用的有效期（契约 5.6，Q-53）。
    ttl_seconds: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    # 预留请求的规范化请求摘要 SHA-256 的小写十六进制（契约 7.1）。
    request_hash: Mapped[str] = mapped_column(CHAR(64, collation=EXACT_COLLATION))
    credential_id: Mapped[str] = mapped_column(ID)
    actor_ref: Mapped[str | None] = mapped_column(REF)
    actor_role: Mapped[str | None] = mapped_column(CODE32)
    request_id: Mapped[str] = mapped_column(REF)
    # 本次命令的数据库时间，同一命令只取一次，与 expires_at 一起由服务层写入，所以没有默认值。
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    release_reason: Mapped[str | None] = mapped_column(CODE32)
    released_credential_id: Mapped[str | None] = mapped_column(ID)
    released_actor_ref: Mapped[str | None] = mapped_column(REF)
    released_actor_role: Mapped[str | None] = mapped_column(CODE32)
    released_request_id: Mapped[str | None] = mapped_column(REF)
    # 离开 active 的时间：释放或到期关单的数据库时间。
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    # 确认销售（或转入待审）记下的付款与确认（契约 5.7）。
    payment_ref: Mapped[str | None] = mapped_column(REF)
    paid_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    # 确认请求的请求摘要；晚到确认重试成功时改记成功的那次。
    commit_request_hash: Mapped[str | None] = mapped_column(CHAR(64, collation=EXACT_COLLATION))
    committed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    late: Mapped[bool] = mapped_column(Boolean, server_default=ZERO)
    # 故障补交新建或经故障补交确认、转入待审的为真，一旦为真不再改回（由服务层保证）。
    fault_mode: Mapped[bool] = mapped_column(Boolean, server_default=ZERO)
    commit_credential_id: Mapped[str | None] = mapped_column(ID)
    commit_actor_ref: Mapped[str | None] = mapped_column(REF)
    commit_actor_role: Mapped[str | None] = mapped_column(CODE32)
    commit_request_id: Mapped[str | None] = mapped_column(REF)
    # 待审结案（契约 5.7）。
    review_closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    review_closed_credential_id: Mapped[str | None] = mapped_column(ID)
    review_closed_actor_ref: Mapped[str | None] = mapped_column(REF)
    review_closed_actor_role: Mapped[str | None] = mapped_column(CODE32)
    review_closed_request_id: Mapped[str | None] = mapped_column(REF)


class OrderLine(Base):
    """契约 5.1 的订单行，一行一条（契约 5.6）。order_line_id 的前缀 oln_ 由应用生成。

    不加 (reservation_id, sku_id) 唯一：SKU 合并改挂之后，同一订单可能有两行指向同一个规范 SKU。
    """

    __tablename__ = "order_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "reservation_id"],
            ["reservations.tenant_id", "reservations.reservation_id"],
        ),
        # 余额行在预留前已预建（契约 4.1）。
        ForeignKeyConstraint(
            ["tenant_id", "pool_id", "sku_id"],
            ["stock_balances.tenant_id", "stock_balances.pool_id", "stock_balances.sku_id"],
        ),
        # 预留与订单一一对应，等价于契约 5.1 的 (tenant_id, channel_id, order_ref, line_ref)。
        UniqueConstraint("tenant_id", "reservation_id", "line_ref"),
        CheckConstraint(
            f"quantity BETWEEN {LINE_QUANTITY_MIN} AND {LINE_QUANTITY_MAX}", name="quantity"
        ),
        *[
            CheckConstraint(f"{column} >= 0", name=f"{column}_nonnegative")
            for column in LINE_ACCUMULATORS
        ],
        # V1 整单确认。
        CheckConstraint("committed_qty = 0 OR committed_qty = quantity", name="whole_commit"),
        CheckConstraint(
            "dispatched_qty + refund_restocked_qty <= committed_qty",
            name="dispatched_restocked_within_committed",
        ),
        CheckConstraint(
            "returned_sellable_qty + returned_unsellable_qty <= dispatched_qty",
            name="returned_within_dispatched",
        ),
        TABLE_OPTIONS,
    )

    order_line_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    reservation_id: Mapped[str] = mapped_column(ID)
    pool_id: Mapped[str] = mapped_column(ID)
    # 首次预留时固定，只在 SKU 合并执行时改挂到规范 SKU（契约 2.4 第 1 步）。
    sku_id: Mapped[str] = mapped_column(ID)
    line_ref: Mapped[str] = mapped_column(REF)
    quantity: Mapped[int] = mapped_column(BigInteger)
    committed_qty: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
    dispatched_qty: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
    refund_restocked_qty: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
    returned_sellable_qty: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
    returned_unsellable_qty: Mapped[int] = mapped_column(BigInteger, server_default=ZERO)
