"""目录的读取与写入（契约 §2.1、§2.2、§2.4、§2.6、§2.7）。

read_* 与 find_* 都按租户过滤，不管事务：调用方在自己的连接上读完再结束事务；
之后的写命令在自己的事务里也用它们读出改后的表示。
*_owner 不按租户过滤，只在按路径 ID 找不到时用来区分不存在与属于别的租户
（后者记 cross_tenant，§7.2）。

写入函数在调用方已开的事务里运行（幂等执行器的事务，已取目录写锁），自己不提交、不回滚；
与现有目录冲突抛 CatalogConflict，由调用方撤销已做的写入。
sku_conflicts、create_sku 与 record_change 供之后的修改与批量导入共用。
"""

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, and_, insert, select
from sqlalchemy.exc import IntegrityError

from app.domain.catalog import (
    ACTIVE,
    BARCODE,
    BARCODE_TAKEN,
    CLAIM_INSERT,
    EVENT_TYPES,
    PRODUCT,
    PRODUCT_CREATED,
    SKU,
    SKU_CODE,
    SKU_CODE_RETIRED,
    SKU_CODE_TAKEN,
    SKU_CREATED,
    Conflict,
    code_key,
    decide_code_claim,
    product_created_changes,
    sku_created_changes,
)
from app.domain.ids import new_id
from app.models.catalog import CatalogChange, Product, Sku, SkuCodeClaim

_DUPLICATE_ENTRY = 1062  # MySQL ER_DUP_ENTRY
# 唯一约束名（app/db.py 的命名约定）；MySQL 8 的报错写成 '表名.约束名'。
_SKU_CODE_UNIQUE = "uq_skus_tenant_id_sku_code"
_BARCODE_UNIQUE = "uq_skus_tenant_id_barcode"
_DUPLICATE_KEY = re.compile(r"for key '(?:[^']*\.)?([^'.]*)'$")


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


# --- 写入 ---


@dataclass(frozen=True)
class SkuFields:
    """新建 SKU 的字段，都已按 §2.6 第 1 条规范化（空条码为 None）。"""

    sku_code: str
    barcode: str | None
    name: str
    reference_price: dict | None  # None 或 {"amount_minor", "currency"}


@dataclass(frozen=True)
class ChangeAuthor:
    """变更记录里取自本请求的列（§2.7）；reason 是正文规范化后的值。

    import_id 只有导入确认建的记录才有。
    """

    credential_id: str
    actor_ref: str
    actor_role: str
    request_id: str
    idempotency_key: str
    reason: str | None
    import_id: str | None = None


class CatalogConflict(Exception):
    """与现有目录冲突（§2.2、§2.6 第 2 条）；conflicts 是 details.conflicts 的各项。"""

    def __init__(self, conflicts: Sequence[Conflict]):
        super().__init__("conflicts with the existing catalog")
        self.conflicts = list(conflicts)


def _claim_owner(connection: Connection, tenant_id: str, sku_code: str) -> tuple[str, str] | None:
    """编码在归属表里登记给的 SKU 与那个 SKU 的当前编码；没登记为 None。"""
    statement = (
        select(SkuCodeClaim.code, SkuCodeClaim.sku_id, Sku.sku_code)
        .select_from(SkuCodeClaim)
        .join(
            Sku,
            and_(Sku.tenant_id == SkuCodeClaim.tenant_id, Sku.sku_id == SkuCodeClaim.sku_id),
        )
        .where(SkuCodeClaim.tenant_id == tenant_id, SkuCodeClaim.code == sku_code)
    )
    # 同 _find_by_code：等值查询走主键，再按比较键复核。
    key = code_key(sku_code)
    for code, sku_id, current_code in connection.execute(statement):
        if code_key(code) == key:
            return sku_id, current_code
    return None


def sku_conflicts(
    connection: Connection, tenant_id: str, sku: SkuFields, index: int | None = None
) -> list[Conflict]:
    """新建这个 SKU 会与本租户现有目录的哪些冲突（§2.2）；index 原样放进各项。

    编码按归属判定：登记给别的 SKU 且是它的当前编码为 SKU_CODE_TAKEN，否则 SKU_CODE_RETIRED；
    非空条码与现有 SKU 的当前条码比较键相同为 BARCODE_TAKEN。
    """
    conflicts = []
    owner = _claim_owner(connection, tenant_id, sku.sku_code)
    owner_sku_id, current_code = owner if owner is not None else (None, None)
    matches = current_code is not None and code_key(current_code) == code_key(sku.sku_code)
    decision = decide_code_claim(owner_sku_id, matches, None)
    if decision != CLAIM_INSERT:
        conflicts.append(Conflict(SKU_CODE, sku.sku_code, decision, index))
    if sku.barcode is not None:
        if find_sku_by_barcode(connection, tenant_id, sku.barcode) is not None:
            conflicts.append(Conflict(BARCODE, sku.barcode, BARCODE_TAKEN, index))
    return conflicts


def _duplicate_key(error: IntegrityError) -> str | None:
    """撞唯一约束时为约束名（主键为 PRIMARY）；别的完整性错误为 None。"""
    args = getattr(error.orig, "args", ())
    if args[:1] != (_DUPLICATE_ENTRY,) or len(args) < 2:
        return None
    match = _DUPLICATE_KEY.search(str(args[1]))
    return None if match is None else match.group(1)


def record_change(
    connection: Connection,
    tenant_id: str,
    author: ChangeAuthor,
    *,
    event_type: str,
    object_type: str,
    object_id: str,
    product_id: str,
    version: int,
    changes: dict,
) -> None:
    """追加一条目录变更记录（§2.7）；version 是对象这次改动后的版本。"""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown catalog event type: {event_type!r}")
    connection.execute(
        insert(CatalogChange).values(
            tenant_id=tenant_id,
            event_type=event_type,
            object_type=object_type,
            object_id=object_id,
            product_id=product_id,
            version=version,
            changes=changes,
            credential_id=author.credential_id,
            actor_ref=author.actor_ref,
            actor_role=author.actor_role,
            reason=author.reason,
            import_id=author.import_id,
            request_id=author.request_id,
            idempotency_key=author.idempotency_key,
        )
    )


def create_sku(
    connection: Connection,
    tenant_id: str,
    product_id: str,
    sku: SkuFields,
    author: ChangeAuthor,
    index: int | None = None,
) -> str:
    """建一个 SKU（active、版本 1）：先插 SKU 行，再插归属行，再写 sku_created；返回 sku_id。

    不查冲突（调用方先用 sku_conflicts 查）。写入仍撞上唯一约束时抛 CatalogConflict，
    只列撞上的那一项：撞 skus 的编码约束为 SKU_CODE_TAKEN、条码约束为 BARCODE_TAKEN，
    撞归属表主键为 SKU_CODE_RETIRED。已做的写入由调用方撤销。
    """
    sku_id = new_id("sku")
    price = sku.reference_price
    try:
        connection.execute(
            insert(Sku).values(
                sku_id=sku_id,
                tenant_id=tenant_id,
                product_id=product_id,
                sku_code=sku.sku_code,
                barcode=sku.barcode,
                name=sku.name,
                reference_price_amount_minor=None if price is None else price["amount_minor"],
                reference_price_currency=None if price is None else price["currency"],
                status=ACTIVE,
                version=1,
            )
        )
    except IntegrityError as error:
        key = _duplicate_key(error)
        if key == _SKU_CODE_UNIQUE:
            conflict = Conflict(SKU_CODE, sku.sku_code, SKU_CODE_TAKEN, index)
        elif key == _BARCODE_UNIQUE and sku.barcode is not None:
            conflict = Conflict(BARCODE, sku.barcode, BARCODE_TAKEN, index)
        else:
            raise
        raise CatalogConflict([conflict]) from None
    try:
        connection.execute(
            insert(SkuCodeClaim).values(tenant_id=tenant_id, code=sku.sku_code, sku_id=sku_id)
        )
    except IntegrityError as error:
        # 归属表只有主键这一个唯一约束：编码已登记，但不是任何 SKU 的当前编码。
        if _duplicate_key(error) is None:
            raise
        conflict = Conflict(SKU_CODE, sku.sku_code, SKU_CODE_RETIRED, index)
        raise CatalogConflict([conflict]) from None
    record_change(
        connection,
        tenant_id,
        author,
        event_type=SKU_CREATED,
        object_type=SKU,
        object_id=sku_id,
        product_id=product_id,
        version=1,
        changes=sku_created_changes(sku.sku_code, sku.barcode, sku.name, sku.reference_price),
    )
    return sku_id


def create_product(
    connection: Connection,
    tenant_id: str,
    name: str,
    skus: Sequence[SkuFields],
    author: ChangeAuthor,
) -> str:
    """新建商品连同它的 SKU（§2.6 第 2 条）；返回 product_id。

    先逐个 SKU 查冲突，有就抛 CatalogConflict 列出全部（带 index），什么也不写；
    否则建商品（active、版本 1）、写 product_created，再按请求顺序建各 SKU。
    请求内的编码与条码重复由调用方先拒绝。
    """
    conflicts = [
        conflict
        for index, sku in enumerate(skus)
        for conflict in sku_conflicts(connection, tenant_id, sku, index)
    ]
    if conflicts:
        raise CatalogConflict(conflicts)
    product_id = new_id("prd")
    connection.execute(
        insert(Product).values(
            product_id=product_id, tenant_id=tenant_id, name=name, status=ACTIVE, version=1
        )
    )
    record_change(
        connection,
        tenant_id,
        author,
        event_type=PRODUCT_CREATED,
        object_type=PRODUCT,
        object_id=product_id,
        product_id=product_id,
        version=1,
        changes=product_created_changes(name),
    )
    for index, sku in enumerate(skus):
        create_sku(connection, tenant_id, product_id, sku, author, index)
    return product_id


def add_sku(
    connection: Connection, tenant_id: str, product_id: str, sku: SkuFields, author: ChangeAuthor
) -> str:
    """往本租户已有的商品加一个 SKU（§2.6 第 3 条），不改商品的版本；返回 sku_id。

    商品是否存在由调用方先查。冲突时抛 CatalogConflict（各项不带 index），什么也不写。
    """
    conflicts = sku_conflicts(connection, tenant_id, sku)
    if conflicts:
        raise CatalogConflict(conflicts)
    return create_sku(connection, tenant_id, product_id, sku, author)
