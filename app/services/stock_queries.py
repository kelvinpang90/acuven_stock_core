"""池、余额、流水与可售量查询的数据库读取（契约 §5.6 可售量、§6.2 第 1 条、§6.3 池的查询、§6.8），
以及 SKU 合并的停售判定（§2.8）。

read_* 都按租户过滤，不管事务：调用方在自己的连接上读完再结束事务；都是普通读，不加锁。
pool_owner 不按租户过滤，只在按 ID 找不到时用来区分不存在与属于别的租户
（后者记 cross_tenant，§7.2）；SKU 的归属用 app/services/catalog.py 的 sku_owner。
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, and_, or_, select

from app.domain.sku_merges import CANONICAL, OPEN_STATUSES, SOURCE, pauses_selling
from app.models.catalog import Product, Sku
from app.models.inventory import QUANTITIES, LedgerEntry, StockBalance, StockPool
from app.models.merges import SkuMerge


@dataclass(frozen=True)
class PoolRecord:
    """池的表示所需的字段；时间是数据库里的 UTC，不带时区。"""

    pool_id: str
    location_id: str
    name: str
    status: str
    went_live_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class BalanceRecord:
    """一个 SKU 在池里的余额；还没有余额行时四个量与版本都为 0。"""

    sku_id: str
    stock_balance: int
    reserved: int
    unavailable: int
    paid_pending_dispatch: int
    balance_version: int
    merged_into_sku_id: str | None


@dataclass(frozen=True)
class AvailabilityRecord:
    """一个 SKU 在池里的可售量所需的字段；还没有余额行时四个量都为 0。"""

    sku_id: str
    stock_balance: int
    reserved: int
    unavailable: int
    paid_pending_dispatch: int
    merged_into_sku_id: str | None
    sku_status: str
    product_status: str
    selling_paused: bool


def selling_paused_sku_ids(
    connection: Connection, tenant_id: str, sku_ids: Collection[str]
) -> set[str]:
    """sku_ids 里停售中的 SKU（§2.8 停售的判定）：普通读本租户未结束的合并单，
    按 pauses_selling 判每个 SKU 在单上的角色。新预留第 5 步与可售量共用。
    """
    if not sku_ids:
        return set()
    wanted = set(sku_ids)
    columns = (SkuMerge.status, SkuMerge.source_sku_id, SkuMerge.canonical_sku_id)
    statement = select(*columns).where(
        SkuMerge.tenant_id == tenant_id,
        SkuMerge.status.in_(OPEN_STATUSES),
        or_(SkuMerge.source_sku_id.in_(wanted), SkuMerge.canonical_sku_id.in_(wanted)),
    )
    paused = set()
    for status, source_sku_id, canonical_sku_id in connection.execute(statement):
        for sku_id, role in ((source_sku_id, SOURCE), (canonical_sku_id, CANONICAL)):
            if sku_id in wanted and pauses_selling(status, role):
                paused.add(sku_id)
    return paused


def pool_owner(connection: Connection, pool_id: str) -> str | None:
    """池所属的租户；不存在为 None。"""
    statement = select(StockPool.tenant_id).where(StockPool.pool_id == pool_id)
    return connection.execute(statement).scalar_one_or_none()


def read_pool(connection: Connection, tenant_id: str, pool_id: str) -> PoolRecord | None:
    """本租户的池；不存在或属于别的租户为 None。"""
    statement = select(
        StockPool.pool_id,
        StockPool.location_id,
        StockPool.name,
        StockPool.status,
        StockPool.went_live_at,
        StockPool.created_at,
    ).where(StockPool.tenant_id == tenant_id, StockPool.pool_id == pool_id)
    row = connection.execute(statement).mappings().first()
    return None if row is None else PoolRecord(**row)


def read_balances(
    connection: Connection, tenant_id: str, pool_id: str, sku_ids: Collection[str]
) -> list[BalanceRecord]:
    """sku_ids 里属于本租户的 SKU 在这个池的余额，按 sku_id 升序。

    不存在或属于别的租户的 SKU 不出现；已合并的照常出现。池已由调用方确认属于本租户。
    """
    if not sku_ids:
        return []
    statement = (
        select(
            Sku.sku_id,
            Sku.merged_into_sku_id,
            *(getattr(StockBalance, name) for name in QUANTITIES),
            StockBalance.balance_version,
        )
        .select_from(Sku)
        .outerjoin(
            StockBalance,
            and_(
                StockBalance.tenant_id == Sku.tenant_id,
                StockBalance.pool_id == pool_id,
                StockBalance.sku_id == Sku.sku_id,
            ),
        )
        .where(Sku.tenant_id == tenant_id, Sku.sku_id.in_(list(sku_ids)))
        .order_by(Sku.sku_id)
    )
    return [
        BalanceRecord(
            sku_id=row["sku_id"],
            **{name: row[name] or 0 for name in QUANTITIES},
            balance_version=row["balance_version"] or 0,
            merged_into_sku_id=row["merged_into_sku_id"],
        )
        for row in connection.execute(statement).mappings()
    ]


def read_availability(
    connection: Connection, tenant_id: str, pool_id: str, sku_ids: Collection[str]
) -> list[AvailabilityRecord]:
    """sku_ids 里属于本租户的 SKU 在这个池的可售量所需的字段，按 sku_id 升序。

    不存在或属于别的租户的 SKU 不出现；已合并、停用的照常出现。池取自凭据的销售入口。
    selling_paused 由 selling_paused_sku_ids 读出，同样是普通读。
    """
    if not sku_ids:
        return []
    paused = selling_paused_sku_ids(connection, tenant_id, sku_ids)
    statement = (
        select(
            Sku.sku_id,
            Sku.merged_into_sku_id,
            Sku.status.label("sku_status"),
            Product.status.label("product_status"),
            *(getattr(StockBalance, name) for name in QUANTITIES),
        )
        .select_from(Sku)
        .join(
            Product,
            and_(Product.tenant_id == Sku.tenant_id, Product.product_id == Sku.product_id),
        )
        .outerjoin(
            StockBalance,
            and_(
                StockBalance.tenant_id == Sku.tenant_id,
                StockBalance.pool_id == pool_id,
                StockBalance.sku_id == Sku.sku_id,
            ),
        )
        .where(Sku.tenant_id == tenant_id, Sku.sku_id.in_(list(sku_ids)))
        .order_by(Sku.sku_id)
    )
    return [
        AvailabilityRecord(
            sku_id=row["sku_id"],
            **{name: row[name] or 0 for name in QUANTITIES},
            merged_into_sku_id=row["merged_into_sku_id"],
            sku_status=row["sku_status"],
            product_status=row["product_status"],
            selling_paused=row["sku_id"] in paused,
        )
        for row in connection.execute(statement).mappings()
    ]


def read_ledger(
    connection: Connection,
    tenant_id: str,
    pool_id: str,
    sku_id: str,
    *,
    start: datetime | None,
    end: datetime | None,
    after_version: int | None,
    limit: int,
) -> list[Mapping[str, Any]]:
    """一个池一个 SKU 的流水，按 balance_version_after 升序，最多 limit 条。

    start、end 按 recorded_at 筛选（含 start、不含 end），after_version 只取版本大于它的；
    为 None 的条件不加。返回按 ledger_entries 列名取值的行，交给 entry_representation。
    """
    statement = select(LedgerEntry.__table__).where(
        LedgerEntry.tenant_id == tenant_id,
        LedgerEntry.pool_id == pool_id,
        LedgerEntry.sku_id == sku_id,
    )
    if start is not None:
        statement = statement.where(LedgerEntry.recorded_at >= start)
    if end is not None:
        statement = statement.where(LedgerEntry.recorded_at < end)
    if after_version is not None:
        statement = statement.where(LedgerEntry.balance_version_after > after_version)
    statement = statement.order_by(LedgerEntry.balance_version_after).limit(limit)
    return list(connection.execute(statement).mappings())
