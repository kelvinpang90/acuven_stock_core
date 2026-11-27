"""平台商品的挂上、改挂与解除（契约 §2.9、§2.5、§3.1、§8.3，0.22，Q-90）。

命令只给后台用（§8.6）；后台的执行器与接口不在这里，这里只有命令体与执行器要的两个钩子。
命令体在执行器以 READ COMMITTED 开的事务里、用 execution.connection 运行，
自己不开、不提交、不回滚事务。执行器要声明 catalog_lock=True、read_committed=True、
balance_rows=listing_balance_rows(...)、before_transaction=prepare_listing_rows(...)：

    run_with_context(
        request,
        context,
        operation=LISTINGS_UNLINK,
        body=body,
        command=lambda execution: unlink_listing(execution, body.model),
        catalog_lock=True,
        read_committed=True,
        balance_rows=listing_balance_rows(context.tenant_id, LISTINGS_UNLINK, body.model),
        before_transaction=prepare_listing_rows(context.tenant_id, LISTINGS_UNLINK, body.model),
    )

开事务之前（§2.7、§2.9 的先后）：幂等锁 → 目录写锁 → 余额行预建（listing_balance_rows）
→ 配额行预建（prepare_listing_rows）→ 以 READ COMMITTED 开业务事务。
两个钩子都用普通读由入口得出它绑定的池、由平台商品得出原 SKU（改挂、解除），
加上正文的目标 SKU（挂上、改挂）；入口不存在、不属于本租户、不是 platform 入口
（不论 active 与否：停用的平台入口上也要能解除），或平台商品、SKU 不存在时少返回那几行、
跳过预建，由事务里判。api 与 backoffice 入口不建配额行。

事务里的锁（§3.1 的锁序，用不到池行与余额行，跳过）：
改挂与解除先普通读平台商品行得出入口与原 SKU（目录写锁保证它在事务里不变；
不能先 FOR UPDATE 它，否则与平台事件“配额行 → 平台商品”的先后相反）
→ channel_allocations 行按 sku_id 升序 FOR UPDATE → sku_listings 行 FOR UPDATE
→ 目标 SKU 行与所属商品行 FOR SHARE。

判定顺序：
- 挂上：入口不存在或属于别的租户 404 NOT_FOUND，不是 platform 或不是 active 422
  → 这个平台规格已有 active 的行 409 LISTING_ALREADY_LINKED → 目标 SKU 的判定 → 写入；
- 改挂：平台商品不存在或属于别的租户 404 LISTING_NOT_FOUND → 它的入口不是 active 422
  → 版本不符 409 VERSION_CONFLICT → 行是 inactive 409 STATE_CONFLICT
  → 改挂到它现在的 SKU：200 与当前表示，不加版本、不写记录 → 目标 SKU 的判定
  → 原 SKU 在这个入口上的配额不为 0 409 LISTING_HAS_QUOTA → 写入；
- 解除：404 → 版本 → inactive → 配额 → 写入；不看入口状态（停用了的平台入口上的平台商品
  也要能解除，否则 SKU 永远停用不了）。
目标 SKU 的判定依次为：不存在或属于别的租户 404 SKU_NOT_FOUND → 已合并 409 SKU_MERGED
→ SKU 或所属商品停用 409 SKU_INACTIVE → 属于一张未结束的合并单（作来源或规范）
409 SKU_MERGE_IN_PROGRESS → 同入口同 SKU 已有别的 active 平台商品 409 LISTING_SKU_TAKEN。
SKU_MERGE_IN_PROGRESS 契约 §2.9 没写，据 §2.8 补：合并只在新建时查平台商品，
不挡这一步，执行完会留下挂着生效平台商品的已合并 SKU。
配额在锁住配额行之后读，所以与并发的改配额、取消加回串行（§2.9）。

入口、平台商品或目标 SKU 属于别的租户时经执行器登记 cross_tenant（随拒绝保留）。
写入：sku_listings（新行 lst_ 前缀、版本 1；改动版本加一；操作主体列取上下文）
与一条 catalog_changes（object_type 为 listing，product_id 为改动后所映射 SKU 的商品，
version 为平台商品行的新版本，changes 只列变了的 sku_id 与 status），与幂等记录同一事务。
成功返回平台商品的表示：挂上新建 201，其余 200。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator
from sqlalchemy import Connection, insert, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.expression import Executable

from app.api.errors import NOT_FOUND, ApiError, invalid_request
from app.api.requests import RequestBody
from app.domain.catalog import (
    ACTIVE,
    INACTIVE,
    LISTING_LINKED,
    LISTING_RELINKED,
    LISTING_UNLINKED,
    STATUS,
    InvalidField,
    normalize_reason,
)
from app.domain.channels import PLATFORM
from app.domain.ids import new_id
from app.domain.requests import Actor
from app.domain.sku_merges import OPEN_STATUSES
from app.models.catalog import NOW, Product, Sku
from app.models.inventory import SalesChannel
from app.models.merges import SkuMerge
from app.models.platform import (
    MAX_PLATFORM_ID_LENGTH,
    ChannelAllocation,
    PoolAllocation,
    SkuListing,
)
from app.services.catalog import ChangeAuthor, record_change, sku_owner
from app.services.idempotency import BalanceRows, BeforeTransaction, CommandResult, Execution
from app.services.security_events import CROSS_TENANT
from app.services.sku_merges import SKU_INACTIVE, SKU_MERGE_IN_PROGRESS, STATE_CONFLICT
from app.services.stock import SKU_MERGED, SKU_NOT_FOUND, format_timestamp

# 操作类型（§7.1、§2.9）。
LISTINGS_LINK = "listings.link"
LISTINGS_RELINK = "listings.relink"
LISTINGS_UNLINK = "listings.unlink"
OPERATIONS = frozenset({LISTINGS_LINK, LISTINGS_RELINK, LISTINGS_UNLINK})

# 对象类型（catalog_changes 的 object_type、cross_tenant 事件）与 ID 前缀。
LISTING = "listing"
CHANNEL = "channel"
SKU = "sku"
LISTING_ID_PREFIX = "lst"

# 错误码（§8.3，0.22）。
LISTING_NOT_FOUND = "LISTING_NOT_FOUND"
LISTING_ALREADY_LINKED = "LISTING_ALREADY_LINKED"
LISTING_SKU_TAKEN = "LISTING_SKU_TAKEN"
LISTING_HAS_QUOTA = "LISTING_HAS_QUOTA"
VERSION_CONFLICT = "VERSION_CONFLICT"

SKU_ID = "sku_id"

_MYSQL_NO_REFERENCED_ROW = 1452  # MySQL ER_NO_REFERENCED_ROW_2：外键指向的行不存在
_PLATFORM_ID = re.compile(rf"[A-Za-z0-9._:-]{{1,{MAX_PLATFORM_ID_LENGTH}}}")


# --- 正文（§2.9） ---


def _platform_id(field: str, value: str) -> str:
    """平台商品 ID 与规格 ID：1–64 个 [A-Za-z0-9._:-] 字符，原样比较（区分大小写）。"""
    if not isinstance(value, str) or not _PLATFORM_ID.fullmatch(value):
        raise InvalidField(field)
    return value


class _ListingBody(RequestBody):
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def check_reason(cls, value: str | None) -> str | None:
        return normalize_reason(value)


class ListingLink(_ListingBody):
    """挂上：平台规格没有行就新建，已有而 inactive 就改挂到所给 SKU 并启用。"""

    channel_id: str
    platform_item_id: str
    platform_model_id: str  # 平台商品没有规格时为 "-"
    sku_id: str

    @field_validator("platform_item_id")
    @classmethod
    def check_platform_item_id(cls, value: str) -> str:
        return _platform_id("platform_item_id", value)

    @field_validator("platform_model_id")
    @classmethod
    def check_platform_model_id(cls, value: str) -> str:
        return _platform_id("platform_model_id", value)


class ListingRelink(_ListingBody):
    """改挂：只对 active 的行。"""

    listing_id: str
    expected_version: int = Field(ge=1)
    sku_id: str


class ListingUnlink(_ListingBody):
    """解除：改为 inactive。"""

    listing_id: str
    expected_version: int = Field(ge=1)


ListingCommandBody = ListingLink | ListingRelink | ListingUnlink


# --- 读出的行 ---


@dataclass(frozen=True)
class ListingRecord:
    """sku_listings 的一行（表示要用的列）。"""

    listing_id: str
    channel_id: str
    platform_item_id: str
    platform_model_id: str
    sku_id: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


_LISTING_COLUMNS = tuple(getattr(SkuListing, field.name) for field in fields(ListingRecord))


def listing_representation(listing: ListingRecord) -> dict[str, Any]:
    """平台商品的表示：键固定且都出现。不读库。"""
    return {
        "listing_id": listing.listing_id,
        "channel_id": listing.channel_id,
        "platform_item_id": listing.platform_item_id,
        "platform_model_id": listing.platform_model_id,
        "sku_id": listing.sku_id,
        "status": listing.status,
        "version": listing.version,
        "created_at": format_timestamp(listing.created_at),
        "updated_at": format_timestamp(listing.updated_at),
    }


def _listing(row) -> ListingRecord | None:
    return None if row is None else ListingRecord(**row)


def read_listing(connection: Connection, tenant_id: str, listing_id: str) -> ListingRecord | None:
    """普通读按租户读平台商品；不存在或属于别的租户为 None。"""
    statement = select(*_LISTING_COLUMNS).where(
        SkuListing.tenant_id == tenant_id, SkuListing.listing_id == listing_id
    )
    return _listing(connection.execute(statement).mappings().first())


def listing_owner(connection: Connection, listing_id: str) -> str | None:
    """平台商品所属的租户；不存在为 None。"""
    statement = select(SkuListing.tenant_id).where(SkuListing.listing_id == listing_id)
    return connection.execute(statement).scalar_one_or_none()


@dataclass(frozen=True)
class _Channel:
    channel_id: str
    pool_id: str
    kind: str
    status: str


def _read_channel(connection: Connection, tenant_id: str, channel_id: str) -> _Channel | None:
    """普通读按租户读入口（入口只由运维命令行开通与改动）；不存在或属于别的租户为 None。"""
    statement = select(
        SalesChannel.channel_id, SalesChannel.pool_id, SalesChannel.kind, SalesChannel.status
    ).where(SalesChannel.tenant_id == tenant_id, SalesChannel.channel_id == channel_id)
    row = connection.execute(statement).mappings().first()
    return None if row is None else _Channel(**row)


def _channel_owner(connection: Connection, channel_id: str) -> str | None:
    statement = select(SalesChannel.tenant_id).where(SalesChannel.channel_id == channel_id)
    return connection.execute(statement).scalar_one_or_none()


# --- 开事务之前的两个钩子 ---


@dataclass(frozen=True)
class _PreparedRows:
    """要预建的行：一个平台入口、它绑定的池与本租户里存在的 SKU（升序）。"""

    channel_id: str
    pool_id: str
    sku_ids: tuple[str, ...]


def _check_operation(operation: str) -> None:
    if operation not in OPERATIONS:
        raise ValueError(f"not a listing operation: {operation!r}")


def _prepared_rows(
    connection: Connection, tenant_id: str, operation: str, body: ListingCommandBody
) -> _PreparedRows | None:
    """普通读：入口（挂上取正文的，改挂与解除取平台商品的）、它绑定的池，
    原 SKU（改挂、解除）与目标 SKU（挂上、改挂）。

    平台商品不存在、入口不存在或不是 platform 入口（不论 active 与否）为 None；
    不存在或属于别的租户的 SKU 不列出。
    """
    if operation == LISTINGS_LINK:
        channel_id, sku_ids = body.channel_id, {body.sku_id}
    else:
        listing = read_listing(connection, tenant_id, body.listing_id)
        if listing is None:
            return None
        channel_id, sku_ids = listing.channel_id, {listing.sku_id}
        if operation == LISTINGS_RELINK:
            sku_ids.add(body.sku_id)
    channel = _read_channel(connection, tenant_id, channel_id)
    if channel is None or channel.kind != PLATFORM:
        return None
    statement = select(Sku.sku_id).where(Sku.tenant_id == tenant_id, Sku.sku_id.in_(sku_ids))
    existing = sorted(connection.execute(statement).scalars())
    return _PreparedRows(channel.channel_id, channel.pool_id, tuple(existing))


def listing_balance_rows(tenant_id: str, operation: str, body: ListingCommandBody) -> BalanceRows:
    """执行器的余额行预建：返回入口绑定的池上原 SKU 与目标 SKU 的 (pool_id, sku_id)。

    找不到入口、平台商品或 SKU 时少返回那几行，由事务里判。
    """
    _check_operation(operation)

    def balance_rows(connection: Connection) -> list[tuple[str, str]]:
        rows = _prepared_rows(connection, tenant_id, operation, body)
        if rows is None:
            return []
        return [(rows.pool_id, sku_id) for sku_id in rows.sku_ids]

    return balance_rows


def _pool_allocation_upsert(tenant_id: str, pool_id: str, sku_id: str) -> Executable:
    """只给主键三列，其余取数据库默认值；已有的行只把 sku_id 设为自身，即不改。"""
    table = PoolAllocation.__table__
    statement = mysql_insert(table).values(tenant_id=tenant_id, pool_id=pool_id, sku_id=sku_id)
    return statement.on_duplicate_key_update(sku_id=table.c.sku_id)


def _channel_allocation_upsert(
    tenant_id: str, channel_id: str, pool_id: str, sku_id: str
) -> Executable:
    """同 _pool_allocation_upsert；pool_id 是入口绑定的池（§3.1 存放，由服务层保证）。"""
    table = ChannelAllocation.__table__
    statement = mysql_insert(table).values(
        tenant_id=tenant_id, channel_id=channel_id, pool_id=pool_id, sku_id=sku_id
    )
    return statement.on_duplicate_key_update(sku_id=table.c.sku_id)


def _upsert(connection: Connection, statement: Executable) -> None:
    """外键失败（余额行不在，极少见）只回滚这一条语句、跳过；别的错误照抛。"""
    try:
        connection.execute(statement)
    except IntegrityError as exc:
        if getattr(exc.orig, "args", ())[:1] != (_MYSQL_NO_REFERENCED_ROW,):
            raise


def prepare_listing_rows(
    tenant_id: str, operation: str, body: ListingCommandBody
) -> BeforeTransaction:
    """执行器的 before_transaction 钩子：在目录写锁与余额行预建之后、开业务事务之前，
    用短事务按 (pool_id, sku_id)、(channel_id, sku_id) 升序预建原 SKU 与目标 SKU
    在这个入口上的 pool_allocations 与 channel_allocations 行（§2.9、§3.1，不改已有的值）。

    入口不存在、不属于本租户、不是 platform 入口，或平台商品、SKU 不存在时跳过，由事务里判。
    返回时不留打开的事务。
    """
    _check_operation(operation)

    def prepare(connection: Connection) -> None:
        rows = _prepared_rows(connection, tenant_id, operation, body)
        # 结束读隐式开的事务：预建在自己的短事务里提交。
        connection.rollback()
        if rows is None or not rows.sku_ids:
            return
        with connection.begin():
            for sku_id in rows.sku_ids:
                _upsert(connection, _pool_allocation_upsert(tenant_id, rows.pool_id, sku_id))
            for sku_id in rows.sku_ids:
                statement = _channel_allocation_upsert(
                    tenant_id, rows.channel_id, rows.pool_id, sku_id
                )
                _upsert(connection, statement)

    return prepare


# --- 事务里的锁 ---


def _tenant_id(execution: Execution) -> str:
    return execution.context.tenant_id


def lock_channel_quotas(
    execution: Execution, channel_id: str, sku_ids: Sequence[str]
) -> dict[str, int]:
    """按 sku_id 升序 FOR UPDATE 锁这个入口上各 SKU 的 channel_allocations 行，返回配额。

    行由 prepare_listing_rows 预建；SKU 不存在时没有行，不出现在结果里（由之后的判定报 404）。
    """
    tenant_id = _tenant_id(execution)
    quotas: dict[str, int] = {}
    for sku_id in sorted(set(sku_ids)):
        statement = (
            select(ChannelAllocation.quota)
            .where(
                ChannelAllocation.tenant_id == tenant_id,
                ChannelAllocation.channel_id == channel_id,
                ChannelAllocation.sku_id == sku_id,
            )
            .with_for_update()
        )
        quota = execution.connection.execute(statement).scalar_one_or_none()
        if quota is not None:
            quotas[sku_id] = quota
    return quotas


def _lock_listing(execution: Execution, *criteria) -> ListingRecord | None:
    statement = (
        select(*_LISTING_COLUMNS)
        .where(SkuListing.tenant_id == _tenant_id(execution), *criteria)
        .with_for_update()
    )
    return _listing(execution.connection.execute(statement).mappings().first())


def _lock_listing_by_id(execution: Execution, listing_id: str) -> ListingRecord:
    """FOR UPDATE 读已普通读到的平台商品行；目录写锁保证它还在，读不到是程序错误。"""
    listing = _lock_listing(execution, SkuListing.listing_id == listing_id)
    if listing is None:
        raise RuntimeError(f"listing {listing_id} is not in the tenant")
    return listing


@dataclass(frozen=True)
class _TargetSku:
    """FOR SHARE 读到的目标 SKU 与所属商品的状态。"""

    sku_id: str
    product_id: str
    status: str
    merged_into_sku_id: str | None
    product_status: str


def _share_sku(execution: Execution, sku_id: str) -> _TargetSku | None:
    """FOR SHARE 读本租户的 SKU 行，再读所属商品行；不存在或属于别的租户为 None。"""
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    statement = (
        select(Sku.sku_id, Sku.product_id, Sku.status, Sku.merged_into_sku_id)
        .where(Sku.tenant_id == tenant_id, Sku.sku_id == sku_id)
        .with_for_update(read=True)
    )
    row = connection.execute(statement).mappings().first()
    if row is None:
        return None
    product = (
        select(Product.status)
        .where(Product.tenant_id == tenant_id, Product.product_id == row["product_id"])
        .with_for_update(read=True)
    )
    return _TargetSku(**row, product_status=connection.execute(product).scalar_one())


# --- 判定 ---


def _cross_tenant(execution: Execution, object_type: str, object_id: str) -> None:
    execution.record_security_event(CROSS_TENANT, {"object_type": object_type, "id": object_id})


def _channel_of(execution: Execution, channel_id: str) -> _Channel:
    """挂上的入口：不存在或属于别的租户 404 NOT_FOUND（两种情形回答完全相同，
    后者登记 cross_tenant）；不是 platform 或不是 active 422 INVALID_REQUEST。
    """
    connection = execution.connection
    channel = _read_channel(connection, _tenant_id(execution), channel_id)
    if channel is None:
        if _channel_owner(connection, channel_id) is not None:
            _cross_tenant(execution, CHANNEL, channel_id)
        raise ApiError(404, NOT_FOUND, f"{CHANNEL} not found")
    _require_active_platform(channel)
    return channel


def _require_active_platform(channel: _Channel) -> None:
    if channel.kind != PLATFORM or channel.status != ACTIVE:
        kind, status = channel.kind, channel.status
        details = {"channel_id": channel.channel_id, "kind": kind, "status": status}
        raise invalid_request("the channel is not an active platform channel", details)


def _existing_listing(execution: Execution, listing_id: str) -> ListingRecord:
    """改挂与解除：普通读平台商品；不存在或属于别的租户 404 LISTING_NOT_FOUND，
    后者登记 cross_tenant。
    """
    connection = execution.connection
    listing = read_listing(connection, _tenant_id(execution), listing_id)
    if listing is not None:
        return listing
    if listing_owner(connection, listing_id) is not None:
        _cross_tenant(execution, LISTING, listing_id)
    raise ApiError(404, LISTING_NOT_FOUND, f"{LISTING} not found")


def _listing_channel(execution: Execution, listing: ListingRecord) -> _Channel:
    """平台商品的入口：外键保证同租户，读不到是程序错误。"""
    channel = _read_channel(execution.connection, _tenant_id(execution), listing.channel_id)
    if channel is None:
        raise RuntimeError(f"channel {listing.channel_id} is not in the tenant")
    return channel


def _check_version(expected_version: int, listing: ListingRecord) -> None:
    if expected_version != listing.version:
        raise ApiError(
            409,
            VERSION_CONFLICT,
            "expected_version does not match the current version",
            details={"current_version": listing.version},
        )


def _require_active_listing(listing: ListingRecord) -> None:
    if listing.status != ACTIVE:
        details = {"status": listing.status}
        raise ApiError(409, STATE_CONFLICT, "the listing is in another state", details=details)


def _open_merge(execution: Execution, sku_id: str):
    """普通读查这个 SKU 作来源或规范的未结束合并单，有几张时取 merge_id 最小的一张。"""
    statement = (
        select(SkuMerge.merge_id, SkuMerge.source_sku_id, SkuMerge.canonical_sku_id)
        .where(
            SkuMerge.tenant_id == _tenant_id(execution),
            SkuMerge.status.in_(sorted(OPEN_STATUSES)),
            or_(SkuMerge.source_sku_id == sku_id, SkuMerge.canonical_sku_id == sku_id),
        )
        .order_by(SkuMerge.merge_id)
        .limit(1)
    )
    return execution.connection.execute(statement).mappings().first()


def _check_target(execution: Execution, sku_id: str, target: _TargetSku | None) -> _TargetSku:
    """目标 SKU：404 SKU_NOT_FOUND → 409 SKU_MERGED → 409 SKU_INACTIVE
    → 409 SKU_MERGE_IN_PROGRESS（details 同 SKU 合并新建的写法）。
    """
    if target is None:
        if sku_owner(execution.connection, sku_id) is not None:
            _cross_tenant(execution, SKU, sku_id)
        raise ApiError(404, SKU_NOT_FOUND, "sku not found", details={"sku_ids": [sku_id]})
    if target.merged_into_sku_id is not None:
        details = {"sku_id": sku_id, "merged_into_sku_id": target.merged_into_sku_id}
        raise ApiError(409, SKU_MERGED, "sku has been merged", details=details)
    if target.status != ACTIVE or target.product_status != ACTIVE:
        raise ApiError(409, SKU_INACTIVE, "sku is inactive", details={"sku_ids": [sku_id]})
    merge = _open_merge(execution, sku_id)
    if merge is not None:
        raise ApiError(
            409,
            SKU_MERGE_IN_PROGRESS,
            "a sku is in another open sku merge",
            details=dict(merge),
        )
    return target


def _require_sku_free(
    execution: Execution, channel_id: str, sku_id: str, listing_id: str | None
) -> None:
    """同入口同 SKU 已有别的 active 平台商品：409 LISTING_SKU_TAKEN，details 带那一条。

    普通读：平台商品只由这三个命令改，它们都持目录写锁，互相串行。
    """
    criteria = [
        SkuListing.tenant_id == _tenant_id(execution),
        SkuListing.channel_id == channel_id,
        SkuListing.sku_id == sku_id,
        SkuListing.status == ACTIVE,
    ]
    if listing_id is not None:
        criteria.append(SkuListing.listing_id != listing_id)
    statement = select(SkuListing.listing_id).where(*criteria).order_by(SkuListing.listing_id)
    taken = execution.connection.execute(statement.limit(1)).scalar_one_or_none()
    if taken is not None:
        details = {"channel_id": channel_id, "sku_id": sku_id, "listing_id": taken}
        message = "the sku already has an active listing on this channel"
        raise ApiError(409, LISTING_SKU_TAKEN, message, details=details)


def _require_no_quota(listing: ListingRecord, quotas: dict[str, int]) -> None:
    """锁住之后原 SKU 在这个入口上的配额不为 0：409 LISTING_HAS_QUOTA（先把配额改为 0）。"""
    quota = quotas.get(listing.sku_id, 0)
    if quota != 0:
        details = {"channel_id": listing.channel_id, "sku_id": listing.sku_id, "quota": quota}
        message = "the sku still has quota on this channel"
        raise ApiError(409, LISTING_HAS_QUOTA, message, details=details)


# --- 写入 ---


def _actor(execution: Execution) -> Actor:
    """平台商品命令要求 catalog_admin 角色（§2.9），操作者必在。"""
    actor = execution.context.actor
    if actor is None:
        raise RuntimeError("a listing command needs the actor")
    return actor


def _operator(execution: Execution) -> dict[str, Any]:
    """sku_listings 上最后一次改动的操作主体列（§10 第 4 条）。"""
    actor = _actor(execution)
    context = execution.context
    return {
        "credential_id": context.credential_id,
        "actor_ref": actor.ref,
        "actor_role": actor.role,
        "request_id": context.request_id,
    }


def _record(
    execution: Execution,
    reason: str | None,
    *,
    event_type: str,
    listing_id: str,
    product_id: str,
    version: int,
    changes: dict[str, Any],
) -> None:
    """一条 object_type 为 listing 的目录变更记录（§2.5、§2.7）。"""
    actor = _actor(execution)
    context = execution.context
    author = ChangeAuthor(
        credential_id=context.credential_id,
        actor_ref=actor.ref,
        actor_role=actor.role,
        request_id=context.request_id,
        idempotency_key=context.idempotency_key,
        reason=reason,
    )
    record_change(
        execution.connection,
        _tenant_id(execution),
        author,
        event_type=event_type,
        object_type=LISTING,
        object_id=listing_id,
        product_id=product_id,
        version=version,
        changes=changes,
    )


def _change(before: Any, after: Any) -> dict[str, Any]:
    return {"before": before, "after": after}


def _update_listing(execution: Execution, listing: ListingRecord, values: dict[str, Any]) -> int:
    """改 sku_id 或 status，版本加一，返回新版本。"""
    version = listing.version + 1
    execution.connection.execute(
        update(SkuListing)
        .where(
            SkuListing.tenant_id == _tenant_id(execution),
            SkuListing.listing_id == listing.listing_id,
        )
        .values(**values, version=version, updated_at=NOW, **_operator(execution))
    )
    return version


def _result(execution: Execution, listing_id: str, status: int = 200) -> CommandResult:
    """改后的表示：同一事务里普通读，看得到本命令的写入。"""
    listing = read_listing(execution.connection, _tenant_id(execution), listing_id)
    return CommandResult(listing_representation(listing), status)


# --- 命令体 ---


def link_listing(execution: Execution, body: ListingLink) -> CommandResult:
    """挂上（listings.link）：新建 201、版本 1；已有 inactive 的行改挂并启用，200。"""
    channel = _channel_of(execution, body.channel_id)
    lock_channel_quotas(execution, channel.channel_id, [body.sku_id])
    listing = _lock_listing(
        execution,
        SkuListing.channel_id == channel.channel_id,
        SkuListing.platform_item_id == body.platform_item_id,
        SkuListing.platform_model_id == body.platform_model_id,
    )
    target = _share_sku(execution, body.sku_id)

    if listing is not None and listing.status == ACTIVE:
        details = {"listing_id": listing.listing_id, "sku_id": listing.sku_id}
        message = "the platform model is already linked; relink it instead"
        raise ApiError(409, LISTING_ALREADY_LINKED, message, details=details)
    target = _check_target(execution, body.sku_id, target)
    existing_id = None if listing is None else listing.listing_id
    _require_sku_free(execution, channel.channel_id, body.sku_id, existing_id)

    if listing is None:
        listing_id = new_id(LISTING_ID_PREFIX)
        execution.connection.execute(
            insert(SkuListing).values(
                listing_id=listing_id,
                tenant_id=_tenant_id(execution),
                channel_id=channel.channel_id,
                platform_item_id=body.platform_item_id,
                platform_model_id=body.platform_model_id,
                sku_id=body.sku_id,
                status=ACTIVE,
                version=1,
                **_operator(execution),
            )
        )
        version, status = 1, 201
        changes = {SKU_ID: _change(None, body.sku_id), STATUS: _change(None, ACTIVE)}
    else:
        listing_id = listing.listing_id
        changes = {}
        if listing.sku_id != body.sku_id:
            changes[SKU_ID] = _change(listing.sku_id, body.sku_id)
        changes[STATUS] = _change(listing.status, ACTIVE)
        version = _update_listing(execution, listing, {SKU_ID: body.sku_id, STATUS: ACTIVE})
        status = 200
    _record(
        execution,
        body.reason,
        event_type=LISTING_LINKED,
        listing_id=listing_id,
        product_id=target.product_id,
        version=version,
        changes=changes,
    )
    return _result(execution, listing_id, status)


def relink_listing(execution: Execution, body: ListingRelink) -> CommandResult:
    """改挂（listings.relink）：把 active 的平台商品挂到另一个 SKU，200。"""
    read = _existing_listing(execution, body.listing_id)
    _require_active_platform(_listing_channel(execution, read))
    quotas = lock_channel_quotas(execution, read.channel_id, [read.sku_id, body.sku_id])
    listing = _lock_listing_by_id(execution, read.listing_id)
    target = _share_sku(execution, body.sku_id)

    _check_version(body.expected_version, listing)
    _require_active_listing(listing)
    if body.sku_id == listing.sku_id:
        return CommandResult(listing_representation(listing))
    target = _check_target(execution, body.sku_id, target)
    _require_sku_free(execution, listing.channel_id, body.sku_id, listing.listing_id)
    _require_no_quota(listing, quotas)

    version = _update_listing(execution, listing, {SKU_ID: body.sku_id})
    _record(
        execution,
        body.reason,
        event_type=LISTING_RELINKED,
        listing_id=listing.listing_id,
        product_id=target.product_id,
        version=version,
        changes={SKU_ID: _change(listing.sku_id, body.sku_id)},
    )
    return _result(execution, listing.listing_id)


def unlink_listing(execution: Execution, body: ListingUnlink) -> CommandResult:
    """解除（listings.unlink）：active 的平台商品改为 inactive，200；不看入口状态。"""
    read = _existing_listing(execution, body.listing_id)
    quotas = lock_channel_quotas(execution, read.channel_id, [read.sku_id])
    listing = _lock_listing_by_id(execution, read.listing_id)

    _check_version(body.expected_version, listing)
    _require_active_listing(listing)
    _require_no_quota(listing, quotas)

    version = _update_listing(execution, listing, {STATUS: INACTIVE})
    # 所映射的 SKU 不变；SKU 的商品不改，普通读即可。
    statement = select(Sku.product_id).where(
        Sku.tenant_id == _tenant_id(execution), Sku.sku_id == listing.sku_id
    )
    product_id = execution.connection.execute(statement).scalar_one()
    _record(
        execution,
        body.reason,
        event_type=LISTING_UNLINKED,
        listing_id=listing.listing_id,
        product_id=product_id,
        version=version,
        changes={STATUS: _change(ACTIVE, INACTIVE)},
    )
    return _result(execution, listing.listing_id)
