"""商品与 SKU 的查询接口（契约 §2.1、§2.4、§2.6 第 6、7 条、§7.2）。

在真 MySQL 上跑；测试直接在库里造商品与 SKU（用自己提交的连接，应用用另一个连接读），
每个测试建自己的租户与凭据，互不干扰；安全事件按每个请求自己的 X-Request-ID 查。
"""

import re
from collections.abc import Iterator
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, insert

from app.config import Settings
from app.domain.requests import CATALOG_ADMIN
from app.domain.service_auth import CATALOG_READ, CATALOG_WRITE, STOCK_READ
from app.main import create_app
from app.models.catalog import Product, SkuCodeClaim
from tests.integration.conftest import TEST_DATABASE_URL, new_id
from tests.integration.test_api_auth import error_of, events, issue
from tests.integration.test_catalog_schema import sku_insert

CREATED_AT = datetime(2026, 11, 14, 9, 30, 15, 123456)
UPDATED_AT = datetime(2026, 11, 14, 10, 0)
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")


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


def _given(values: dict) -> dict:
    """值为 None 的列不写：用数据库的默认值（时间）或 NULL。"""
    return {name: value for name, value in values.items() if value is not None}


def add_product(db: Connection, tenant_id: str, **overrides: object) -> str:
    """时间默认取固定值；传 created_at=None 等于不写，用数据库的默认值。"""
    product_id = new_id("prd")
    values = {
        "product_id": product_id,
        "tenant_id": tenant_id,
        "name": "Tee",
        "status": "active",
        "version": 1,
        "created_at": CREATED_AT,
        "updated_at": UPDATED_AT,
    }
    db.execute(insert(Product).values(_given(values | overrides)))
    return product_id


def add_sku(db: Connection, tenant_id: str, product_id: str, **overrides: object) -> str:
    """同 add_product；其余列的默认值见 sku_insert。"""
    values = {"sku_id": new_id("sku"), "created_at": CREATED_AT, "updated_at": UPDATED_AT}
    values = _given(values | overrides)
    db.execute(sku_insert(tenant_id, product_id, **values))
    return values["sku_id"]


def claim(db: Connection, tenant_id: str, code: str, sku_id: str) -> None:
    db.execute(insert(SkuCodeClaim).values(tenant_id=tenant_id, code=code, sku_id=sku_id))


def ordered_sku_ids(count: int) -> list[str]:
    """按 sku_id 升序的一组 ID：同一前缀加一位递增的数字。"""
    base = new_id("sku")[:-1]
    return [f"{base}{i}" for i in range(count)]


def request_id() -> str:
    return f"catalog-test-{uuid4().hex}"


def data_of(response) -> dict:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    return body["data"]


def listed(client: TestClient, headers: dict, params) -> list[str]:
    data = data_of(client.get("/v1/skus", params=params, headers=headers))
    assert set(data) == {"skus"}
    return [sku["sku_id"] for sku in data["skus"]]


# --- 按 ID 查询与表示 ---


def test_a_product_has_all_fields_and_its_skus_in_sku_id_order(client, engine):
    caller = issue(engine)
    first, second, third = ordered_sku_ids(3)
    with engine.begin() as db:
        product_id = add_product(
            db, caller.tenant_id, name="Basic Tee", status="inactive", version=4
        )
        # 故意不按 sku_id 顺序插入。
        add_sku(db, caller.tenant_id, product_id, sku_id=third, sku_code="TEE-L", name="Tee L")
        add_sku(
            db,
            caller.tenant_id,
            product_id,
            sku_id=first,
            sku_code="TEE-S",
            barcode="9551234567890",
            name="Tee S",
            reference_price_amount_minor=12900,
            reference_price_currency="MYR",
            version=3,
        )
        add_sku(
            db,
            caller.tenant_id,
            product_id,
            sku_id=second,
            sku_code="TEE-M",
            name="Tee M",
            status="inactive",
            merged_into_sku_id=first,
            version=2,
        )

    data = data_of(client.get(f"/v1/products/{product_id}", headers=caller.auth))
    common = {
        "product_id": product_id,
        "product_status": "inactive",
        "created_at": "2026-11-14T09:30:15.123456Z",
        "updated_at": "2026-11-14T10:00:00.000000Z",
    }
    assert data == {
        "product_id": product_id,
        "name": "Basic Tee",
        "status": "inactive",
        "version": 4,
        "created_at": "2026-11-14T09:30:15.123456Z",
        "updated_at": "2026-11-14T10:00:00.000000Z",
        "skus": [
            {
                **common,
                "sku_id": first,
                "sku_code": "TEE-S",
                "barcode": "9551234567890",
                "name": "Tee S",
                "reference_price": {"amount_minor": 12900, "currency": "MYR"},
                "status": "active",
                "merged_into_sku_id": None,
                "version": 3,
            },
            {
                **common,
                "sku_id": second,
                "sku_code": "TEE-M",
                "barcode": None,
                "name": "Tee M",
                "reference_price": None,
                "status": "inactive",
                "merged_into_sku_id": first,
                "version": 2,
            },
            {
                **common,
                "sku_id": third,
                "sku_code": "TEE-L",
                "barcode": None,
                "name": "Tee L",
                "reference_price": None,
                "status": "active",
                "merged_into_sku_id": None,
                "version": 1,
            },
        ],
    }


def test_a_product_without_skus_has_an_empty_list(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
    assert data_of(client.get(f"/v1/products/{product_id}", headers=caller.auth))["skus"] == []


def test_a_merged_sku_is_returned_with_its_canonical_sku(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        canonical = add_sku(db, caller.tenant_id, product_id)
        merged = add_sku(
            db,
            caller.tenant_id,
            product_id,
            sku_code="OLD-TEE",
            status="inactive",
            merged_into_sku_id=canonical,
            reference_price_amount_minor=0,
            reference_price_currency="USD",
        )

    data = data_of(client.get(f"/v1/skus/{merged}", headers=caller.auth))
    assert data == {
        "sku_id": merged,
        "product_id": product_id,
        "sku_code": "OLD-TEE",
        "barcode": None,
        "name": "Tee",
        "reference_price": {"amount_minor": 0, "currency": "USD"},
        "status": "inactive",
        "product_status": "active",
        "merged_into_sku_id": canonical,
        "version": 1,
        "created_at": "2026-11-14T09:30:15.123456Z",
        "updated_at": "2026-11-14T10:00:00.000000Z",
    }
    canonical_data = data_of(client.get(f"/v1/skus/{canonical}", headers=caller.auth))
    assert canonical_data["merged_into_sku_id"] is None


def test_database_default_times_are_utc_with_microseconds_and_z(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id, created_at=None, updated_at=None)
        sku_id = add_sku(db, caller.tenant_id, product_id, created_at=None, updated_at=None)

    product = data_of(client.get(f"/v1/products/{product_id}", headers=caller.auth))
    sku = data_of(client.get(f"/v1/skus/{sku_id}", headers=caller.auth))
    for value in (product["created_at"], product["updated_at"], sku["created_at"]):
        assert TIMESTAMP.fullmatch(value), value
    assert product["skus"] == [sku]


def test_missing_objects_are_404(client, engine):
    caller = issue(engine)
    missing_product, missing_sku = new_id("prd"), new_id("sku")
    for url, code in [
        (f"/v1/products/{missing_product}", "PRODUCT_NOT_FOUND"),
        (f"/v1/skus/{missing_sku}", "SKU_NOT_FOUND"),
    ]:
        rid = request_id()
        error = error_of(client.get(url, headers=caller.auth | {"X-Request-ID": rid}), 404)
        assert error["code"] == code
        assert events(engine, rid) == []


# --- 跨租户 ---


def test_another_tenants_product_and_sku_look_missing_and_are_recorded(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    with engine.begin() as db:
        foreign_product = add_product(db, foreign.tenant_id)
        foreign_sku = add_sku(db, foreign.tenant_id, foreign_product)

    for object_type, foreign_id, missing_id, code in [
        ("product", foreign_product, new_id("prd"), "PRODUCT_NOT_FOUND"),
        ("sku", foreign_sku, new_id("sku"), "SKU_NOT_FOUND"),
    ]:
        path = "/v1/products/" if object_type == "product" else "/v1/skus/"
        # 同一个请求 ID 发两次：响应完全相同，只有跨租户那次记事件。
        rid = request_id()
        headers = caller.auth | {"X-Request-ID": rid}
        cross = client.get(path + foreign_id, headers=headers)
        missing = client.get(path + missing_id, headers=headers)
        assert error_of(cross, 404)["code"] == code
        assert cross.json() == missing.json()

        [event] = events(engine, rid)
        assert event["event_type"] == "cross_tenant"
        assert event["tenant_id"] == caller.tenant_id
        assert event["credential_id"] == caller.credential_id
        assert event["http_method"] == "GET"
        assert event["path"] == path + foreign_id
        assert event["details"] == {"object_type": object_type, "id": foreign_id}


# --- 列表查询 ---


def test_lookup_by_code_and_barcode_ignores_case_and_surrounding_whitespace(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        sku_id = add_sku(
            db, caller.tenant_id, product_id, sku_code="Tee-Black M", barcode="AbC-123"
        )
        add_sku(db, caller.tenant_id, product_id, sku_code="Tee-Black L", barcode="AbC-124")

    single = data_of(client.get(f"/v1/skus/{sku_id}", headers=caller.auth))
    for params in [
        {"sku_code": "Tee-Black M"},
        {"sku_code": "  tee-black m\t"},
        {"sku_code": "TEE-BLACK M"},
        {"barcode": "abc-123"},
        {"barcode": " ABC-123 "},
    ]:
        data = data_of(client.get("/v1/skus", params=params, headers=caller.auth))
        assert data == {"skus": [single]}

    for params in [{"sku_code": "Tee-Black"}, {"sku_code": "Tee-BlackM"}, {"barcode": "ABC-12"}]:
        assert listed(client, caller.auth, params) == []


def test_a_retired_code_is_not_found(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        sku_id = add_sku(db, caller.tenant_id, product_id, sku_code="NEW-1")
        claim(db, caller.tenant_id, "OLD-1", sku_id)
        claim(db, caller.tenant_id, "NEW-1", sku_id)

    assert listed(client, caller.auth, {"sku_code": "OLD-1"}) == []
    assert listed(client, caller.auth, {"sku_code": "new-1"}) == [sku_id]


def test_another_tenants_code_or_barcode_is_not_found(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    with engine.begin() as db:
        foreign_product = add_product(db, foreign.tenant_id)
        add_sku(db, foreign.tenant_id, foreign_product, sku_code="SHARED-1", barcode="B-1")

    for params in [{"sku_code": "SHARED-1"}, {"barcode": "B-1"}]:
        rid = request_id()
        assert listed(client, caller.auth | {"X-Request-ID": rid}, params) == []
        assert events(engine, rid) == []

    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        own = add_sku(db, caller.tenant_id, product_id, sku_code="shared-1", barcode="b-1")
    for params in [{"sku_code": "SHARED-1"}, {"barcode": "B-1"}]:
        assert listed(client, caller.auth, params) == [own]


def test_ids_returns_only_this_tenants_skus_in_sku_id_order(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    first, second, third, foreign_sku = ordered_sku_ids(4)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        other_product = add_product(db, caller.tenant_id)
        add_sku(db, caller.tenant_id, product_id, sku_id=third)
        add_sku(db, caller.tenant_id, other_product, sku_id=first)
        add_sku(
            db,
            caller.tenant_id,
            product_id,
            sku_id=second,
            status="inactive",
            merged_into_sku_id=first,
        )
        foreign_product = add_product(db, foreign.tenant_id)
        add_sku(db, foreign.tenant_id, foreign_product, sku_id=foreign_sku)

    rid = request_id()
    ids = ",".join([third, foreign_sku, "not-an-id", first, new_id("sku"), second])
    headers = caller.auth | {"X-Request-ID": rid}
    data = data_of(client.get("/v1/skus", params={"ids": ids}, headers=headers))
    assert [sku["sku_id"] for sku in data["skus"]] == [first, second, third]
    single = data_of(client.get(f"/v1/skus/{second}", headers=caller.auth))
    assert data["skus"][1] == single
    assert events(engine, rid) == []

    assert listed(client, caller.auth, {"ids": first}) == [first]
    assert listed(client, caller.auth, {"ids": foreign_sku}) == []


def test_ids_accepts_up_to_100(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        sku_id = add_sku(db, caller.tenant_id, product_id)
    ids = [sku_id] + [new_id("sku") for _ in range(99)]
    assert listed(client, caller.auth, {"ids": ",".join(ids)}) == [sku_id]


@pytest.mark.parametrize(
    "params",
    [
        pytest.param([], id="none"),
        pytest.param([("sku_code", "A-1"), ("barcode", "B-1")], id="code_and_barcode"),
        pytest.param([("sku_code", "A-1"), ("ids", "sku_1")], id="code_and_ids"),
        pytest.param([("barcode", "B-1"), ("ids", "sku_1")], id="barcode_and_ids"),
        pytest.param([("sku_code", "A-1"), ("sku_code", "A-1")], id="code_twice"),
        pytest.param([("barcode", "B-1"), ("barcode", "B-2")], id="barcode_twice"),
        pytest.param([("ids", "sku_1"), ("ids", "sku_2")], id="ids_twice"),
        pytest.param([("sku_code", "A-1"), ("limit", "1")], id="unknown_with_known"),
        pytest.param([("name", "Tee")], id="unknown_alone"),
        pytest.param([("SKU_CODE", "A-1")], id="name_is_case_sensitive"),
        pytest.param([("barcode", "")], id="barcode_empty"),
        pytest.param([("barcode", "  \t ")], id="barcode_blank"),
        pytest.param([("sku_code", "")], id="code_empty"),
        pytest.param([("sku_code", "   ")], id="code_blank"),
        pytest.param([("sku_code", "x" * 65)], id="code_too_long"),
        pytest.param([("sku_code", "café")], id="code_not_ascii"),
        pytest.param([("sku_code", "a\tb")], id="code_control_character"),
        pytest.param([("barcode", "b" * 65)], id="barcode_too_long"),
        pytest.param([("barcode", "ü-1")], id="barcode_not_ascii"),
        pytest.param([("ids", "")], id="ids_empty"),
        pytest.param([("ids", "sku_1,,sku_2")], id="ids_empty_item"),
        pytest.param([("ids", "sku_1,")], id="ids_trailing_comma"),
        pytest.param([("ids", "sku_1,sku_2,sku_1")], id="ids_duplicate"),
        pytest.param([("ids", ",".join(f"sku_{i}" for i in range(101)))], id="ids_over_100"),
    ],
)
def test_bad_list_parameters_are_422(client, engine, params):
    caller = issue(engine)
    rid = request_id()
    headers = caller.auth | {"X-Request-ID": rid}
    error = error_of(client.get("/v1/skus", params=params, headers=headers), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert events(engine, rid) == []


# --- 授权 ---


def test_catalog_queries_need_catalog_read(client, engine):
    caller = issue(engine, scopes=[CATALOG_WRITE, STOCK_READ])
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        sku_id = add_sku(db, caller.tenant_id, product_id)

    for url in [f"/v1/products/{product_id}", f"/v1/skus/{sku_id}", f"/v1/skus?ids={sku_id}"]:
        rid = request_id()
        response = client.get(url, headers=caller.auth | {"X-Request-ID": rid})
        assert error_of(response, 403)["code"] == "FORBIDDEN"
        [event] = events(engine, rid)
        assert event["event_type"] == "forbidden"
        assert event["details"] == {"category": "scope", "missing_scopes": [CATALOG_READ]}


def test_a_missing_scope_comes_before_bad_list_parameters(client, engine):
    caller = issue(engine, scopes=[STOCK_READ])
    response = client.get("/v1/skus?ids=&limit=1", headers=caller.auth)
    assert error_of(response, 403)["code"] == "FORBIDDEN"


def test_catalog_queries_need_no_role(client, engine):
    caller = issue(engine, scopes=[CATALOG_READ])
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        sku_id = add_sku(db, caller.tenant_id, product_id)

    urls = [f"/v1/products/{product_id}", f"/v1/skus/{sku_id}", f"/v1/skus?ids={sku_id}"]
    actor = {"X-Actor-Ref": "admin-7", "X-Actor-Role": CATALOG_ADMIN}
    for url in urls:
        assert client.get(url, headers=caller.auth).status_code == 200
        assert client.get(url, headers=caller.auth | actor).status_code == 200


def test_health_endpoints_are_unchanged(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").json() == {"status": "ok"}
