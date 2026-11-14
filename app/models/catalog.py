"""商品身份目录的表（契约第 1 节、2.1、2.2、2.3、2.7；租户隔离见第 10 节第 1 条）。

只有表结构与数据库约束。版本比较、改编码时的归属检查等由服务层在同一事务里做（契约 2.2）；
导入的逐行判定、事件类型的取值、目录写锁由应用做（契约 2.3、2.7）。
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
    Index,
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
# 与 app/models/inventory.py 的同名类型相同；那边导入本模块，这里不能反过来导入。
REF = String(128, collation=EXACT_COLLATION)
CODE32 = String(32, collation=EXACT_COLLATION)
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


# 确认后才有的列；预览时都为空，确认后都非空（契约 2.3）。
CONFIRMATION_COLUMNS = (
    "result",
    "confirmed_credential_id",
    "confirmed_actor_ref",
    "confirmed_request_id",
    "confirmed_at",
)


class CatalogImport(Base):
    """一次批量导入一行，永久保留（契约 2.3）。

    过期（expired）由 expires_at 推出，不存；确认时间记在 confirmed_at，没有 updated_at。
    """

    __tablename__ = "catalog_imports"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        # 与上一条都以 tenant_id 开头、指向 credentials，按命名约定会同名，这里显式命名。
        ForeignKeyConstraint(
            ["tenant_id", "confirmed_credential_id"],
            ["credentials.tenant_id", "credentials.credential_id"],
            name="fk_catalog_imports_confirmed_credential_id_credentials",
        ),
        # 复合外键 (tenant_id, import_id) 的引用目标。
        UniqueConstraint("tenant_id", "import_id"),
        CheckConstraint("status IN ('previewed', 'confirmed')", name="status"),
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
    status: Mapped[str] = mapped_column(STATUS)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    # 各行原文与预览结果。
    rows: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    # Python 的 None 存成 SQL NULL 而不是 JSON 的 null，否则预览行过不了 confirmation 检查。
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    # 预览要求 catalog_admin 角色，必有凭据与操作者。
    credential_id: Mapped[str] = mapped_column(ID)
    actor_ref: Mapped[str] = mapped_column(REF)
    request_id: Mapped[str] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
    confirmed_credential_id: Mapped[str | None] = mapped_column(ID)
    confirmed_actor_ref: Mapped[str | None] = mapped_column(REF)
    confirmed_request_id: Mapped[str | None] = mapped_column(REF)
    confirmed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class CatalogChange(Base):
    """目录变更记录：只追加，永久保留；既是目录审计，也是变更流的来源（契约 2.5、2.7）。

    id 按提交顺序递增靠目录写锁保证，不靠表结构。event_type 不加检查约束，由应用校验，
    以后加事件类型不改表。SKU 事件的 object_id 属于 product_id 由服务层保证。
    """

    __tablename__ = "catalog_changes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "product_id"], ["products.tenant_id", "products.product_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"], ["credentials.tenant_id", "credentials.credential_id"]
        ),
        # 导入确认建的才有，import_id 为空时不检查。
        ForeignKeyConstraint(
            ["tenant_id", "import_id"], ["catalog_imports.tenant_id", "catalog_imports.import_id"]
        ),
        # 变更流按租户、按 id 翻页；按对象查历史。
        Index("ix_catalog_changes_tenant_id_id", "tenant_id", "id"),
        Index("ix_catalog_changes_tenant_id_object_id_id", "tenant_id", "object_id", "id"),
        CheckConstraint("object_type IN ('product', 'sku')", name="object_type"),
        CheckConstraint(
            "object_type <> 'product' OR object_id = product_id", name="product_object"
        ),
        _version(),
        CheckConstraint("JSON_TYPE(changes) = 'OBJECT'", name="changes"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.tenant_id"))
    event_type: Mapped[str] = mapped_column(CODE32)
    object_type: Mapped[str] = mapped_column(STATUS)
    object_id: Mapped[str] = mapped_column(ID)
    product_id: Mapped[str] = mapped_column(ID)
    # 对象这次改动后的版本。
    version: Mapped[int] = mapped_column(Integer)
    # {字段名: {"before": …, "after": …}}
    changes: Mapped[dict[str, Any]] = mapped_column(JSON)
    # 目录写命令都要求 catalog_admin 角色，凭据、操作者与角色必有。
    credential_id: Mapped[str] = mapped_column(ID)
    actor_ref: Mapped[str] = mapped_column(REF)
    actor_role: Mapped[str] = mapped_column(CODE32)
    reason: Mapped[str | None] = mapped_column(String(500))
    import_id: Mapped[str | None] = mapped_column(ID)
    request_id: Mapped[str] = mapped_column(REF)
    idempotency_key: Mapped[str] = mapped_column(REF)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
