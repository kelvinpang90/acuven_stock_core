"""开通：租户、地点、库存池与销售入口（契约第 1 节、第 3 节、第 10 节第 4 条）。

只建不改：停用、改名、删除与库存池切 live 不在这里。V1 每租户一个地点、一个池、一个入口由运维照做，
这里不另加数量限制。开通不写安全事件（契约 7.2 的事件类型只含凭据与令牌的操作）。
"""

from argparse import Namespace
from typing import Any

from sqlalchemy import Connection, insert, select
from sqlalchemy.exc import IntegrityError

from app.cli import ACTIVE, CliError, clean_name, require_tenant
from app.domain.ids import new_id
from app.models.catalog import Tenant
from app.models.inventory import Location, SalesChannel, StockPool

# 期初只在 initializing 可用（契约第 6 节）；切到 live 随期初任务做。
INITIALIZING = "initializing"
# 一个池只允许一个销售入口（契约第 3 节，C-07）的数据库唯一约束。
CHANNEL_POOL_UNIQUE = "uq_sales_channels_tenant_id_pool_id"
POOL_HAS_CHANNEL = "pool already has a sales channel"


def create_tenant(conn: Connection, args: Namespace) -> dict[str, Any]:
    row = {"tenant_id": new_id("ten"), "name": clean_name(args.name), "status": ACTIVE}
    conn.execute(insert(Tenant).values(row))
    return row


def create_location(conn: Connection, args: Namespace) -> dict[str, Any]:
    name = clean_name(args.name)
    require_tenant(conn, args.tenant, active=True)
    row = {
        "location_id": new_id("loc"),
        "tenant_id": args.tenant,
        "name": name,
        "status": ACTIVE,
    }
    conn.execute(insert(Location).values(row))
    return row


def create_pool(conn: Connection, args: Namespace) -> dict[str, Any]:
    name = clean_name(args.name)
    require_tenant(conn, args.tenant, active=True)
    location = conn.execute(
        select(Location.status)
        .where(Location.tenant_id == args.tenant, Location.location_id == args.location)
        .with_for_update(read=True)
    ).one_or_none()
    if location is None:
        raise CliError("location not found for this tenant")
    if location.status != ACTIVE:
        raise CliError("location is not active")
    row = {
        "pool_id": new_id("pool"),
        "tenant_id": args.tenant,
        "location_id": args.location,
        "name": name,
        "status": INITIALIZING,
    }
    conn.execute(insert(StockPool).values(row))
    return row


def create_channel(conn: Connection, args: Namespace) -> dict[str, Any]:
    name = clean_name(args.name)
    require_tenant(conn, args.tenant, active=True)
    pool = conn.execute(
        select(StockPool.pool_id)
        .where(StockPool.tenant_id == args.tenant, StockPool.pool_id == args.pool)
        .with_for_update(read=True)
    ).one_or_none()
    if pool is None:
        raise CliError("pool not found for this tenant")
    existing = select(SalesChannel.channel_id).where(
        SalesChannel.tenant_id == args.tenant, SalesChannel.pool_id == args.pool
    )
    if conn.execute(existing).first() is not None:
        raise CliError(POOL_HAS_CHANNEL)
    row = {
        "channel_id": new_id("chan"),
        "tenant_id": args.tenant,
        "pool_id": args.pool,
        "name": name,
        "status": ACTIVE,
    }
    try:
        conn.execute(insert(SalesChannel).values(row))
    except IntegrityError as error:
        # 并发时另一个命令先建了入口：上面的检查没看到，由唯一约束兜底，报同一句。
        if CHANNEL_POOL_UNIQUE in str(error.orig):
            raise CliError(POOL_HAS_CHANNEL) from None
        raise
    return row


def show(conn: Connection, args: Namespace) -> dict[str, Any]:
    if args.tenant is None:
        tenants = conn.execute(
            select(Tenant.tenant_id, Tenant.name, Tenant.status).order_by(Tenant.tenant_id)
        )
        return {"tenants": [dict(row._mapping) for row in tenants]}
    tenant = require_tenant(conn, args.tenant, active=False)
    locations = conn.execute(
        select(Location.location_id, Location.tenant_id, Location.name, Location.status)
        .where(Location.tenant_id == args.tenant)
        .order_by(Location.location_id)
    )
    pools = conn.execute(
        select(
            StockPool.pool_id,
            StockPool.tenant_id,
            StockPool.location_id,
            StockPool.name,
            StockPool.status,
        )
        .where(StockPool.tenant_id == args.tenant)
        .order_by(StockPool.pool_id)
    )
    channels = conn.execute(
        select(
            SalesChannel.channel_id,
            SalesChannel.tenant_id,
            SalesChannel.pool_id,
            SalesChannel.name,
            SalesChannel.status,
        )
        .where(SalesChannel.tenant_id == args.tenant)
        .order_by(SalesChannel.channel_id)
    )
    return {
        "tenant": dict(tenant._mapping),
        "locations": [dict(row._mapping) for row in locations],
        "pools": [dict(row._mapping) for row in pools],
        "channels": [dict(row._mapping) for row in channels],
    }


def register(subparsers: Any) -> None:
    tenant = subparsers.add_parser("tenant", help="tenants")
    tenant_actions = tenant.add_subparsers(dest="action", required=True)
    tenant_create = tenant_actions.add_parser("create", help="create an active tenant")
    tenant_create.add_argument("--name", required=True)
    tenant_create.set_defaults(handler=create_tenant)

    location = subparsers.add_parser("location", help="locations")
    location_actions = location.add_subparsers(dest="action", required=True)
    location_create = location_actions.add_parser("create", help="create an active location")
    location_create.add_argument("--tenant", required=True, help="tenant ID")
    location_create.add_argument("--name", required=True)
    location_create.set_defaults(handler=create_location)

    pool = subparsers.add_parser("pool", help="stock pools")
    pool_actions = pool.add_subparsers(dest="action", required=True)
    pool_create = pool_actions.add_parser("create", help="create an initializing stock pool")
    pool_create.add_argument("--tenant", required=True, help="tenant ID")
    pool_create.add_argument("--location", required=True, help="location ID of the same tenant")
    pool_create.add_argument("--name", required=True)
    pool_create.set_defaults(handler=create_pool)

    channel = subparsers.add_parser("channel", help="sales channels")
    channel_actions = channel.add_subparsers(dest="action", required=True)
    channel_create = channel_actions.add_parser(
        "create", help="create an active sales channel (one per pool)"
    )
    channel_create.add_argument("--tenant", required=True, help="tenant ID")
    channel_create.add_argument("--pool", required=True, help="stock pool ID of the same tenant")
    channel_create.add_argument("--name", required=True)
    channel_create.set_defaults(handler=create_channel)

    show_parser = subparsers.add_parser(
        "show", help="list tenants, or one tenant's locations, pools and channels"
    )
    show_parser.add_argument("--tenant", help="tenant ID")
    show_parser.set_defaults(handler=show)
