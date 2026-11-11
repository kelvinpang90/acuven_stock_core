"""商品身份目录的表（契约第 1 节、2.1、2.2；租户隔离见第 10 节第 1 条）。

只有表结构与数据库约束。版本比较、改编码时的归属检查等由服务层在同一事务里做（契约 2.2）。
"""

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# ID 类列与 status 列精确比较；编码类列唯一性不区分大小写（契约 2.2 数据库列约定）。
EXACT_COLLATION = "utf8mb4_bin"
CODE_COLLATION = "utf8mb4_0900_ai_ci"
TABLE_OPTIONS = {"mysql_charset": "utf8mb4", "mysql_collate": CODE_COLLATION}

ID = String(32, collation=EXACT_COLLATION)
STATUS = String(16, collation=EXACT_COLLATION)
CODE = String(64, collation=CODE_COLLATION)
TIMESTAMP = DATETIME(fsp=6)
NOW = text("CURRENT_TIMESTAMP(6)")


def _status() -> CheckConstraint:
    return CheckConstraint("status IN ('active', 'inactive')", name="status")


def _version() -> CheckConstraint:
    return CheckConstraint("version >= 1", name="version")


def _code(column: str) -> CheckConstraint:
    # 非空串且首尾无空格；值为 NULL 时不受此约束（条码可空）。
    return CheckConstraint(
        f"{column} <> '' AND TRIM({column}) = {column}", name=f"{column}_trimmed"
    )


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (_status(), TABLE_OPTIONS)

    tenant_id: Mapped[str] = mapped_column(ID, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(STATUS)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        # 复合外键 (tenant_id, product_id) 的引用目标。
        UniqueConstraint("tenant_id", "product_id"),
        _status(),
        _version(),
        TABLE_OPTIONS,
    )

    product_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(STATUS)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class Sku(Base):
    __tablename__ = "skus"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "product_id"], ["products.tenant_id", "products.product_id"]
        ),
        # 只能合并到同租户的 SKU（契约第 10 节第 1 条）。
        ForeignKeyConstraint(
            ["tenant_id", "merged_into_sku_id"], ["skus.tenant_id", "skus.sku_id"]
        ),
        # 复合外键 (tenant_id, sku_id) 的引用目标。
        UniqueConstraint("tenant_id", "sku_id"),
        UniqueConstraint("tenant_id", "sku_code"),
        # 空条码存 NULL；MySQL 唯一索引允许多个 NULL。
        UniqueConstraint("tenant_id", "barcode"),
        _status(),
        _version(),
        _code("sku_code"),
        _code("barcode"),
        CheckConstraint(
            "(reference_price_amount_minor IS NULL) = (reference_price_currency IS NULL)",
            name="reference_price_pair",
        ),
        CheckConstraint("reference_price_amount_minor >= 0", name="reference_price_nonnegative"),
        CheckConstraint("merged_into_sku_id <> sku_id", name="merged_not_self"),
        CheckConstraint(
            "merged_into_sku_id IS NULL OR status = 'inactive'", name="merged_inactive"
        ),
        TABLE_OPTIONS,
    )

    sku_id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID)
    product_id: Mapped[str] = mapped_column(ID)
    sku_code: Mapped[str] = mapped_column(CODE)
    barcode: Mapped[str | None] = mapped_column(CODE)
    name: Mapped[str] = mapped_column(String(200))
    reference_price_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    reference_price_currency: Mapped[str | None] = mapped_column(CHAR(3))
    status: Mapped[str] = mapped_column(STATUS)
    version: Mapped[int] = mapped_column(Integer)
    merged_into_sku_id: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class SkuCodeHistory(Base):
    """只追加；无唯一约束，同一 SKU 可以改回自己用过的编码。"""

    __tablename__ = "sku_code_history"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "sku_id"], ["skus.tenant_id", "skus.sku_id"]),
        _code("old_code"),
        _code("new_code"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ID)
    sku_id: Mapped[str] = mapped_column(ID)
    old_code: Mapped[str] = mapped_column(CODE)
    new_code: Mapped[str] = mapped_column(CODE)
    actor_ref: Mapped[str] = mapped_column(String(128, collation=EXACT_COLLATION))
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)


class SkuCodeClaim(Base):
    """每个用过的编码登记一次，永久归属第一次使用它的 SKU（契约 2.2）。

    SKU 表不反向外键到这里：双向外键成环，MySQL 无法延迟检查。
    """

    __tablename__ = "sku_code_claims"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "sku_id"], ["skus.tenant_id", "skus.sku_id"]),
        _code("code"),
        TABLE_OPTIONS,
    )

    tenant_id: Mapped[str] = mapped_column(ID, primary_key=True)
    code: Mapped[str] = mapped_column(CODE, primary_key=True)
    sku_id: Mapped[str] = mapped_column(ID)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
