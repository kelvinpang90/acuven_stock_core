"""期初导入与池切 live 的读取与写入（契约 §6.2、§6.3）。

判定用 app/domain/stock_imports.py 的纯函数；锁池行、锁余额行与写流水用 app/services/stock.py。
read_import、import_owner 同 app/services/catalog_imports.py 的：
前者按租户过滤，后者只用来区分不存在与属于别的租户。
写入函数在幂等执行器已开的事务里运行，自己不提交、不回滚。

rows 列每行一个对象：judge_rows 给出的 index、sku_id、quantity、result，
加 source（行的原文按 json.dumps 序列化成的字符串，照 §2.3 的写法）。
result 列是确认的逐行结果：plan_confirm 给出的各项，created 行的 entry_id 已填。
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import Connection, DateTime, and_, insert, literal_column, select, text, update

from app.api.auth import ObjectPool
from app.domain.ids import new_id
from app.domain.ledger import EntryType
from app.domain.requests import Actor
from app.domain.stock_imports import (
    CREATED,
    OK,
    QUANTITY,
    SKU_ID,
    SkuState,
    judge_rows,
    plan_confirm,
)
from app.models.catalog import Sku
from app.models.inventory import StockBalance, StockImport, StockPool
from app.services.idempotency import Execution
from app.services.stock import (
    LIVE,
    LockedBalance,
    append_entry,
    command_now,
    lock_balances,
    new_command_id,
    require_initializing,
)
from app.services.stock_queries import PoolRecord

PREVIEWED = "previewed"
CONFIRMED = "confirmed"
# 期初流水的 source_type（§6.3），source_ref 是 import_id。
STOCK_IMPORT = "stock_import"

PREVIEW_KEYS = ("index", SKU_ID, QUANTITY, "result")

# 与 created_at 的默认值在同一条语句里取数据库当前时间（§6.3 有效期 24 小时）。
_EXPIRES_AT = text("CURRENT_TIMESTAMP(6) + INTERVAL 24 HOUR")
_DB_NOW = literal_column("CURRENT_TIMESTAMP(6)", DateTime())


@dataclass(frozen=True)
class ImportRecord:
    """一次期初导入；时间是数据库里的 UTC，不带时区。"""

    import_id: str
    pool_id: str
    status: str  # 库里存的：previewed 或 confirmed
    expired: bool  # expires_at 不晚于数据库当前时间
    expires_at: datetime
    created_at: datetime
    confirmed_at: datetime | None
    rows: list[dict]
    result: list[dict] | None  # 确认后才有


# --- 预览 ---


def sku_lookup(
    connection: Connection, tenant_id: str, pool_id: str, sku_ids: Iterable[str]
) -> dict[str, SkuState]:
    """本租户里 sku_ids 中存在的 SKU 及其在这个池的期初状态；普通读、不锁行。

    别的租户的 SKU 与不存在的一样不出现（预览只在本租户里查，§6.3）。
    """
    wanted = sorted(set(sku_ids))
    if not wanted:
        return {}
    statement = (
        select(Sku.sku_id, Sku.merged_into_sku_id, StockBalance.opening_entry_id)
        .select_from(Sku)
        .outerjoin(
            StockBalance,
            and_(
                StockBalance.tenant_id == Sku.tenant_id,
                StockBalance.pool_id == pool_id,
                StockBalance.sku_id == Sku.sku_id,
            ),
        )
        .where(Sku.tenant_id == tenant_id, Sku.sku_id.in_(wanted))
    )
    return {
        row.sku_id: SkuState(
            merged=row.merged_into_sku_id is not None, opened=row.opening_entry_id is not None
        )
        for row in connection.execute(statement)
    }


def save_preview(
    connection: Connection,
    tenant_id: str,
    pool_id: str,
    rows: Sequence[Mapping],
    *,
    credential_id: str,
    actor_ref: str,
    request_id: str,
) -> str:
    """按本租户现有 SKU 与这个池的余额行逐行判定（§6.3），存为 previewed 的导入。

    rows 是正文里的行（json 解析的原值，每个都是对象）。不写流水、不锁余额行。返回 import_id。
    """
    sku_ids = [row[SKU_ID] for row in rows if isinstance(row.get(SKU_ID), str)]
    previews = judge_rows(rows, sku_lookup(connection, tenant_id, pool_id, sku_ids))
    import_id = new_id("simp")
    stored = [
        preview | {"source": json.dumps(row, ensure_ascii=False)}
        for row, preview in zip(rows, previews, strict=True)
    ]
    connection.execute(
        insert(StockImport).values(
            import_id=import_id,
            tenant_id=tenant_id,
            pool_id=pool_id,
            status=PREVIEWED,
            expires_at=_EXPIRES_AT,
            rows=stored,
            credential_id=credential_id,
            actor_ref=actor_ref,
            request_id=request_id,
        )
    )
    return import_id


# --- 读取 ---


def _import_query(tenant_id: str, import_id: str):
    return select(
        StockImport.import_id,
        StockImport.pool_id,
        StockImport.status,
        (StockImport.expires_at <= _DB_NOW).label("expired"),
        StockImport.expires_at,
        StockImport.created_at,
        StockImport.confirmed_at,
        StockImport.rows,
        StockImport.result,
    ).where(StockImport.tenant_id == tenant_id, StockImport.import_id == import_id)


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
    statement = select(StockImport.tenant_id).where(StockImport.import_id == import_id)
    return connection.execute(statement).scalar_one_or_none()


def find_import_pool(connection: Connection, import_id: str) -> ObjectPool | None:
    """池授权用：导入所属的租户与池，不按租户过滤；不存在为 None。"""
    statement = select(StockImport.tenant_id, StockImport.pool_id).where(
        StockImport.import_id == import_id
    )
    row = connection.execute(statement).first()
    return None if row is None else ObjectPool(*row)


def previews_of(record: ImportRecord) -> list[dict]:
    """存的各行去掉原文，即预览的逐行结果，按行下标顺序。"""
    return [
        {key: row[key] for key in PREVIEW_KEYS}
        for row in sorted(record.rows, key=lambda row: row["index"])
    ]


def opening_rows(connection: Connection, tenant_id: str, import_id: str) -> list[tuple[str, str]]:
    """余额行预建用：本租户这个导入里预览为 ok 的行的 (pool_id, sku_id)；导入不存在时为空。"""
    record = read_import(connection, tenant_id, import_id)
    if record is None:
        return []
    return [(record.pool_id, row[SKU_ID]) for row in record.rows if row["result"] == OK]


# --- 确认 ---


def _locked_lookup(execution: Execution, balances: Mapping[str, LockedBalance]) -> dict:
    """锁住余额行之后的重查（§6.3 第 6 步）：按 sku_id 升序 FOR SHARE 读本租户的 SKU 行
    （同 require_not_merged，但不抛错），有效期初看锁住的余额行上的 opening_entry_id。

    不在本租户的 SKU 不出现，plan_confirm 会把它记为 sku_not_found。
    """
    tenant_id = execution.access.principal.tenant_id
    lookup = {}
    for sku_id in sorted(balances):
        statement = (
            select(Sku.merged_into_sku_id)
            .where(Sku.tenant_id == tenant_id, Sku.sku_id == sku_id)
            .with_for_update(read=True)
        )
        row = execution.connection.execute(statement).first()
        if row is None:
            continue
        lookup[sku_id] = SkuState(
            merged=row.merged_into_sku_id is not None,
            opened=balances[sku_id].opening_entry_id is not None,
        )
    return lookup


def confirm_import(execution: Execution, record: ImportRecord, actor: Actor) -> None:
    """确认一个已加锁、未确认、未过期的导入（§6.3 确认的第 4–6 步）。

    池已 live：409 POOL_ALREADY_LIVE。
    否则按 (pool_id, sku_id) 升序锁住预览为 ok 的各行的余额行，逐行重查；
    通过且数量大于 0 的写一条 OPENING_BALANCE，为 0 的不写。行与行互不影响。
    最后把导入改为 confirmed，存逐行结果与确认的凭据、操作者、请求 ID 与命令时间。
    """
    now = command_now(execution)
    require_initializing(execution, record.pool_id)
    previews = previews_of(record)
    sku_ids = [preview[SKU_ID] for preview in previews if preview["result"] == OK]
    balances = lock_balances(execution, record.pool_id, sku_ids)
    plan = plan_confirm(previews, _locked_lookup(execution, balances))

    command_id = new_command_id()
    for line in plan:
        if line["outcome"] == CREATED and line[QUANTITY] > 0:
            entry = append_entry(
                execution,
                balances[line[SKU_ID]],
                EntryType.OPENING_BALANCE,
                (line[QUANTITY], 0, 0, 0),
                command_id,
                now=now,
                source_type=STOCK_IMPORT,
                source_ref=record.import_id,
            )
            line["entry_id"] = entry["entry_id"]

    principal = execution.access.principal
    execution.connection.execute(
        update(StockImport)
        .where(
            StockImport.tenant_id == principal.tenant_id,
            StockImport.import_id == record.import_id,
        )
        .values(
            status=CONFIRMED,
            result=plan,
            confirmed_credential_id=principal.credential_id,
            confirmed_actor_ref=actor.ref,
            confirmed_request_id=execution.request.state.request_id,
            confirmed_at=now,
        )
    )


# --- 池切 live ---


def go_live(execution: Execution, pool_id: str, actor: Actor) -> PoolRecord:
    """FOR UPDATE 读池行（§6.3）；已 live 原样返回，否则改为 live 并记 went_live 四列。

    返回池的当前表示所需的字段，取自加锁读（普通读读的是快照，可能看不到并发的切换）。
    池已由池授权确认属于本租户。
    """
    principal = execution.access.principal
    where = (StockPool.tenant_id == principal.tenant_id, StockPool.pool_id == pool_id)
    statement = (
        select(
            StockPool.pool_id,
            StockPool.location_id,
            StockPool.name,
            StockPool.status,
            StockPool.went_live_at,
            StockPool.created_at,
        )
        .where(*where)
        .with_for_update()
    )
    pool = PoolRecord(**execution.connection.execute(statement).mappings().one())
    if pool.status == LIVE:
        return pool
    now = command_now(execution)
    execution.connection.execute(
        update(StockPool)
        .where(*where)
        .values(
            status=LIVE,
            went_live_at=now,
            went_live_credential_id=principal.credential_id,
            went_live_actor_ref=actor.ref,
            went_live_request_id=execution.request.state.request_id,
        )
    )
    return replace(pool, status=LIVE, went_live_at=now)
