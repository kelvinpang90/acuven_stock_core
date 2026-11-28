"""平台事件测试共用的造数与清理（契约 §5.10、§3.1、§2.9、§3.2）。

create_world 直接插行建一个租户：地点、live 的池（切 live 的主体是 system 主体）、
一个 platform 入口（连同连接行与租户的写入暂停行）与一个 backoffice 入口，提交。
SKU、平台商品与配额行由各自的函数另建、提交。

测试结束时在 finally 里 purge_tenant 删掉这个租户的全部行：
池行上 went_live_credential_id 指向 credentials 的外键是 use_alter 的，
与 credentials → sales_channels → stock_pools 成环，按表的先后删不干净，
所以关掉外键检查、逐表按 tenant_id 删。只给测试用。
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, Engine, insert, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import SQLAlchemyError

from app.domain.ids import new_id
from app.models.catalog import Product, Sku, Tenant
from app.models.inventory import Location, SalesChannel, StockBalance, StockPool
from app.models.platform import ChannelAllocation, PoolAllocation, SkuListing
from app.models.platform_ops import PlatformConnection, PlatformWritePause
from app.models.service_auth import Credential

ACTIVE = "active"
AUTO = "auto"
SHOPEE = "shopee"
TIKTOK_SHOP = "tiktok_shop"
WENT_LIVE_AT = datetime(2026, 11, 20, 8, 0)
CONFIRMED_AT = datetime(2026, 11, 26, 8, 0)


@dataclass(frozen=True)
class PlatformWorld:
    tenant_id: str
    pool_id: str
    channel_id: str  # active 的 platform 入口，连接为 auto、active
    backoffice_channel_id: str
    system_credential_id: str
    platform: str
    platform_shop_id: str


def _add_platform_channel(
    db: Connection,
    tenant_id: str,
    pool_id: str,
    *,
    platform: str,
    platform_shop_id: str,
    mode: str,
    connection_status: str,
    status: str,
) -> str:
    channel_id = new_id("chan")
    db.execute(
        insert(SalesChannel).values(
            channel_id=channel_id,
            tenant_id=tenant_id,
            pool_id=pool_id,
            name="Platform shop",
            status=status,
            kind="platform",
            platform=platform,
            platform_shop_id=platform_shop_id,
        )
    )
    db.execute(
        insert(PlatformConnection).values(
            tenant_id=tenant_id, channel_id=channel_id, mode=mode, status=connection_status
        )
    )
    return channel_id


def create_world(
    engine: Engine,
    *,
    platform: str = SHOPEE,
    platform_shop_id: str = "shop-1",
    mode: str = AUTO,
    connection_status: str = ACTIVE,
) -> PlatformWorld:
    """一个租户、它的 system 主体、live 的池、一个 platform 入口
    与一个 backoffice 入口，提交。
    """
    tenant_id, location_id, pool_id = new_id("ten"), new_id("loc"), new_id("pool")
    credential_id = new_id("cred")
    with engine.begin() as db:
        db.execute(insert(Tenant).values(tenant_id=tenant_id, name="Tenant", status=ACTIVE))
        db.execute(
            insert(Location).values(
                location_id=location_id, tenant_id=tenant_id, name="Warehouse", status=ACTIVE
            )
        )
        db.execute(
            insert(Credential).values(
                credential_id=credential_id,
                tenant_id=tenant_id,
                channel_id=None,
                name="platform_events",
                scopes=[],
                kind="system",
                status=ACTIVE,
            )
        )
        db.execute(
            insert(StockPool).values(
                pool_id=pool_id,
                tenant_id=tenant_id,
                location_id=location_id,
                name="Main",
                status="live",
                went_live_at=WENT_LIVE_AT,
                went_live_credential_id=credential_id,
                went_live_actor_ref="admin-1",
                went_live_request_id=new_id("req"),
            )
        )
        channel_id = _add_platform_channel(
            db,
            tenant_id,
            pool_id,
            platform=platform,
            platform_shop_id=platform_shop_id,
            mode=mode,
            connection_status=connection_status,
            status=ACTIVE,
        )
        db.execute(insert(PlatformWritePause).values(tenant_id=tenant_id))
        backoffice_channel_id = new_id("chan")
        db.execute(
            insert(SalesChannel).values(
                channel_id=backoffice_channel_id,
                tenant_id=tenant_id,
                pool_id=pool_id,
                name="Backoffice",
                status=ACTIVE,
                kind="backoffice",
            )
        )
    return PlatformWorld(
        tenant_id=tenant_id,
        pool_id=pool_id,
        channel_id=channel_id,
        backoffice_channel_id=backoffice_channel_id,
        system_credential_id=credential_id,
        platform=platform,
        platform_shop_id=platform_shop_id,
    )


def add_platform_channel(
    engine: Engine,
    world: PlatformWorld,
    *,
    platform: str = TIKTOK_SHOP,
    platform_shop_id: str = "shop-2",
    mode: str = AUTO,
    connection_status: str = ACTIVE,
    status: str = ACTIVE,
) -> str:
    """同一个池上再开一个 platform 入口（连同连接行），提交；返回 channel_id。"""
    with engine.begin() as db:
        return _add_platform_channel(
            db,
            world.tenant_id,
            world.pool_id,
            platform=platform,
            platform_shop_id=platform_shop_id,
            mode=mode,
            connection_status=connection_status,
            status=status,
        )


def add_admin(engine: Engine, world: PlatformWorld) -> str:
    """这个租户的一个 admin 主体（后台登记的收件箱行记它），提交；返回 credential_id。"""
    credential_id = new_id("cred")
    with engine.begin() as db:
        db.execute(
            insert(Credential).values(
                credential_id=credential_id,
                tenant_id=world.tenant_id,
                channel_id=None,
                name="backoffice admin",
                scopes=[],
                kind="admin",
                status=ACTIVE,
            )
        )
    return credential_id


def add_sku(engine: Engine, tenant_id: str) -> str:
    """一个 active 的商品连同它唯一的 SKU，提交；返回 sku_id。"""
    product_id, sku_id = new_id("prd"), new_id("sku")
    with engine.begin() as db:
        db.execute(
            insert(Product).values(
                product_id=product_id, tenant_id=tenant_id, name="Tee", status=ACTIVE, version=1
            )
        )
        db.execute(
            insert(Sku).values(
                sku_id=sku_id,
                tenant_id=tenant_id,
                product_id=product_id,
                sku_code=new_id("code"),
                name="Tee",
                status=ACTIVE,
                version=1,
            )
        )
    return sku_id


def add_listing(
    engine: Engine,
    world: PlatformWorld,
    sku_id: str,
    platform_item_id: str,
    platform_model_id: str = "-",
    *,
    status: str = ACTIVE,
    channel_id: str | None = None,
) -> str:
    """一条平台商品（默认挂在 world 的 platform 入口上），提交；返回 listing_id。"""
    listing_id = new_id("lst")
    with engine.begin() as db:
        db.execute(
            insert(SkuListing).values(
                listing_id=listing_id,
                tenant_id=world.tenant_id,
                channel_id=channel_id or world.channel_id,
                platform_item_id=platform_item_id,
                platform_model_id=platform_model_id,
                sku_id=sku_id,
                status=status,
                version=1,
                credential_id=world.system_credential_id,
                actor_ref=None,
                actor_role=None,
                request_id=new_id("req"),
            )
        )
    return listing_id


def set_allocation(
    engine: Engine,
    world: PlatformWorld,
    sku_id: str,
    *,
    quota: int = 0,
    pending: int = 0,
    manual_confirmed_quota: int | None = None,
    channel_id: str | None = None,
    stock_balance: int | None = None,
    safety_stock: int | None = None,
    review_hold: bool = False,
) -> None:
    """建好（或改成）这个 SKU 的余额行、池配额行与这个入口的配额行，提交。

    stock_balance、safety_stock 为 None 时不改（新建取默认值 0 与 2）；
    stock_balance 直接写进余额行（没有预留与不可售时即 available），不写流水。
    """
    channel_id = channel_id or world.channel_id
    balance = StockBalance.__table__
    pool = PoolAllocation.__table__
    channel = ChannelAllocation.__table__
    values = {
        "quota": quota,
        "pending": pending,
        "manual_confirmed_quota": manual_confirmed_quota,
        "manual_confirmed_at": None if manual_confirmed_quota is None else CONFIRMED_AT,
        "review_hold": review_hold,
        "review_reason": "readback mismatch" if review_hold else None,
        "review_since": CONFIRMED_AT if review_hold else None,
    }
    balance_values = {} if stock_balance is None else {"stock_balance": stock_balance}
    pool_values = {} if safety_stock is None else {"safety_stock": safety_stock}
    with engine.begin() as db:
        db.execute(
            mysql_insert(balance)
            .values(
                balance_id=new_id("bal"),
                tenant_id=world.tenant_id,
                pool_id=world.pool_id,
                sku_id=sku_id,
                **balance_values,
            )
            .on_duplicate_key_update(balance_id=balance.c.balance_id, **balance_values)
        )
        db.execute(
            mysql_insert(pool)
            .values(tenant_id=world.tenant_id, pool_id=world.pool_id, sku_id=sku_id, **pool_values)
            .on_duplicate_key_update(sku_id=pool.c.sku_id, **pool_values)
        )
        db.execute(
            mysql_insert(channel)
            .values(
                tenant_id=world.tenant_id,
                channel_id=channel_id,
                pool_id=world.pool_id,
                sku_id=sku_id,
                **values,
            )
            .on_duplicate_key_update(**values)
        )


_TENANT_TABLES = text(
    "SELECT c.TABLE_NAME FROM information_schema.COLUMNS AS c"
    " JOIN information_schema.TABLES AS t"
    " ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME"
    " WHERE c.TABLE_SCHEMA = DATABASE() AND c.COLUMN_NAME = 'tenant_id'"
    " AND t.TABLE_TYPE = 'BASE TABLE'"
    " ORDER BY c.TABLE_NAME"
)


def purge_tenant(engine: Engine, tenant_id: str) -> None:
    """删掉这个租户在所有带 tenant_id 列的表里的行，提交。

    在一个连接上关掉外键检查再删；finally 里设回，设不回就作废这个连接，不放回连接池。
    """
    connection = engine.connect()
    try:
        connection.execute(text("SET SESSION foreign_key_checks = 0"))
        tables = list(connection.execute(_TENANT_TABLES).scalars())
        for table in tables:
            connection.execute(
                text(f"DELETE FROM `{table}` WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        connection.commit()
    finally:
        try:
            connection.rollback()
            connection.execute(text("SET SESSION foreign_key_checks = 1"))
            connection.rollback()
        except SQLAlchemyError:
            connection.invalidate()
        connection.close()
