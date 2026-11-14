"""商品与 SKU 的修改、停用与重新启用（契约 §2.2、§2.4、§2.6 第 4、5 条、§2.7、§7.2）。

在真 MySQL 上跑；每个测试建自己的租户与凭据，现有目录（含已合并的 SKU）由测试直接在库里造。
“什么也没改”按本租户的商品、SKU、归属行、变更记录与编码历史在请求前后完全相同来判断。
"""

from collections.abc import Iterator
from datetime import datetime
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text, update

from app.api.catalog import format_timestamp
from app.config import Settings
from app.domain.requests import CATALOG_ADMIN
from app.main import create_app
from app.models.catalog import Product, Sku, SkuCodeClaim, SkuCodeHistory
from app.models.inventory import IdempotencyRecord
from app.services import catalog as catalog_service
from tests.integration.conftest import TEST_DATABASE_URL, new_id
from tests.integration.test_api_auth import Caller, error_of, events, issue
from tests.integration.test_catalog_create_api import (
    PRICE,
    catalog_of,
    changes_of,
    current_sku,
    new_key,
    renamed_sku,
    rows,
    write_headers,
)
from tests.integration.test_catalog_queries_api import (
    add_product,
    add_sku,
    data_of,
    request_id,
)

OTHER_PRICE = {"amount_minor": 9900, "currency": "MYR"}


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


# --- 请求 ---


def send(client: TestClient, caller: Caller, method: str, path: str, body=None, **headers):
    """body 为 None 时不带请求体。"""
    return client.request(method, path, json=body, headers=write_headers(caller, **headers))


def patch_product(client, caller, product_id: str, body: dict, **headers):
    return send(client, caller, "PATCH", f"/v1/products/{product_id}", body, **headers)


def patch_sku(client, caller, sku_id: str, body: dict, **headers):
    return send(client, caller, "PATCH", f"/v1/skus/{sku_id}", body, **headers)


def command(client, caller, path: str, **headers):
    """停用或重新启用：POST，没有请求体。"""
    return send(client, caller, "POST", path, **headers)


def get(client: TestClient, caller: Caller, path: str) -> dict:
    return data_of(client.get(path, headers=caller.auth))


# --- 库里的数据 ---


def state_of(engine: Engine, tenant_id: str) -> dict[str, list[dict]]:
    return catalog_of(engine, tenant_id) | {"history": rows(engine, SkuCodeHistory, tenant_id)}


def row_of(engine: Engine, model, tenant_id: str, object_id: str) -> dict:
    key = model.__table__.primary_key.columns.keys()[0]
    [found] = [row for row in rows(engine, model, tenant_id) if row[key] == object_id]
    return found


def claims_of(engine: Engine, tenant_id: str) -> dict[str, str]:
    return {row["code"]: row["sku_id"] for row in rows(engine, SkuCodeClaim, tenant_id)}


def db_now(engine: Engine) -> datetime:
    with engine.connect() as db:
        return db.execute(text("SELECT CURRENT_TIMESTAMP(6)")).scalar_one()


class Catalog(NamedTuple):
    caller: Caller
    product_id: str
    sku_id: str  # TEE-S，条码 BAR-S，名称 Tee S，参考价 PRICE
    taken_id: str  # 当前编码 TAKEN-1，条码 BAR-1
    renamed_id: str  # 从 OLD-2 改成了 NEW-2，条码 BAR-2


@pytest.fixture
def existing(engine) -> Catalog:
    """调用方的租户里有一个 active 的商品（版本 1）与它的三个 SKU（版本都是 1）。"""
    caller = issue(engine)
    tenant_id = caller.tenant_id
    with engine.begin() as db:
        product_id = add_product(db, tenant_id)
        sku_id = current_sku(
            db,
            tenant_id,
            product_id,
            "TEE-S",
            barcode="BAR-S",
            name="Tee S",
            reference_price_amount_minor=PRICE["amount_minor"],
            reference_price_currency=PRICE["currency"],
        )
        taken_id = current_sku(db, tenant_id, product_id, "TAKEN-1", barcode="BAR-1")
        renamed_id = renamed_sku(db, tenant_id, product_id, "OLD-2", "NEW-2", barcode="BAR-2")
    return Catalog(caller, product_id, sku_id, taken_id, renamed_id)


def assert_change(change: dict, *, event_type: str, object_id: str, version: int, **expected):
    """一条变更记录的事件、对象、版本与作者；expected 里另给 product_id、changes、reason 等。"""
    assert change["event_type"] == event_type
    assert change["object_id"] == object_id
    assert change["version"] == version
    assert (change["actor_ref"], change["actor_role"]) == ("admin-7", CATALOG_ADMIN)
    assert change["import_id"] is None
    for name, value in expected.items():
        assert change[name] == value, name


# --- 修改成功 ---


def test_updating_a_product_bumps_its_version_and_records_the_name(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id, status="inactive", version=2)
        sku_id = current_sku(db, caller.tenant_id, product_id, "TEE-S")
    skus_before = rows(engine, Sku, caller.tenant_id)
    key, rid = new_key(), request_id()
    started = db_now(engine)

    body = {"expected_version": 2, "name": "  Basic Tee ", "reason": " rename "}
    data = data_of(patch_product(client, caller, product_id, body, key=key, rid=rid))
    finished = db_now(engine)

    # inactive 的商品也可以改，状态不变。
    assert (data["name"], data["status"], data["version"]) == ("Basic Tee", "inactive", 3)
    assert [sku["sku_id"] for sku in data["skus"]] == [sku_id]
    assert get(client, caller, f"/v1/products/{product_id}") == data
    row = row_of(engine, Product, caller.tenant_id, product_id)
    assert started <= row["updated_at"] <= finished
    assert data["updated_at"] == format_timestamp(row["updated_at"])
    assert rows(engine, Sku, caller.tenant_id) == skus_before

    [change] = changes_of(engine, caller.tenant_id)
    assert_change(
        change,
        event_type="product_updated",
        object_id=product_id,
        version=3,
        object_type="product",
        product_id=product_id,
        changes={"name": {"before": "Tee", "after": "Basic Tee"}},
        reason="rename",
        credential_id=caller.credential_id,
        request_id=rid,
        idempotency_key=key,
    )


def test_name_and_price_changed_together_make_one_record(client, engine, existing):
    caller = existing.caller
    started = db_now(engine)
    body = {"expected_version": 1, "name": "Tee Small", "reference_price": OTHER_PRICE}
    data = data_of(patch_sku(client, caller, existing.sku_id, body))
    finished = db_now(engine)

    assert (data["name"], data["reference_price"]) == ("Tee Small", OTHER_PRICE)
    assert data["version"] == 2
    assert (data["sku_code"], data["barcode"], data["status"]) == ("TEE-S", "BAR-S", "active")
    assert get(client, caller, f"/v1/skus/{existing.sku_id}") == data
    row = row_of(engine, Sku, caller.tenant_id, existing.sku_id)
    assert started <= row["updated_at"] <= finished
    assert data["updated_at"] == format_timestamp(row["updated_at"])

    [change] = changes_of(engine, caller.tenant_id)
    assert_change(
        change,
        event_type="sku_updated",
        object_id=existing.sku_id,
        version=2,
        object_type="sku",
        product_id=existing.product_id,
        changes={
            "name": {"before": "Tee S", "after": "Tee Small"},
            "reference_price": {"before": PRICE, "after": OTHER_PRICE},
        },
        reason=None,
    )
    # 没改编码：不写编码历史，归属不变。
    assert rows(engine, SkuCodeHistory, caller.tenant_id) == []
    # 商品的版本不变。
    assert row_of(engine, Product, caller.tenant_id, existing.product_id)["version"] == 1


def test_values_equal_to_the_current_ones_change_nothing(client, engine, existing):
    caller = existing.caller
    product_before = get(client, caller, f"/v1/products/{existing.product_id}")
    sku_before = get(client, caller, f"/v1/skus/{existing.sku_id}")
    stored = state_of(engine, caller.tenant_id)

    body = {"expected_version": 1, "name": " Tee ", "reason": "no-op"}
    assert data_of(patch_product(client, caller, existing.product_id, body)) == product_before
    body = {
        "expected_version": 1,
        "sku_code": " TEE-S ",
        "barcode": "BAR-S",
        "name": "Tee S",
        "reference_price": dict(PRICE),
    }
    assert data_of(patch_sku(client, caller, existing.sku_id, body)) == sku_before
    assert state_of(engine, caller.tenant_id) == stored


def test_changing_only_the_case_of_the_code_is_a_change(client, engine, existing):
    caller = existing.caller
    claims = claims_of(engine, caller.tenant_id)
    key, rid = new_key(), request_id()

    body = {"expected_version": 1, "sku_code": "tee-s", "barcode": "bar-s", "reason": "lower"}
    data = data_of(patch_sku(client, caller, existing.sku_id, body, key=key, rid=rid))
    assert (data["sku_code"], data["barcode"], data["version"]) == ("tee-s", "bar-s", 2)

    # 编码已归属自己，不再插入归属行；照样写编码历史。
    assert claims_of(engine, caller.tenant_id) == claims
    [history] = rows(engine, SkuCodeHistory, caller.tenant_id)
    assert history["sku_id"] == existing.sku_id
    assert (history["old_code"], history["new_code"]) == ("TEE-S", "tee-s")
    assert (history["actor_ref"], history["reason"]) == ("admin-7", "lower")
    [change] = changes_of(engine, caller.tenant_id)
    assert change["changes"] == {
        "sku_code": {"before": "TEE-S", "after": "tee-s"},
        "barcode": {"before": "BAR-S", "after": "bar-s"},
    }


def test_a_new_code_is_claimed_and_the_old_one_goes_to_the_history(client, engine, existing):
    caller = existing.caller
    claims = claims_of(engine, caller.tenant_id)

    body = {"expected_version": 1, "sku_code": " TEE-SMALL ", "reason": " split sizes "}
    data = data_of(patch_sku(client, caller, existing.sku_id, body))
    assert (data["sku_code"], data["version"]) == ("TEE-SMALL", 2)

    assert claims_of(engine, caller.tenant_id) == claims | {"TEE-SMALL": existing.sku_id}
    [history] = rows(engine, SkuCodeHistory, caller.tenant_id)
    assert (history["old_code"], history["new_code"]) == ("TEE-S", "TEE-SMALL")
    assert (history["actor_ref"], history["reason"]) == ("admin-7", "split sizes")
    [change] = changes_of(engine, caller.tenant_id)
    assert change["changes"] == {"sku_code": {"before": "TEE-S", "after": "TEE-SMALL"}}
    assert change["reason"] == "split sizes"

    # 旧编码退役：按当前编码查不到，别的 SKU 也不能用。
    assert get(client, caller, "/v1/skus?sku_code=TEE-S") == {"skus": []}
    body = {"expected_version": 1, "sku_code": "tee-s"}
    error = error_of(patch_sku(client, caller, existing.taken_id, body), 409)
    assert error["code"] == "SKU_CODE_RETIRED"


def test_going_back_to_an_own_retired_code_inserts_no_claim(client, engine, existing):
    caller = existing.caller
    claims = claims_of(engine, caller.tenant_id)

    body = {"expected_version": 1, "sku_code": "old-2"}
    assert data_of(patch_sku(client, caller, existing.renamed_id, body))["sku_code"] == "old-2"
    assert claims_of(engine, caller.tenant_id) == claims
    [history] = rows(engine, SkuCodeHistory, caller.tenant_id)
    assert (history["old_code"], history["new_code"]) == ("NEW-2", "old-2")
    assert history["reason"] is None


def test_barcode_and_price_can_be_cleared_and_the_barcode_reused(client, engine, existing):
    caller = existing.caller
    body = {"expected_version": 1, "barcode": None, "reference_price": None}
    data = data_of(patch_sku(client, caller, existing.sku_id, body))
    assert (data["barcode"], data["reference_price"], data["version"]) == (None, None, 2)
    row = row_of(engine, Sku, caller.tenant_id, existing.sku_id)
    assert row["barcode"] is None
    assert (row["reference_price_amount_minor"], row["reference_price_currency"]) == (None, None)
    [change] = changes_of(engine, caller.tenant_id)
    assert change["changes"] == {
        "barcode": {"before": "BAR-S", "after": None},
        "reference_price": {"before": PRICE, "after": None},
    }

    # 空白串也是清空；清空后别的 SKU 能用这个条码。
    body = {"expected_version": 1, "barcode": "   "}
    assert data_of(patch_sku(client, caller, existing.taken_id, body))["barcode"] is None
    body = {"expected_version": 1, "barcode": "bar-s"}
    assert data_of(patch_sku(client, caller, existing.renamed_id, body))["barcode"] == "bar-s"


def test_codes_used_only_by_another_tenant_are_free(client, engine, existing):
    foreign = issue(engine)
    with engine.begin() as db:
        foreign_product = add_product(db, foreign.tenant_id)
        current_sku(db, foreign.tenant_id, foreign_product, "SHARED-1", barcode="SB-1")
        renamed_sku(db, foreign.tenant_id, foreign_product, "SHARED-OLD", "SHARED-NEW")
    caller = existing.caller

    body = {"expected_version": 1, "sku_code": "shared-1", "barcode": "sb-1"}
    data = data_of(patch_sku(client, caller, existing.sku_id, body))
    assert (data["sku_code"], data["barcode"]) == ("shared-1", "sb-1")
    body = {"expected_version": 1, "sku_code": "shared-old"}
    assert data_of(patch_sku(client, caller, existing.taken_id, body))["sku_code"] == "shared-old"


def test_the_same_key_replays_without_updating_again(client, engine, existing):
    caller = existing.caller
    key = new_key()
    body = {"expected_version": 1, "name": "Tee Small"}
    first = patch_sku(client, caller, existing.sku_id, body, key=key)
    assert data_of(first)["version"] == 2
    stored = state_of(engine, caller.tenant_id)

    replay = patch_sku(client, caller, existing.sku_id, body, key=key)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert state_of(engine, caller.tenant_id) == stored


# --- 修改被拒：编码与条码冲突 ---


@pytest.mark.parametrize(
    ("fields", "conflicts"),
    [
        pytest.param(
            {"sku_code": "taken-1"},
            [{"field": "sku_code", "value": "taken-1", "code": "SKU_CODE_TAKEN"}],
            id="current_code",
        ),
        pytest.param(
            {"sku_code": " Old-2 "},
            [{"field": "sku_code", "value": "Old-2", "code": "SKU_CODE_RETIRED"}],
            id="retired_code",
        ),
        pytest.param(
            {"barcode": "bar-1"},
            [{"field": "barcode", "value": "bar-1", "code": "BARCODE_TAKEN"}],
            id="barcode",
        ),
        pytest.param(
            {"sku_code": "NEW-2", "barcode": "BAR-1", "name": "Tee X"},
            [
                {"field": "sku_code", "value": "NEW-2", "code": "SKU_CODE_TAKEN"},
                {"field": "barcode", "value": "BAR-1", "code": "BARCODE_TAKEN"},
            ],
            id="both",
        ),
    ],
)
def test_a_conflict_with_another_sku_is_409(client, engine, existing, fields, conflicts):
    caller = existing.caller
    stored = state_of(engine, caller.tenant_id)
    response = patch_sku(client, caller, existing.sku_id, {"expected_version": 1} | fields)
    error = error_of(response, 409)
    assert error["code"] == conflicts[0]["code"]
    assert error["details"] == {"conflicts": conflicts}
    assert state_of(engine, caller.tenant_id) == stored


@pytest.fixture
def conflict_check_misses(monkeypatch) -> None:
    """修改的冲突查询一项都报不出来，写入只能撞上唯一约束。"""
    monkeypatch.setattr(catalog_service, "sku_update_conflicts", lambda *args, **kwargs: [])


@pytest.mark.parametrize(
    ("fields", "conflict"),
    [
        pytest.param(
            {"sku_code": "taken-1"},
            {"field": "sku_code", "value": "taken-1", "code": "SKU_CODE_TAKEN"},
            id="current_code",
        ),
        pytest.param(
            {"sku_code": "Old-2"},
            {"field": "sku_code", "value": "Old-2", "code": "SKU_CODE_RETIRED"},
            id="retired_code",
        ),
        pytest.param(
            {"barcode": "bar-2"},
            {"field": "barcode", "value": "bar-2", "code": "BARCODE_TAKEN"},
            id="barcode",
        ),
        pytest.param(
            # 归属表撞上时只列编码那一项。
            {"sku_code": "OLD-2", "barcode": "BAR-1"},
            {"field": "sku_code", "value": "OLD-2", "code": "SKU_CODE_RETIRED"},
            id="code_and_barcode",
        ),
    ],
)
def test_a_unique_constraint_hit_is_409_not_500(
    client, engine, existing, conflict_check_misses, fields, conflict
):
    caller = existing.caller
    stored = state_of(engine, caller.tenant_id)
    response = patch_sku(client, caller, existing.sku_id, {"expected_version": 1} | fields)
    error = error_of(response, 409)
    assert error["code"] == conflict["code"]
    assert error["details"] == {"conflicts": [conflict]}
    assert state_of(engine, caller.tenant_id) == stored


def test_a_code_without_a_claim_row_hits_the_skus_constraint(client, engine, existing):
    """别的 SKU 的当前编码没登记归属（数据不一致）：归属行插得进，改 SKU 行时撞编码约束。"""
    caller = existing.caller
    with engine.begin() as db:
        add_sku(db, caller.tenant_id, existing.product_id, sku_code="LEGACY-1")
    stored = state_of(engine, caller.tenant_id)

    body = {"expected_version": 1, "sku_code": "legacy-1"}
    error = error_of(patch_sku(client, caller, existing.sku_id, body), 409)
    assert error["code"] == "SKU_CODE_TAKEN"
    assert error["details"] == {
        "conflicts": [{"field": "sku_code", "value": "legacy-1", "code": "SKU_CODE_TAKEN"}]
    }
    assert state_of(engine, caller.tenant_id) == stored


# --- 修改被拒：判定顺序 ---


RENAME = {"expected_version": 1, "name": "X"}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PATCH", "/v1/products/{}", RENAME),
        ("POST", "/v1/products/{}/deactivate", None),
        ("POST", "/v1/products/{}/reactivate", None),
        ("PATCH", "/v1/skus/{}", RENAME),
        ("POST", "/v1/skus/{}/deactivate", None),
        ("POST", "/v1/skus/{}/reactivate", None),
    ],
)
def test_a_missing_and_another_tenants_object_get_the_same_404(client, engine, method, path, body):
    caller = issue(engine)
    foreign = issue(engine)
    with engine.begin() as db:
        foreign_product = add_product(db, foreign.tenant_id, status="inactive")
        foreign_sku = add_sku(db, foreign.tenant_id, foreign_product, status="inactive")
    foreign_before = state_of(engine, foreign.tenant_id)
    if path.startswith("/v1/products/"):
        object_type, code, foreign_id = "product", "PRODUCT_NOT_FOUND", foreign_product
        missing_id = new_id("prd")
    else:
        object_type, code, foreign_id = "sku", "SKU_NOT_FOUND", foreign_sku
        missing_id = new_id("sku")

    # 同一个请求 ID：响应完全相同，只有跨租户那次记事件。
    rid = request_id()
    cross_path = path.format(foreign_id)
    cross = send(client, caller, method, cross_path, body, rid=rid)
    missing = send(client, caller, method, path.format(missing_id), body, rid=rid)
    assert error_of(cross, 404)["code"] == code
    assert cross.json() == missing.json()

    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert (event["tenant_id"], event["credential_id"]) == (caller.tenant_id, caller.credential_id)
    assert (event["http_method"], event["path"]) == (method, cross_path)
    assert event["details"] == {"object_type": object_type, "id": foreign_id}
    assert state_of(engine, foreign.tenant_id) == foreign_before
    assert changes_of(engine, caller.tenant_id) == []


def test_a_version_mismatch_is_409_with_the_current_version(client, engine, existing):
    caller = existing.caller
    with engine.begin() as db:
        statement = update(Product).where(Product.product_id == existing.product_id)
        db.execute(statement.values(version=4))
    stored = state_of(engine, caller.tenant_id)

    body = {"expected_version": 3, "name": "X"}
    error = error_of(patch_product(client, caller, existing.product_id, body), 409)
    assert (error["code"], error["details"]) == ("VERSION_CONFLICT", {"current_version": 4})

    # 版本不符同时编码冲突：报 VERSION_CONFLICT。
    body = {"expected_version": 2, "sku_code": "taken-1", "barcode": "bar-1"}
    error = error_of(patch_sku(client, caller, existing.sku_id, body), 409)
    assert (error["code"], error["details"]) == ("VERSION_CONFLICT", {"current_version": 1})
    assert state_of(engine, caller.tenant_id) == stored


def test_the_later_of_two_updates_from_the_same_version_is_rejected(client, engine, existing):
    caller = existing.caller
    first = patch_sku(client, caller, existing.sku_id, {"expected_version": 1, "name": "A"})
    assert data_of(first)["version"] == 2
    stored = state_of(engine, caller.tenant_id)

    second = patch_sku(client, caller, existing.sku_id, {"expected_version": 1, "name": "B"})
    error = error_of(second, 409)
    assert (error["code"], error["details"]) == ("VERSION_CONFLICT", {"current_version": 2})
    assert state_of(engine, caller.tenant_id) == stored
    assert get(client, caller, f"/v1/skus/{existing.sku_id}")["name"] == "A"


@pytest.mark.parametrize(
    ("kind", "body", "loc"),
    [
        pytest.param("product", {"name": "X"}, ["expected_version"], id="no_expected_version"),
        pytest.param(
            "product", {"expected_version": 0, "name": "X"}, ["expected_version"], id="version_0"
        ),
        pytest.param(
            "product",
            {"expected_version": "1", "name": "X"},
            ["expected_version"],
            id="version_string",
        ),
        pytest.param(
            "product", {"expected_version": 1, "reason": "why"}, None, id="product_nothing"
        ),
        pytest.param("product", {"expected_version": 1, "name": None}, ["name"], id="name_null"),
        pytest.param(
            "product", {"expected_version": 1, "status": "inactive"}, ["status"], id="status"
        ),
        pytest.param("sku", {"expected_version": 1}, None, id="sku_nothing"),
        pytest.param(
            "sku", {"expected_version": 1, "sku_code": None}, ["sku_code"], id="code_null"
        ),
        pytest.param("sku", {"expected_version": 1, "name": None}, ["name"], id="sku_name_null"),
        pytest.param(
            "sku", {"expected_version": 1, "barcode": "ü-1"}, ["barcode"], id="barcode_not_ascii"
        ),
        pytest.param(
            "sku",
            {"expected_version": 1, "product_id": new_id("prd")},
            ["product_id"],
            id="product_id",
        ),
        pytest.param(
            "sku",
            {"expected_version": 1, "merged_into_sku_id": None},
            ["merged_into_sku_id"],
            id="merged_into_sku_id",
        ),
        pytest.param(
            "sku", {"expected_version": 1, "status": "active"}, ["status"], id="sku_status"
        ),
    ],
)
def test_invalid_bodies_are_422_before_the_lock(client, engine, existing, kind, body, loc):
    caller = existing.caller
    stored = state_of(engine, caller.tenant_id)
    if kind == "product":
        response = patch_product(client, caller, existing.product_id, body)
    else:
        response = patch_sku(client, caller, existing.sku_id, body)
    error = error_of(response, 422)
    assert error["code"] == "INVALID_REQUEST"
    if loc is not None:
        assert loc in [item["loc"] for item in error["details"]["errors"]]
    assert state_of(engine, caller.tenant_id) == stored
    assert rows(engine, IdempotencyRecord, caller.tenant_id) == []


# --- 已合并的 SKU ---


class Merged(NamedTuple):
    caller: Caller
    canonical_id: str
    merged_id: str  # 编码 MERGED-1、条码 M-BAR，inactive，版本 2


@pytest.fixture
def merged(engine) -> Merged:
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        canonical_id = current_sku(db, caller.tenant_id, product_id, "CANON-1")
        merged_id = current_sku(
            db,
            caller.tenant_id,
            product_id,
            "MERGED-1",
            barcode="M-BAR",
            status="inactive",
            merged_into_sku_id=canonical_id,
            version=2,
        )
    return Merged(caller, canonical_id, merged_id)


@pytest.mark.parametrize("barcode", [None, "  "], ids=["null", "blank"])
def test_a_merged_skus_barcode_can_be_cleared_for_the_canonical_sku(
    client, engine, merged, barcode
):
    caller = merged.caller
    body = {"expected_version": 2, "barcode": barcode, "reason": "free the barcode"}
    data = data_of(patch_sku(client, caller, merged.merged_id, body))
    assert (data["barcode"], data["version"], data["status"]) == (None, 3, "inactive")
    assert data["merged_into_sku_id"] == merged.canonical_id
    [change] = changes_of(engine, caller.tenant_id)
    assert_change(
        change,
        event_type="sku_updated",
        object_id=merged.merged_id,
        version=3,
        changes={"barcode": {"before": "M-BAR", "after": None}},
        reason="free the barcode",
    )

    body = {"expected_version": 1, "barcode": "m-bar"}
    assert data_of(patch_sku(client, caller, merged.canonical_id, body))["barcode"] == "m-bar"


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"name": "X"}, id="name"),
        pytest.param({"barcode": "OTHER-BAR"}, id="barcode_value"),
        pytest.param({"barcode": None, "name": "X"}, id="barcode_and_name"),
        pytest.param({"reference_price": None}, id="price"),
        pytest.param({"sku_code": "MERGED-1"}, id="same_code"),
    ],
)
@pytest.mark.parametrize("expected_version", [2, 7], ids=["version_ok", "version_wrong"])
def test_other_changes_to_a_merged_sku_are_409(client, engine, merged, fields, expected_version):
    caller = merged.caller
    stored = state_of(engine, caller.tenant_id)
    body = {"expected_version": expected_version} | fields
    error = error_of(patch_sku(client, caller, merged.merged_id, body), 409)
    assert error["code"] == "SKU_MERGED"
    assert error["details"] == {"merged_into_sku_id": merged.canonical_id}
    assert state_of(engine, caller.tenant_id) == stored


def test_a_merged_sku_cannot_be_reactivated_and_deactivating_it_changes_nothing(
    client, engine, merged
):
    caller = merged.caller
    before = get(client, caller, f"/v1/skus/{merged.merged_id}")
    stored = state_of(engine, caller.tenant_id)

    error = error_of(command(client, caller, f"/v1/skus/{merged.merged_id}/reactivate"), 409)
    assert error["code"] == "SKU_MERGED"
    response = command(client, caller, f"/v1/skus/{merged.merged_id}/deactivate")
    assert data_of(response) == before
    assert state_of(engine, caller.tenant_id) == stored


# --- 停用与重新启用 ---


def test_deactivating_and_reactivating_a_product_leaves_its_skus_alone(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id, version=3)
        current_sku(db, caller.tenant_id, product_id, "TEE-S")
        current_sku(db, caller.tenant_id, product_id, "TEE-M", status="inactive")
    skus_before = rows(engine, Sku, caller.tenant_id)
    key, rid = new_key(), request_id()
    started = db_now(engine)

    path = f"/v1/products/{product_id}"
    data = data_of(command(client, caller, f"{path}/deactivate", key=key, rid=rid))
    finished = db_now(engine)
    assert (data["status"], data["version"]) == ("inactive", 4)
    assert {sku["product_status"] for sku in data["skus"]} == {"inactive"}
    assert sorted(sku["status"] for sku in data["skus"]) == ["active", "inactive"]
    assert rows(engine, Sku, caller.tenant_id) == skus_before
    row = row_of(engine, Product, caller.tenant_id, product_id)
    assert started <= row["updated_at"] <= finished
    assert data["updated_at"] == format_timestamp(row["updated_at"])

    data = data_of(command(client, caller, f"{path}/reactivate"))
    assert (data["status"], data["version"]) == ("active", 5)
    assert {sku["product_status"] for sku in data["skus"]} == {"active"}
    assert rows(engine, Sku, caller.tenant_id) == skus_before

    deactivated, reactivated = changes_of(engine, caller.tenant_id)
    assert_change(
        deactivated,
        event_type="product_deactivated",
        object_id=product_id,
        version=4,
        object_type="product",
        product_id=product_id,
        changes={"status": {"before": "active", "after": "inactive"}},
        reason=None,
        request_id=rid,
        idempotency_key=key,
    )
    assert_change(
        reactivated,
        event_type="product_reactivated",
        object_id=product_id,
        version=5,
        changes={"status": {"before": "inactive", "after": "active"}},
        reason=None,
    )


def test_deactivating_and_reactivating_a_sku(client, engine, existing):
    caller = existing.caller
    path = f"/v1/skus/{existing.sku_id}"
    started = db_now(engine)
    data = data_of(command(client, caller, f"{path}/deactivate"))
    finished = db_now(engine)
    assert (data["status"], data["product_status"], data["version"]) == ("inactive", "active", 2)
    row = row_of(engine, Sku, caller.tenant_id, existing.sku_id)
    assert started <= row["updated_at"] <= finished
    assert row_of(engine, Product, caller.tenant_id, existing.product_id)["version"] == 1

    data = data_of(command(client, caller, f"{path}/reactivate"))
    assert (data["status"], data["version"]) == ("active", 3)

    deactivated, reactivated = changes_of(engine, caller.tenant_id)
    assert_change(
        deactivated,
        event_type="sku_deactivated",
        object_id=existing.sku_id,
        version=2,
        object_type="sku",
        product_id=existing.product_id,
        changes={"status": {"before": "active", "after": "inactive"}},
        reason=None,
    )
    assert_change(
        reactivated,
        event_type="sku_reactivated",
        object_id=existing.sku_id,
        version=3,
        changes={"status": {"before": "inactive", "after": "active"}},
        reason=None,
    )
    assert rows(engine, SkuCodeHistory, caller.tenant_id) == []


def test_a_sku_can_be_reactivated_while_its_product_is_inactive(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id, status="inactive")
        sku_id = current_sku(db, caller.tenant_id, product_id, "TEE-S", status="inactive")

    data = data_of(command(client, caller, f"/v1/skus/{sku_id}/reactivate"))
    assert (data["status"], data["product_status"], data["version"]) == ("active", "inactive", 2)
    [change] = changes_of(engine, caller.tenant_id)
    assert change["event_type"] == "sku_reactivated"


def test_reaching_a_status_already_held_changes_nothing(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        active_product = add_product(db, caller.tenant_id)
        inactive_product = add_product(db, caller.tenant_id, status="inactive", version=2)
        active_sku = current_sku(db, caller.tenant_id, active_product, "A-1")
        inactive_sku = current_sku(db, caller.tenant_id, active_product, "A-2", status="inactive")
    stored = state_of(engine, caller.tenant_id)

    for path in [
        f"/v1/products/{active_product}/reactivate",
        f"/v1/products/{inactive_product}/deactivate",
        f"/v1/skus/{active_sku}/reactivate",
        f"/v1/skus/{inactive_sku}/deactivate",
    ]:
        current = get(client, caller, path.rsplit("/", 1)[0])
        assert data_of(command(client, caller, path)) == current
    assert state_of(engine, caller.tenant_id) == stored


@pytest.mark.parametrize("action", ["deactivate", "reactivate"])
def test_a_non_empty_body_is_422(client, engine, existing, action):
    caller = existing.caller
    stored = state_of(engine, caller.tenant_id)
    for path in [f"/v1/products/{existing.product_id}", f"/v1/skus/{existing.sku_id}"]:
        response = send(client, caller, "POST", f"{path}/{action}", {})
        assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert state_of(engine, caller.tenant_id) == stored
    assert rows(engine, IdempotencyRecord, caller.tenant_id) == []
