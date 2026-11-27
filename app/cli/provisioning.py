"""开通：租户、地点、库存池与入口（契约第 1 节、第 3 节、第 10 节第 4 条）。

只建不改：停用、改名、删除与库存池切 live 不在这里。每租户几个地点、几个池由运维照做，
这里不另加数量限制；一个池的入口组合在开通入口时检查（契约第 3 节，Q-81）。
开通不写安全事件（契约 7.2 的事件类型只含凭据与令牌的操作）；开通 platform 入口时顺带建的
system 主体没有令牌，不是签发，也不记。
开通 platform 入口时在同一事务里建它的连接行（人工模式、未连接，契约 3.2）与租户的写入暂停行：
人工模式没有授权这一步，没有别处会建它们。
"""

from argparse import Namespace
from typing import Any

from sqlalchemy import Connection, Row, and_, insert, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError

from app.cli import ACTIVE, CliError, clean_name, require_tenant
from app.domain.channels import (
    API,
    CHANNEL_KINDS,
    PLATFORM,
    PLATFORMS,
    combination_error,
    is_platform_shop_id,
)
from app.domain.ids import new_id
from app.domain.service_auth import SYSTEM
from app.models.catalog import Tenant
from app.models.inventory import Location, SalesChannel, StockPool
from app.models.platform_ops import PlatformConnection, PlatformWritePause
from app.models.service_auth import Credential

# 期初只在 initializing 可用（契约第 6 节）；切到 live 随期初任务做。
INITIALIZING = "initializing"
# 一个平台店铺在本租户只登记一次的数据库唯一约束。
CHANNEL_SHOP_UNIQUE = "uq_sales_channels_tenant_id_platform_platform_shop_id"
SHOP_HAS_CHANNEL = "this platform shop already has a channel in this tenant"
# 每个租户一个 system 主体（契约第 10 节第 4 条），平台事件处理以它的名义写。
SYSTEM_PRINCIPAL_NAME = "platform_events"


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


def _channel_platform(args: Namespace) -> tuple[str | None, str | None]:
    """种类与 --platform、--platform-shop-id 的搭配，以及店铺 ID 的格式。"""
    if args.kind == PLATFORM:
        if args.platform is None or args.platform_shop_id is None:
            raise CliError("platform channels need --platform and --platform-shop-id")
        if not is_platform_shop_id(args.platform_shop_id):
            raise CliError("platform shop ID must be 1 to 64 letters, digits or . _ : -")
        return args.platform, args.platform_shop_id
    if args.platform is not None or args.platform_shop_id is not None:
        raise CliError("--platform and --platform-shop-id are only for platform channels")
    return None, None


def _lock_pool_and_tenant(conn: Connection, tenant_id: str, pool_id: str) -> None:
    """先以 FOR UPDATE 锁池行、再锁租户行。

    销售与库存命令先 FOR SHARE 读池行、再经租户外键对租户行取共享锁，这里照同一先后，免得死锁。
    锁租户行使同租户的开通入口（含不同的池）串行：每个租户只建一个 system 主体。
    不用 require_tenant：它先取共享锁，同一行再取排他锁的锁升级会让同租户并发开通死锁。
    报错的先后照旧（租户找不到或停用先于池找不到），所以先用不加锁的普通读核对租户。
    """
    tenant = select(Tenant.status).where(Tenant.tenant_id == tenant_id)
    status = conn.execute(tenant).scalar_one_or_none()
    if status is None:
        raise CliError("tenant not found")
    if status != ACTIVE:
        raise CliError("tenant is not active")
    pool = conn.execute(
        select(StockPool.pool_id)
        .where(StockPool.tenant_id == tenant_id, StockPool.pool_id == pool_id)
        .with_for_update()
    ).one_or_none()
    if pool is None:
        raise CliError("pool not found for this tenant")
    # 锁住之后复核：等锁期间租户可能被停用。
    status = conn.execute(tenant.with_for_update()).scalar_one_or_none()
    if status is None:
        raise CliError("tenant not found")
    if status != ACTIVE:
        raise CliError("tenant is not active")


def _system_principal(conn: Connection, tenant_id: str) -> str:
    """这个租户的 system 主体的 credential_id；还没有就建一个。

    建的是 kind system、name platform_events、scopes 为空、不带入口、active 的主体。

    已锁住租户行，同租户的开通入口串行。读要加锁：普通读用的是第一条普通读时的快照（早于等锁），
    看不到并发的另一个开通刚提交的 system 主体，会给同一租户建两个。
    """
    existing = conn.execute(
        select(Credential.credential_id)
        .where(Credential.tenant_id == tenant_id, Credential.kind == SYSTEM)
        .order_by(Credential.credential_id)
        .limit(1)
        .with_for_update(read=True)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    credential_id = new_id("cred")
    conn.execute(
        insert(Credential).values(
            credential_id=credential_id,
            tenant_id=tenant_id,
            channel_id=None,
            name=SYSTEM_PRINCIPAL_NAME,
            scopes=[],
            kind=SYSTEM,
            status=ACTIVE,
        )
    )
    return credential_id


def _open_connection(conn: Connection, tenant_id: str, channel_id: str) -> None:
    """新 platform 入口的连接行，以及这个租户还没有的写入暂停行，都取数据库默认值。

    写入暂停行已有就不动（并发开通也不撞主键）；恢复时运行手册的 SQL 只改已有的行。
    """
    conn.execute(insert(PlatformConnection).values(tenant_id=tenant_id, channel_id=channel_id))
    pause = PlatformWritePause.__table__
    statement = mysql_insert(pause).values(tenant_id=tenant_id)
    conn.execute(statement.on_duplicate_key_update(tenant_id=pause.c.tenant_id))


def create_channel(conn: Connection, args: Namespace) -> dict[str, Any]:
    name = clean_name(args.name)
    platform, platform_shop_id = _channel_platform(args)
    _lock_pool_and_tenant(conn, args.tenant, args.pool)
    # 加锁读，理由同 _system_principal：否则同一个池上会开出两个 api 或两个 backoffice。
    existing_kinds = conn.execute(
        select(SalesChannel.kind)
        .where(SalesChannel.tenant_id == args.tenant, SalesChannel.pool_id == args.pool)
        .with_for_update(read=True)
    ).scalars()
    refusal = combination_error(existing_kinds, args.kind)
    if refusal is not None:
        raise CliError(refusal)
    if platform is not None:
        same_shop = conn.execute(
            select(SalesChannel.channel_id)
            .where(
                SalesChannel.tenant_id == args.tenant,
                SalesChannel.platform == platform,
                SalesChannel.platform_shop_id == platform_shop_id,
            )
            .with_for_update(read=True)
        ).first()
        if same_shop is not None:
            raise CliError(SHOP_HAS_CHANNEL)
    row = {
        "channel_id": new_id("chan"),
        "tenant_id": args.tenant,
        "pool_id": args.pool,
        "name": name,
        "status": ACTIVE,
        "kind": args.kind,
        "platform": platform,
        "platform_shop_id": platform_shop_id,
    }
    try:
        conn.execute(insert(SalesChannel).values(row))
    except IntegrityError as error:
        # 上面的检查没看到的同一店铺由唯一约束兜底，报同一句。
        if CHANNEL_SHOP_UNIQUE in str(error.orig):
            raise CliError(SHOP_HAS_CHANNEL) from None
        raise
    if args.kind == PLATFORM:
        system_credential_id = _system_principal(conn, args.tenant)
        _open_connection(conn, args.tenant, row["channel_id"])
        return row | {"system_credential_id": system_credential_id}
    return row


def _channel_output(row: Row) -> dict[str, Any]:
    """platform 入口另带它的连接的模式与状态；别的种类没有连接，不带这两个键。"""
    channel = dict(row._mapping)
    if channel["kind"] != PLATFORM:
        del channel["connection_mode"], channel["connection_status"]
    return channel


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
            SalesChannel.kind,
            SalesChannel.platform,
            SalesChannel.platform_shop_id,
            PlatformConnection.mode.label("connection_mode"),
            PlatformConnection.status.label("connection_status"),
        )
        .outerjoin_from(
            SalesChannel,
            PlatformConnection,
            and_(
                PlatformConnection.tenant_id == SalesChannel.tenant_id,
                PlatformConnection.channel_id == SalesChannel.channel_id,
            ),
        )
        .where(SalesChannel.tenant_id == args.tenant)
        .order_by(SalesChannel.channel_id)
    )
    return {
        "tenant": dict(tenant._mapping),
        "locations": [dict(row._mapping) for row in locations],
        "pools": [dict(row._mapping) for row in pools],
        "channels": [_channel_output(row) for row in channels],
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
        "create", help="create an active channel (see --kind for the allowed combinations)"
    )
    channel_create.add_argument("--tenant", required=True, help="tenant ID")
    channel_create.add_argument("--pool", required=True, help="stock pool ID of the same tenant")
    channel_create.add_argument("--name", required=True)
    channel_create.add_argument(
        "--kind",
        choices=CHANNEL_KINDS,
        default=API,
        help="a pool has one api channel alone, or platform channels and one backoffice channel",
    )
    channel_create.add_argument(
        "--platform", choices=PLATFORMS, help="platform channels only (required for them)"
    )
    channel_create.add_argument(
        "--platform-shop-id", help="platform channels only: the shop ID on the platform"
    )
    channel_create.set_defaults(handler=create_channel)

    show_parser = subparsers.add_parser(
        "show", help="list tenants, or one tenant's locations, pools and channels"
    )
    show_parser.add_argument("--tenant", help="tenant ID")
    show_parser.set_defaults(handler=show)
