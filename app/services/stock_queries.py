"""池、余额与流水查询的数据库读取（契约 §6.2 第 1 条、§6.3 池的查询、§6.8）。

read_* 都按租户过滤，不管事务：调用方在自己的连接上读完再结束事务。
pool_owner 不按租户过滤，只在按 ID 找不到时用来区分不存在与属于别的租户
（后者记 cross_tenant，§7.2）；SKU 的归属用 app/services/catalog.py 的 sku_owner。
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, and_, select

from app.models.catalog import Sku
from app.models.inventory import QUANTITIES, LedgerEntry, StockBalance, StockPool


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
