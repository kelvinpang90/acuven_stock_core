"""目录变更流的拉取（契约 §2.5、§2.7）。

在真 MySQL 上跑；每个测试建自己的租户与凭据。翻页与游标的测试直接在库里造变更记录，
字段的测试经新建、修改、停用接口产生记录。id 是全库共用的自增值，
别的租户的记录自然在本租户的记录之间留下空隙。
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, func, insert, select

from app.api.catalog import format_timestamp
from app.config import Settings
from app.domain.requests import CATALOG_ADMIN
from app.domain.service_auth import CATALOG_READ, STOCK_READ, STOCK_WRITE
from app.main import create_app
from app.models.catalog import CatalogChange
from tests.integration.conftest import TEST_DATABASE_URL
from tests.integration.test_api_auth import Caller, error_of, events, issue
from tests.integration.test_catalog_create_api import (
    PRICE,
    changes_of,
    create_product,
    created,
    new_key,
    product_body,
    sku_body,
)
from tests.integration.test_catalog_queries_api import add_product, data_of, request_id
from tests.integration.test_catalog_updates_api import OTHER_PRICE, command, patch_sku

PATH = "/v1/catalog-changes"
FIELDS = {
    "cursor",
    "event_type",
    "object_type",
    "object_id",
    "product_id",
    "version",
    "changes",
    "actor_ref",
    "reason",
    "import_id",
    "request_id",
    "occurred_at",
}


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    app = create_app(Settings(env="demo", git_sha="c" * 40, database_url=TEST_DATABASE_URL))
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.state.engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


# --- 造数据 ---


def add_change(db: Connection, caller: Caller, product_id: str, **overrides: object) -> int:
    """本租户的一条商品变更记录；返回它的 id。"""
    values = {
        "tenant_id": caller.tenant_id,
        "event_type": "product_updated",
        "object_type": "product",
        "object_id": product_id,
        "product_id": product_id,
        "version": 2,
        "changes": {"name": {"before": "Tee", "after": "Basic Tee"}},
        "credential_id": caller.credential_id,
        "actor_ref": "admin-7",
        "actor_role": CATALOG_ADMIN,
        "request_id": request_id(),
        "idempotency_key": new_key(),
    }
    result = db.execute(insert(CatalogChange).values(values | overrides))
    return result.inserted_primary_key[0]


def add_changes(engine: Engine, caller: Caller, count: int) -> list[int]:
    """本租户一个商品与它的 count 条变更记录（版本 2 起递增），各自提交；返回 id。"""
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
    ids = []
    for version in range(2, count + 2):
        with engine.begin() as db:
            ids.append(add_change(db, caller, product_id, version=version))
    return ids


def max_change_id(engine: Engine) -> int:
    with engine.connect() as db:
        return db.execute(select(func.max(CatalogChange.id))).scalar_one() or 0


# --- 请求 ---


def pull(client: TestClient, caller: Caller, **params: str) -> dict:
    data = data_of(client.get(PATH, params=params, headers=caller.auth))
    assert set(data) == {"changes", "next_cursor"}
    return data


def cursors(data: dict) -> list[str]:
    return [change["cursor"] for change in data["changes"]]


def as_cursors(ids: list[int]) -> list[str]:
    return [str(change_id) for change_id in ids]


# --- 从头拉取与租户隔离 ---


def test_without_after_the_tenants_records_come_in_id_order(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    # 两个租户的记录交错写入。
    ids = []
    foreign_ids = []
    for _ in range(3):
        ids += add_changes(engine, caller, 1)
        foreign_ids += add_changes(engine, foreign, 1)

    data = pull(client, caller)
    assert cursors(data) == as_cursors(ids)
    assert data["next_cursor"] == str(ids[-1])
    assert not set(cursors(data)) & set(as_cursors(foreign_ids))
    assert cursors(pull(client, foreign)) == as_cursors(foreign_ids)


def test_without_after_and_without_records_next_cursor_is_null(client, engine):
    caller = issue(engine)
    # 别的租户有记录也看不到。
    add_changes(engine, issue(engine), 2)
    assert pull(client, caller) == {"changes": [], "next_cursor": None}
    assert pull(client, caller, limit="1") == {"changes": [], "next_cursor": None}


def test_the_default_limit_is_100(client, engine):
    caller = issue(engine)
    ids = add_changes(engine, caller, 101)
    data = pull(client, caller)
    assert cursors(data) == as_cursors(ids[:100])
    assert data["next_cursor"] == str(ids[99])
    assert cursors(pull(client, caller, after=data["next_cursor"])) == [str(ids[100])]


# --- 翻页与游标 ---


def test_paging_with_next_cursor_misses_and_repeats_nothing(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    ids = []
    for _ in range(5):
        ids += add_changes(engine, caller, 1)
        add_changes(engine, foreign, 1)

    pages = []
    data = pull(client, caller, limit="2")
    while data["changes"]:
        pages.append(cursors(data))
        assert data["next_cursor"] == data["changes"][-1]["cursor"]
        data = pull(client, caller, after=data["next_cursor"], limit="2")
    assert pages == [as_cursors(ids[0:2]), as_cursors(ids[2:4]), as_cursors(ids[4:5])]
    # 没有新记录：原样返回 after。
    assert data == {"changes": [], "next_cursor": str(ids[-1])}

    # 之后写入的记录接着上次的游标拉到。
    later = add_changes(engine, caller, 2)
    data = pull(client, caller, after=str(ids[-1]), limit="2")
    assert cursors(data) == as_cursors(later)
    assert data["next_cursor"] == str(later[-1])


def test_an_old_cursor_returns_the_same_records_again(client, engine):
    caller = issue(engine)
    ids = add_changes(engine, caller, 4)
    first = pull(client, caller, limit="1")
    second = pull(client, caller, after=first["next_cursor"], limit="2")
    assert cursors(second) == as_cursors(ids[1:3])

    assert pull(client, caller, after=first["next_cursor"], limit="2") == second
    assert pull(client, caller, limit="1") == first


def test_a_cursor_in_a_gap_returns_the_records_after_it(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    [before] = add_changes(engine, caller, 1)
    [gap] = add_changes(engine, foreign, 1)
    [after] = add_changes(engine, caller, 1)
    assert before < gap < after

    # 别的租户的 id 对本租户是空隙：照常返回其后的记录。
    data = pull(client, caller, after=str(gap))
    assert cursors(data) == [str(after)]
    assert data["next_cursor"] == str(after)


def test_a_cursor_beyond_every_record_returns_what_comes_after_it(client, engine):
    caller = issue(engine)
    ids = add_changes(engine, caller, 2)
    beyond = max_change_id(engine) + 500

    # 比任何记录都大（如整机恢复后网店保存的游标）：不报错，原样返回 after。
    assert pull(client, caller, after=str(beyond)) == {"changes": [], "next_cursor": str(beyond)}

    # 之后有 id 更大的记录时照常返回（自增起点已抬高）。
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        later = add_change(db, caller, product_id, id=beyond + 500)
    data = pull(client, caller, after=str(beyond))
    assert cursors(data) == [str(later)]
    assert data["next_cursor"] == str(later)
    assert cursors(pull(client, caller)) == as_cursors([*ids, later])


def test_the_largest_cursor_is_accepted(client, engine):
    caller = issue(engine)
    add_changes(engine, caller, 1)
    largest = "9223372036854775807"
    assert pull(client, caller, after=largest) == {"changes": [], "next_cursor": largest}


# --- 字段 ---


def test_records_from_the_catalog_commands_have_the_contract_fields(client, engine):
    caller = issue(engine)
    create_rid = request_id()
    body = product_body(
        sku_body("TEE-S", name="Tee S", reference_price=PRICE),
        sku_body("TEE-M", barcode="BAR-M"),
        name="Basic Tee",
        reason="launch",
    )
    product = created(create_product(client, caller, body, rid=create_rid))
    product_id = product["product_id"]
    by_code = {sku["sku_code"]: sku["sku_id"] for sku in product["skus"]}
    small, medium = by_code["TEE-S"], by_code["TEE-M"]

    update_rid = request_id()
    patch = {"expected_version": 1, "name": "Tee Small", "reference_price": OTHER_PRICE}
    data_of(patch_sku(client, caller, small, patch, rid=update_rid))
    deactivate_rid = request_id()
    data_of(command(client, caller, f"/v1/skus/{small}/deactivate", rid=deactivate_rid))

    changes = pull(client, caller)["changes"]
    stored = changes_of(engine, caller.tenant_id)
    assert len(changes) == len(stored) == 5
    for change, row in zip(changes, stored, strict=True):
        assert set(change) == FIELDS
        assert change["cursor"] == str(row["id"])
        assert change["occurred_at"] == format_timestamp(row["created_at"])
        # 存的 JSON 原样输出。
        assert change["changes"] == row["changes"]
        assert change["actor_ref"] == "admin-7"
        assert change["import_id"] is None
        assert change["product_id"] == product_id

    # 新建商品：一条 product_created，后接各 SKU 的 sku_created（按请求顺序）。
    events_seen = [
        (change["event_type"], change["object_type"], change["object_id"], change["version"])
        for change in changes
    ]
    assert events_seen == [
        ("product_created", "product", product_id, 1),
        ("sku_created", "sku", small, 1),
        ("sku_created", "sku", medium, 1),
        ("sku_updated", "sku", small, 2),
        ("sku_deactivated", "sku", small, 3),
    ]
    for change in changes[:3]:
        assert (change["reason"], change["request_id"]) == ("launch", create_rid)
        assert all(field["before"] is None for field in change["changes"].values())
    assert changes[0]["changes"] == {
        "name": {"before": None, "after": "Basic Tee"},
        "status": {"before": None, "after": "active"},
    }

    # 同时改名称与参考价：只有一条 sku_updated，changes 含两个字段。
    updated = changes[3]
    assert updated["changes"] == {
        "name": {"before": "Tee S", "after": "Tee Small"},
        "reference_price": {"before": PRICE, "after": OTHER_PRICE},
    }
    assert (updated["reason"], updated["request_id"]) == (None, update_rid)

    # 停用记 status 的前后值。
    deactivated = changes[4]
    assert deactivated["changes"] == {"status": {"before": "active", "after": "inactive"}}
    assert (deactivated["reason"], deactivated["request_id"]) == (None, deactivate_rid)


def test_a_stored_reason_is_passed_through(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        add_change(db, caller, product_id, reason="seasonal rename")
    [change] = pull(client, caller)["changes"]
    assert change["reason"] == "seasonal rename"
    assert change["changes"] == {"name": {"before": "Tee", "after": "Basic Tee"}}


# --- 参数 ---


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("after=0", id="after_zero"),
        pytest.param("after=01", id="after_leading_zero"),
        pytest.param("after=abc", id="after_not_a_number"),
        pytest.param("after=", id="after_empty"),
        pytest.param("after=-1", id="after_negative"),
        pytest.param("after=%2B1", id="after_plus"),
        pytest.param("after=1.5", id="after_fraction"),
        pytest.param("after=9223372036854775808", id="after_over_max"),
        pytest.param("after=99999999999999999999", id="after_too_long"),
        pytest.param("limit=0", id="limit_zero"),
        pytest.param("limit=101", id="limit_101"),
        pytest.param("limit=%2B5", id="limit_plus"),
        pytest.param("limit=-5", id="limit_minus"),
        pytest.param("limit=05", id="limit_leading_zero"),
        pytest.param("limit=abc", id="limit_not_a_number"),
        pytest.param("limit=", id="limit_empty"),
        pytest.param("after=1&after=2", id="after_twice"),
        pytest.param("after=1&after=1", id="after_twice_same"),
        pytest.param("limit=5&limit=5", id="limit_twice"),
        pytest.param("foo=1", id="unknown"),
        pytest.param("after=1&cursor=1", id="unknown_with_after"),
        pytest.param("event_type=sku_updated", id="filter_not_supported"),
    ],
)
def test_invalid_parameters_are_422(client, engine, query):
    caller = issue(engine)
    add_changes(engine, caller, 1)
    response = client.get(f"{PATH}?{query}", headers=caller.auth)
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"


def test_without_catalog_read_it_is_403(client, engine):
    caller = issue(engine, scopes=[STOCK_READ, STOCK_WRITE])
    add_changes(engine, caller, 1)
    rid = request_id()
    response = client.get(PATH, headers=caller.auth | {"X-Request-ID": rid})
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["event_type"] == "forbidden"


def test_no_role_and_no_actor_headers_are_needed(client, engine):
    caller = issue(engine, scopes=[CATALOG_READ])
    ids = add_changes(engine, caller, 1)
    response = client.get(PATH, headers=caller.auth)
    assert cursors(data_of(response)) == as_cursors(ids)
