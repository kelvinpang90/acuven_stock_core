"""批量导入的读取与写入（契约 §2.3）。

表 catalog_imports 一次导入一行。rows 列的每个元素是一行
（stored_rows 生成，row_previews 读回）：
  index；
  original：调用方这一行的原文，json.dumps（ensure_ascii 为假）成字符串再存。
    不把原始值直接放进 JSON 列：MySQL 的 JSON 列限嵌套 100 层，
    超出 64 位的整数会转成浮点；
  external_ref、product_ref：行里是字符串时为原值，否则为 None（回显用）；
  result、invalid_fields：预览结果；
  values：只在 ok 时有，规范化后的字段值
    （只有字符串、null 与不超过 2^53−1 的整数）。
result 列确认后才有：{"rows": [逐行结果]}，逐行结果由接口层给出、原样存。

read_import、lock_import 按租户过滤，不管事务；import_owner 不按租户过滤，
只在按路径 ID 找不到时用来区分不存在与属于别的租户（后者记 cross_tenant）。
写入在调用方已开的事务里运行（幂等执行器的事务），自己不提交、不回滚。
时间都按数据库当前时间：有效期在插入的同一条 SQL 里算，过期在读的 SQL 里判定。
"""

import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import Connection, insert, literal_column, select, update
from sqlalchemy.dialects.mysql import DATETIME

from app.domain.catalog import code_key
from app.domain.catalog_imports import (
    EXTERNAL_REF,
    PRODUCT_REF,
    ImportedSku,
    RowPreview,
)
from app.domain.ids import new_id
from app.models.catalog import NOW, CatalogImport, Sku, SkuCodeClaim

# 库里的状态；expired 不存，由 previewed 且 expires_at 不晚于数据库当前时间推出。
PREVIEWED = "previewed"
CONFIRMED = "confirmed"
EXPIRED = "expired"

_EXPIRES_AT = literal_column("CURRENT_TIMESTAMP(6) + INTERVAL 24 HOUR", DATETIME(fsp=6))
# MySQL 返回 0 或 1，_record 转成 bool。
_EXPIRED = CatalogImport.expires_at <= literal_column("CURRENT_TIMESTAMP(6)", DATETIME(fsp=6))


@dataclass(frozen=True)
class ImportRecord:
    """一次导入；时间是数据库里的 UTC，不带时区。"""

    import_id: str
    status: str  # 库里的 previewed 或 confirmed
    expired: bool  # expires_at 不晚于读的时候的数据库当前时间
    expires_at: datetime
    created_at: datetime
    confirmed_at: datetime | None
    rows: list[dict]
    result: dict | None

    @property
    def current_status(self) -> str:
        """表示里的 status：previewed、expired 或 confirmed。"""
        if self.status == CONFIRMED:
            return CONFIRMED
        return EXPIRED if self.expired else PREVIEWED


def echo(value) -> str | None:
    """external_ref、product_ref 的回显。

    行里是字符串时原样，否则（没带、数字、对象等）为 None。
    """
    return value if isinstance(value, str) else None


# --- rows 列的存放格式 ---


def stored_rows(rows: Sequence[Mapping], previews: Sequence[RowPreview]) -> list[dict]:
    """各行原文与预览结果；previews 是 preview_import(rows, …) 的结果。"""
    return [
        {
            "index": preview.index,
            "original": json.dumps(row, ensure_ascii=False),
            EXTERNAL_REF: echo(preview.external_ref),
            PRODUCT_REF: echo(preview.product_ref),
            "result": preview.result,
            "invalid_fields": list(preview.invalid_fields),
            "values": None if preview.values is None else asdict(preview.values),
        }
        for row, preview in zip(rows, previews, strict=True)
    ]


def row_previews(rows: Iterable[Mapping]) -> list[RowPreview]:
    """把 rows 列读回预览的逐行结果，供汇总与确认计划用。

    external_ref、product_ref 是回显值：不是字符串的原值读回为 None。
    两者在汇总与确认计划里只用来分组（product_ref 不合规的行不归任何组），
    所以按读回的值分组与按原值分组的结果相同。
    """
    return [
        RowPreview(
            index=row["index"],
            external_ref=row[EXTERNAL_REF],
            product_ref=row[PRODUCT_REF],
            result=row["result"],
            invalid_fields=tuple(row["invalid_fields"]),
            values=None if row["values"] is None else ImportedSku(**row["values"]),
        )
        for row in rows
    ]


# --- 查现有目录 ---


def existing_code_keys(
    connection: Connection,
    tenant_id: str,
    sku_codes: Collection[str],
    barcodes: Collection[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """本租户里与 sku_codes、barcodes 相同的现有编码与条码的比较键（code_key）。

    编码查 sku_code_claims（任何 SKU 的当前与退役编码）；
    条码查 skus 的当前条码（任何状态的 SKU）。
    编码类列的排序规则不区分大小写，IN 查出的可能多于比较键相等的，
    由调用方按比较键判定。
    """
    claimed = frozenset()
    if sku_codes:
        statement = select(SkuCodeClaim.code).where(
            SkuCodeClaim.tenant_id == tenant_id,
            SkuCodeClaim.code.in_(sorted(set(sku_codes))),
        )
        claimed = frozenset(code_key(code) for code in connection.execute(statement).scalars())
    taken = frozenset()
    if barcodes:
        statement = select(Sku.barcode).where(
            Sku.tenant_id == tenant_id, Sku.barcode.in_(sorted(set(barcodes)))
        )
        found = connection.execute(statement).scalars()
        taken = frozenset(code_key(barcode) for barcode in found)
    return claimed, taken


# --- 读 ---


def _import_query():
    return select(
        CatalogImport.import_id,
        CatalogImport.status,
        _EXPIRED.label("expired"),
        CatalogImport.expires_at,
        CatalogImport.created_at,
        CatalogImport.confirmed_at,
        CatalogImport.rows,
        CatalogImport.result,
    )


def _record(row) -> ImportRecord | None:
    if row is None:
        return None
    return ImportRecord(**(dict(row) | {"expired": bool(row["expired"])}))


def read_import(connection: Connection, tenant_id: str, import_id: str) -> ImportRecord | None:
    """本租户的导入；不存在或属于别的租户为 None。"""
    statement = _import_query().where(
        CatalogImport.tenant_id == tenant_id, CatalogImport.import_id == import_id
    )
    return _record(connection.execute(statement).mappings().first())


def lock_import(connection: Connection, tenant_id: str, import_id: str) -> ImportRecord | None:
    """同 read_import，但用 SELECT … FOR UPDATE 读（确认用）。"""
    statement = (
        _import_query()
        .where(CatalogImport.tenant_id == tenant_id, CatalogImport.import_id == import_id)
        .with_for_update()
    )
    return _record(connection.execute(statement).mappings().first())


def import_owner(connection: Connection, import_id: str) -> str | None:
    """导入所属的租户；不存在为 None。"""
    statement = select(CatalogImport.tenant_id).where(CatalogImport.import_id == import_id)
    return connection.execute(statement).scalar_one_or_none()


# --- 写 ---


def insert_import(
    connection: Connection,
    tenant_id: str,
    rows: list[dict],
    *,
    credential_id: str,
    actor_ref: str,
    request_id: str,
) -> str:
    """保存一次预览：previewed，有效期为数据库当前时间加 24 小时；返回 import_id。

    rows 是 stored_rows 的结果；凭据、操作者与请求 ID 取自预览请求。
    """
    import_id = new_id("imp")
    connection.execute(
        insert(CatalogImport).values(
            import_id=import_id,
            tenant_id=tenant_id,
            status=PREVIEWED,
            expires_at=_EXPIRES_AT,
            rows=rows,
            credential_id=credential_id,
            actor_ref=actor_ref,
            request_id=request_id,
        )
    )
    return import_id


def confirm_import(
    connection: Connection,
    tenant_id: str,
    import_id: str,
    result_rows: list[dict],
    *,
    credential_id: str,
    actor_ref: str,
    request_id: str,
) -> None:
    """把加了锁的导入改为 confirmed。

    存逐行结果与确认的凭据、操作者、请求 ID，确认时间取数据库当前时间。
    """
    connection.execute(
        update(CatalogImport)
        .where(CatalogImport.tenant_id == tenant_id, CatalogImport.import_id == import_id)
        .values(
            status=CONFIRMED,
            result={"rows": result_rows},
            confirmed_credential_id=credential_id,
            confirmed_actor_ref=actor_ref,
            confirmed_request_id=request_id,
            confirmed_at=NOW,
        )
    )
