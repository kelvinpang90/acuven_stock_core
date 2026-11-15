"""商品批量导入的预览、确认与查询（契约 §2.3、§2.7、§7.2）。

在真 MySQL 上跑；每个测试建自己的租户与凭据，现有目录由测试直接在库里造（自己提交的连接）。
“什么也没建”按本租户的商品、SKU、归属行与变更记录在请求前后完全相同来判断。
"""

import json
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, literal_column, make_url, select, text, update

from app.config import Settings
from app.domain.catalog import catalog_lock_name
from app.domain.requests import CATALOG_ADMIN, STOCK_ADMIN
from app.domain.service_auth import CATALOG_READ, generate_token, token_digest
from app.main import create_app
from app.models.catalog import CatalogImport
from app.models.inventory import IdempotencyRecord
from app.services import idempotency
from tests.integration.conftest import TEST_DATABASE_URL, add_credential, new_id
from tests.integration.test_api_auth import Caller, error_of, events, issue
from tests.integration.test_catalog_create_api import catalog_of, current_sku, renamed_sku, rows
from tests.integration.test_catalog_queries_api import TIMESTAMP, add_product, request_id
from tests.integration.test_service_auth_schema import token_insert

ADMIN = {"X-Actor-Ref": "admin-7", "X-Actor-Role": CATALOG_ADMIN}
PRICE = {"amount_minor": 12900, "currency": "MYR"}
EMPTY_CATALOG = {"products": [], "skus": [], "claims": [], "changes": []}
RESULTS = (
    "invalid_format",
    "duplicate_sku_code",
    "duplicate_barcode",
    "conflicts_existing",
    "group_blocked",
    "ok",
)
REPRESENTATION_KEYS = {
    "import_id",
    "status",
    "expires_at",
    "created_at",
    "confirmed_at",
    "rows",
    "summary",
    "result",
}
A_SECOND_AGO = literal_column("CURRENT_TIMESTAMP(6) - INTERVAL 1 SECOND")


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


def new_key() -> str:
    return f"shop:import:{uuid4().hex}:1"


def write_headers(caller: Caller, key: str | None = None, rid: str | None = None) -> dict:
    extra = {"Idempotency-Key": key or new_key(), "X-Request-ID": rid or request_id()}
    return caller.auth | ADMIN | extra


def row(external_ref: object, product_ref: object, sku_code: str, **fields: object) -> dict:
    """一行导入；名称由商品引用与编码推出，同组的 product_name 一致。"""
    values = {
        "external_ref": external_ref,
        "product_ref": product_ref,
        "product_name": f"Product {product_ref}",
        "sku_code": sku_code,
        "sku_name": f"Sku {sku_code.strip()}",
    }
    return values | fields


def without(values: dict, *fields: str) -> dict:
    return {name: value for name, value in values.items() if name not in fields}


def preview(client: TestClient, caller: Caller, sent: list, **headers: str | None):
    return client.post(
        "/v1/catalog-imports", json={"rows": sent}, headers=write_headers(caller, **headers)
    )


def data_of(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert set(body["data"]) == REPRESENTATION_KEYS
    return body["data"]


def previewed(client: TestClient, caller: Caller, sent: list, **headers: str | None) -> dict:
    return data_of(preview(client, caller, sent, **headers), 201)


def confirm(client: TestClient, caller: Caller, import_id: str, **headers: str | None):
    url = f"/v1/catalog-imports/{import_id}/confirm"
    return client.post(url, headers=write_headers(caller, **headers))


def get_import(client: TestClient, caller: Caller, import_id: str):
    return client.get(f"/v1/catalog-imports/{import_id}", headers=caller.auth)


def results_of(data: dict) -> list[str]:
    return [line["result"] for line in data["rows"]]


def counts(**given: int) -> dict:
    return {result: given.get(result, 0) for result in RESULTS}


# --- 库里的数据 ---


def stored_import(engine: Engine, import_id: str) -> dict:
    columns = CatalogImport.__table__.columns
    with engine.connect() as db:
        statement = select(*columns).where(CatalogImport.import_id == import_id)
        return dict(db.execute(statement).mappings().one())


def expire(engine: Engine, import_id: str) -> None:
    """把有效期改到数据库时间的一秒之前；status 仍为 previewed。"""
    with engine.begin() as db:
        statement = update(CatalogImport).where(CatalogImport.import_id == import_id)
        db.execute(statement.values(expires_at=A_SECOND_AGO))


def db_now(engine: Engine) -> datetime:
    with engine.connect() as db:
        return db.execute(text("SELECT CURRENT_TIMESTAMP(6)")).scalar()


def parse_time(value: str) -> datetime:
    assert TIMESTAMP.fullmatch(value), value
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")


def reader_of(engine: Engine, caller: Caller) -> Caller:
    """同租户的另一个凭据，只有 catalog.read。"""
    plain = generate_token()
    with engine.begin() as db:
        credential_id = add_credential(db, caller.tenant_id, scopes=[CATALOG_READ])
        digest = token_digest(plain)
        db.execute(token_insert(caller.tenant_id, credential_id, token_digest=digest))
    return Caller(caller.tenant_id, caller.pool_id, credential_id, plain)


# --- 预览 ---


def test_preview_judges_each_row_and_stores_the_import_without_touching_the_catalog(client, engine):
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        current_sku(db, caller.tenant_id, product_id, "TAKEN-1", barcode="BAR-1")
        renamed_sku(db, caller.tenant_id, product_id, "OLD-2", "NEW-2")
    catalog_before = catalog_of(engine, caller.tenant_id)
    sent = [
        row("v-1", "p-1", " A-1 ", barcode=" B-1 ", reference_price=PRICE),
        row("v-2", "p-1", "A-2"),
        row("v-3", "p-2", "taken-1"),
        row("v-4", "p-2", "A-4"),
        without(row("v-5", "p-3", "A-5"), "sku_name"),
        row("v-6", "p-4", "D-1"),
        row("v-7", "p-5", "d-1"),
        row("v-8", "p-6", "A-8", barcode="BC-9"),
        row("v-9", "p-7", "A-9", barcode="bc-9"),
        row("v-10", "p-8", "Old-2"),
        row("v-11", "p-9", "A-11", barcode="bar-1"),
    ]
    key, rid = new_key(), request_id()
    before = db_now(engine)
    data = previewed(client, caller, sent, key=key, rid=rid)
    after = db_now(engine)

    import_id = data["import_id"]
    assert import_id.startswith("imp_")
    assert data["status"] == "previewed"
    assert (data["confirmed_at"], data["result"]) == (None, None)
    # 有效期是数据库时间加 24 小时，与 created_at 在同一条 SQL 里算。
    expires_at = parse_time(data["expires_at"])
    created_at = parse_time(data["created_at"])
    day, slack = timedelta(hours=24), timedelta(seconds=5)
    assert before + day - slack <= expires_at <= after + day + slack
    assert expires_at - created_at == day

    assert results_of(data) == [
        "ok",
        "ok",
        "conflicts_existing",
        "group_blocked",
        "invalid_format",
        "duplicate_sku_code",
        "duplicate_sku_code",
        "duplicate_barcode",
        "duplicate_barcode",
        "conflicts_existing",
        "conflicts_existing",
    ]
    for index, line in enumerate(data["rows"]):
        assert line["index"] == index
        assert line["external_ref"] == sent[index]["external_ref"]
        assert line["product_ref"] == sent[index]["product_ref"]
        assert line["invalid_fields"] == (["sku_name"] if index == 4 else [])
    assert data["summary"] == {
        "counts": counts(
            ok=2,
            invalid_format=1,
            duplicate_sku_code=2,
            duplicate_barcode=2,
            conflicts_existing=3,
            group_blocked=1,
        ),
        "executable_groups": 1,
        "executable_rows": 2,
    }

    stored = stored_import(engine, import_id)
    assert stored["tenant_id"] == caller.tenant_id
    assert stored["status"] == "previewed"
    assert stored["credential_id"] == caller.credential_id
    assert (stored["actor_ref"], stored["request_id"]) == ("admin-7", rid)
    for column in ("result", "confirmed_credential_id", "confirmed_actor_ref"):
        assert stored[column] is None
    assert (stored["confirmed_request_id"], stored["confirmed_at"]) == (None, None)
    assert [json.loads(line["original"]) for line in stored["rows"]] == sent
    assert stored["rows"][0]["values"] == {
        "external_ref": "v-1",
        "product_ref": "p-1",
        "product_name": "Product p-1",
        "sku_code": "A-1",
        "sku_name": "Sku A-1",
        "barcode": "B-1",
        "reference_price": PRICE,
    }
    assert [line["values"] is None for line in stored["rows"]] == [False, False] + [True] * 9

    # 预览不写目录。
    assert catalog_of(engine, caller.tenant_id) == catalog_before
    assert data_of(get_import(client, caller, import_id)) == data


def test_preview_does_not_wait_for_the_catalog_lock(client, engine):
    caller = issue(engine)
    name = catalog_lock_name(make_url(TEST_DATABASE_URL).database, caller.tenant_id)
    with engine.connect() as holder:
        assert holder.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": name}).scalar() == 1
        try:
            started = time.monotonic()
            data = previewed(client, caller, [row("v-1", "p-1", "A-1")])
            elapsed = time.monotonic() - started
        finally:
            holder.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})
    assert results_of(data) == ["ok"]
    # 取目录锁要等 5 秒；没去等。
    assert elapsed < idempotency.CATALOG_LOCK_WAIT_SECONDS / 2


def test_codes_used_only_by_another_tenant_are_ok(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    with engine.begin() as db:
        foreign_product = add_product(db, foreign.tenant_id)
        current_sku(db, foreign.tenant_id, foreign_product, "SHARED-1", barcode="SB-1")
        renamed_sku(db, foreign.tenant_id, foreign_product, "SHARED-OLD", "SHARED-NEW")
    sent = [
        row("v-1", "p-1", "shared-1", barcode="sb-1"),
        row("v-2", "p-2", "SHARED-OLD"),
        row("v-3", "p-3", "SHARED-NEW"),
    ]
    data = previewed(client, caller, sent)
    assert results_of(data) == ["ok", "ok", "ok"]
    assert data["summary"]["executable_groups"] == 3


def test_up_to_100_rows_are_accepted(client, engine):
    caller = issue(engine)
    sent = [row(f"v-{i}", f"p-{i // 4}", f"C-{i}") for i in range(100)]
    summary = previewed(client, caller, sent)["summary"]
    assert summary["counts"] == counts(ok=100)
    assert (summary["executable_groups"], summary["executable_rows"]) == (25, 100)


VALID_ROW = row("v-1", "p-1", "A-1")


@pytest.mark.parametrize(
    ("content", "status"),
    [
        pytest.param(b'{"rows": [', 422, id="not_json"),
        pytest.param(b'{"rows": [{"external_ref": 1.5}]}', 422, id="decimal_number_in_a_row"),
        pytest.param(
            b'{"rows": [{"sku_code": "A", "sku_code": "B"}]}', 422, id="duplicate_key_in_a_row"
        ),
        pytest.param(json.dumps({"rows": [VALID_ROW], "note": "x"}), 422, id="extra_field"),
        pytest.param(json.dumps({}), 422, id="rows_missing"),
        pytest.param(json.dumps({"rows": VALID_ROW}), 422, id="rows_object"),
        pytest.param(json.dumps({"rows": None}), 422, id="rows_null"),
        pytest.param(json.dumps({"rows": []}), 422, id="rows_empty"),
        pytest.param(json.dumps({"rows": [VALID_ROW, "x"]}), 422, id="element_string"),
        pytest.param(json.dumps({"rows": [VALID_ROW, [VALID_ROW]]}), 422, id="element_array"),
        pytest.param(json.dumps({"rows": [VALID_ROW, None]}), 422, id="element_null"),
        pytest.param(
            json.dumps({"rows": [row(f"v-{i}", f"p-{i}", f"A-{i}") for i in range(101)]}),
            413,
            id="101_rows",
        ),
    ],
)
def test_the_whole_preview_is_rejected_and_nothing_is_stored(client, engine, content, status):
    caller = issue(engine)
    headers = write_headers(caller) | {"Content-Type": "application/json"}
    response = client.post("/v1/catalog-imports", content=content, headers=headers)
    error = error_of(response, status)
    assert error["code"] == ("INVALID_REQUEST" if status == 422 else "REQUEST_TOO_LARGE")
    assert rows(engine, CatalogImport, caller.tenant_id) == []
    assert rows(engine, IdempotencyRecord, caller.tenant_id) == []


def test_row_problems_only_mark_their_own_row(client, engine):
    caller = issue(engine)
    deep: list = []
    for _ in range(119):
        deep = [deep]  # 嵌套 120 层的数组，超过 MySQL JSON 列的上限
    huge = 2**70  # 超出 64 位
    sent = [
        row("v-1", "p-1", "A-1"),
        row(huge, "p-2", "A-2", sku_name=deep),
        {"external_ref": "v-3", "product_ref": "p-3"},
        row("v-4", 7, "A-4"),
        row("v-5", "p-5", "A-5", barcode=12, reference_price=PRICE | {"amount_minor": -1}, x=1),
        row("v-dup", "p-6", "A-6"),
        row("v-dup", "p-7", "A-7"),
    ]
    data = previewed(client, caller, sent)

    assert results_of(data) == ["ok"] + ["invalid_format"] * 6
    assert [line["invalid_fields"] for line in data["rows"]] == [
        [],
        ["external_ref", "sku_name"],
        ["product_name", "sku_code", "sku_name"],
        ["product_ref"],
        ["barcode", "reference_price", "unknown_field"],
        ["external_ref"],
        ["external_ref"],
    ]
    # 不是字符串的引用回显为 null。
    assert [line["external_ref"] for line in data["rows"]] == [
        "v-1",
        None,
        "v-3",
        "v-4",
        "v-5",
        "v-dup",
        "v-dup",
    ]
    assert [line["product_ref"] for line in data["rows"]] == [
        "p-1",
        "p-2",
        "p-3",
        None,
        "p-5",
        "p-6",
        "p-7",
    ]
    assert data["summary"] == {
        "counts": counts(ok=1, invalid_format=6),
        "executable_groups": 1,
        "executable_rows": 1,
    }

    # 原文按字符串存，与发来的完全一致（大整数不丢精度，深嵌套照存）。
    stored = stored_import(engine, data["import_id"])
    originals = [json.loads(line["original"]) for line in stored["rows"]]
    assert originals == sent
    assert originals[1]["external_ref"] == huge
    assert data_of(get_import(client, caller, data["import_id"])) == data


# --- 确认 ---


def test_confirm_creates_each_clean_group_and_reports_every_row(client, engine):
    caller = issue(engine)
    sent = [
        row("v-0", "p-1", "G1-A", barcode="G1-BAR", reference_price=PRICE),
        row("v-1", "p-2", "G2-A"),
        row("v-2", "p-1", "G1-B"),
        row("v-3", "p-2", "G2-B"),
        row("v-4", "p-3", "G3-A"),
        row("v-5", "p-4", "G4-A"),
        without(row("v-6", "p-4", "G4-B"), "product_name"),
    ]
    preview_data = previewed(client, caller, sent)
    import_id = preview_data["import_id"]
    assert results_of(preview_data) == ["ok"] * 5 + ["group_blocked", "invalid_format"]

    # 预览之后，G2 的一个编码被占用。
    with engine.begin() as db:
        occupied_product = add_product(db, caller.tenant_id)
        current_sku(db, caller.tenant_id, occupied_product, "g2-a")
    catalog_before = catalog_of(engine, caller.tenant_id)

    key, rid = new_key(), request_id()
    data = data_of(confirm(client, caller, import_id, key=key, rid=rid))
    assert data["status"] == "confirmed"
    parse_time(data["confirmed_at"])
    for field in ("import_id", "expires_at", "created_at", "rows", "summary"):
        assert data[field] == preview_data[field]

    result = data["result"]
    assert [
        (line["index"], line["external_ref"], line["product_ref"], line["outcome"])
        for line in result
    ] == [
        (0, "v-0", "p-1", "created"),
        (1, "v-1", "p-2", "failed"),
        (2, "v-2", "p-1", "created"),
        (3, "v-3", "p-2", "failed"),
        (4, "v-4", "p-3", "created"),
        (5, "v-5", "p-4", "skipped"),
        (6, "v-6", "p-4", "skipped"),
    ]
    assert [(line["preview_result"], line["failure"]) for line in result] == [
        (None, None),
        (None, "conflicts_existing"),
        (None, None),
        (None, "group_blocked"),
        (None, None),
        ("group_blocked", None),
        ("invalid_format", None),
    ]
    for line in result:
        assert set(line) == {
            "index",
            "external_ref",
            "product_ref",
            "outcome",
            "product_id",
            "sku_id",
            "preview_result",
            "failure",
        }
        if line["outcome"] == "created":
            assert line["product_id"].startswith("prd_")
            assert line["sku_id"].startswith("sku_")
        else:
            assert (line["product_id"], line["sku_id"]) == (None, None)
    g1_product = result[0]["product_id"]
    g3_product = result[4]["product_id"]
    assert result[2]["product_id"] == g1_product
    assert g1_product != g3_product

    # 目录里多了 G1、G3 两个商品与它们的 SKU，G2 一个都没建。
    catalog_after = catalog_of(engine, caller.tenant_id)
    new_products = [p for p in catalog_after["products"] if p not in catalog_before["products"]]
    assert {(p["product_id"], p["name"]) for p in new_products} == {
        (g1_product, "Product p-1"),
        (g3_product, "Product p-3"),
    }
    assert all((p["status"], p["version"]) == ("active", 1) for p in new_products)
    new_skus = {s["sku_id"]: s for s in catalog_after["skus"] if s not in catalog_before["skus"]}
    assert set(new_skus) == {result[i]["sku_id"] for i in (0, 2, 4)}
    g1_a = new_skus[result[0]["sku_id"]]
    assert (g1_a["product_id"], g1_a["sku_code"]) == (g1_product, "G1-A")
    assert (g1_a["name"], g1_a["barcode"]) == ("Sku G1-A", "G1-BAR")
    assert (g1_a["reference_price_amount_minor"], g1_a["reference_price_currency"]) == (
        12900,
        "MYR",
    )
    assert new_skus[result[2]["sku_id"]]["sku_code"] == "G1-B"
    assert new_skus[result[4]["sku_id"]]["product_id"] == g3_product
    new_claims = {c["code"]: c["sku_id"] for c in catalog_after["claims"]}
    assert new_claims["G1-A"] == result[0]["sku_id"]
    assert "G2-B" not in new_claims

    # 变更记录按组的顺序，都带 import_id。
    changes = catalog_after["changes"][len(catalog_before["changes"]) :]
    assert [(c["event_type"], c["object_id"]) for c in changes] == [
        ("product_created", g1_product),
        ("sku_created", result[0]["sku_id"]),
        ("sku_created", result[2]["sku_id"]),
        ("product_created", g3_product),
        ("sku_created", result[4]["sku_id"]),
    ]
    for change in changes:
        assert change["import_id"] == import_id
        assert change["credential_id"] == caller.credential_id
        assert (change["actor_ref"], change["actor_role"]) == ("admin-7", CATALOG_ADMIN)
        assert (change["request_id"], change["idempotency_key"]) == (rid, key)
        assert change["reason"] is None
        assert change["version"] == 1

    stored = stored_import(engine, import_id)
    assert stored["status"] == "confirmed"
    assert stored["confirmed_credential_id"] == caller.credential_id
    assert (stored["confirmed_actor_ref"], stored["confirmed_request_id"]) == ("admin-7", rid)
    assert stored["confirmed_at"] is not None
    assert stored["result"] == {"rows": result}
    assert data_of(get_import(client, caller, import_id)) == data


def test_an_import_that_builds_nothing_is_still_confirmed(client, engine):
    caller = issue(engine)
    sent = [without(row("v-1", "p-1", "A-1"), "sku_code"), row("v-2", "p-2", "A-2")]
    import_id = previewed(client, caller, sent)["import_id"]
    with engine.begin() as db:
        current_sku(db, caller.tenant_id, add_product(db, caller.tenant_id), "a-2")
    catalog_before = catalog_of(engine, caller.tenant_id)

    data = data_of(confirm(client, caller, import_id))
    assert data["status"] == "confirmed"
    outcomes = [
        (line["outcome"], line["preview_result"], line["failure"]) for line in data["result"]
    ]
    assert outcomes == [
        ("skipped", "invalid_format", None),
        ("failed", None, "conflicts_existing"),
    ]
    assert catalog_of(engine, caller.tenant_id) == catalog_before
    assert stored_import(engine, import_id)["status"] == "confirmed"


def test_a_missing_and_another_tenants_import_get_the_same_404(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    foreign_import = previewed(client, foreign, [row("v-1", "p-1", "A-1")])["import_id"]
    foreign_before = stored_import(engine, foreign_import)

    # 同一个请求 ID：响应完全相同，只有跨租户那次记事件。
    rid = request_id()
    cross = confirm(client, caller, foreign_import, rid=rid)
    missing = confirm(client, caller, new_id("imp"), rid=rid)
    assert error_of(cross, 404)["code"] == "IMPORT_NOT_FOUND"
    assert cross.json() == missing.json()

    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert (event["tenant_id"], event["credential_id"]) == (caller.tenant_id, caller.credential_id)
    assert (event["http_method"], event["path"]) == (
        "POST",
        f"/v1/catalog-imports/{foreign_import}/confirm",
    )
    assert event["details"] == {"object_type": "import", "id": foreign_import}
    assert stored_import(engine, foreign_import) == foreign_before
    assert catalog_of(engine, foreign.tenant_id) == EMPTY_CATALOG
    assert catalog_of(engine, caller.tenant_id) == EMPTY_CATALOG


def test_confirming_again_returns_the_current_representation_without_building(client, engine):
    caller = issue(engine)
    import_id = previewed(client, caller, [row("v-1", "p-1", "A-1")])["import_id"]
    key, rid = new_key(), request_id()
    first = confirm(client, caller, import_id, key=key, rid=rid)
    data = data_of(first)
    assert [line["outcome"] for line in data["result"]] == ["created"]
    catalog_before = catalog_of(engine, caller.tenant_id)
    stored_before = stored_import(engine, import_id)

    # 换新键：200 与当前表示，不再执行；过了有效期也一样（已确认的判定在前）。
    again = confirm(client, caller, import_id)
    assert "Idempotency-Replayed" not in again.headers
    assert data_of(again) == data
    expire(engine, import_id)
    after_expiry = data_of(confirm(client, caller, import_id))
    assert after_expiry["status"] == "confirmed"
    assert after_expiry["result"] == data["result"]
    assert catalog_of(engine, caller.tenant_id) == catalog_before
    stored_after = stored_import(engine, import_id)
    assert stored_after["confirmed_request_id"] == rid
    assert without(stored_after, "expires_at") == without(stored_before, "expires_at")

    # 同键重试原样重放首次的响应。
    replay = confirm(client, caller, import_id, key=key)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert catalog_of(engine, caller.tenant_id) == catalog_before


def test_an_expired_import_is_409_and_builds_nothing(client, engine):
    caller = issue(engine)
    import_id = previewed(client, caller, [row("v-1", "p-1", "A-1")])["import_id"]
    expire(engine, import_id)

    error = error_of(confirm(client, caller, import_id), 409)
    assert error["code"] == "IMPORT_EXPIRED"
    assert catalog_of(engine, caller.tenant_id) == EMPTY_CATALOG
    stored = stored_import(engine, import_id)
    assert (stored["status"], stored["result"], stored["confirmed_at"]) == ("previewed", None, None)


def test_confirm_takes_no_body(client, engine):
    caller = issue(engine)
    import_id = previewed(client, caller, [row("v-1", "p-1", "A-1")])["import_id"]
    key = new_key()
    headers = write_headers(caller, key=key) | {"Content-Type": "application/json"}
    url = f"/v1/catalog-imports/{import_id}/confirm"
    error = error_of(client.post(url, content=b"{}", headers=headers), 422)
    assert error["code"] == "INVALID_REQUEST"
    assert stored_import(engine, import_id)["status"] == "previewed"
    assert catalog_of(engine, caller.tenant_id) == EMPTY_CATALOG

    # 拒绝没保存，同键不带请求体照常确认。
    assert data_of(confirm(client, caller, import_id, key=key))["status"] == "confirmed"


# --- 查询 ---


def test_get_shows_previewed_expired_and_confirmed_with_only_catalog_read(client, engine):
    caller = issue(engine)
    reader = reader_of(engine, caller)
    previewed_data = previewed(client, caller, [row("v-1", "p-1", "A-1")])
    expired_data = previewed(client, caller, [row("v-1", "p-1", "B-1")])
    confirmed_import = previewed(client, caller, [row("v-1", "p-1", "C-1")])["import_id"]
    confirmed_data = data_of(confirm(client, caller, confirmed_import))
    expire(engine, expired_data["import_id"])

    # 只有 catalog.read、不带操作者头。
    assert data_of(get_import(client, reader, previewed_data["import_id"])) == previewed_data

    got = data_of(get_import(client, reader, expired_data["import_id"]))
    assert got["status"] == "expired"
    assert parse_time(got["expires_at"]) < parse_time(expired_data["expires_at"])
    assert without(got, "status", "expires_at") == without(expired_data, "status", "expires_at")
    # 库里仍是 previewed。
    assert stored_import(engine, expired_data["import_id"])["status"] == "previewed"

    assert data_of(get_import(client, reader, confirmed_import)) == confirmed_data


def test_get_of_a_missing_and_another_tenants_import_is_the_same_404(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    foreign_import = previewed(client, foreign, [row("v-1", "p-1", "A-1")])["import_id"]

    rid = request_id()
    headers = caller.auth | {"X-Request-ID": rid}
    cross = client.get(f"/v1/catalog-imports/{foreign_import}", headers=headers)
    missing = client.get(f"/v1/catalog-imports/{new_id('imp')}", headers=headers)
    assert error_of(cross, 404)["code"] == "IMPORT_NOT_FOUND"
    assert cross.json() == missing.json()

    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert event["http_method"] == "GET"
    assert event["path"] == f"/v1/catalog-imports/{foreign_import}"
    assert event["details"] == {"object_type": "import", "id": foreign_import}


# --- 授权 ---


def test_preview_and_confirm_need_catalog_write(client, engine):
    caller = issue(engine)
    reader = reader_of(engine, caller)
    import_id = previewed(client, caller, [row("v-1", "p-1", "A-1")])["import_id"]
    for response in (
        preview(client, reader, [row("v-2", "p-2", "A-2")]),
        confirm(client, reader, import_id),
    ):
        assert error_of(response, 403)["code"] == "FORBIDDEN"
    assert len(rows(engine, CatalogImport, caller.tenant_id)) == 1
    assert stored_import(engine, import_id)["status"] == "previewed"


def test_preview_and_confirm_need_the_catalog_admin_role(client, engine):
    caller = issue(engine)
    import_id = previewed(client, caller, [row("v-1", "p-1", "A-1")])["import_id"]
    role = {"X-Actor-Role": STOCK_ADMIN}
    for url, body in (
        ("/v1/catalog-imports", {"rows": [row("v-2", "p-2", "A-2")]}),
        (f"/v1/catalog-imports/{import_id}/confirm", None),
    ):
        rid = request_id()
        response = client.post(url, json=body, headers=write_headers(caller, rid=rid) | role)
        assert error_of(response, 403)["code"] == "FORBIDDEN"
        [event] = events(engine, rid)
        assert event["details"] == {"category": "role", "role": STOCK_ADMIN}
    assert len(rows(engine, CatalogImport, caller.tenant_id)) == 1
    assert stored_import(engine, import_id)["status"] == "previewed"
