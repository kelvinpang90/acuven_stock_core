"""批量导入的读取与写入（契约 §2.3、§2.7）。

判定用 app/domain/catalog_imports.py 的纯函数；
建商品、SKU 与变更记录用 app/services/catalog.py。
read_import、import_owner 同 catalog 的 read_*、*_owner：
前者按租户过滤，后者只用来区分不存在与属于别的租户。
写入函数在调用方已开的事务里运行（幂等执行器的事务），自己不提交、不回滚。

rows 列每行一个对象：index、external_ref、product_ref、result、invalid_fields、
values（ok 行规范化后的字段值，其余为 null）、
source（行的原文按 json.dumps 序列化成的字符串）。
原文不直接放进 JSON 列：MySQL 的 JSON 列限嵌套 100 层，超出 64 位的整数会转成浮点。
external_ref、product_ref 在行里是字符串时原样，否则为 null：
存的与回显的都不含调用方的任意值；非字符串本来就不合规、不归任何组，换成 null 不改变分组与汇总。
result 列是逐行结果的列表，即表示里的 result。
"""

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import Connection, DateTime, insert, literal_column, select, text, update

from app.domain.catalog import InvalidField, code_key, normalize_barcode, normalize_sku_code
from app.domain.catalog_imports import (
    BARCODE,
    CREATED,
    FAILED,
    SKIPPED,
    SKU_CODE,
    ImportedSku,
    RowOutcome,
    RowPreview,
    plan_confirm,
    preview_import,
    recheck_conflicts,
)
from app.domain.ids import new_id
from app.models.catalog import NOW, CatalogImport, Sku, SkuCodeClaim
from app.services import catalog
from app.services.catalog import ChangeAuthor, SkuFields

PREVIEWED = "previewed"
CONFIRMED = "confirmed"

# 与 created_at 的默认值在同一条语句里取数据库当前时间（§2.3 有效期 24 小时）。
_EXPIRES_AT = text("CURRENT_TIMESTAMP(6) + INTERVAL 24 HOUR")
_DB_NOW = literal_column("CURRENT_TIMESTAMP(6)", DateTime())


@dataclass(frozen=True)
class ImportRecord:
    """一次导入；时间是数据库里的 UTC，不带时区。"""

    import_id: str
    status: str  # 库里存的：previewed 或 confirmed
    expired: bool  # expires_at 不晚于数据库当前时间
    expires_at: datetime
    created_at: datetime
    confirmed_at: datetime | None
    rows: list[dict]
    result: list[dict] | None  # 确认后才有


# --- 现有目录 ---


def existing_code_keys(
    connection: Connection, tenant_id: str, sku_codes: Collection[str], barcodes: Collection[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """本租户归属表里与 sku_codes 相同的编码、现有 SKU（任何状态）里与 barcodes 相同的
    当前条码，各自的比较键。

    编码类列的排序规则不区分大小写，IN 查出的再取比较键，由调用方按比较键判定。
    """
    claimed: frozenset[str] = frozenset()
    if sku_codes:
        statement = select(SkuCodeClaim.code).where(
            SkuCodeClaim.tenant_id == tenant_id, SkuCodeClaim.code.in_(sorted(set(sku_codes)))
        )
        claimed = frozenset(code_key(code) for code in connection.execute(statement).scalars())
    taken: frozenset[str] = frozenset()
    if barcodes:
        statement = select(Sku.barcode).where(
            Sku.tenant_id == tenant_id, Sku.barcode.in_(sorted(set(barcodes)))
        )
        taken = frozenset(code_key(barcode) for barcode in connection.execute(statement).scalars())
    return claimed, taken


def _normalized(rows: Sequence[Mapping], field: str, normalize) -> list[str]:
    """各行 field 规范化后的非空值；不合规或没带的行不算（预览会把它记为 invalid_format）。"""
    found = []
    for row in rows:
        try:
            value = normalize(row.get(field), field)
        except InvalidField:
            continue
        if value is not None:
            found.append(value)
    return found


# --- 预览 ---


def _echo(value) -> str | None:
    return value if isinstance(value, str) else None


def _stored_row(row: Mapping, preview: RowPreview) -> dict:
    return {
        "index": preview.index,
        "external_ref": _echo(preview.external_ref),
        "product_ref": _echo(preview.product_ref),
        "result": preview.result,
        "invalid_fields": list(preview.invalid_fields),
        "values": None if preview.values is None else asdict(preview.values),
        "source": json.dumps(row, ensure_ascii=False),
    }


def save_preview(
    connection: Connection,
    tenant_id: str,
    rows: Sequence[Mapping],
    *,
    credential_id: str,
    actor_ref: str,
    request_id: str,
) -> str:
    """按本租户现有目录逐行判定（§2.3），保存为 previewed 的导入；返回 import_id。不写目录。

    rows 是正文里的行（json 解析的原值，每个都是对象）。
    """
    claimed, taken = existing_code_keys(
        connection,
        tenant_id,
        _normalized(rows, SKU_CODE, normalize_sku_code),
        _normalized(rows, BARCODE, normalize_barcode),
    )
    previews = preview_import(rows, claimed, taken)
    import_id = new_id("imp")
    connection.execute(
        insert(CatalogImport).values(
            import_id=import_id,
            tenant_id=tenant_id,
            status=PREVIEWED,
            expires_at=_EXPIRES_AT,
            rows=[_stored_row(row, preview) for row, preview in zip(rows, previews, strict=True)],
            credential_id=credential_id,
            actor_ref=actor_ref,
            request_id=request_id,
        )
    )
    return import_id


# --- 读取 ---


def _import_query(tenant_id: str, import_id: str):
    return select(
        CatalogImport.import_id,
        CatalogImport.status,
        (CatalogImport.expires_at <= _DB_NOW).label("expired"),
        CatalogImport.expires_at,
        CatalogImport.created_at,
        CatalogImport.confirmed_at,
        CatalogImport.rows,
        CatalogImport.result,
    ).where(CatalogImport.tenant_id == tenant_id, CatalogImport.import_id == import_id)


def _record(row) -> ImportRecord | None:
    if row is None:
        return None
    return ImportRecord(**{**row, "expired": bool(row["expired"])})


def read_import(connection: Connection, tenant_id: str, import_id: str) -> ImportRecord | None:
    """本租户的导入；不存在或属于别的租户为 None。"""
    statement = _import_query(tenant_id, import_id)
    return _record(connection.execute(statement).mappings().first())


def lock_import(connection: Connection, tenant_id: str, import_id: str) -> ImportRecord | None:
    """同 read_import，用 SELECT … FOR UPDATE 读（属于别的租户时不加锁）。"""
    statement = _import_query(tenant_id, import_id).with_for_update()
    return _record(connection.execute(statement).mappings().first())


def import_owner(connection: Connection, import_id: str) -> str | None:
    """导入所属的租户；不存在为 None。"""
    statement = select(CatalogImport.tenant_id).where(CatalogImport.import_id == import_id)
    return connection.execute(statement).scalar_one_or_none()


def previews_of(record: ImportRecord) -> list[RowPreview]:
    """把存的各行还原成预览结果，按行下标顺序。"""
    return [
        RowPreview(
            index=row["index"],
            external_ref=row["external_ref"],
            product_ref=row["product_ref"],
            result=row["result"],
            invalid_fields=tuple(row["invalid_fields"]),
            values=None if row["values"] is None else ImportedSku(**row["values"]),
        )
        for row in sorted(record.rows, key=lambda row: row["index"])
    ]


# --- 确认 ---


def _result_row(outcome: RowOutcome, ids: Mapping[int, tuple[str, str]]) -> dict:
    if outcome.outcome == CREATED:
        outcome = outcome.with_ids(*ids[outcome.index])
    return {
        "index": outcome.index,
        "external_ref": outcome.external_ref,
        "product_ref": outcome.product_ref,
        "outcome": outcome.outcome,
        "product_id": outcome.product_id,
        "sku_id": outcome.sku_id,
        "preview_result": outcome.result if outcome.outcome == SKIPPED else None,
        "failure": outcome.result if outcome.outcome == FAILED else None,
    }


def confirm_import(
    connection: Connection, tenant_id: str, record: ImportRecord, author: ChangeAuthor
) -> None:
    """确认一个已加锁、未确认、未过期的导入（§2.3）。

    按当前目录重查预览为 ok 的各行，整组都没有冲突的组按在 rows 里首次出现的顺序建商品与 SKU
    （变更记录带 author.import_id），再把导入改为 confirmed，存逐行结果与确认的凭据、操作者、
    请求 ID 与数据库时间。调用方已取目录写锁，重查之后不会再有冲突；万一建时仍冲突，
    CatalogConflict 照常抛出，由调用方整笔回滚。
    """
    previews = previews_of(record)
    planned = [preview.values for preview in previews if preview.values is not None]
    claimed, taken = existing_code_keys(
        connection,
        tenant_id,
        [values.sku_code for values in planned],
        [values.barcode for values in planned if values.barcode is not None],
    )
    plan = plan_confirm(previews, recheck_conflicts(previews, claimed, taken))

    ids: dict[int, tuple[str, str]] = {}
    for product in plan.products:
        skus = [
            SkuFields(
                sku_code=sku.sku_code,
                barcode=sku.barcode,
                name=sku.name,
                reference_price=sku.reference_price,
            )
            for sku in product.skus
        ]
        product_id, sku_ids = catalog.create_product_with_skus(
            connection, tenant_id, product.name, skus, author
        )
        for sku, sku_id in zip(product.skus, sku_ids, strict=True):
            ids[sku.index] = (product_id, sku_id)

    connection.execute(
        update(CatalogImport)
        .where(CatalogImport.tenant_id == tenant_id, CatalogImport.import_id == record.import_id)
        .values(
            status=CONFIRMED,
            result=[_result_row(outcome, ids) for outcome in plan.rows],
            confirmed_credential_id=author.credential_id,
            confirmed_actor_ref=author.actor_ref,
            confirmed_request_id=author.request_id,
            confirmed_at=NOW,
        )
    )
