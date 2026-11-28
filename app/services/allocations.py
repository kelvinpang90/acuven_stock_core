"""改配额与安全库存（契约 §3.1 allocations.update，0.22，Q-81、Q-102；§3.2 Q-99）。

命令只给后台用（§8.6）；后台的执行器与接口不在这里，
这里只有命令体与开事务之前的钩子。命令体在执行器以 READ COMMITTED
开的事务里、用 execution.connection 运行，自己不开、不提交、不回滚事务。
正文是 JSON 对象原样：形状要先知道池的 platform 入口才能判，
由命令体调 validate_allocation_request。pool_id 与 sku_id 的格式
由调用方在调执行器之前用 check_allocation_ids 判（不合格 422），钩子要用它们：

    check_allocation_ids(body.raw)
    run_with_context(
        request,
        context,
        operation=ALLOCATIONS_UPDATE,
        body=body,
        command=lambda execution: update_allocations(execution, body.raw),
        read_committed=True,
        before_transaction=prepare_allocation_rows(
            context.tenant_id, body.raw["pool_id"], body.raw["sku_id"]
        ),
    )

开事务之前（prepare_allocation_rows）：普通读确认池与 SKU 属于本租户、
取池的全部 platform 入口（不论 active 与否），再用短事务依次预建余额行、
pool_allocations 行与各入口按 channel_id 升序的 channel_allocations 行
（§3.1、§4.1，取默认值、不改已有的值）。池或 SKU 不属于本租户、
池没有 platform 入口时跳过，由事务里判。

事务里的锁（§3.1 的锁序）：池行 FOR SHARE → 余额行 FOR UPDATE
→ pool_allocations 行 FOR UPDATE → 各入口的 channel_allocations 行
FOR UPDATE（按 channel_id 升序）→ 各入口上这个 SKU 的平台商品 FOR SHARE
→ SKU 行 FOR SHARE。平台连接行与人工核对用普通读，逾期按数据库时间算。

判定顺序：池不存在或属于别的租户 404 POOL_NOT_FOUND
→ 池没有 platform 入口 422（契约没写，据 §3.1“只有组合 2 的池有这两张表的行”补）
→ 正文形状 422 → SKU 不存在或属于别的租户 404 SKU_NOT_FOUND
→ 增加 inactive 入口的配额 422（补的）→ judge_allocation_update 的 409。
SKU 已合并或停用不拒：减配额要能做。池的 platform 入口不论 active 与否
都算在 F 的求和与表示里。池或 SKU 属于别的租户时经执行器登记 cross_tenant。

写入：新值与当前值全部相同时 200 与当前表示，不加版本、不写记录
（同目录“已是目标状态”）；否则写新的 safety_stock 与变了的入口的 quota，
pool_allocations.version 加一，写一条 allocation_changes
（changes 只列变了的 S 与各入口 Q 的前后值），与幂等记录同一事务。
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, func, insert, select, text, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.expression import Executable

from app.api.auth import POOL_NOT_FOUND
from app.api.errors import ApiError, invalid_request
from app.domain import allocations as rules
from app.domain.allocations import (
    ALLOCATION_EXCEEDS_UNALLOCATED,
    MANUAL_CHECK_OVERDUE,
    NO_ACTIVE_LISTING,
    QUOTA_CHANGED,
    VERSION_CONFLICT,
    AllocationRequest,
    AllocationSnapshot,
    Rejection,
    judge_allocation_update,
    manual_check_overdue,
    unallocated,
    validate_allocation_request,
)
from app.domain.catalog import ACTIVE
from app.domain.channels import PLATFORM
from app.domain.ids import CROCKFORD_ALPHABET, MAX_ID_LENGTH, ULID_LENGTH, new_id
from app.domain.requests import Actor
from app.models.catalog import NOW, Sku
from app.models.inventory import SalesChannel, StockBalance, StockPool
from app.models.platform import AllocationChange, ChannelAllocation, PoolAllocation, SkuListing
from app.models.platform_ops import ManualCheck, PlatformConnection
from app.services.catalog import sku_owner
from app.services.idempotency import BeforeTransaction, CommandResult, Execution
from app.services.security_events import CROSS_TENANT
from app.services.stock import lock_balances, require_skus_exist
from app.services.stock_queries import pool_owner

# 操作类型（§7.1、§3.1）。
ALLOCATIONS_UPDATE = "allocations.update"

# 错误码（§3.1、§8.3，0.22）与消息；判定在 app/domain/allocations.py 的纯函数里。
CONFLICT_MESSAGES = {
    VERSION_CONFLICT: "expected_version does not match the current version",
    QUOTA_CHANGED: "the quota of a channel changed since it was read",
    NO_ACTIVE_LISTING: "the sku has no active listing on the channel",
    MANUAL_CHECK_OVERDUE: "a manual channel is overdue for its manual check",
    ALLOCATION_EXCEEDS_UNALLOCATED: "the allocation exceeds the unallocated stock",
}

# 对象类型（cross_tenant 事件）。
POOL = "pool"

POOL_ID = rules.POOL_ID
SKU_ID = rules.SKU_ID
ID_FIELDS = (POOL_ID, SKU_ID)

_MYSQL_NO_REFERENCED_ROW = 1452  # MySQL ER_NO_REFERENCED_ROW_2：外键指向的行不存在
# 稳定 ID 的形状（§1）：小写前缀、下划线、26 个 Crockford Base32 字符，总长不超过 32。
_ID = re.compile(rf"[a-z]+_[{re.escape(CROCKFORD_ALPHABET)}]{{{ULID_LENGTH}}}")

# channel_id → (quota, pending)。
Allocations = Mapping[str, tuple[int, int]]


# --- 调执行器之前的 ID 格式 ---


def is_valid_id(value) -> bool:
    return isinstance(value, str) and len(value) <= MAX_ID_LENGTH and bool(_ID.fullmatch(value))


def check_allocation_ids(body) -> None:
    """正文的 pool_id、sku_id 依次要在且是合格的 ID：不合格 422 INVALID_REQUEST，
    details.field 是第一个出错的字段（同 validate_allocation_request 的写法）。
    """
    if not isinstance(body, Mapping):
        raise invalid_request("request body must be a JSON object", {"field": "body"})
    for field in ID_FIELDS:
        if not is_valid_id(body.get(field)):
            raise invalid_request(f"invalid {field}", {"field": field})


# --- 开事务之前的钩子 ---


def _platform_channels(connection: Connection, tenant_id: str, pool_id: str) -> dict[str, str]:
    """普通读这个池的全部 platform 入口（不论 active 与否）：channel_id → status，
    按 channel_id 升序。入口只由运维命令行开通与改动。
    """
    statement = (
        select(SalesChannel.channel_id, SalesChannel.status)
        .where(
            SalesChannel.tenant_id == tenant_id,
            SalesChannel.pool_id == pool_id,
            SalesChannel.kind == PLATFORM,
        )
        .order_by(SalesChannel.channel_id)
    )
    return dict(connection.execute(statement).all())


def _balance_upsert(tenant_id: str, pool_id: str, sku_id: str) -> Executable:
    """同执行器的余额行预建：只给四列，已有的行不改。"""
    table = StockBalance.__table__
    statement = mysql_insert(table).values(
        balance_id=new_id("bal"), tenant_id=tenant_id, pool_id=pool_id, sku_id=sku_id
    )
    return statement.on_duplicate_key_update(balance_id=table.c.balance_id)


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
    """外键失败（极少见）只回滚这一条语句、跳过；别的错误照抛。"""
    try:
        connection.execute(statement)
    except IntegrityError as exc:
        if getattr(exc.orig, "args", ())[:1] != (_MYSQL_NO_REFERENCED_ROW,):
            raise


def prepare_allocation_rows(tenant_id: str, pool_id: str, sku_id: str) -> BeforeTransaction:
    """执行器的 before_transaction 钩子：开业务事务之前用短事务预建这个池这个 SKU 的
    余额行、pool_allocations 行与这个池全部 platform 入口的 channel_allocations 行
    （按 channel_id 升序）。

    池或 SKU 不属于本租户、池没有 platform 入口时跳过，由事务里判。
    返回时不留打开的事务。
    """

    def prepare(connection: Connection) -> None:
        channel_ids: list[str] = []
        owned = pool_owner(connection, pool_id) == tenant_id
        if owned and sku_owner(connection, sku_id) == tenant_id:
            channel_ids = list(_platform_channels(connection, tenant_id, pool_id))
        # 结束读隐式开的事务：预建在自己的短事务里提交。
        connection.rollback()
        if not channel_ids:
            return
        with connection.begin():
            _upsert(connection, _balance_upsert(tenant_id, pool_id, sku_id))
            _upsert(connection, _pool_allocation_upsert(tenant_id, pool_id, sku_id))
            for channel_id in channel_ids:
                statement = _channel_allocation_upsert(tenant_id, channel_id, pool_id, sku_id)
                _upsert(connection, statement)

    return prepare


# --- 事务里的锁 ---


def _tenant_id(execution: Execution) -> str:
    return execution.context.tenant_id


def _cross_tenant(execution: Execution, object_type: str, object_id: str) -> None:
    execution.record_security_event(CROSS_TENANT, {"object_type": object_type, "id": object_id})


def _lock_pool(execution: Execution, pool_id: str) -> None:
    """FOR SHARE 按租户读池行；不存在或属于别的租户 404 POOL_NOT_FOUND
    （两种情形回答完全相同，后者登记 cross_tenant）。
    """
    connection = execution.connection
    statement = (
        select(StockPool.pool_id)
        .where(StockPool.tenant_id == _tenant_id(execution), StockPool.pool_id == pool_id)
        .with_for_update(read=True)
    )
    if connection.execute(statement).scalar_one_or_none() is not None:
        return
    if pool_owner(connection, pool_id) is not None:
        _cross_tenant(execution, POOL, pool_id)
    raise ApiError(404, POOL_NOT_FOUND, f"{POOL} not found")


@dataclass(frozen=True)
class _PoolAllocation:
    safety_stock: int
    version: int


def _lock_pool_allocation(execution: Execution, pool_id: str, sku_id: str) -> _PoolAllocation:
    """FOR UPDATE 读 pool_allocations 行。行由 prepare_allocation_rows 预建；
    读不到（SKU 在预建之后才建成，极少见）是程序错误：500，同键重试时预建会补上。
    """
    statement = (
        select(PoolAllocation.safety_stock, PoolAllocation.version)
        .where(
            PoolAllocation.tenant_id == _tenant_id(execution),
            PoolAllocation.pool_id == pool_id,
            PoolAllocation.sku_id == sku_id,
        )
        .with_for_update()
    )
    row = execution.connection.execute(statement).mappings().first()
    if row is None:
        raise RuntimeError(f"no pool allocation row for pool {pool_id} and sku {sku_id}")
    return _PoolAllocation(**row)


def lock_channel_allocations(
    execution: Execution, sku_id: str, channel_ids: Sequence[str]
) -> dict[str, tuple[int, int]]:
    """按 channel_id 升序 FOR UPDATE 锁各入口在这个 SKU 上的 channel_allocations 行，
    返回 channel_id → (quota, pending)。行由 prepare_allocation_rows 预建，读不到是程序错误。
    """
    tenant_id = _tenant_id(execution)
    rows: dict[str, tuple[int, int]] = {}
    for channel_id in sorted(set(channel_ids)):
        statement = (
            select(ChannelAllocation.quota, ChannelAllocation.pending)
            .where(
                ChannelAllocation.tenant_id == tenant_id,
                ChannelAllocation.channel_id == channel_id,
                ChannelAllocation.sku_id == sku_id,
            )
            .with_for_update()
        )
        row = execution.connection.execute(statement).first()
        if row is None:
            message = f"no channel allocation row for channel {channel_id} and sku {sku_id}"
            raise RuntimeError(message)
        rows[channel_id] = (row.quota, row.pending)
    return rows


def _share_listings(execution: Execution, sku_id: str, channel_ids: Sequence[str]) -> frozenset:
    """配额行锁住之后按 channel_id 升序 FOR SHARE 读各入口上这个 SKU 的平台商品
    （§3.1、§2.9）；返回这个 SKU 有 active 平台商品的入口。
    """
    tenant_id = _tenant_id(execution)
    listed = set()
    for channel_id in sorted(set(channel_ids)):
        statuses = execution.connection.execute(
            select(SkuListing.status)
            .where(
                SkuListing.tenant_id == tenant_id,
                SkuListing.channel_id == channel_id,
                SkuListing.sku_id == sku_id,
            )
            .order_by(SkuListing.listing_id)
            .with_for_update(read=True)
        ).scalars()
        if ACTIVE in set(statuses):
            listed.add(channel_id)
    return frozenset(listed)


def _share_sku(execution: Execution, sku_id: str) -> None:
    """FOR SHARE 读 SKU 行；已合并或停用都不拒。之前已判过存在，读不到是程序错误。"""
    statement = (
        select(Sku.sku_id)
        .where(Sku.tenant_id == _tenant_id(execution), Sku.sku_id == sku_id)
        .with_for_update(read=True)
    )
    if execution.connection.execute(statement).scalar_one_or_none() is None:
        raise RuntimeError(f"sku {sku_id} is not in the tenant")


def _overdue_channels(execution: Execution, channel_ids: Sequence[str]) -> frozenset:
    """普通读各入口的连接行与人工核对，以数据库时间判人工核对逾期（§3.2，Q-99）；
    没有连接行的入口不算逾期。
    """
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    ids = list(channel_ids)
    connections = connection.execute(
        select(
            PlatformConnection.channel_id,
            PlatformConnection.mode,
            PlatformConnection.mode_changed_at,
        ).where(
            PlatformConnection.tenant_id == tenant_id,
            PlatformConnection.channel_id.in_(ids),
        )
    ).all()
    if not connections:
        return frozenset()
    statement = (
        select(ManualCheck.channel_id, func.max(ManualCheck.checked_until))
        .where(ManualCheck.tenant_id == tenant_id, ManualCheck.channel_id.in_(ids))
        .group_by(ManualCheck.channel_id)
    )
    checked = dict(connection.execute(statement).all())
    now = connection.execute(text("SELECT CURRENT_TIMESTAMP(6)")).scalar_one()
    return frozenset(
        row.channel_id
        for row in connections
        if manual_check_overdue(row.mode, checked.get(row.channel_id), row.mode_changed_at, now)
    )


# --- 判定 ---


def _validated(body, channel_ids: Sequence[str]) -> AllocationRequest:
    result = validate_allocation_request(body, channel_ids)
    if isinstance(result, Rejection):
        raise invalid_request("request body validation failed", result.details)
    return result


def _require_active_for_increase(
    request: AllocationRequest,
    statuses: Mapping[str, str],
    current: Allocations,
) -> None:
    """要增加 inactive 入口的配额：422 INVALID_REQUEST（契约没写，补的）；减或不变照常。"""
    for change in request.quotas or ():
        channel_id = change.channel_id
        status = statuses[channel_id]
        if status != ACTIVE and change.quota > current[channel_id][0]:
            details = {"channel_id": channel_id, "status": status}
            raise invalid_request("cannot increase the quota of an inactive channel", details)


def _conflict(rejection: Rejection) -> ApiError:
    message = CONFLICT_MESSAGES.get(rejection.code)
    if message is None:
        raise RuntimeError(f"unexpected allocation rejection: {rejection.code}")
    return ApiError(409, rejection.code, message, details=rejection.details)


# --- 写入与表示 ---


def _actor(execution: Execution) -> Actor:
    """改配额要求 stock_admin 角色（§3.1），操作者必在。"""
    actor = execution.context.actor
    if actor is None:
        raise RuntimeError("an allocation update needs the actor")
    return actor


def _change(before: Any, after: Any) -> dict[str, Any]:
    return {"before": before, "after": after}


def allocation_representation(
    pool_id: str,
    sku_id: str,
    version: int,
    safety_stock: int,
    available: int,
    channels: Allocations,
) -> dict[str, Any]:
    """改配额的表示；channels 是这个池全部 platform 入口的 (quota, pending)，
    按 channel_id 升序列出。不读库。
    """
    channel_ids = sorted(channels)
    quotas = [channels[channel_id][0] for channel_id in channel_ids]
    pendings = [channels[channel_id][1] for channel_id in channel_ids]
    return {
        "pool_id": pool_id,
        "sku_id": sku_id,
        "version": version,
        "safety_stock": safety_stock,
        "available": available,
        "unallocated": unallocated(available, safety_stock, quotas, pendings),
        "channels": [
            {"channel_id": channel_id, "quota": quota, "pending": pending}
            for channel_id, quota, pending in zip(channel_ids, quotas, pendings, strict=True)
        ],
    }


def _write(
    execution: Execution,
    request: AllocationRequest,
    pool_id: str,
    sku_id: str,
    current: _PoolAllocation,
    safety_stock: int,
    quota_changes: Mapping[str, tuple[int, int]],
) -> int:
    """写新值、version 加一、写一条 allocation_changes；返回新版本。

    quota_changes 是变了的入口的 (before, after)。
    """
    connection = execution.connection
    tenant_id = _tenant_id(execution)
    version = current.version + 1
    connection.execute(
        update(PoolAllocation)
        .where(
            PoolAllocation.tenant_id == tenant_id,
            PoolAllocation.pool_id == pool_id,
            PoolAllocation.sku_id == sku_id,
        )
        .values(safety_stock=safety_stock, version=version, updated_at=NOW)
    )
    for channel_id in sorted(quota_changes):
        connection.execute(
            update(ChannelAllocation)
            .where(
                ChannelAllocation.tenant_id == tenant_id,
                ChannelAllocation.channel_id == channel_id,
                ChannelAllocation.sku_id == sku_id,
            )
            .values(quota=quota_changes[channel_id][1], updated_at=NOW)
        )

    changes: dict[str, Any] = {}
    if safety_stock != current.safety_stock:
        changes[rules.SAFETY_STOCK] = _change(current.safety_stock, safety_stock)
    if quota_changes:
        changes[rules.QUOTAS] = {
            channel_id: _change(*change) for channel_id, change in sorted(quota_changes.items())
        }
    actor = _actor(execution)
    context = execution.context
    connection.execute(
        insert(AllocationChange).values(
            tenant_id=tenant_id,
            pool_id=pool_id,
            sku_id=sku_id,
            changes=changes,
            reason=request.reason,
            credential_id=context.credential_id,
            actor_ref=actor.ref,
            actor_role=actor.role,
            request_id=context.request_id,
        )
    )
    return version


# --- 命令体 ---


def update_allocations(execution: Execution, body) -> CommandResult:
    """改配额与安全库存（allocations.update）：成功 200 与表示。"""
    check_allocation_ids(body)
    pool_id, sku_id = body[POOL_ID], body[SKU_ID]

    _lock_pool(execution, pool_id)
    statuses = _platform_channels(execution.connection, _tenant_id(execution), pool_id)
    if not statuses:
        details = {"pool_id": pool_id}
        raise invalid_request("the pool has no platform channel", details)
    channel_ids = list(statuses)
    request = _validated(body, channel_ids)
    require_skus_exist(execution, [sku_id])

    balance = lock_balances(execution, pool_id, [sku_id])[sku_id]
    pool_allocation = _lock_pool_allocation(execution, pool_id, sku_id)
    current = lock_channel_allocations(execution, sku_id, channel_ids)
    listed = _share_listings(execution, sku_id, channel_ids)
    _share_sku(execution, sku_id)
    overdue = _overdue_channels(execution, channel_ids)

    _require_active_for_increase(request, statuses, current)
    available = balance.quantities.available
    allocations = tuple(
        rules.ChannelAllocation(
            channel_id=channel_id,
            quota=current[channel_id][0],
            pending=current[channel_id][1],
            has_active_listing=channel_id in listed,
            manual_overdue=channel_id in overdue,
        )
        for channel_id in sorted(current)
    )
    snapshot = AllocationSnapshot(
        version=pool_allocation.version,
        safety_stock=pool_allocation.safety_stock,
        available=available,
        channels=allocations,
    )
    result = judge_allocation_update(snapshot, request)
    if isinstance(result, Rejection):
        raise _conflict(result)

    safety_stock = result.safety_stock
    quota_changes = {
        channel_id: (current[channel_id][0], quota)
        for channel_id, quota in result.quotas.items()
        if quota != current[channel_id][0]
    }
    if safety_stock == pool_allocation.safety_stock and not quota_changes:
        # 已是目标状态：不加版本、不写记录。
        version = pool_allocation.version
    else:
        version = _write(
            execution, request, pool_id, sku_id, pool_allocation, safety_stock, quota_changes
        )
    after = {
        channel_id: (result.quotas[channel_id], pending)
        for channel_id, (_, pending) in current.items()
    }
    data = allocation_representation(pool_id, sku_id, version, safety_stock, available, after)
    return CommandResult(data)
