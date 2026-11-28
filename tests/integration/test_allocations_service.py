"""改配额与安全库存（契约 §3.1 allocations.update 与锁序、§3.2 人工核对逾期，0.22，Q-102）。

在真 MySQL 上跑。命令只给后台用，后台的执行器与接口还没有，
所以测试给应用挂一个测试专用的写路由：正文只做宽松的 JSON 对象解析，
先用 check_allocation_ids 判 pool_id 与 sku_id 的格式，再经执行器核心
run_with_context 调命令体（read_committed 为真，before_transaction 为
prepare_allocation_rows）。命令上下文按幂等键取测试登记的：
主体是直接插入的 admin 凭据，操作者是 stock_admin 角色组；request_id、方法与路径取本次请求。

每个测试建自己的两个租户（本租户与“别的租户”）：本租户一个池挂两个 active 的
platform 入口（shp 为 auto，tts 为 manual）、一个 inactive 的 platform 入口与一个 backoffice
入口，另一个池只挂一个 api 入口；别的租户一个池挂一个 platform 入口。
数据都在 db 夹具之外提交，测试结束时在 finally 里按外键先后删掉。
"""

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from pydantic import ConfigDict
from sqlalchemy import Engine, delete, insert, select, text, update

from app.api.requests import ParsedBody, RequestBody, json_body
from app.config import Settings
from app.domain.requests import STOCK_ADMIN, Actor
from app.main import create_app
from app.models.catalog import Product, Sku, Tenant
from app.models.inventory import IdempotencyRecord, Location, SalesChannel, StockBalance, StockPool
from app.models.platform import AllocationChange, ChannelAllocation, PoolAllocation, SkuListing
from app.models.platform_ops import ManualCheck, PlatformConnection
from app.models.service_auth import Credential, SecurityEvent
from app.services import allocations, idempotency
from app.services.allocations import ALLOCATIONS_UPDATE
from app.services.command_context import CommandContext
from tests.integration.conftest import (
    TEST_DATABASE_URL,
    add_credential,
    add_pool,
    add_sku,
    add_tenant,
    balance_insert,
    channel_insert,
    new_id,
)
from tests.integration.test_api_auth import error_of, events

ACTOR = Actor("admin-1", STOCK_ADMIN)
URL = "/test/allocations"
REPRESENTATION_KEYS = {
    "pool_id",
    "sku_id",
    "version",
    "safety_stock",
    "available",
    "unallocated",
    "channels",
}

# 测试登记的命令上下文，按幂等键取；每个测试结束时清空。
CONTEXTS: dict[str, CommandContext] = {}


# --- 测试专用路由 ---


class AnyObject(RequestBody):
    """只做 JSON 对象解析，键不预先限定：形状由命令体按池的 platform 入口判。"""

    model_config = ConfigDict(extra="allow", strict=True)


def _mount(app) -> None:
    parse = json_body(AnyObject)

    def endpoint(request: Request, body: Annotated[ParsedBody, Depends(parse)]) -> Response:
        raw = body.raw
        allocations.check_allocation_ids(raw)
        registered = CONTEXTS[request.headers["Idempotency-Key"]]
        context = replace(
            registered,
            request_id=request.state.request_id,
            http_method=request.method,
            path=request.scope["path"],
        )
        prepare = allocations.prepare_allocation_rows(
            context.tenant_id, raw["pool_id"], raw["sku_id"]
        )
        return idempotency.run_with_context(
            request,
            context,
            operation=ALLOCATIONS_UPDATE,
            body=body,
            command=lambda execution: allocations.update_allocations(execution, raw),
            read_committed=True,
            before_transaction=prepare,
        )

    app.add_api_route(URL, endpoint, methods=["POST"])


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    app = create_app(Settings(env="demo", git_sha="c" * 40, database_url=TEST_DATABASE_URL))
    _mount(app)
    try:
        yield TestClient(app)
    finally:
        app.state.engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


@pytest.fixture(autouse=True)
def clear_contexts() -> Iterator[None]:
    yield
    CONTEXTS.clear()


# --- 造数据与清理 ---


@dataclass(frozen=True)
class World:
    tenant_id: str
    pool_id: str
    shp: str  # active 的 platform 入口，连接为 auto
    tts: str  # active 的 platform 入口，连接为 manual
    off: str  # inactive 的 platform 入口
    backoffice_channel_id: str
    api_pool_id: str  # 只挂一个 api 入口的池
    credential_id: str  # admin 主体
    other_tenant_id: str
    other_pool_id: str
    other_channel_id: str  # 别的租户的 platform 入口

    @property
    def platform_channels(self) -> list[str]:
        return sorted([self.shp, self.tts, self.off])


def _add_channel(db, tenant_id: str, pool_id: str, **values: object) -> str:
    channel_id = new_id("chn")
    db.execute(channel_insert(tenant_id, pool_id, channel_id=channel_id, **values))
    return channel_id


def _add_platform(db, tenant_id: str, pool_id: str, platform: str, shop_id: str, **values) -> str:
    """一个 platform 入口连同它的连接行（mode 默认 auto，切换时刻取数据库时间）。"""
    mode = values.pop("mode", "auto")
    values |= {"kind": "platform", "platform": platform, "platform_shop_id": shop_id}
    channel_id = _add_channel(db, tenant_id, pool_id, **values)
    db.execute(
        insert(PlatformConnection).values(tenant_id=tenant_id, channel_id=channel_id, mode=mode)
    )
    return channel_id


def cleanup(engine: Engine, tenant_ids: list[str]) -> None:
    """按外键先后删掉这些租户的全部数据（含配额变更记录、连接行与 admin 主体）。"""
    with engine.begin() as db:
        for model in (
            SecurityEvent,
            IdempotencyRecord,
            AllocationChange,
            SkuListing,
            ManualCheck,
            PlatformConnection,
            ChannelAllocation,
            PoolAllocation,
            StockBalance,
            Credential,
            SalesChannel,
            StockPool,
            Location,
        ):
            db.execute(delete(model).where(model.tenant_id.in_(tenant_ids)))
        tenant_skus = Sku.tenant_id.in_(tenant_ids)
        db.execute(update(Sku).where(tenant_skus).values(merged_into_sku_id=None))
        db.execute(delete(Sku).where(tenant_skus))
        db.execute(delete(Product).where(Product.tenant_id.in_(tenant_ids)))
        db.execute(delete(Tenant).where(Tenant.tenant_id.in_(tenant_ids)))


@pytest.fixture
def world(engine: Engine) -> Iterator[World]:
    tenant_ids: list[str] = []
    try:
        with engine.begin() as db:
            tenant_id = add_tenant(db)
            other_tenant_id = add_tenant(db)
            tenant_ids += [tenant_id, other_tenant_id]
            pool_id = add_pool(db, tenant_id)
            api_pool_id = add_pool(db, tenant_id)
            other_pool_id = add_pool(db, other_tenant_id)
            _add_channel(db, tenant_id, api_pool_id, kind="api")
            other_channel_id = _add_platform(db, other_tenant_id, other_pool_id, "shopee", "s-1")
            built = World(
                tenant_id=tenant_id,
                pool_id=pool_id,
                shp=_add_platform(db, tenant_id, pool_id, "shopee", "shop-1"),
                tts=_add_platform(db, tenant_id, pool_id, "tiktok_shop", "shop-2", mode="manual"),
                off=_add_platform(db, tenant_id, pool_id, "shopee", "shop-3", status="inactive"),
                backoffice_channel_id=_add_channel(db, tenant_id, pool_id, kind="backoffice"),
                api_pool_id=api_pool_id,
                credential_id=add_credential(db, tenant_id, name="Admin", scopes=[], kind="admin"),
                other_tenant_id=other_tenant_id,
                other_pool_id=other_pool_id,
                other_channel_id=other_channel_id,
            )
        yield built
    finally:
        if tenant_ids:
            cleanup(engine, tenant_ids)


def new_sku(engine: Engine, tenant_id: str) -> str:
    with engine.begin() as db:
        return add_sku(db, tenant_id)


def seed(
    engine: Engine,
    world: World,
    sku_id: str,
    *,
    stock_balance: int = 22,
    paid_pending_dispatch: int = 3,
    safety_stock: int = 2,
    quotas: dict[str, int] | None = None,
    pendings: dict[str, int] | None = None,
) -> None:
    """余额行、池配额行与三个 platform 入口的配额行，版本 0，提交。

    默认是契约 §3.1 例子一第 5 步之后：账面 22 / 0 / 0 / 3 / 22，S = 2，Q[shp] 8，Q[tts] 4。
    """
    quotas = {world.shp: 8, world.tts: 4} if quotas is None else quotas
    pendings = pendings or {}
    with engine.begin() as db:
        db.execute(
            balance_insert(
                world.tenant_id,
                world.pool_id,
                sku_id,
                stock_balance=stock_balance,
                paid_pending_dispatch=paid_pending_dispatch,
            )
        )
        db.execute(
            insert(PoolAllocation).values(
                tenant_id=world.tenant_id,
                pool_id=world.pool_id,
                sku_id=sku_id,
                safety_stock=safety_stock,
            )
        )
        for channel_id in world.platform_channels:
            db.execute(
                insert(ChannelAllocation).values(
                    tenant_id=world.tenant_id,
                    channel_id=channel_id,
                    pool_id=world.pool_id,
                    sku_id=sku_id,
                    quota=quotas.get(channel_id, 0),
                    pending=pendings.get(channel_id, 0),
                )
            )


def add_listing(engine: Engine, world: World, sku_id: str, channel_id: str, **values) -> str:
    listing_id = new_id("lst")
    with engine.begin() as db:
        db.execute(
            insert(SkuListing).values(
                listing_id=listing_id,
                tenant_id=world.tenant_id,
                channel_id=channel_id,
                platform_item_id=f"item-{uuid4().hex[:12]}",
                platform_model_id="-",
                sku_id=sku_id,
                status=values.get("status", "active"),
                version=1,
                credential_id=world.credential_id,
                actor_ref="admin-1",
                actor_role="catalog_admin",
                request_id=new_id("req"),
            )
        )
    return listing_id


def listed_sku(engine: Engine, world: World, **seeded) -> str:
    """一个按 seed 建好配额行、在 shp 与 tts 上都有生效平台商品的 SKU。"""
    sku_id = new_sku(engine, world.tenant_id)
    seed(engine, world, sku_id, **seeded)
    for channel_id in (world.shp, world.tts):
        add_listing(engine, world, sku_id, channel_id)
    return sku_id


def make_overdue(engine: Engine, channel_id: str) -> None:
    """人工模式的入口切换于 13 小时之前、没有核对过：逾期（§3.2，超过 12 小时）。"""
    with engine.begin() as db:
        now = db.execute(text("SELECT CURRENT_TIMESTAMP(6)")).scalar_one()
        db.execute(
            update(PlatformConnection)
            .where(PlatformConnection.channel_id == channel_id)
            .values(mode_changed_at=now - timedelta(hours=13))
        )


def set_row(engine: Engine, world: World, sku_id: str, channel_id: str, **values) -> None:
    with engine.begin() as db:
        db.execute(
            update(ChannelAllocation)
            .where(
                ChannelAllocation.tenant_id == world.tenant_id,
                ChannelAllocation.channel_id == channel_id,
                ChannelAllocation.sku_id == sku_id,
            )
            .values(**values)
        )


@dataclass(frozen=True)
class State:
    version: int | None
    safety_stock: int | None
    channels: dict[str, tuple[int, int]]  # channel_id → (quota, pending)


def state_of(engine: Engine, tenant_id: str, pool_id: str, sku_id: str) -> State:
    """池配额行与各入口配额行的当前值；没有池配额行时 version 与 safety_stock 为 None。"""
    with engine.connect() as db:
        pool = db.execute(
            select(PoolAllocation.version, PoolAllocation.safety_stock).where(
                PoolAllocation.tenant_id == tenant_id,
                PoolAllocation.pool_id == pool_id,
                PoolAllocation.sku_id == sku_id,
            )
        ).first()
        rows = db.execute(
            select(
                ChannelAllocation.channel_id,
                ChannelAllocation.quota,
                ChannelAllocation.pending,
            ).where(
                ChannelAllocation.tenant_id == tenant_id,
                ChannelAllocation.pool_id == pool_id,
                ChannelAllocation.sku_id == sku_id,
            )
        ).all()
    channels = {row.channel_id: (row.quota, row.pending) for row in rows}
    if pool is None:
        return State(None, None, channels)
    return State(pool.version, pool.safety_stock, channels)


def state(engine: Engine, world: World, sku_id: str) -> State:
    return state_of(engine, world.tenant_id, world.pool_id, sku_id)


def balance_exists(engine: Engine, tenant_id: str, pool_id: str, sku_id: str) -> bool:
    statement = select(StockBalance.balance_id).where(
        StockBalance.tenant_id == tenant_id,
        StockBalance.pool_id == pool_id,
        StockBalance.sku_id == sku_id,
    )
    with engine.connect() as db:
        return db.execute(statement).first() is not None


def changes_of(engine: Engine, tenant_ids: list[str]) -> list[dict]:
    """这些租户的配额变更记录，按 id 升序。"""
    statement = (
        select(*AllocationChange.__table__.columns)
        .where(AllocationChange.tenant_id.in_(tenant_ids))
        .order_by(AllocationChange.id)
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def all_changes(engine: Engine, world: World) -> list[dict]:
    return changes_of(engine, [world.tenant_id, world.other_tenant_id])


def records_of(engine: Engine, key: str) -> int:
    statement = select(IdempotencyRecord.idempotency_key).where(
        IdempotencyRecord.idempotency_key == key
    )
    with engine.connect() as db:
        return len(db.execute(statement).all())


# --- 请求 ---


def new_key() -> str:
    return f"backoffice:allocation:{uuid4().hex}"


def call(client: TestClient, world: World, body: dict, key: str | None = None):
    key = key or new_key()
    CONTEXTS[key] = CommandContext(
        tenant_id=world.tenant_id,
        channel_id=world.backoffice_channel_id,
        pool_id=world.pool_id,
        credential_id=world.credential_id,
        actor=ACTOR,
        request_id="replaced-by-the-route",
        idempotency_key=key,
        reservation_ttl_seconds=900,
    )
    headers = {"Idempotency-Key": key, "X-Request-ID": f"allocation-test-{uuid4().hex}"}
    return client.post(URL, json=body, headers=headers)


def body_of(world: World, sku_id: str, **values: object) -> dict:
    body = {"pool_id": world.pool_id, "sku_id": sku_id, "expected_version": 0, "reason": "plan"}
    return body | values


def quota(channel_id: str, expected: int, new: int) -> dict:
    return {"channel_id": channel_id, "expected_quota": expected, "quota": new}


def rid_of(response) -> str:
    return response.headers["X-Request-ID"]


def data_of(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["request_id"] == rid_of(response)
    return body["data"]


def expected_channels(world: World, values: dict[str, tuple[int, int]]) -> list[dict]:
    """三个 platform 入口按 channel_id 升序；values 里没有的为 (0, 0)。"""
    channels = []
    for channel_id in world.platform_channels:
        quota_value, pending = values.get(channel_id, (0, 0))
        channels.append({"channel_id": channel_id, "quota": quota_value, "pending": pending})
    return channels


# --- 契约 §3.1 例子一 ---


def test_example_one_step_six_raises_the_shopee_quota_to_fourteen(client, engine, world):
    sku = listed_sku(engine, world)
    body = body_of(world, sku, quotas=[quota(world.shp, 8, 14)], reason="  restock  ")
    response = call(client, world, body)

    data = data_of(response)
    assert set(data) == REPRESENTATION_KEYS
    assert (data["pool_id"], data["sku_id"]) == (world.pool_id, sku)
    assert (data["version"], data["safety_stock"], data["available"]) == (1, 2, 22)
    # 22 − 2 − (14 + 4) − 0 = 2。
    assert data["unallocated"] == 2
    expected = {world.shp: (14, 0), world.tts: (4, 0)}
    assert data["channels"] == expected_channels(world, expected)

    after = state(engine, world, sku)
    assert (after.version, after.safety_stock) == (1, 2)
    assert after.channels == {world.shp: (14, 0), world.tts: (4, 0), world.off: (0, 0)}
    [change] = all_changes(engine, world)
    assert change["tenant_id"] == world.tenant_id
    assert (change["pool_id"], change["sku_id"]) == (world.pool_id, sku)
    assert change["changes"] == {"quotas": {world.shp: {"before": 8, "after": 14}}}
    assert change["reason"] == "restock"
    assert change["credential_id"] == world.credential_id
    assert (change["actor_ref"], change["actor_role"]) == (ACTOR.ref, STOCK_ADMIN)
    assert change["request_id"] == rid_of(response)
    assert events(engine, rid_of(response)) == []


def test_example_one_step_six_prime_seventeen_exceeds_the_unallocated(client, engine, world):
    sku = listed_sku(engine, world)
    before = state(engine, world, sku)

    response = call(client, world, body_of(world, sku, quotas=[quota(world.shp, 8, 17)]))
    error = error_of(response, 409)
    assert error["code"] == "ALLOCATION_EXCEEDS_UNALLOCATED"
    # 22 − 2 − 21 = −1。
    assert error["details"] == {
        "available": 22,
        "safety_stock": 2,
        "quota_total": 21,
        "pending_total": 0,
        "unallocated": -1,
    }
    assert state(engine, world, sku) == before
    assert all_changes(engine, world) == []


def test_safety_stock_and_several_quotas_change_together(client, engine, world):
    sku = listed_sku(engine, world)
    quotas = [quota(world.tts, 4, 2), quota(world.shp, 8, 9)]
    data = data_of(call(client, world, body_of(world, sku, safety_stock=5, quotas=quotas)))
    # 22 − 5 − (9 + 2) − 0 = 6。
    assert (data["version"], data["safety_stock"], data["unallocated"]) == (1, 5, 6)

    [change] = all_changes(engine, world)
    assert change["changes"] == {
        "safety_stock": {"before": 2, "after": 5},
        "quotas": {
            world.shp: {"before": 8, "after": 9},
            world.tts: {"before": 4, "after": 2},
        },
    }

    # 下一次要带新版本；只列变了的键。
    body = body_of(world, sku, expected_version=1, safety_stock=5, quotas=[quota(world.tts, 2, 3)])
    data = data_of(call(client, world, body))
    assert (data["version"], data["unallocated"]) == (2, 5)
    *_, change = all_changes(engine, world)
    assert change["changes"] == {"quotas": {world.tts: {"before": 2, "after": 3}}}


def test_the_target_state_already_reached_changes_nothing(client, engine, world):
    sku = listed_sku(engine, world)
    body = body_of(world, sku, safety_stock=2, quotas=[quota(world.shp, 8, 8)])

    data = data_of(call(client, world, body))
    assert (data["version"], data["safety_stock"], data["unallocated"]) == (0, 2, 8)
    assert data["channels"] == expected_channels(world, {world.shp: (8, 0), world.tts: (4, 0)})
    assert state(engine, world, sku).version == 0
    assert all_changes(engine, world) == []


def test_missing_rows_are_pre_created_before_the_transaction(client, engine, world):
    sku = new_sku(engine, world.tenant_id)
    assert not balance_exists(engine, world.tenant_id, world.pool_id, sku)

    data = data_of(call(client, world, body_of(world, sku, safety_stock=0)))
    assert (data["version"], data["safety_stock"], data["available"]) == (1, 0, 0)
    assert data["unallocated"] == 0
    assert data["channels"] == expected_channels(world, {})
    assert balance_exists(engine, world.tenant_id, world.pool_id, sku)
    after = state(engine, world, sku)
    assert (after.version, after.safety_stock) == (1, 0)
    assert after.channels == {channel_id: (0, 0) for channel_id in world.platform_channels}
    [change] = all_changes(engine, world)
    assert change["changes"] == {"safety_stock": {"before": 2, "after": 0}}


# --- 版本与配额冲突 ---


def test_a_stale_version_is_a_version_conflict(client, engine, world):
    sku = listed_sku(engine, world)
    data_of(call(client, world, body_of(world, sku, safety_stock=3)))

    response = call(client, world, body_of(world, sku, safety_stock=4))
    error = error_of(response, 409)
    assert error["code"] == "VERSION_CONFLICT"
    assert error["details"] == {"current_version": 1}
    assert state(engine, world, sku).safety_stock == 3
    assert len(all_changes(engine, world)) == 1


def test_a_changed_quota_conflicts_only_for_that_channel(client, engine, world):
    sku = listed_sku(engine, world)
    # 平台事件在页面读取之后把 Q[shp] 从 8 减到 7（不加 version）。
    set_row(engine, world, sku, world.shp, quota=7)

    quotas = [quota(world.tts, 4, 5), quota(world.shp, 8, 10)]
    error = error_of(call(client, world, body_of(world, sku, quotas=quotas)), 409)
    assert error["code"] == "QUOTA_CHANGED"
    assert error["details"] == {"quotas": [{"channel_id": world.shp, "quota": 7}]}
    assert state(engine, world, sku).channels[world.tts] == (4, 0)
    assert all_changes(engine, world) == []

    # 只改别的入口不冲突。
    data = data_of(call(client, world, body_of(world, sku, quotas=[quota(world.tts, 4, 5)])))
    assert data["version"] == 1
    after = state(engine, world, sku)
    assert after.channels[world.shp] == (7, 0)
    assert after.channels[world.tts] == (5, 0)


# --- 增加配额才做的三项判定 ---


def test_increasing_a_channel_without_an_active_listing_is_rejected(client, engine, world):
    sku = new_sku(engine, world.tenant_id)
    seed(engine, world, sku)
    add_listing(engine, world, sku, world.shp)
    add_listing(engine, world, sku, world.tts, status="inactive")

    body = body_of(world, sku, quotas=[quota(world.shp, 8, 9), quota(world.tts, 4, 5)])
    error = error_of(call(client, world, body), 409)
    assert error["code"] == "NO_ACTIVE_LISTING"
    assert error["details"] == {"channel_id": world.tts}
    assert state(engine, world, sku).version == 0

    # 只减不判。
    data = data_of(call(client, world, body_of(world, sku, quotas=[quota(world.tts, 4, 1)])))
    assert data["version"] == 1
    assert state(engine, world, sku).channels[world.tts] == (1, 0)


def test_an_overdue_manual_channel_blocks_increasing_another_channel(client, engine, world):
    sku = listed_sku(engine, world)
    make_overdue(engine, world.tts)

    body = body_of(world, sku, quotas=[quota(world.shp, 8, 9)])
    error = error_of(call(client, world, body), 409)
    assert error["code"] == "MANUAL_CHECK_OVERDUE"
    assert error["details"] == {"channel_id": world.tts}
    assert state(engine, world, sku).version == 0

    # 一次核对补上之后解除。
    with engine.begin() as db:
        now = db.execute(text("SELECT CURRENT_TIMESTAMP(6)")).scalar_one()
        db.execute(
            insert(ManualCheck).values(
                manual_check_id=new_id("mck"),
                tenant_id=world.tenant_id,
                channel_id=world.tts,
                checked_from=now - timedelta(hours=1),
                checked_until=now - timedelta(minutes=1),
                found_discrepancy=False,
                credential_id=world.credential_id,
                actor_ref=ACTOR.ref,
                actor_role=STOCK_ADMIN,
                request_id=new_id("req"),
            )
        )
    data = data_of(call(client, world, body))
    assert data["version"] == 1


def test_decreasing_or_changing_only_s_skips_the_increase_checks(client, engine, world):
    # 没有平台商品、tts 逾期、而且超额：22 − 2 − (12 + 10) − 0 = −2。
    sku = new_sku(engine, world.tenant_id)
    seed(engine, world, sku, quotas={world.shp: 12, world.tts: 10})
    make_overdue(engine, world.tts)

    data = data_of(call(client, world, body_of(world, sku, quotas=[quota(world.shp, 12, 11)])))
    assert (data["version"], data["unallocated"]) == (1, -1)

    # 超额时只改 S（提高 S 让超额更多）也照常。
    data = data_of(call(client, world, body_of(world, sku, expected_version=1, safety_stock=4)))
    assert (data["version"], data["safety_stock"], data["unallocated"]) == (2, 4, -3)

    # 增加配额则被拒（这里先报第一项不满足的：没有平台商品）。
    body = body_of(world, sku, expected_version=2, quotas=[quota(world.shp, 11, 12)])
    error = error_of(call(client, world, body), 409)
    assert error["code"] == "NO_ACTIVE_LISTING"
    assert len(all_changes(engine, world)) == 2


def test_pending_and_inactive_channels_count_in_the_unallocated(client, engine, world):
    sku = listed_sku(
        engine,
        world,
        quotas={world.shp: 8, world.tts: 4, world.off: 3},
        pendings={world.tts: 2, world.off: 1},
    )
    # 22 − 2 − (8 + 4 + 3) − (2 + 1) = 2；把 shp 加 3 就超了。
    body = body_of(world, sku, quotas=[quota(world.shp, 8, 11)])
    error = error_of(call(client, world, body), 409)
    assert error["code"] == "ALLOCATION_EXCEEDS_UNALLOCATED"
    assert error["details"] == {
        "available": 22,
        "safety_stock": 2,
        "quota_total": 18,
        "pending_total": 3,
        "unallocated": -1,
    }

    # inactive 入口的配额能减，不能加。
    data = data_of(call(client, world, body_of(world, sku, quotas=[quota(world.off, 3, 0)])))
    assert data["unallocated"] == 5
    assert data["channels"] == expected_channels(
        world, {world.shp: (8, 0), world.tts: (4, 2), world.off: (0, 1)}
    )
    body = body_of(world, sku, expected_version=1, quotas=[quota(world.off, 0, 1)])
    error = error_of(call(client, world, body), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"] == {"channel_id": world.off, "status": "inactive"}
    assert state(engine, world, sku).channels[world.off] == (0, 1)
    assert len(all_changes(engine, world)) == 1


def test_a_merged_or_inactive_sku_can_still_be_changed(client, engine, world):
    sku = listed_sku(engine, world)
    canonical = new_sku(engine, world.tenant_id)
    with engine.begin() as db:
        db.execute(
            update(Sku)
            .where(Sku.sku_id == sku)
            .values(status="inactive", merged_into_sku_id=canonical)
        )

    data = data_of(call(client, world, body_of(world, sku, quotas=[quota(world.shp, 8, 0)])))
    assert data["version"] == 1
    assert state(engine, world, sku).channels[world.shp] == (0, 0)


# --- 404 与 422 ---


def assert_nothing_written(engine: Engine, world: World, tenant_id: str, pool_id: str, sku_id):
    assert all_changes(engine, world) == []
    assert state_of(engine, tenant_id, pool_id, sku_id) == State(None, None, {})


def test_a_missing_or_foreign_pool_is_not_found_alike(client, engine, world):
    sku = new_sku(engine, world.tenant_id)
    missing = call(client, world, body_of(world, sku, pool_id=new_id("pool"), safety_stock=1))
    foreign = call(client, world, body_of(world, sku, pool_id=world.other_pool_id, safety_stock=1))

    errors = [error_of(missing, 404), error_of(foreign, 404)]
    assert errors[0] == errors[1]
    assert errors[0]["code"] == "POOL_NOT_FOUND"
    assert events(engine, rid_of(missing)) == []
    [event] = events(engine, rid_of(foreign))
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "pool", "id": world.other_pool_id}
    assert_nothing_written(engine, world, world.other_tenant_id, world.other_pool_id, sku)
    assert not balance_exists(engine, world.other_tenant_id, world.other_pool_id, sku)


def test_a_pool_without_platform_channels_is_invalid(client, engine, world):
    sku = new_sku(engine, world.tenant_id)
    response = call(client, world, body_of(world, sku, pool_id=world.api_pool_id, safety_stock=1))

    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"] == {"pool_id": world.api_pool_id}
    assert_nothing_written(engine, world, world.tenant_id, world.api_pool_id, sku)
    assert not balance_exists(engine, world.tenant_id, world.api_pool_id, sku)


def test_a_missing_or_foreign_sku_is_not_found(client, engine, world):
    missing_id = new_id("sku")
    foreign_id = new_sku(engine, world.other_tenant_id)
    responses = [
        call(client, world, body_of(world, sku_id, safety_stock=1))
        for sku_id in (missing_id, foreign_id)
    ]

    for response, sku_id in zip(responses, (missing_id, foreign_id), strict=True):
        error = error_of(response, 404)
        assert error["code"] == "SKU_NOT_FOUND"
        assert error["details"] == {"sku_ids": [sku_id]}
        assert_nothing_written(engine, world, world.tenant_id, world.pool_id, sku_id)
        assert not balance_exists(engine, world.tenant_id, world.pool_id, sku_id)
    assert events(engine, rid_of(responses[0])) == []
    [event] = events(engine, rid_of(responses[1]))
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "sku", "id": foreign_id}


@pytest.mark.parametrize("channel", ["backoffice", "foreign", "missing"])
def test_a_quota_for_a_channel_that_is_not_a_platform_channel_of_the_pool(
    client, engine, world, channel
):
    channel_id = {
        "backoffice": world.backoffice_channel_id,
        "foreign": world.other_channel_id,
        "missing": new_id("chn"),
    }[channel]
    sku = listed_sku(engine, world)
    before = state(engine, world, sku)

    response = call(client, world, body_of(world, sku, quotas=[quota(channel_id, 0, 1)]))
    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"] == {"field": "quotas"}
    assert state(engine, world, sku) == before
    assert all_changes(engine, world) == []
    assert events(engine, rid_of(response)) == []


def test_neither_safety_stock_nor_quotas_is_invalid(client, engine, world):
    sku = listed_sku(engine, world)
    before = state(engine, world, sku)

    error = error_of(call(client, world, body_of(world, sku)), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"] == {"field": "quotas"}
    assert state(engine, world, sku) == before
    assert all_changes(engine, world) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pool_id", "not-an-id"),
        ("pool_id", None),
        ("pool_id", 7),
        ("sku_id", "sku_" + "0" * 30),
        ("sku_id", "SKU_" + "0" * 26),
    ],
)
def test_malformed_ids_are_rejected_before_the_executor(client, engine, world, field, value):
    sku = new_sku(engine, world.tenant_id)
    key = new_key()
    body = body_of(world, sku, safety_stock=1) | {field: value}

    error = error_of(call(client, world, body, key), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"] == {"field": field}
    # 在调执行器之前判出：不保存幂等记录。
    assert records_of(engine, key) == 0
    assert_nothing_written(engine, world, world.tenant_id, world.pool_id, sku)


# --- 幂等 ---


def test_the_same_key_replays_the_original_result(client, engine, world):
    sku = listed_sku(engine, world)
    body = body_of(world, sku, quotas=[quota(world.shp, 8, 14)])
    key = new_key()
    first = call(client, world, body, key)
    data_of(first)

    replay = call(client, world, body, key)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert state(engine, world, sku).version == 1
    assert len(all_changes(engine, world)) == 1


# --- 并发 ---


def test_a_quota_increase_waits_for_a_locked_allocation_row_and_sees_the_new_pending(
    client, engine, world, monkeypatch
):
    # 22 − 2 − (8 + 4) − 0 = 8：把 shp 加到 14 本来可以（F' = 2）。
    sku = listed_sku(engine, world)
    reached, go = threading.Event(), threading.Event()
    real_lock = allocations.lock_channel_allocations

    def gated(execution, sku_id, channel_ids):
        # 余额行与池配额行已锁住；走到锁配额行这一步时停住，等另一个连接先锁住 tts 的行。
        reached.set()
        assert go.wait(10)
        return real_lock(execution, sku_id, channel_ids)

    monkeypatch.setattr(allocations, "lock_channel_allocations", gated)
    where = (
        ChannelAllocation.tenant_id == world.tenant_id,
        ChannelAllocation.channel_id == world.tts,
        ChannelAllocation.sku_id == sku,
    )
    body = body_of(world, sku, quotas=[quota(world.shp, 8, 14)])
    with ThreadPoolExecutor(max_workers=1) as pool, engine.connect() as holder:
        try:
            future = pool.submit(call, client, world, body)
            assert reached.wait(10)
            locked = select(ChannelAllocation.pending).where(*where).with_for_update()
            assert holder.execute(locked).scalar_one() == 0
            # 平台新订单：tts 卖出 3 件进 pending。
            holder.execute(update(ChannelAllocation).where(*where).values(pending=3))
            go.set()
            # 改配额在 tts 的配额行上等着这个连接提交。
            time.sleep(1)
            assert not future.done()
            holder.commit()
            response = future.result(timeout=30)
        finally:
            go.set()
            holder.rollback()

    error = error_of(response, 409)
    assert error["code"] == "ALLOCATION_EXCEEDS_UNALLOCATED"
    # 按提交之后的 pending 判：22 − 2 − (14 + 4) − 3 = −1。
    assert error["details"] == {
        "available": 22,
        "safety_stock": 2,
        "quota_total": 18,
        "pending_total": 3,
        "unallocated": -1,
    }
    after = state(engine, world, sku)
    assert after.version == 0
    assert after.channels[world.shp] == (8, 0)
    assert after.channels[world.tts] == (4, 3)
    assert all_changes(engine, world) == []
