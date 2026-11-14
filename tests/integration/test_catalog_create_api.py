"""新建商品与加规格（契约 §2.2、§2.6 第 1–3 条、§2.7、§7.2）。

在真 MySQL 上跑；每个测试建自己的租户与凭据，现有目录由测试直接在库里造（自己提交的连接）。
“什么也没建”按本租户的商品、SKU、归属行与变更记录在请求前后完全相同来判断。
"""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.config import Settings
from app.domain.requests import CATALOG_ADMIN, STOCK_ADMIN
from app.domain.service_auth import CATALOG_READ
from app.main import create_app
from app.models.catalog import CatalogChange, Product, Sku, SkuCodeClaim
from app.models.inventory import IdempotencyRecord
from app.services import catalog as catalog_service
from tests.integration.conftest import TEST_DATABASE_URL, new_id
from tests.integration.test_api_auth import Caller, error_of, events, issue
from tests.integration.test_catalog_queries_api import (
    TIMESTAMP,
    add_product,
    add_sku,
    claim,
    request_id,
)

ADMIN = {"X-Actor-Ref": "admin-7", "X-Actor-Role": CATALOG_ADMIN}
PRICE = {"amount_minor": 12900, "currency": "MYR"}


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


@pytest.fixture
def conflict_check_misses(monkeypatch) -> None:
    """冲突查询一项都报不出来（如查询之后编码才被占用），写入只能撞上唯一约束。"""
    monkeypatch.setattr(catalog_service, "sku_conflicts", lambda *args, **kwargs: [])


# --- 请求 ---


def new_key() -> str:
    return f"shop:product:{uuid4().hex}:create:1"


def write_headers(caller: Caller, key: str | None = None, rid: str | None = None) -> dict:
    extra = {"Idempotency-Key": key or new_key(), "X-Request-ID": rid or request_id()}
    return caller.auth | ADMIN | extra


def sku_body(code: str, **fields: object) -> dict:
    return {"sku_code": code, "name": f"Tee {code}"} | fields


def product_body(*skus: dict, **fields: object) -> dict:
    return {"name": "Tee", "skus": list(skus)} | fields


def create_product(client: TestClient, caller: Caller, body: dict, **headers: str | None):
    return client.post("/v1/products", json=body, headers=write_headers(caller, **headers))


def add_to(client: TestClient, caller: Caller, product_id: str, body: dict, **headers):
    url = f"/v1/products/{product_id}/skus"
    return client.post(url, json=body, headers=write_headers(caller, **headers))


def created(response) -> dict:
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    return body["data"]


# --- 库里的数据 ---


def rows(engine: Engine, model, tenant_id: str) -> list[dict]:
    table = model.__table__
    statement = select(*table.columns).where(table.c.tenant_id == tenant_id)
    statement = statement.order_by(*table.primary_key.columns)
    with engine.connect() as db:
        return [dict(row) for row in db.execute(statement).mappings()]


def catalog_of(engine: Engine, tenant_id: str) -> dict[str, list[dict]]:
    """本租户的商品、SKU、归属行与变更记录（变更记录按 id 升序）。"""
    models = {"products": Product, "skus": Sku, "claims": SkuCodeClaim, "changes": CatalogChange}
    return {name: rows(engine, model, tenant_id) for name, model in models.items()}


def changes_of(engine: Engine, tenant_id: str) -> list[dict]:
    return rows(engine, CatalogChange, tenant_id)


def current_sku(db, tenant_id: str, product_id: str, code: str, **overrides: object) -> str:
    """一个 SKU 与它当前编码的归属行，同建 SKU 时一样。"""
    sku_id = add_sku(db, tenant_id, product_id, sku_code=code, **overrides)
    claim(db, tenant_id, code, sku_id)
    return sku_id


def renamed_sku(db, tenant_id: str, product_id: str, old: str, new: str, **overrides) -> str:
    """改过编码的 SKU：old 已退役、new 是当前编码，两者都登记给它。"""
    sku_id = add_sku(db, tenant_id, product_id, sku_code=new, **overrides)
    claim(db, tenant_id, old, sku_id)
    claim(db, tenant_id, new, sku_id)
    return sku_id


# --- 新建商品 ---


def test_creating_a_product_stores_and_returns_it_with_change_records(client, engine):
    caller = issue(engine)
    key, rid = new_key(), request_id()
    body = product_body(
        sku_body(" TEE-S ", name="Tee S", barcode=" 9551234567890 ", reference_price=PRICE),
        sku_body("TEE-M", barcode="   "),
        sku_body("TEE-L", name="  Tee L  ", barcode=None, reference_price=None),
        name="  Basic Tee ",
        reason=" first launch ",
    )
    data = created(create_product(client, caller, body, key=key, rid=rid))

    product_id = data["product_id"]
    assert product_id.startswith("prd_")
    assert (data["name"], data["status"], data["version"]) == ("Basic Tee", "active", 1)
    assert TIMESTAMP.fullmatch(data["created_at"]) and TIMESTAMP.fullmatch(data["updated_at"])
    sku_ids = [sku["sku_id"] for sku in data["skus"]]
    assert sku_ids == sorted(sku_ids)
    by_code = {sku["sku_code"]: sku for sku in data["skus"]}
    assert set(by_code) == {"TEE-S", "TEE-M", "TEE-L"}
    for sku in data["skus"]:
        assert sku["sku_id"].startswith("sku_")
        assert sku["product_id"] == product_id
        assert (sku["status"], sku["product_status"], sku["version"]) == ("active", "active", 1)
        assert sku["merged_into_sku_id"] is None
    assert by_code["TEE-S"]["barcode"] == "9551234567890"
    assert by_code["TEE-S"]["reference_price"] == PRICE
    assert by_code["TEE-M"]["barcode"] is None
    assert by_code["TEE-L"]["name"] == "Tee L"
    assert by_code["TEE-L"]["reference_price"] is None

    # 表示与库里的一致：按 ID 读回来完全相同。
    response = client.get(f"/v1/products/{product_id}", headers=caller.auth)
    assert response.json()["data"] == data

    stored = catalog_of(engine, caller.tenant_id)
    assert [row["product_id"] for row in stored["products"]] == [product_id]
    # 空白条码存 NULL，不是空串。
    barcodes = {row["sku_code"]: row["barcode"] for row in stored["skus"]}
    assert barcodes == {"TEE-S": "9551234567890", "TEE-M": None, "TEE-L": None}
    claims = {row["code"]: row["sku_id"] for row in stored["claims"]}
    assert claims == {code: sku["sku_id"] for code, sku in by_code.items()}

    # 先 product_created，再按请求顺序（不是 sku_id 顺序）每个 SKU 一条 sku_created。
    changes = stored["changes"]
    assert [(c["event_type"], c["object_id"]) for c in changes] == [
        ("product_created", product_id),
        ("sku_created", by_code["TEE-S"]["sku_id"]),
        ("sku_created", by_code["TEE-M"]["sku_id"]),
        ("sku_created", by_code["TEE-L"]["sku_id"]),
    ]
    for change in changes:
        assert change["product_id"] == product_id
        assert change["version"] == 1
        assert change["credential_id"] == caller.credential_id
        assert (change["actor_ref"], change["actor_role"]) == ("admin-7", CATALOG_ADMIN)
        assert (change["request_id"], change["idempotency_key"]) == (rid, key)
        assert change["reason"] == "first launch"
        assert change["import_id"] is None
        assert all(field["before"] is None for field in change["changes"].values())
    assert changes[0]["object_type"] == "product"
    assert changes[0]["changes"] == {
        "name": {"before": None, "after": "Basic Tee"},
        "status": {"before": None, "after": "active"},
    }
    assert changes[1]["object_type"] == "sku"
    assert changes[1]["changes"] == {
        "sku_code": {"before": None, "after": "TEE-S"},
        "barcode": {"before": None, "after": "9551234567890"},
        "name": {"before": None, "after": "Tee S"},
        "reference_price": {"before": None, "after": PRICE},
        "status": {"before": None, "after": "active"},
    }
    assert changes[2]["changes"]["barcode"] == {"before": None, "after": None}
    assert changes[3]["changes"]["reference_price"] == {"before": None, "after": None}


def test_reason_is_optional(client, engine):
    caller = issue(engine)
    created(create_product(client, caller, product_body(sku_body("A-1"))))
    assert {change["reason"] for change in changes_of(engine, caller.tenant_id)} == {None}


def test_the_same_key_replays_without_creating_again(client, engine):
    caller = issue(engine)
    key = new_key()
    body = product_body(sku_body("A-1"), sku_body("A-2"))
    first = create_product(client, caller, body, key=key)
    data = created(first)
    stored = catalog_of(engine, caller.tenant_id)

    replay = create_product(client, caller, body, key=key)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert catalog_of(engine, caller.tenant_id) == stored
    assert [row["product_id"] for row in stored["products"]] == [data["product_id"]]


def test_up_to_100_skus_are_accepted(client, engine):
    caller = issue(engine)
    body = product_body(*[sku_body(f"C-{i}") for i in range(100)])
    assert len(created(create_product(client, caller, body))["skus"]) == 100


def test_codes_used_only_by_another_tenant_are_free(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    with engine.begin() as db:
        foreign_product = add_product(db, foreign.tenant_id)
        current_sku(db, foreign.tenant_id, foreign_product, "SHARED-1", barcode="SB-1")
        renamed_sku(db, foreign.tenant_id, foreign_product, "SHARED-OLD", "SHARED-NEW")

    body = product_body(
        sku_body("shared-1", barcode="sb-1"), sku_body("SHARED-OLD"), sku_body("SHARED-NEW")
    )
    data = created(create_product(client, caller, body))
    codes = sorted(sku["sku_code"] for sku in data["skus"])
    assert codes == ["SHARED-NEW", "SHARED-OLD", "shared-1"]


# --- 新建被拒：请求体（第 7 步，取锁之前，不保存） ---


def assert_rejected_before_the_lock(engine: Engine, caller: Caller) -> None:
    """新租户的目录仍是空的，也没有保存幂等记录。"""
    empty = {"products": [], "skus": [], "claims": [], "changes": []}
    assert catalog_of(engine, caller.tenant_id) == empty
    assert rows(engine, IdempotencyRecord, caller.tenant_id) == []


def test_empty_skus_is_422(client, engine):
    caller = issue(engine)
    error = error_of(create_product(client, caller, product_body()), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert {"loc": ["skus"], "type": "too_short"} in error["details"]["errors"]
    assert_rejected_before_the_lock(engine, caller)


def test_more_than_100_skus_is_413(client, engine):
    caller = issue(engine)
    body = product_body(*[sku_body(f"C-{i}") for i in range(101)])
    assert error_of(create_product(client, caller, body), 413)["code"] == "REQUEST_TOO_LARGE"
    assert_rejected_before_the_lock(engine, caller)


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        pytest.param(product_body(sku_body("A-1"), name="  "), ["name"], id="name_blank"),
        pytest.param(product_body(sku_body("A-1"), name="x" * 201), ["name"], id="name_too_long"),
        pytest.param(product_body(sku_body("A-1"), name=None), ["name"], id="name_null"),
        pytest.param(
            product_body(sku_body("A-1"), sku_body("A-2", name="a\nb")),
            ["skus", 1, "name"],
            id="sku_name_control_character",
        ),
        pytest.param(product_body(sku_body("café")), ["skus", 0, "sku_code"], id="code_not_ascii"),
        pytest.param(product_body(sku_body("x" * 65)), ["skus", 0, "sku_code"], id="code_too_long"),
        pytest.param(product_body(sku_body(" ")), ["skus", 0, "sku_code"], id="code_blank"),
        pytest.param(product_body(sku_body(None)), ["skus", 0, "sku_code"], id="code_null"),
        pytest.param(product_body({"name": "Tee"}), ["skus", 0, "sku_code"], id="code_missing"),
        pytest.param(
            product_body(sku_body("A-1", barcode="ü-1")),
            ["skus", 0, "barcode"],
            id="barcode_not_ascii",
        ),
        pytest.param(
            product_body(sku_body("A-1", barcode=123)), ["skus", 0, "barcode"], id="barcode_number"
        ),
        pytest.param(
            product_body(sku_body("A-1", reference_price=PRICE | {"amount_minor": -1})),
            ["skus", 0, "reference_price"],
            id="price_negative",
        ),
        pytest.param(
            product_body(sku_body("A-1", reference_price=PRICE | {"currency": "myr"})),
            ["skus", 0, "reference_price"],
            id="price_currency_lowercase",
        ),
        pytest.param(
            product_body(sku_body("A-1", reference_price=PRICE | {"amount_minor": True})),
            ["skus", 0, "reference_price"],
            id="price_amount_boolean",
        ),
        pytest.param(
            product_body(sku_body("A-1", reference_price=PRICE | {"tax": 0})),
            ["skus", 0, "reference_price"],
            id="price_extra_field",
        ),
        pytest.param(product_body(sku_body("A-1"), reason=""), ["reason"], id="reason_empty"),
        pytest.param(product_body(sku_body("A-1"), status="active"), ["status"], id="unknown"),
        pytest.param(
            product_body(sku_body("A-1", reason="x")), ["skus", 0, "reason"], id="unknown_in_sku"
        ),
        pytest.param({"name": "Tee", "skus": {"sku_code": "A-1"}}, ["skus"], id="skus_object"),
    ],
)
def test_invalid_fields_are_422_at_their_location(client, engine, body, loc):
    caller = issue(engine)
    error = error_of(create_product(client, caller, body), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert loc in [item["loc"] for item in error["details"]["errors"]]
    assert_rejected_before_the_lock(engine, caller)


@pytest.mark.parametrize(
    ("skus", "loc"),
    [
        pytest.param(
            [sku_body("TEE-S"), sku_body("A-1"), sku_body(" tee-s ")],
            ["skus", 2, "sku_code"],
            id="code_differs_only_in_case",
        ),
        pytest.param(
            [sku_body("A-1", barcode="B-1"), sku_body("A-2", barcode=" b-1")],
            ["skus", 1, "barcode"],
            id="barcode",
        ),
    ],
)
def test_duplicates_in_one_request_are_422_and_the_key_can_be_reused(client, engine, skus, loc):
    caller = issue(engine)
    key = new_key()
    error = error_of(create_product(client, caller, product_body(*skus), key=key), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"]["errors"] == [{"loc": loc, "type": "duplicate"}]
    assert_rejected_before_the_lock(engine, caller)

    # 拒绝没保存，改正后同键重发照常新建。
    fixed = [sku_body(f"FIXED-{i}") for i in range(len(skus))]
    created(create_product(client, caller, product_body(*fixed), key=key))


def test_blank_and_missing_barcodes_are_not_duplicates(client, engine):
    caller = issue(engine)
    body = product_body(
        sku_body("A-1", barcode=" "), sku_body("A-2", barcode=None), sku_body("A-3")
    )
    created(create_product(client, caller, body))


# --- 新建被拒：与现有目录冲突 ---


@pytest.fixture
def existing(engine):
    """调用方的租户里已有一个商品，它有两个 SKU。

    一个的当前编码是 TAKEN-1、条码 BAR-1；另一个从 OLD-2 改成了 NEW-2、条码 BAR-2。
    """
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        current_sku(db, caller.tenant_id, product_id, "TAKEN-1", barcode="BAR-1")
        renamed_sku(db, caller.tenant_id, product_id, "OLD-2", "NEW-2", barcode="BAR-2")
    return caller


@pytest.mark.parametrize(
    ("sku", "conflict"),
    [
        pytest.param(
            sku_body("taken-1"),
            {"field": "sku_code", "value": "taken-1", "code": "SKU_CODE_TAKEN"},
            id="current_code",
        ),
        pytest.param(
            sku_body(" Old-2 "),
            {"field": "sku_code", "value": "Old-2", "code": "SKU_CODE_RETIRED"},
            id="retired_code",
        ),
        pytest.param(
            sku_body("FRESH-1", barcode="bar-1"),
            {"field": "barcode", "value": "bar-1", "code": "BARCODE_TAKEN"},
            id="barcode",
        ),
    ],
)
def test_a_conflict_with_the_catalog_is_409(client, engine, existing, sku, conflict):
    before = catalog_of(engine, existing.tenant_id)
    body = product_body(sku_body("FRESH-0"), sku)
    error = error_of(create_product(client, existing, body), 409)
    assert error["code"] == conflict["code"]
    assert error["details"] == {"conflicts": [{"index": 1} | conflict]}
    assert catalog_of(engine, existing.tenant_id) == before


def test_all_conflicts_are_listed_in_order_and_nothing_is_created(client, engine, existing):
    before = catalog_of(engine, existing.tenant_id)
    body = product_body(
        sku_body("FRESH-0", barcode="bar-1"),
        sku_body("old-2"),
        sku_body("FRESH-2"),
        sku_body("Taken-1", barcode="bar-2"),
    )
    error = error_of(create_product(client, existing, body), 409)
    # SKU_CODE_TAKEN 只出现在后面，顶层码仍按固定顺序取它。
    assert error["code"] == "SKU_CODE_TAKEN"
    assert error["details"]["conflicts"] == [
        {"index": 0, "field": "barcode", "value": "bar-1", "code": "BARCODE_TAKEN"},
        {"index": 1, "field": "sku_code", "value": "old-2", "code": "SKU_CODE_RETIRED"},
        {"index": 3, "field": "sku_code", "value": "Taken-1", "code": "SKU_CODE_TAKEN"},
        {"index": 3, "field": "barcode", "value": "bar-2", "code": "BARCODE_TAKEN"},
    ]
    assert catalog_of(engine, existing.tenant_id) == before


def test_retired_comes_before_barcode_in_the_top_level_code(client, engine, existing):
    body = product_body(sku_body("FRESH-0", barcode="BAR-1"), sku_body("OLD-2"))
    assert error_of(create_product(client, existing, body), 409)["code"] == "SKU_CODE_RETIRED"


# --- 加规格 ---


def test_a_sku_can_be_added_to_an_inactive_product_without_changing_its_version(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id, status="inactive", version=3)
        current_sku(db, caller.tenant_id, product_id, "TEE-S")
    product_before = rows(engine, Product, caller.tenant_id)
    key, rid = new_key(), request_id()

    body = sku_body(
        " TEE-M ", name=" Tee M", barcode=" ", reference_price=PRICE, reason=" restock "
    )
    data = created(add_to(client, caller, product_id, body, key=key, rid=rid))
    assert data["sku_id"].startswith("sku_")
    assert data["product_id"] == product_id
    assert (data["sku_code"], data["barcode"], data["name"]) == ("TEE-M", None, "Tee M")
    assert data["reference_price"] == PRICE
    assert (data["status"], data["product_status"], data["version"]) == ("active", "inactive", 1)
    sku_id = data["sku_id"]
    assert client.get(f"/v1/skus/{sku_id}", headers=caller.auth).json()["data"] == data

    # 商品的版本、状态与更新时间都不变。
    assert rows(engine, Product, caller.tenant_id) == product_before
    claims = {row["code"]: row["sku_id"] for row in rows(engine, SkuCodeClaim, caller.tenant_id)}
    assert claims["TEE-M"] == data["sku_id"]
    [change] = changes_of(engine, caller.tenant_id)
    assert change["event_type"] == "sku_created"
    assert (change["object_type"], change["object_id"]) == ("sku", data["sku_id"])
    assert (change["product_id"], change["version"]) == (product_id, 1)
    assert change["credential_id"] == caller.credential_id
    assert (change["actor_ref"], change["actor_role"]) == ("admin-7", CATALOG_ADMIN)
    assert (change["request_id"], change["idempotency_key"]) == (rid, key)
    assert change["reason"] == "restock"
    assert change["changes"]["barcode"] == {"before": None, "after": None}
    assert change["changes"]["sku_code"] == {"before": None, "after": "TEE-M"}


def test_a_missing_and_another_tenants_product_get_the_same_404(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    with engine.begin() as db:
        foreign_product = add_product(db, foreign.tenant_id)
    foreign_before = catalog_of(engine, foreign.tenant_id)

    # 同一个请求 ID：响应完全相同，只有跨租户那次记事件。
    rid = request_id()
    body = sku_body("A-1")
    cross = add_to(client, caller, foreign_product, body, rid=rid)
    missing = add_to(client, caller, new_id("prd"), body, rid=rid)
    assert error_of(cross, 404)["code"] == "PRODUCT_NOT_FOUND"
    assert cross.json() == missing.json()

    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert (event["tenant_id"], event["credential_id"]) == (caller.tenant_id, caller.credential_id)
    assert event["http_method"] == "POST"
    assert event["path"] == f"/v1/products/{foreign_product}/skus"
    assert event["details"] == {"object_type": "product", "id": foreign_product}
    assert catalog_of(engine, foreign.tenant_id) == foreign_before
    assert changes_of(engine, caller.tenant_id) == []


def test_added_sku_conflicts_have_no_index(client, engine, existing):
    [product] = rows(engine, Product, existing.tenant_id)
    before = catalog_of(engine, existing.tenant_id)
    body = sku_body("OLD-2", barcode="bar-1")
    error = error_of(add_to(client, existing, product["product_id"], body), 409)
    assert error["code"] == "SKU_CODE_RETIRED"
    assert error["details"]["conflicts"] == [
        {"field": "sku_code", "value": "OLD-2", "code": "SKU_CODE_RETIRED"},
        {"field": "barcode", "value": "bar-1", "code": "BARCODE_TAKEN"},
    ]
    assert catalog_of(engine, existing.tenant_id) == before


def test_invalid_added_sku_fields_are_422(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
    for body, loc in [
        (sku_body("café"), ["sku_code"]),
        (sku_body("A-1", reason="\t"), ["reason"]),
        (sku_body("A-1", product_id=product_id), ["product_id"]),
    ]:
        error = error_of(add_to(client, caller, product_id, body), 422)
        assert loc in [item["loc"] for item in error["details"]["errors"]]
    assert changes_of(engine, caller.tenant_id) == []


# --- 冲突查询漏报时撞上唯一约束 ---


@pytest.mark.parametrize(
    ("sku", "conflict"),
    [
        pytest.param(
            sku_body("taken-1"),
            {"field": "sku_code", "value": "taken-1", "code": "SKU_CODE_TAKEN"},
            id="current_code",
        ),
        pytest.param(
            sku_body("Old-2"),
            {"field": "sku_code", "value": "Old-2", "code": "SKU_CODE_RETIRED"},
            id="retired_code",
        ),
        pytest.param(
            sku_body("FRESH-1", barcode="bar-2"),
            {"field": "barcode", "value": "bar-2", "code": "BARCODE_TAKEN"},
            id="barcode",
        ),
    ],
)
def test_a_unique_constraint_hit_is_409_not_500(
    client, engine, existing, conflict_check_misses, sku, conflict
):
    [product] = rows(engine, Product, existing.tenant_id)
    before = catalog_of(engine, existing.tenant_id)

    # 第 0 个 SKU 已经写进去了，撞上第 1 个时连同商品一起撤销。
    body = product_body(sku_body("FRESH-0"), sku)
    error = error_of(create_product(client, existing, body), 409)
    assert error["code"] == conflict["code"]
    assert error["details"] == {"conflicts": [{"index": 1} | conflict]}
    assert catalog_of(engine, existing.tenant_id) == before

    error = error_of(add_to(client, existing, product["product_id"], sku), 409)
    assert error["code"] == conflict["code"]
    assert error["details"] == {"conflicts": [conflict]}
    assert catalog_of(engine, existing.tenant_id) == before


# --- 授权 ---


def product_urls(engine: Engine, caller: Caller) -> list[tuple[str, dict]]:
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
    return [
        ("/v1/products", product_body(sku_body("A-1"))),
        (f"/v1/products/{product_id}/skus", sku_body("A-1")),
    ]


def test_writes_need_catalog_write(client, engine):
    caller = issue(engine, scopes=[CATALOG_READ])
    for url, body in product_urls(engine, caller):
        rid = request_id()
        response = client.post(url, json=body, headers=write_headers(caller, rid=rid))
        assert error_of(response, 403)["code"] == "FORBIDDEN"
        [event] = events(engine, rid)
        assert event["details"] == {"category": "scope", "missing_scopes": ["catalog.write"]}
    assert changes_of(engine, caller.tenant_id) == []


def test_writes_need_the_actor_headers(client, engine):
    caller = issue(engine)
    for url, body in product_urls(engine, caller):
        headers = caller.auth | {"Idempotency-Key": new_key()}
        response = client.post(url, json=body, headers=headers)
        assert error_of(response, 400)["code"] == "INVALID_REQUEST"
    assert changes_of(engine, caller.tenant_id) == []


def test_writes_need_the_catalog_admin_role(client, engine):
    caller = issue(engine)
    for url, body in product_urls(engine, caller):
        rid = request_id()
        headers = write_headers(caller, rid=rid) | {"X-Actor-Role": STOCK_ADMIN}
        response = client.post(url, json=body, headers=headers)
        assert error_of(response, 403)["code"] == "FORBIDDEN"
        [event] = events(engine, rid)
        assert event["details"] == {"category": "role", "role": STOCK_ADMIN}
    assert changes_of(engine, caller.tenant_id) == []
