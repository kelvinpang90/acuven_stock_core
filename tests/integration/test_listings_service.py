"""平台商品的挂上、改挂与解除（契约 §2.9、§2.5 的三种 listing 事件、§3.1 的锁序、§8.3，0.22）。

在真 MySQL 上跑。命令只给后台用，后台的执行器与接口还没有，
所以测试给应用挂三个测试专用的写路由，经执行器核心 run_with_context 调命令体：
catalog_lock、read_committed 为真，balance_rows 与 before_transaction
用 app/services/listings.py 的两个钩子。命令上下文按幂等键取测试登记的：
主体是直接插入的 admin 凭据，操作者是 catalog_admin 角色组；
request_id、方法与路径取本次请求。

每个测试建自己的两个租户（本租户与“别的租户”）、池与入口：
一个池挂 active 与 inactive 两个平台入口和一个 backoffice 入口，另一个池挂一个 api 入口。
数据都在 db 夹具之外提交，测试结束时在 finally 里按外键先后删掉：
object_type 为 listing 的 catalog_changes 挡 0015 的降级，admin 主体挡 0012 的降级，
一个池挂多个入口挡 0013 的降级；删掉之后会话结束时 migrated_engine 才能照常降到 base。
"""

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, insert, select, update

from app.api.requests import ParsedBody, RequestBody, json_body
from app.config import Settings
from app.domain.requests import CATALOG_ADMIN, Actor
from app.main import create_app
from app.models.catalog import CatalogChange, Product, Sku, Tenant
from app.models.inventory import IdempotencyRecord, Location, SalesChannel, StockBalance, StockPool
from app.models.merges import SkuMerge
from app.models.platform import ChannelAllocation, PoolAllocation, SkuListing
from app.models.service_auth import Credential, SecurityEvent
from app.services import idempotency, listings
from app.services.command_context import CommandContext
from app.services.idempotency import CommandResult, Execution
from app.services.listings import (
    LISTINGS_LINK,
    LISTINGS_RELINK,
    LISTINGS_UNLINK,
    ListingLink,
    ListingRelink,
    ListingUnlink,
)
from tests.integration.conftest import (
    TEST_DATABASE_URL,
    add_credential,
    add_pool,
    add_tenant,
    channel_insert,
    new_id,
)
from tests.integration.test_api_auth import error_of, events

ACTOR = Actor("admin-1", CATALOG_ADMIN)
ROUTES: dict[str, tuple[str, type[RequestBody], Callable[[Execution, object], CommandResult]]] = {
    LISTINGS_LINK: ("/test/listings/link", ListingLink, listings.link_listing),
    LISTINGS_RELINK: ("/test/listings/relink", ListingRelink, listings.relink_listing),
    LISTINGS_UNLINK: ("/test/listings/unlink", ListingUnlink, listings.unlink_listing),
}
LISTING_KEYS = {
    "listing_id",
    "channel_id",
    "platform_item_id",
    "platform_model_id",
    "sku_id",
    "status",
    "version",
    "created_at",
    "updated_at",
}

# 测试登记的命令上下文，按幂等键取；每个测试结束时清空。
CONTEXTS: dict[str, CommandContext] = {}


# --- 测试专用路由 ---


def _mount(app, operation: str) -> None:
    url, model, command = ROUTES[operation]
    parse = json_body(model)

    def endpoint(request: Request, body: Annotated[ParsedBody, Depends(parse)]) -> Response:
        registered = CONTEXTS[request.headers["Idempotency-Key"]]
        context = replace(
            registered,
            request_id=request.state.request_id,
            http_method=request.method,
            path=request.scope["path"],
        )
        tenant_id = context.tenant_id
        return idempotency.run_with_context(
            request,
            context,
            operation=operation,
            body=body,
            command=lambda execution: command(execution, body.model),
            catalog_lock=True,
            read_committed=True,
            balance_rows=listings.listing_balance_rows(tenant_id, operation, body.model),
            before_transaction=listings.prepare_listing_rows(tenant_id, operation, body.model),
        )

    app.add_api_route(url, endpoint, methods=["POST"])


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    app = create_app(Settings(env="demo", git_sha="c" * 40, database_url=TEST_DATABASE_URL))
    for operation in ROUTES:
        _mount(app, operation)
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
    channel_id: str  # active 的平台入口
    inactive_channel_id: str  # inactive 的平台入口
    backoffice_channel_id: str
    api_channel_id: str  # 另一个池上的 api 入口
    credential_id: str  # admin 主体
    other_tenant_id: str
    other_channel_id: str  # 别的租户的平台入口
    other_credential_id: str


def _add_channel(db, tenant_id: str, pool_id: str, **values: object) -> str:
    channel_id = new_id("chn")
    db.execute(channel_insert(tenant_id, pool_id, channel_id=channel_id, **values))
    return channel_id


def _platform(platform: str, shop_id: str) -> dict[str, object]:
    return {"kind": "platform", "platform": platform, "platform_shop_id": shop_id}


def _admin(db, tenant_id: str) -> str:
    return add_credential(db, tenant_id, name="Admin", scopes=[], kind="admin")


def cleanup(engine: Engine, tenant_ids: list[str]) -> None:
    """按外键先后删掉这些租户的全部数据（含 listing 的目录变更记录与 admin 主体）。"""
    with engine.begin() as db:
        for model in (
            SecurityEvent,
            IdempotencyRecord,
            CatalogChange,
            SkuListing,
            ChannelAllocation,
            PoolAllocation,
            StockBalance,
            SkuMerge,
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
            built = World(
                tenant_id=tenant_id,
                pool_id=pool_id,
                channel_id=_add_channel(db, tenant_id, pool_id, **_platform("shopee", "shop-1")),
                inactive_channel_id=_add_channel(
                    db, tenant_id, pool_id, status="inactive", **_platform("tiktok_shop", "shop-2")
                ),
                backoffice_channel_id=_add_channel(db, tenant_id, pool_id, kind="backoffice"),
                api_channel_id=_add_channel(db, tenant_id, api_pool_id, kind="api"),
                credential_id=_admin(db, tenant_id),
                other_tenant_id=other_tenant_id,
                other_channel_id=_add_channel(
                    db, other_tenant_id, other_pool_id, **_platform("shopee", "shop-1")
                ),
                other_credential_id=_admin(db, other_tenant_id),
            )
        yield built
    finally:
        if tenant_ids:
            cleanup(engine, tenant_ids)


def new_sku(
    engine: Engine,
    tenant_id: str,
    *,
    status: str = "active",
    product_status: str = "active",
    merged_into: str | None = None,
) -> str:
    """一个新商品连同它唯一的 SKU；返回 sku_id。"""
    product_id, sku_id = new_id("prd"), new_id("sku")
    with engine.begin() as db:
        db.execute(
            insert(Product).values(
                product_id=product_id,
                tenant_id=tenant_id,
                name="Tee",
                status=product_status,
                version=1,
            )
        )
        db.execute(
            insert(Sku).values(
                sku_id=sku_id,
                tenant_id=tenant_id,
                product_id=product_id,
                sku_code=new_id("code"),
                name="Tee",
                status=status,
                version=1,
                merged_into_sku_id=merged_into,
            )
        )
    return sku_id


def product_of(engine: Engine, sku_id: str) -> str:
    with engine.connect() as db:
        return db.execute(select(Sku.product_id).where(Sku.sku_id == sku_id)).scalar_one()


def add_merge(engine: Engine, world: World, source_sku_id: str, canonical_sku_id: str) -> str:
    """一张 selling_paused 的合并单（未结束）。"""
    merge_id = new_id("smg")
    with engine.begin() as db:
        db.execute(
            insert(SkuMerge).values(
                merge_id=merge_id,
                tenant_id=world.tenant_id,
                source_sku_id=source_sku_id,
                canonical_sku_id=canonical_sku_id,
                status="selling_paused",
                credential_id=world.credential_id,
                actor_ref="stock-admin-1",
                actor_role="stock_admin",
                request_id=new_id("req"),
            )
        )
    return merge_id


def add_other_listing(engine: Engine, world: World) -> str:
    """别的租户的一条 active 平台商品。"""
    sku_id = new_sku(engine, world.other_tenant_id)
    listing_id = new_id("lst")
    with engine.begin() as db:
        db.execute(
            insert(SkuListing).values(
                listing_id=listing_id,
                tenant_id=world.other_tenant_id,
                channel_id=world.other_channel_id,
                platform_item_id="other-item",
                platform_model_id="-",
                sku_id=sku_id,
                status="active",
                version=1,
                credential_id=world.other_credential_id,
                actor_ref="admin-2",
                actor_role=CATALOG_ADMIN,
                request_id=new_id("req"),
            )
        )
    return listing_id


def _quota_where(world: World, sku_id: str, channel_id: str | None = None) -> tuple:
    return (
        ChannelAllocation.tenant_id == world.tenant_id,
        ChannelAllocation.channel_id == (channel_id or world.channel_id),
        ChannelAllocation.sku_id == sku_id,
    )


def set_quota(engine: Engine, world: World, sku_id: str, quota: int) -> None:
    statement = update(ChannelAllocation).where(*_quota_where(world, sku_id))
    with engine.begin() as db:
        db.execute(statement.values(quota=quota))


def quota_of(engine: Engine, world: World, sku_id: str, channel_id: str | None = None):
    """这个入口上这个 SKU 的配额；没有配额行为 None。"""
    statement = select(ChannelAllocation.quota).where(*_quota_where(world, sku_id, channel_id))
    with engine.connect() as db:
        return db.execute(statement).scalar_one_or_none()


def pool_allocation_exists(engine: Engine, world: World, sku_id: str) -> bool:
    statement = select(PoolAllocation.sku_id).where(
        PoolAllocation.tenant_id == world.tenant_id,
        PoolAllocation.pool_id == world.pool_id,
        PoolAllocation.sku_id == sku_id,
    )
    with engine.connect() as db:
        return db.execute(statement).first() is not None


def channel_allocations(engine: Engine, channel_id: str) -> list[dict]:
    statement = select(*ChannelAllocation.__table__.columns).where(
        ChannelAllocation.channel_id == channel_id
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def listing_row(engine: Engine, listing_id: str) -> dict:
    statement = select(*SkuListing.__table__.columns).where(SkuListing.listing_id == listing_id)
    with engine.connect() as db:
        return dict(db.execute(statement).mappings().one())


def listings_of(engine: Engine, world: World) -> list[dict]:
    tenant_listings = SkuListing.tenant_id == world.tenant_id
    statement = select(*SkuListing.__table__.columns).where(tenant_listings)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def changes_of(engine: Engine, world: World) -> list[dict]:
    """本租户的目录变更记录，按 id 升序。"""
    statement = (
        select(*CatalogChange.__table__.columns)
        .where(CatalogChange.tenant_id == world.tenant_id)
        .order_by(CatalogChange.id)
    )
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


# --- 请求 ---


def new_key() -> str:
    return f"backoffice:listing:{uuid4().hex}"


def call(client: TestClient, world: World, operation: str, body: dict, key: str | None = None):
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
    headers = {"Idempotency-Key": key, "X-Request-ID": f"listing-test-{uuid4().hex}"}
    return client.post(ROUTES[operation][0], json=body, headers=headers)


def link_body(world: World, sku_id: str, **values: object) -> dict:
    body = {
        "channel_id": world.channel_id,
        "platform_item_id": f"item-{uuid4().hex[:12]}",
        "platform_model_id": "-",
        "sku_id": sku_id,
    }
    return body | values


def link(client: TestClient, world: World, sku_id: str, **values: object):
    return call(client, world, LISTINGS_LINK, link_body(world, sku_id, **values))


def relink(client: TestClient, world: World, listing: dict, sku_id: str, **values: object):
    body = {"listing_id": listing["listing_id"], "expected_version": listing["version"]}
    return call(client, world, LISTINGS_RELINK, body | {"sku_id": sku_id} | values)


def unlink(client: TestClient, world: World, listing: dict, **values: object):
    body = {"listing_id": listing["listing_id"], "expected_version": listing["version"]}
    return call(client, world, LISTINGS_UNLINK, body | values)


def rid_of(response) -> str:
    return response.headers["X-Request-ID"]


def data_of(response, status: int) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["request_id"] == rid_of(response)
    return body["data"]


def linked(client: TestClient, world: World, sku_id: str, **values: object) -> dict:
    return data_of(link(client, world, sku_id, **values), 201)


def assert_change(
    change: dict,
    world: World,
    response,
    *,
    event_type: str,
    listing: dict,
    product_id: str,
    changes: dict,
    reason: str | None = None,
) -> None:
    assert change["event_type"] == event_type
    assert change["object_type"] == "listing"
    assert change["object_id"] == listing["listing_id"]
    assert change["product_id"] == product_id
    assert change["version"] == listing["version"]
    assert change["changes"] == changes
    assert change["credential_id"] == world.credential_id
    assert change["actor_ref"] == ACTOR.ref
    assert change["actor_role"] == CATALOG_ADMIN
    assert change["reason"] == reason
    assert change["import_id"] is None
    assert change["request_id"] == rid_of(response)
    assert change["idempotency_key"] is not None


# --- 挂上 ---


def test_link_creates_a_listing_at_version_one(client, engine, world):
    sku = new_sku(engine, world.tenant_id)
    body = link_body(world, sku, platform_model_id="Model.1:a-b_c", reason="  first link  ")
    response = call(client, world, LISTINGS_LINK, body)

    listing = data_of(response, 201)
    assert set(listing) == LISTING_KEYS
    assert listing["listing_id"].startswith("lst_")
    assert listing["channel_id"] == world.channel_id
    assert listing["platform_item_id"] == body["platform_item_id"]
    assert listing["platform_model_id"] == "Model.1:a-b_c"
    assert listing["sku_id"] == sku
    assert listing["status"] == "active"
    assert listing["version"] == 1
    assert listing["created_at"].endswith("Z") and listing["updated_at"].endswith("Z")

    row = listing_row(engine, listing["listing_id"])
    assert row["tenant_id"] == world.tenant_id
    assert row["credential_id"] == world.credential_id
    assert (row["actor_ref"], row["actor_role"]) == (ACTOR.ref, CATALOG_ADMIN)
    assert row["request_id"] == rid_of(response)

    [change] = changes_of(engine, world)
    assert_change(
        change,
        world,
        response,
        event_type="listing_linked",
        listing=listing,
        product_id=product_of(engine, sku),
        changes={
            "sku_id": {"before": None, "after": sku},
            "status": {"before": None, "after": "active"},
        },
        reason="first link",
    )

    # 开事务之前预建了这个入口上目标 SKU 的两级配额行，取默认值。
    assert pool_allocation_exists(engine, world, sku)
    assert quota_of(engine, world, sku) == 0
    assert events(engine, rid_of(response)) == []


def test_link_relinks_an_inactive_listing_to_the_given_sku_and_reactivates_it(
    client, engine, world
):
    first, second = new_sku(engine, world.tenant_id), new_sku(engine, world.tenant_id)
    body = link_body(world, first)
    created = data_of(call(client, world, LISTINGS_LINK, body), 201)
    unlinked = data_of(unlink(client, world, created), 200)
    assert (unlinked["status"], unlinked["version"]) == ("inactive", 2)

    response = call(client, world, LISTINGS_LINK, body | {"sku_id": second})
    listing = data_of(response, 200)
    assert listing["listing_id"] == created["listing_id"]
    assert (listing["sku_id"], listing["status"], listing["version"]) == (second, "active", 3)
    assert listing["created_at"] == created["created_at"]

    *_, change = changes_of(engine, world)
    assert_change(
        change,
        world,
        response,
        event_type="listing_linked",
        listing=listing,
        product_id=product_of(engine, second),
        changes={
            "sku_id": {"before": first, "after": second},
            "status": {"before": "inactive", "after": "active"},
        },
    )


def test_relinking_an_inactive_listing_to_its_own_sku_records_only_the_status(
    client, engine, world
):
    sku = new_sku(engine, world.tenant_id)
    body = link_body(world, sku)
    created = data_of(call(client, world, LISTINGS_LINK, body), 201)
    data_of(unlink(client, world, created), 200)

    listing = data_of(call(client, world, LISTINGS_LINK, body), 200)
    assert (listing["sku_id"], listing["status"], listing["version"]) == (sku, "active", 3)
    *_, change = changes_of(engine, world)
    assert change["changes"] == {"status": {"before": "inactive", "after": "active"}}


def test_link_an_active_listing_is_already_linked(client, engine, world):
    first, second = new_sku(engine, world.tenant_id), new_sku(engine, world.tenant_id)
    body = link_body(world, first)
    created = data_of(call(client, world, LISTINGS_LINK, body), 201)

    error = error_of(call(client, world, LISTINGS_LINK, body | {"sku_id": second}), 409)
    assert error["code"] == "LISTING_ALREADY_LINKED"
    assert error["details"] == {"listing_id": created["listing_id"], "sku_id": first}
    assert listing_row(engine, created["listing_id"])["version"] == 1
    assert len(changes_of(engine, world)) == 1


def test_a_sku_with_another_active_listing_on_the_channel_is_taken(client, engine, world):
    first, second = new_sku(engine, world.tenant_id), new_sku(engine, world.tenant_id)
    taken = linked(client, world, first)

    error = error_of(link(client, world, first), 409)
    assert error["code"] == "LISTING_SKU_TAKEN"
    expected = {"channel_id": world.channel_id, "sku_id": first, "listing_id": taken["listing_id"]}
    assert error["details"] == expected

    other = linked(client, world, second)
    error = error_of(relink(client, world, other, first), 409)
    assert error["code"] == "LISTING_SKU_TAKEN"
    assert error["details"] == expected
    assert listing_row(engine, other["listing_id"])["sku_id"] == second
    assert len(listings_of(engine, world)) == 2
    assert len(changes_of(engine, world)) == 2


@pytest.mark.parametrize("channel", ["api", "backoffice", "inactive"])
def test_link_needs_an_active_platform_channel(client, engine, world, channel):
    channel_id = {
        "api": world.api_channel_id,
        "backoffice": world.backoffice_channel_id,
        "inactive": world.inactive_channel_id,
    }[channel]
    sku = new_sku(engine, world.tenant_id)

    response = link(client, world, sku, channel_id=channel_id)
    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"]["channel_id"] == channel_id
    assert listings_of(engine, world) == []
    assert changes_of(engine, world) == []
    assert events(engine, rid_of(response)) == []
    if channel != "inactive":
        # 不给 api、backoffice 入口建配额行。
        assert channel_allocations(engine, channel_id) == []
        assert not pool_allocation_exists(engine, world, sku)


def test_a_missing_or_foreign_channel_is_not_found_alike(client, engine, world):
    sku = new_sku(engine, world.tenant_id)
    missing = link(client, world, sku, channel_id=new_id("chn"))
    foreign = link(client, world, sku, channel_id=world.other_channel_id)

    errors = [error_of(missing, 404), error_of(foreign, 404)]
    assert errors[0] == errors[1]
    assert errors[0]["code"] == "NOT_FOUND"
    assert events(engine, rid_of(missing)) == []
    [event] = events(engine, rid_of(foreign))
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "channel", "id": world.other_channel_id}
    assert channel_allocations(engine, world.other_channel_id) == []
    assert listings_of(engine, world) == []


# --- 目标 SKU（挂上与改挂） ---


TARGET_CASES = ["merged", "inactive", "product_inactive", "missing", "foreign", "merging"]


def bad_target(engine: Engine, world: World, case: str) -> tuple[str, str, dict]:
    """返回目标 SKU、期望的错误码与 details。"""
    tenant_id = world.tenant_id
    if case == "merged":
        canonical = new_sku(engine, tenant_id)
        sku = new_sku(engine, tenant_id, status="inactive", merged_into=canonical)
        return sku, "SKU_MERGED", {"sku_id": sku, "merged_into_sku_id": canonical}
    if case == "inactive":
        sku = new_sku(engine, tenant_id, status="inactive")
        return sku, "SKU_INACTIVE", {"sku_ids": [sku]}
    if case == "product_inactive":
        sku = new_sku(engine, tenant_id, product_status="inactive")
        return sku, "SKU_INACTIVE", {"sku_ids": [sku]}
    if case == "missing":
        sku = new_id("sku")
        return sku, "SKU_NOT_FOUND", {"sku_ids": [sku]}
    if case == "foreign":
        sku = new_sku(engine, world.other_tenant_id)
        return sku, "SKU_NOT_FOUND", {"sku_ids": [sku]}
    assert case == "merging"
    sku, canonical = new_sku(engine, tenant_id), new_sku(engine, tenant_id)
    merge_id = add_merge(engine, world, sku, canonical)
    details = {"merge_id": merge_id, "source_sku_id": sku, "canonical_sku_id": canonical}
    return sku, "SKU_MERGE_IN_PROGRESS", details


def assert_target_rejected(engine, world, response, sku, case, code, details) -> None:
    error = error_of(response, 404 if code == "SKU_NOT_FOUND" else 409)
    assert error["code"] == code
    assert error["details"] == details
    found = events(engine, rid_of(response))
    if case == "foreign":
        [event] = found
        assert event["event_type"] == "cross_tenant"
        assert event["details"] == {"object_type": "sku", "id": sku}
    else:
        assert found == []


@pytest.mark.parametrize("case", TARGET_CASES)
def test_link_to_an_unusable_sku_is_rejected(client, engine, world, case):
    sku, code, details = bad_target(engine, world, case)
    response = link(client, world, sku)
    assert_target_rejected(engine, world, response, sku, case, code, details)
    assert listings_of(engine, world) == []
    assert changes_of(engine, world) == []


@pytest.mark.parametrize("case", TARGET_CASES)
def test_relink_to_an_unusable_sku_is_rejected(client, engine, world, case):
    current = new_sku(engine, world.tenant_id)
    listing = linked(client, world, current)
    sku, code, details = bad_target(engine, world, case)

    response = relink(client, world, listing, sku)
    assert_target_rejected(engine, world, response, sku, case, code, details)
    row = listing_row(engine, listing["listing_id"])
    assert (row["sku_id"], row["version"]) == (current, 1)
    assert len(changes_of(engine, world)) == 1


# --- 改挂与解除 ---


def test_relink_to_the_current_sku_changes_nothing(client, engine, world):
    sku = new_sku(engine, world.tenant_id)
    listing = linked(client, world, sku)

    assert data_of(relink(client, world, listing, sku), 200) == listing
    assert listing_row(engine, listing["listing_id"])["version"] == 1
    assert len(changes_of(engine, world)) == 1


@pytest.mark.parametrize("operation", [LISTINGS_RELINK, LISTINGS_UNLINK])
def test_relink_and_unlink_check_the_version(client, engine, world, operation):
    first, second = new_sku(engine, world.tenant_id), new_sku(engine, world.tenant_id)
    listing = linked(client, world, first)
    stale = listing | {"version": 2}

    if operation == LISTINGS_RELINK:
        response = relink(client, world, stale, second)
    else:
        response = unlink(client, world, stale)
    error = error_of(response, 409)
    assert error["code"] == "VERSION_CONFLICT"
    assert error["details"] == {"current_version": 1}
    assert listing_row(engine, listing["listing_id"])["version"] == 1
    assert len(changes_of(engine, world)) == 1


@pytest.mark.parametrize("operation", [LISTINGS_RELINK, LISTINGS_UNLINK])
def test_relink_and_unlink_of_an_inactive_listing_are_state_conflicts(
    client, engine, world, operation
):
    first, second = new_sku(engine, world.tenant_id), new_sku(engine, world.tenant_id)
    inactive = data_of(unlink(client, world, linked(client, world, first)), 200)

    if operation == LISTINGS_RELINK:
        response = relink(client, world, inactive, second)
    else:
        response = unlink(client, world, inactive)
    error = error_of(response, 409)
    assert error["code"] == "STATE_CONFLICT"
    assert error["details"] == {"status": "inactive"}
    row = listing_row(engine, inactive["listing_id"])
    assert (row["sku_id"], row["status"], row["version"]) == (first, "inactive", 2)
    assert len(changes_of(engine, world)) == 2


@pytest.mark.parametrize("operation", [LISTINGS_RELINK, LISTINGS_UNLINK])
def test_a_missing_or_foreign_listing_is_not_found_alike(client, engine, world, operation):
    sku = new_sku(engine, world.tenant_id)
    foreign_id = add_other_listing(engine, world)
    responses = []
    for listing_id in (new_id("lst"), foreign_id):
        listing = {"listing_id": listing_id, "version": 1}
        if operation == LISTINGS_RELINK:
            responses.append(relink(client, world, listing, sku))
        else:
            responses.append(unlink(client, world, listing))

    missing, foreign = (error_of(response, 404) for response in responses)
    assert missing == foreign
    assert missing["code"] == "LISTING_NOT_FOUND"
    assert events(engine, rid_of(responses[0])) == []
    [event] = events(engine, rid_of(responses[1]))
    assert event["event_type"] == "cross_tenant"
    assert event["details"] == {"object_type": "listing", "id": foreign_id}
    assert listing_row(engine, foreign_id)["version"] == 1
    assert changes_of(engine, world) == []


@pytest.mark.parametrize("operation", [LISTINGS_RELINK, LISTINGS_UNLINK])
def test_quota_on_the_original_sku_blocks_relink_and_unlink(client, engine, world, operation):
    first, second = new_sku(engine, world.tenant_id), new_sku(engine, world.tenant_id)
    listing = linked(client, world, first)
    set_quota(engine, world, first, 3)

    def send():
        if operation == LISTINGS_RELINK:
            return relink(client, world, listing, second, reason="moved")
        return unlink(client, world, listing, reason="moved")

    error = error_of(send(), 409)
    assert error["code"] == "LISTING_HAS_QUOTA"
    assert error["details"] == {"channel_id": world.channel_id, "sku_id": first, "quota": 3}
    # 什么也不改。
    row = listing_row(engine, listing["listing_id"])
    assert (row["sku_id"], row["status"], row["version"]) == (first, "active", 1)
    assert len(changes_of(engine, world)) == 1
    assert quota_of(engine, world, first) == 3

    set_quota(engine, world, first, 0)
    response = send()
    changed = data_of(response, 200)
    assert changed["version"] == 2
    *_, change = changes_of(engine, world)
    if operation == LISTINGS_RELINK:
        assert (changed["sku_id"], changed["status"]) == (second, "active")
        assert_change(
            change,
            world,
            response,
            event_type="listing_relinked",
            listing=changed,
            product_id=product_of(engine, second),
            changes={"sku_id": {"before": first, "after": second}},
            reason="moved",
        )
        # 改挂也预建了目标 SKU 在这个入口上的配额行。
        assert quota_of(engine, world, second) == 0
        assert pool_allocation_exists(engine, world, second)
    else:
        assert (changed["sku_id"], changed["status"]) == (first, "inactive")
        assert_change(
            change,
            world,
            response,
            event_type="listing_unlinked",
            listing=changed,
            product_id=product_of(engine, first),
            changes={"status": {"before": "active", "after": "inactive"}},
            reason="moved",
        )
    row = listing_row(engine, listing["listing_id"])
    assert row["version"] == 2
    assert row["request_id"] == rid_of(response)


def test_unlink_works_on_an_inactive_platform_channel_but_relink_does_not(client, engine, world):
    first, second = new_sku(engine, world.tenant_id), new_sku(engine, world.tenant_id)
    listing = linked(client, world, first)
    with engine.begin() as db:
        db.execute(
            update(SalesChannel)
            .where(SalesChannel.channel_id == world.channel_id)
            .values(status="inactive")
        )

    error = error_of(relink(client, world, listing, second), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"]["channel_id"] == world.channel_id

    unlinked = data_of(unlink(client, world, listing), 200)
    assert (unlinked["status"], unlinked["version"]) == ("inactive", 2)
    *_, change = changes_of(engine, world)
    assert change["event_type"] == "listing_unlinked"


# --- 幂等 ---


def test_the_same_key_replays_and_different_content_conflicts(client, engine, world):
    first, second = new_sku(engine, world.tenant_id), new_sku(engine, world.tenant_id)
    body = link_body(world, first)
    key = new_key()
    created = call(client, world, LISTINGS_LINK, body, key)
    data_of(created, 201)

    replay = call(client, world, LISTINGS_LINK, body, key)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == created.json()
    assert len(listings_of(engine, world)) == 1
    assert len(changes_of(engine, world)) == 1

    conflict = call(client, world, LISTINGS_LINK, body | {"sku_id": second}, key)
    error = error_of(conflict, 409)
    assert error["code"] == "IDEMPOTENCY_CONFLICT"
    [event] = events(engine, rid_of(conflict))
    assert event["event_type"] == "idempotency_conflict"
    [row] = listings_of(engine, world)
    assert (row["sku_id"], row["version"]) == (first, 1)
    assert len(changes_of(engine, world)) == 1


# --- 并发 ---


def test_unlink_waits_for_a_concurrent_quota_change_and_then_sees_it(
    client, engine, world, monkeypatch
):
    sku = new_sku(engine, world.tenant_id)
    listing = linked(client, world, sku)
    reached, go = threading.Event(), threading.Event()
    real_lock = listings.lock_channel_quotas

    def gated(execution, channel_id, sku_ids):
        # 配额行已在开事务前预建；解除走到锁配额行这一步时停住，等另一个连接先锁住它。
        reached.set()
        assert go.wait(10)
        return real_lock(execution, channel_id, sku_ids)

    monkeypatch.setattr(listings, "lock_channel_quotas", gated)
    where = _quota_where(world, sku)
    with ThreadPoolExecutor(max_workers=1) as pool, engine.connect() as holder:
        try:
            future = pool.submit(unlink, client, world, listing)
            assert reached.wait(10)
            locked = select(ChannelAllocation.quota).where(*where).with_for_update()
            assert holder.execute(locked).scalar_one() == 0
            holder.execute(update(ChannelAllocation).where(*where).values(quota=3))
            go.set()
            # 解除在配额行上等着这个连接提交。
            time.sleep(1)
            assert not future.done()
            holder.commit()
            response = future.result(timeout=30)
        finally:
            go.set()
            holder.rollback()

    error = error_of(response, 409)
    assert error["code"] == "LISTING_HAS_QUOTA"
    assert error["details"] == {"channel_id": world.channel_id, "sku_id": sku, "quota": 3}
    row = listing_row(engine, listing["listing_id"])
    assert (row["status"], row["version"]) == ("active", 1)
    assert len(changes_of(engine, world)) == 1
    assert quota_of(engine, world, sku) == 3
