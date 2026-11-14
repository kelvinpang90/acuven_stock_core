"""目录的读取（契约 §2.1、§2.4、§2.6 第 6、7 条）。

read_* 与 find_* 都按租户过滤，不管事务：调用方在自己的连接上读完再结束事务；
之后的写命令在自己的事务里也用它们读出改后的表示。
*_owner 不按租户过滤，只在按路径 ID 找不到时用来区分不存在与属于别的租户
（后者记 cross_tenant，§7.2）。
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, and_, select

from app.domain.catalog import code_key
from app.models.catalog import Product, Sku


@dataclass(frozen=True)
class SkuRecord:
    """SKU 的表示所需的字段（§2.6 第 7 条）；时间是数据库里的 UTC，不带时区。"""

    sku_id: str
    product_id: str
    sku_code: str
    barcode: str | None
    name: str
    reference_price: dict | None  # None 或 {"amount_minor", "currency"}
    status: str
    product_status: str  # 所属商品的状态
    merged_into_sku_id: str | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ProductRecord:
    product_id: str
    name: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
    skus: tuple[SkuRecord, ...]  # 按 sku_id 升序


_PRODUCT_COLUMNS = (
    Product.product_id,
    Product.name,
    Product.status,
    Product.version,
    Product.created_at,
    Product.updated_at,
)


def _sku_query():
    """SKU 连同所属商品的状态，按 sku_id 升序。"""
    return (
        select(
            Sku.sku_id,
            Sku.product_id,
            Sku.sku_code,
            Sku.barcode,
            Sku.name,
            Sku.reference_price_amount_minor,
            Sku.reference_price_currency,
            Sku.status,
            Product.status.label("product_status"),
            Sku.merged_into_sku_id,
            Sku.version,
            Sku.created_at,
            Sku.updated_at,
        )
        .select_from(Sku)
        .join(
            Product,
            and_(Product.tenant_id == Sku.tenant_id, Product.product_id == Sku.product_id),
        )
        .order_by(Sku.sku_id)
    )


def _sku_record(row) -> SkuRecord:
    amount = row["reference_price_amount_minor"]
    # 数据库保证金额与币种同时有或同时无。
    price = None
    if amount is not None:
        price = {"amount_minor": amount, "currency": row["reference_price_currency"]}
    return SkuRecord(
        sku_id=row["sku_id"],
        product_id=row["product_id"],
        sku_code=row["sku_code"],
        barcode=row["barcode"],
        name=row["name"],
        reference_price=price,
        status=row["status"],
        product_status=row["product_status"],
        merged_into_sku_id=row["merged_into_sku_id"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _skus(connection: Connection, tenant_id: str, *criteria) -> list[SkuRecord]:
    statement = _sku_query().where(Sku.tenant_id == tenant_id, *criteria)
    return [_sku_record(row) for row in connection.execute(statement).mappings()]


def read_product(connection: Connection, tenant_id: str, product_id: str) -> ProductRecord | None:
    """本租户的商品连同它的全部 SKU（含已合并的）；不存在或属于别的租户为 None。"""
    tenant_product = and_(Product.tenant_id == tenant_id, Product.product_id == product_id)
    row = connection.execute(select(*_PRODUCT_COLUMNS).where(tenant_product)).mappings().first()
    if row is None:
        return None
    skus = _skus(connection, tenant_id, Sku.product_id == product_id)
    return ProductRecord(**row, skus=tuple(skus))


def read_sku(connection: Connection, tenant_id: str, sku_id: str) -> SkuRecord | None:
    """本租户的 SKU，已合并的照常返回；不存在或属于别的租户为 None。"""
    found = _skus(connection, tenant_id, Sku.sku_id == sku_id)
    return found[0] if found else None


def read_skus(connection: Connection, tenant_id: str, sku_ids: Collection[str]) -> list[SkuRecord]:
    """sku_ids 里属于本租户的 SKU，按 sku_id 升序；不存在或属于别的租户的不出现。"""
    if not sku_ids:
        return []
    return _skus(connection, tenant_id, Sku.sku_id.in_(list(sku_ids)))


def _find_by_code(connection: Connection, tenant_id: str, column, value: str) -> SkuRecord | None:
    # 编码类列的排序规则不区分大小写，等值查询走唯一索引；
    # 再按比较键复核，与 §2.6 第 1 条的判重一致。
    key = code_key(value)
    for sku in _skus(connection, tenant_id, column == value):
        if code_key(getattr(sku, column.key)) == key:
            return sku
    return None


def find_sku_by_code(connection: Connection, tenant_id: str, sku_code: str) -> SkuRecord | None:
    """按当前编码查（sku_code 已规范化，按比较键匹配）；退役编码查不到。"""
    return _find_by_code(connection, tenant_id, Sku.sku_code, sku_code)


def find_sku_by_barcode(connection: Connection, tenant_id: str, barcode: str) -> SkuRecord | None:
    """按当前条码查（barcode 已规范化、非空，按比较键匹配）。"""
    return _find_by_code(connection, tenant_id, Sku.barcode, barcode)


def product_owner(connection: Connection, product_id: str) -> str | None:
    """商品所属的租户；不存在为 None。"""
    statement = select(Product.tenant_id).where(Product.product_id == product_id)
    return connection.execute(statement).scalar_one_or_none()


def sku_owner(connection: Connection, sku_id: str) -> str | None:
    """SKU 所属的租户；不存在为 None。"""
    statement = select(Sku.tenant_id).where(Sku.sku_id == sku_id)
    return connection.execute(statement).scalar_one_or_none()
