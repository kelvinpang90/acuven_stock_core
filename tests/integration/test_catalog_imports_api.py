"""批量导入的预览、确认与查询（契约 §2.3、§2.7、§7.2、§10 第 4 条）。

在真 MySQL 上跑；每个测试建自己的租户与凭据，现有目录由测试直接在库里造（自己提交的连接）。
“目录没变”按本租户的商品、SKU、归属行与变更记录在请求前后完全相同来判断。
"""

import json
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update

from app.config import Settings
from app.domain.requests import CATALOG_ADMIN, STOCK_ADMIN
from app.domain.service_auth import CATALOG_READ, CATALOG_WRITE, generate_token, token_digest
from app.main import create_app
from app.models.catalog import CatalogImport, Product
from app.models.inventory import IdempotencyRecord
from tests.integration.conftest import TEST_DATABASE_URL, add_credential, new_id
from tests.integration.test_api_auth import A_SECOND_AGO, Caller, error_of, events, issue
from tests.integration.test_catalog_create_api import (
    PRICE,
    catalog_of,
    changes_of,
    created,
    current_sku,
    new_key,
    renamed_sku,
    rows,
    write_headers,
)
from tests.integration.test_catalog_queries_api import TIMESTAMP, add_product, data_of, request_id
from tests.integration.test_catalog_updates_api import db_now
from tests.integration.test_idempotency import catalog_lock, get_lock, release_lock
from tests.integration.test_service_auth_schema import token_insert

IMPORTS = "/v1/catalog-imports"
RESULTS = (
    "invalid_format",
    "duplicate_sku_code",
    "duplicate_barcode",
    "conflicts_existing",
    "group_blocked",
    "ok",
)
REPRESENTATION = {
    "import_id",
    "status",
    "expires_at",
    "created_at",
    "confirmed_at",
    "rows",
    "summary",
    "result",
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


# --- 请求 ---


def row(external_ref: object, product_ref: object, code: str, **fields: object) -> dict:
    """一行导入；同一 product_ref 的行商品名称相同，fields 覆盖或追加字段。"""
    return {
        "external_ref": external_ref,
        "product_ref": product_ref,
        "product_name": f"Product {product_ref}",
        "sku_code": code,
        "sku_name": f"Sku {code}",
    } | fields


def post_preview(client: TestClient, caller: Caller, sent: list, **headers: str):
    return client.post(IMPORTS, json={"rows": sent}, headers=write_headers(caller, **headers))


def preview(client: TestClient, caller: Caller, sent: list) -> dict:
    return created(post_preview(client, caller, sent))


def post_confirm(client: TestClient, caller: Caller, import_id: str, **headers: str):
    url = f"{IMPORTS}/{import_id}/confirm"
    return client.post(url, headers=write_headers(caller, **headers))


def confirm(client: TestClient, caller: Caller, import_id: str) -> dict:
    return data_of(post_confirm(client, caller, import_id))


def get_import(client: TestClient, caller: Caller, import_id: str) -> dict:
    return data_of(client.get(f"{IMPORTS}/{import_id}", headers=caller.auth))


def results(data: dict) -> list[str]:
    return [line["result"] for line in data["rows"]]


def outcomes(data: dict) -> list[tuple]:
    return [(line["outcome"], line["preview_result"], line["failure"]) for line in data["result"]]


def counts(**given: int) -> dict:
    """六种结果各自的行数，没给的为 0。"""
    return {result: given.get(result, 0) for result in RESULTS}


def parse_timestamp(value: str) -> datetime:
    assert TIMESTAMP.fullmatch(value), value
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")


# --- 库里的数据 ---


def stored_import(engine: Engine, tenant_id: str, import_id: str) -> dict:
    stored = rows(engine, CatalogImport, tenant_id)
    [found] = [line for line in stored if line["import_id"] == import_id]
    return found


def expire(engine: Engine, import_id: str) -> None:
    """把有效期改到数据库时间之前。"""
    with engine.begin() as db:
        statement = update(CatalogImport).where(CatalogImport.import_id == import_id)
        db.execute(statement.values(expires_at=A_SECOND_AGO))


def reader_of(engine: Engine, caller: Caller) -> Caller:
    """同租户另一个只有 catalog.read 的凭据。"""
    plain = generate_token()
    with engine.begin() as db:
        credential_id = add_credential(db, caller.tenant_id, scopes=[CATALOG_READ])
        db.execute(token_insert(caller.tenant_id, credential_id, token_digest=token_digest(plain)))
    return Caller(caller.tenant_id, caller.pool_id, credential_id, plain)


@pytest.fixture
def existing(engine) -> Caller:
    """调用方的租户里已有一个商品与它的两个 SKU。

    一个的当前编码是 TAKEN-1、条码 BAR-1；另一个从 OLD-2 改成了 NEW-2、条码 BAR-2。
    """
    caller = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, caller.tenant_id)
        current_sku(db, caller.tenant_id, product_id, "TAKEN-1", barcode="BAR-1")
        renamed_sku(db, caller.tenant_id, product_id, "OLD-2", "NEW-2", barcode="BAR-2")
    return caller


# --- 预览 ---


def test_a_preview_judges_each_row_and_stores_the_import(client, engine, existing):
    caller = existing
    before = catalog_of(engine, caller.tenant_id)
    sent = [
        row("v-0", "p-1", "A-0", barcode=" B-0 ", reference_price=PRICE),
        row("v-1", "p-1", "A-1"),
        row("v-2", "p-2", "taken-1"),
        row("v-3", "p-2", "A-3"),
        {"external_ref": "v-4", "product_ref": "p-3", "product_name": "P", "sku_code": "A-4"},
        row("v-5", "p-4", "DUP-1"),
        row("v-6", "p-5", " dup-1 "),
        row("v-7", "p-6", "A-7", barcode="DB-1"),
        row("v-8", "p-7", "A-8", barcode="db-1"),
        row("v-9", "p-8", "Old-2"),
        row("v-10", "p-9", "A-10", barcode="bar-2"),
    ]
    key, rid = new_key(), request_id()
    data = created(post_preview(client, caller, sent, key=key, rid=rid))
    now = db_now(engine)

    assert set(data) == REPRESENTATION
    assert data["import_id"].startswith("imp_")
    assert (data["status"], data["confirmed_at"], data["result"]) == ("previewed", None, None)
    expected = [
        "ok",
        "ok",
        "conflicts_existing",  # 现有 SKU 的当前编码，大小写不同
        "group_blocked",
        "invalid_format",  # 缺 sku_name
        "duplicate_sku_code",
        "duplicate_sku_code",
        "duplicate_barcode",
        "duplicate_barcode",
        "conflicts_existing",  # 退役编码
        "conflicts_existing",  # 现有 SKU 的当前条码
    ]
    assert data["rows"] == [
        {
            "index": index,
            "external_ref": line["external_ref"],
            "product_ref": line["product_ref"],
            "result": result,
            "invalid_fields": ["sku_name"] if result == "invalid_format" else [],
        }
        for index, (line, result) in enumerate(zip(sent, expected, strict=True))
    ]
    assert data["summary"] == {
        "counts": counts(
            invalid_format=1,
            duplicate_sku_code=2,
            duplicate_barcode=2,
            conflicts_existing=3,
            group_blocked=1,
            ok=2,
        ),
        "executable_groups": 1,
        "executable_rows": 2,
    }

    # 有效期在插入的同一条语句里取数据库时间：与创建时间正好差 24 小时，与数据库时间只差几秒。
    expires_at = parse_timestamp(data["expires_at"])
    created_at = parse_timestamp(data["created_at"])
    assert expires_at - created_at == timedelta(hours=24)
    assert abs(expires_at - (now + timedelta(hours=24))) < timedelta(seconds=10)

    stored = stored_import(engine, caller.tenant_id, data["import_id"])
    assert stored["status"] == "previewed"
    assert stored["credential_id"] == caller.credential_id
    assert (stored["actor_ref"], stored["request_id"]) == ("admin-7", rid)
    assert (stored["expires_at"], stored["created_at"]) == (expires_at, created_at)
    assert stored["result"] is None
    assert stored["confirmed_credential_id"] is None and stored["confirmed_at"] is None
    assert [json.loads(line["source"]) for line in stored["rows"]] == sent
    # 合格行另存规范化后的字段值，确认时按它建。
    assert stored["rows"][0]["values"] == {
        "external_ref": "v-0",
        "product_ref": "p-1",
        "product_name": "Product p-1",
        "sku_code": "A-0",
        "sku_name": "Sku A-0",
        "barcode": "B-0",
        "reference_price": PRICE,
    }

    # 预览不写目录。
    assert catalog_of(engine, caller.tenant_id) == before
    assert get_import(client, caller, data["import_id"]) == data


def test_codes_and_barcodes_of_another_tenant_do_not_conflict(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    with engine.begin() as db:
        product_id = add_product(db, foreign.tenant_id)
        current_sku(db, foreign.tenant_id, product_id, "SHARED-1", barcode="SB-1")
        renamed_sku(db, foreign.tenant_id, product_id, "SHARED-OLD", "SHARED-NEW")

    sent = [row("v-0", "p-1", "shared-1", barcode="sb-1"), row("v-1", "p-1", "SHARED-OLD")]
    assert results(preview(client, caller, sent)) == ["ok", "ok"]


def test_a_preview_does_not_take_the_catalog_lock(client, engine):
    caller = issue(engine)
    name = catalog_lock(caller)
    with engine.connect() as holder:
        assert get_lock(holder, name) == 1
        try:
            data = preview(client, caller, [row("v-0", "p-1", "A-0")])
        finally:
            release_lock(holder, name)
    assert results(data) == ["ok"]


def test_up_to_100_rows_are_accepted(client, engine):
    caller = issue(engine)
    sent = [row(f"v-{i}", f"p-{i}", f"A-{i}") for i in range(100)]
    data = preview(client, caller, sent)
    assert data["summary"]["counts"] == counts(ok=100)


def body_of(value: object) -> bytes:
    return json.dumps(value).encode()


@pytest.mark.parametrize(
    ("content", "status"),
    [
        pytest.param(b'{"rows": [', 422, id="not_json"),
        pytest.param(body_of({"rows": [row("v-0", "p-1", "A-0")], "note": "x"}), 422, id="extra"),
        pytest.param(body_of({}), 422, id="no_rows"),
        pytest.param(body_of({"rows": row("v-0", "p-1", "A-0")}), 422, id="rows_object"),
        pytest.param(body_of({"rows": []}), 422, id="rows_empty"),
        pytest.param(body_of({"rows": [row("v-0", "p-1", "A-0"), "v-1"]}), 422, id="string_row"),
        pytest.param(body_of({"rows": [[row("v-0", "p-1", "A-0")]]}), 422, id="array_row"),
        pytest.param(body_of({"rows": [None]}), 422, id="null_row"),
        pytest.param(
            body_of({"rows": [row(f"v-{i}", f"p-{i}", f"A-{i}") for i in range(101)]}),
            413,
            id="101_rows",
        ),
    ],
)
def test_a_bad_body_is_rejected_as_a_whole(client, engine, content, status):
    caller = issue(engine)
    headers = write_headers(caller) | {"Content-Type": "application/json"}
    response = client.post(IMPORTS, content=content, headers=headers)
    code = "REQUEST_TOO_LARGE" if status == 413 else "INVALID_REQUEST"
    assert error_of(response, status)["code"] == code
    assert rows(engine, CatalogImport, caller.tenant_id) == []
    assert rows(engine, IdempotencyRecord, caller.tenant_id) == []


def test_problems_inside_a_row_only_make_that_row_invalid_format(client, engine):
    caller = issue(engine)
    nested: object = "x"
    for _ in range(120):
        nested = [nested]
    price = {"amount_minor": 2**64, "currency": "MYR"}
    sent = [
        row("v-0", "p-0", "A-0"),
        # 嵌套超过 MySQL JSON 列的 100 层、整数超出 64 位：原值不能放进 JSON 列。
        row(2**70, "p-1", "A-1", sku_name=nested),
        {"external_ref": "v-2", "product_ref": "p-2", "product_name": "P", "sku_code": "A-2"},
        row("v-3", "p-3", "A-3", colour="red"),
        row("v-4", {"id": 4}, "A-4"),
        row("v-5", 5, "A-5", barcode=7, reference_price=price),
    ]
    content = body_of({"rows": sent})
    key = new_key()
    headers = write_headers(caller, key=key) | {"Content-Type": "application/json"}
    response = client.post(IMPORTS, content=content, headers=headers)
    data = created(response)

    # 不是字符串的 external_ref、product_ref 回显为 null。
    shown = [
        (line["external_ref"], line["product_ref"], line["result"], line["invalid_fields"])
        for line in data["rows"]
    ]
    assert shown == [
        ("v-0", "p-0", "ok", []),
        (None, "p-1", "invalid_format", ["external_ref", "sku_name"]),
        ("v-2", "p-2", "invalid_format", ["sku_name"]),
        ("v-3", "p-3", "invalid_format", ["unknown_field"]),
        ("v-4", None, "invalid_format", ["product_ref"]),
        ("v-5", None, "invalid_format", ["product_ref", "barcode", "reference_price"]),
    ]
    assert data["summary"] == {
        "counts": counts(invalid_format=5, ok=1),
        "executable_groups": 1,
        "executable_rows": 1,
    }

    # 库里存的原文与发来的一致：没有被截断层数、也没有转成浮点。
    stored = stored_import(engine, caller.tenant_id, data["import_id"])
    sources = [json.loads(line["source"]) for line in stored["rows"]]
    assert sources == sent
    assert sources[1]["external_ref"] == 2**70 and isinstance(sources[1]["external_ref"], int)
    assert sources[1]["sku_name"] == nested
    assert stored["rows"][1]["external_ref"] is None

    # 响应进了幂等记录：同键原样重放。
    replay = client.post(IMPORTS, content=content, headers=headers)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == response.json()


# --- 确认 ---


def test_confirming_builds_each_clean_group_and_records_the_import(client, engine):
    caller = issue(engine)
    sent = [
        row("v-0", "p-1", "A-0", barcode="B-0", reference_price=PRICE),
        row("v-1", "p-2", "A-1"),
        row("v-2", "p-1", "A-2"),
        row("v-3", "p-2", "A-3"),
        row("v-4", "p-3", "A-4"),
        row("v-5", "p-4", "A-5", sku_name=None),
        row("v-6", "p-4", "A-6"),
    ]
    previewed = preview(client, caller, sent)
    assert results(previewed) == ["ok"] * 5 + ["invalid_format", "group_blocked"]

    # 预览之后 A-3 被别的商品占用（大小写不同）。
    with engine.begin() as db:
        occupant = add_product(db, caller.tenant_id)
        current_sku(db, caller.tenant_id, occupant, "a-3")

    import_id = previewed["import_id"]
    key, rid = new_key(), request_id()
    data = data_of(post_confirm(client, caller, import_id, key=key, rid=rid))

    assert set(data) == REPRESENTATION
    assert data["status"] == "confirmed"
    confirmed_at = parse_timestamp(data["confirmed_at"])
    assert confirmed_at >= parse_timestamp(data["created_at"])
    # 预览的逐行结果与汇总不因确认而变。
    assert (data["rows"], data["summary"]) == (previewed["rows"], previewed["summary"])

    result = data["result"]
    assert [line["index"] for line in result] == list(range(len(sent)))
    refs = [(line["external_ref"], line["product_ref"]) for line in sent]
    assert [(line["external_ref"], line["product_ref"]) for line in result] == refs
    assert outcomes(data) == [
        ("created", None, None),
        ("failed", None, "group_blocked"),
        ("created", None, None),
        ("failed", None, "conflicts_existing"),
        ("created", None, None),
        ("skipped", "invalid_format", None),
        ("skipped", "group_blocked", None),
    ]
    for line in result:
        if line["outcome"] != "created":
            assert (line["product_id"], line["sku_id"]) == (None, None)

    # 同组的行建在同一个商品下；商品名称取 product_name，SKU 名称取 sku_name。
    first_product, second_product = result[0]["product_id"], result[4]["product_id"]
    assert result[2]["product_id"] == first_product != second_product
    product = data_of(client.get(f"/v1/products/{first_product}", headers=caller.auth))
    assert product["name"] == "Product p-1"
    skus = {sku["sku_id"]: sku for sku in product["skus"]}
    assert set(skus) == {result[0]["sku_id"], result[2]["sku_id"]}
    sku = skus[result[0]["sku_id"]]
    assert (sku["sku_code"], sku["name"], sku["barcode"]) == ("A-0", "Sku A-0", "B-0")
    assert (sku["reference_price"], sku["status"], sku["version"]) == (PRICE, "active", 1)
    assert skus[result[2]["sku_id"]]["sku_code"] == "A-2"

    # p-2 整组没建：本租户只有占用者与新建的两个商品。
    products = {line["product_id"] for line in rows(engine, Product, caller.tenant_id)}
    assert products == {occupant, first_product, second_product}

    # 按组在 rows 里首次出现的顺序写变更记录，都带 import_id。
    changes = changes_of(engine, caller.tenant_id)
    assert [(change["event_type"], change["object_id"]) for change in changes] == [
        ("product_created", first_product),
        ("sku_created", result[0]["sku_id"]),
        ("sku_created", result[2]["sku_id"]),
        ("product_created", second_product),
        ("sku_created", result[4]["sku_id"]),
    ]
    for change in changes:
        assert change["import_id"] == import_id
        assert change["credential_id"] == caller.credential_id
        assert (change["actor_ref"], change["actor_role"]) == ("admin-7", CATALOG_ADMIN)
        assert (change["request_id"], change["idempotency_key"]) == (rid, key)
        assert change["reason"] is None

    stored = stored_import(engine, caller.tenant_id, import_id)
    assert stored["status"] == "confirmed"
    assert stored["confirmed_credential_id"] == caller.credential_id
    assert (stored["confirmed_actor_ref"], stored["confirmed_request_id"]) == ("admin-7", rid)
    assert stored["confirmed_at"] == confirmed_at
    assert stored["result"] == result
    assert get_import(client, caller, import_id) == data


def test_an_import_with_no_group_built_is_still_confirmed(client, engine):
    caller = issue(engine)
    sent = [row("v-0", "p-1", "A-0"), row("v-1", "p-2", "A-1", sku_name=" ")]
    import_id = preview(client, caller, sent)["import_id"]
    with engine.begin() as db:
        current_sku(db, caller.tenant_id, add_product(db, caller.tenant_id), "A-0")
    before = catalog_of(engine, caller.tenant_id)

    data = confirm(client, caller, import_id)
    assert data["status"] == "confirmed"
    assert outcomes(data) == [
        ("failed", None, "conflicts_existing"),
        ("skipped", "invalid_format", None),
    ]
    assert catalog_of(engine, caller.tenant_id) == before
    assert stored_import(engine, caller.tenant_id, import_id)["status"] == "confirmed"


def test_a_missing_and_another_tenants_import_get_the_same_404(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    foreign_import = preview(client, foreign, [row("v-0", "p-1", "A-0")])["import_id"]
    foreign_before = stored_import(engine, foreign.tenant_id, foreign_import)

    # 同一个请求 ID：响应完全相同，只有跨租户那次记事件。
    rid = request_id()
    cross = post_confirm(client, caller, foreign_import, rid=rid)
    missing = post_confirm(client, caller, new_id("imp"), rid=rid)
    assert error_of(cross, 404)["code"] == "IMPORT_NOT_FOUND"
    assert cross.json() == missing.json()

    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert (event["tenant_id"], event["credential_id"]) == (caller.tenant_id, caller.credential_id)
    assert (event["http_method"], event["path"]) == ("POST", f"{IMPORTS}/{foreign_import}/confirm")
    assert event["details"] == {"object_type": "import", "id": foreign_import}
    assert stored_import(engine, foreign.tenant_id, foreign_import) == foreign_before
    assert changes_of(engine, foreign.tenant_id) == []


def test_confirming_again_returns_the_current_representation(client, engine):
    caller = issue(engine)
    import_id = preview(client, caller, [row("v-0", "p-1", "A-0")])["import_id"]
    key = new_key()
    first = post_confirm(client, caller, import_id, key=key)
    data = data_of(first)
    catalog = catalog_of(engine, caller.tenant_id)
    stored = stored_import(engine, caller.tenant_id, import_id)

    # 换新键：已确认过，200 与当前表示，不再建。
    again = post_confirm(client, caller, import_id)
    assert "Idempotency-Replayed" not in again.headers
    assert data_of(again) == data

    # 同键：原样重放首次的响应。
    replay = post_confirm(client, caller, import_id, key=key)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()

    assert catalog_of(engine, caller.tenant_id) == catalog
    assert stored_import(engine, caller.tenant_id, import_id) == stored


def test_an_expired_import_cannot_be_confirmed(client, engine):
    caller = issue(engine)
    import_id = preview(client, caller, [row("v-0", "p-1", "A-0")])["import_id"]
    expire(engine, import_id)
    before = catalog_of(engine, caller.tenant_id)

    error = error_of(post_confirm(client, caller, import_id), 409)
    assert error["code"] == "IMPORT_EXPIRED"
    assert catalog_of(engine, caller.tenant_id) == before
    stored = stored_import(engine, caller.tenant_id, import_id)
    assert (stored["status"], stored["result"], stored["confirmed_at"]) == ("previewed", None, None)


def test_confirm_takes_no_body(client, engine):
    caller = issue(engine)
    import_id = preview(client, caller, [row("v-0", "p-1", "A-0")])["import_id"]
    url = f"{IMPORTS}/{import_id}/confirm"
    response = client.post(url, json={}, headers=write_headers(caller))
    assert error_of(response, 422)["code"] == "INVALID_REQUEST"
    assert stored_import(engine, caller.tenant_id, import_id)["status"] == "previewed"
    assert changes_of(engine, caller.tenant_id) == []


# --- 查询 ---


def test_get_shows_previewed_expired_and_confirmed_imports(client, engine):
    caller = issue(engine)
    previewed = preview(client, caller, [row("v-0", "p-1", "A-0")])
    expired = preview(client, caller, [row("v-1", "p-2", "A-1")])
    expire(engine, expired["import_id"])
    confirmed = preview(client, caller, [row("v-2", "p-3", "A-2")])
    confirmed = confirm(client, caller, confirmed["import_id"])

    # 只有 catalog.read、不带操作者头的凭据也能查。
    reader = reader_of(engine, caller)
    assert get_import(client, reader, previewed["import_id"]) == previewed
    assert get_import(client, reader, confirmed["import_id"]) == confirmed

    shown = get_import(client, reader, expired["import_id"])
    assert shown["status"] == "expired"
    assert parse_timestamp(shown["expires_at"]) <= db_now(engine)
    unchanged = REPRESENTATION - {"status", "expires_at"}
    assert {name: shown[name] for name in unchanged} == {name: expired[name] for name in unchanged}
    # expired 由有效期推出，库里仍是 previewed。
    assert stored_import(engine, caller.tenant_id, expired["import_id"])["status"] == "previewed"


def test_get_of_another_tenants_import_is_404_and_recorded(client, engine):
    caller = issue(engine)
    foreign = issue(engine)
    foreign_import = preview(client, foreign, [row("v-0", "p-1", "A-0")])["import_id"]

    rid = request_id()
    headers = caller.auth | {"X-Request-ID": rid}
    cross = client.get(f"{IMPORTS}/{foreign_import}", headers=headers)
    missing = client.get(f"{IMPORTS}/{new_id('imp')}", headers=headers)
    assert error_of(cross, 404)["code"] == "IMPORT_NOT_FOUND"
    assert cross.json() == missing.json()

    [event] = events(engine, rid)
    assert event["event_type"] == "cross_tenant"
    assert (event["http_method"], event["path"]) == ("GET", f"{IMPORTS}/{foreign_import}")
    assert event["details"] == {"object_type": "import", "id": foreign_import}


# --- 授权 ---


def test_get_needs_catalog_read(client, engine):
    caller = issue(engine, scopes=[CATALOG_WRITE])
    import_id = preview(client, caller, [row("v-0", "p-1", "A-0")])["import_id"]
    rid = request_id()
    response = client.get(f"{IMPORTS}/{import_id}", headers=caller.auth | {"X-Request-ID": rid})
    assert error_of(response, 403)["code"] == "FORBIDDEN"
    [event] = events(engine, rid)
    assert event["details"] == {"category": "scope", "missing_scopes": [CATALOG_READ]}


def test_preview_and_confirm_need_catalog_write_and_catalog_admin(client, engine):
    caller = issue(engine)
    import_id = preview(client, caller, [row("v-0", "p-1", "A-0")])["import_id"]
    reader = reader_of(engine, caller)
    requests = [
        (IMPORTS, {"rows": [row("v-1", "p-2", "A-1")]}),
        (f"{IMPORTS}/{import_id}/confirm", None),
    ]
    for url, body in requests:
        response = client.post(url, json=body, headers=write_headers(reader))
        assert error_of(response, 403)["code"] == "FORBIDDEN"
        headers = write_headers(caller) | {"X-Actor-Role": STOCK_ADMIN}
        response = client.post(url, json=body, headers=headers)
        assert error_of(response, 403)["code"] == "FORBIDDEN"
    stored = rows(engine, CatalogImport, caller.tenant_id)
    assert [line["import_id"] for line in stored] == [import_id]
    assert stored_import(engine, caller.tenant_id, import_id)["status"] == "previewed"
