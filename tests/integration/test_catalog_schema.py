"""目录表的约束在真 MySQL 上生效（契约 2.2、2.3、2.4、2.7 与第 10 节第 1 条）。

这些规则由数据库兜底，服务层的 bug 也绕不过去。连接夹具 db 与 assert_rejected_by 见 conftest。
"""

from datetime import datetime

from sqlalchemy import Connection, insert, update
from sqlalchemy.sql.expression import Executable

from app.models.catalog import (
    CatalogChange,
    CatalogImport,
    Product,
    Sku,
    SkuCodeClaim,
    SkuCodeHistory,
    Tenant,
)
from tests.integration.conftest import add_credential, assert_rejected_by, new_id

EXPIRES_AT = datetime(2026, 11, 15, 9, 0)
CONFIRMED_AT = datetime(2026, 11, 14, 10, 0)


def add_tenant_with_product(db: Connection) -> tuple[str, str]:
    tenant_id, product_id = new_id("ten"), new_id("prd")
    db.execute(insert(Tenant).values(tenant_id=tenant_id, name="Tenant", status="active"))
    db.execute(
        insert(Product).values(
            product_id=product_id, tenant_id=tenant_id, name="Tee", status="active", version=1
        )
    )
    return tenant_id, product_id


def sku_insert(tenant_id: str, product_id: str, **overrides: object) -> Executable:
    values = {
        "sku_id": new_id("sku"),
        "tenant_id": tenant_id,
        "product_id": product_id,
        "sku_code": new_id("code"),
        "name": "Tee",
        "status": "active",
        "version": 1,
    }
    return insert(Sku).values(values | overrides)


def add_sku(db: Connection, tenant_id: str, product_id: str, **overrides: object) -> str:
    sku_id = new_id("sku")
    db.execute(sku_insert(tenant_id, product_id, sku_id=sku_id, **overrides))
    return sku_id


def sku_update(sku_id: str, **values: object) -> Executable:
    return update(Sku).where(Sku.sku_id == sku_id).values(values)


def test_sku_code_is_unique_within_a_tenant(db: Connection):
    tenant, product = add_tenant_with_product(db)
    add_sku(db, tenant, product, sku_code="TEE-RED-M")
    assert_rejected_by(
        db, sku_insert(tenant, product, sku_code="TEE-RED-M"), "uq_skus_tenant_id_sku_code"
    )


def test_another_tenant_may_use_the_same_sku_code(db: Connection):
    first_tenant, first_product = add_tenant_with_product(db)
    second_tenant, second_product = add_tenant_with_product(db)
    add_sku(db, first_tenant, first_product, sku_code="TEE-RED-M")
    add_sku(db, second_tenant, second_product, sku_code="TEE-RED-M")


def test_codes_differing_only_in_case_are_the_same_code(db: Connection):
    tenant, product = add_tenant_with_product(db)
    sku = add_sku(db, tenant, product, sku_code="tee-red-m", barcode="abc123")
    assert_rejected_by(
        db, sku_insert(tenant, product, sku_code="TEE-RED-M"), "uq_skus_tenant_id_sku_code"
    )
    assert_rejected_by(
        db, sku_insert(tenant, product, barcode="ABC123"), "uq_skus_tenant_id_barcode"
    )
    # 退役编码的归属也一样：只差大小写的编码不能登记给别的 SKU（契约 2.2，Q-06）。
    other = add_sku(db, tenant, product)
    db.execute(insert(SkuCodeClaim).values(tenant_id=tenant, code="old-code", sku_id=sku))
    assert_rejected_by(
        db,
        insert(SkuCodeClaim).values(tenant_id=tenant, code="OLD-CODE", sku_id=other),
        "sku_code_claims.PRIMARY",
    )


def test_non_null_barcode_is_unique_but_many_skus_may_have_none(db: Connection):
    tenant, product = add_tenant_with_product(db)
    add_sku(db, tenant, product, barcode=None)
    add_sku(db, tenant, product, barcode=None)
    add_sku(db, tenant, product, barcode="9555000000017")
    assert_rejected_by(
        db, sku_insert(tenant, product, barcode="9555000000017"), "uq_skus_tenant_id_barcode"
    )


def test_empty_or_padded_codes_are_rejected(db: Connection):
    # 空条码必须存 NULL：空串会让第二个无条码的 SKU 撞上唯一约束。
    tenant, product = add_tenant_with_product(db)
    assert_rejected_by(db, sku_insert(tenant, product, barcode=""), "ck_skus_barcode_trimmed")
    assert_rejected_by(db, sku_insert(tenant, product, barcode=" 123"), "ck_skus_barcode_trimmed")
    assert_rejected_by(db, sku_insert(tenant, product, sku_code=""), "ck_skus_sku_code_trimmed")
    assert_rejected_by(db, sku_insert(tenant, product, sku_code="TEE "), "ck_skus_sku_code_trimmed")


def test_sku_cannot_hang_under_another_tenants_product(db: Connection):
    tenant, _ = add_tenant_with_product(db)
    _, other_product = add_tenant_with_product(db)
    assert_rejected_by(db, sku_insert(tenant, other_product), "fk_skus_tenant_id_products")


def test_sku_cannot_merge_into_itself(db: Connection):
    tenant, product = add_tenant_with_product(db)
    sku = add_sku(db, tenant, product)
    merge = sku_update(sku, status="inactive", merged_into_sku_id=sku)
    assert_rejected_by(db, merge, "ck_skus_merged_not_self")


def test_sku_cannot_merge_into_another_tenants_sku(db: Connection):
    tenant, product = add_tenant_with_product(db)
    other_tenant, other_product = add_tenant_with_product(db)
    source = add_sku(db, tenant, product)
    foreign_target = add_sku(db, other_tenant, other_product)
    merge = sku_update(source, status="inactive", merged_into_sku_id=foreign_target)
    assert_rejected_by(db, merge, "fk_skus_tenant_id_skus")


def test_merged_sku_must_be_inactive(db: Connection):
    tenant, product = add_tenant_with_product(db)
    source = add_sku(db, tenant, product)
    target = add_sku(db, tenant, product)
    merge_still_active = sku_update(source, merged_into_sku_id=target)
    assert_rejected_by(db, merge_still_active, "ck_skus_merged_inactive")

    db.execute(sku_update(source, status="inactive", merged_into_sku_id=target))
    # 已合并的 SKU 不能重新启用（契约 2.4）。
    assert_rejected_by(db, sku_update(source, status="active"), "ck_skus_merged_inactive")


def test_status_must_be_an_exact_known_value(db: Connection):
    tenant, product = add_tenant_with_product(db)
    assert_rejected_by(db, sku_insert(tenant, product, status="ACTIVE"), "ck_skus_status")
    assert_rejected_by(db, sku_insert(tenant, product, status="archived"), "ck_skus_status")


def test_version_starts_at_one(db: Connection):
    tenant, product = add_tenant_with_product(db)
    assert_rejected_by(db, sku_insert(tenant, product, version=0), "ck_skus_version")
    product_v0 = insert(Product).values(
        product_id=new_id("prd"), tenant_id=tenant, name="Tee", status="active", version=0
    )
    assert_rejected_by(db, product_v0, "ck_products_version")


def test_reference_price_is_non_negative_and_has_amount_and_currency_together(db: Connection):
    tenant, product = add_tenant_with_product(db)
    add_sku(db, tenant, product, reference_price_amount_minor=12900, reference_price_currency="MYR")
    add_sku(db, tenant, product, reference_price_amount_minor=0, reference_price_currency="MYR")
    assert_rejected_by(
        db,
        sku_insert(
            tenant, product, reference_price_amount_minor=-1, reference_price_currency="MYR"
        ),
        "ck_skus_reference_price_nonnegative",
    )
    assert_rejected_by(
        db,
        sku_insert(tenant, product, reference_price_amount_minor=12900),
        "ck_skus_reference_price_pair",
    )
    assert_rejected_by(
        db,
        sku_insert(tenant, product, reference_price_currency="MYR"),
        "ck_skus_reference_price_pair",
    )


def test_code_history_allows_changing_back_to_an_old_code(db: Connection):
    # A→B→A→B：历史只追加，没有唯一约束（契约第 14 节第 3 条）。
    tenant, product = add_tenant_with_product(db)
    sku = add_sku(db, tenant, product, sku_code="A")
    for old, new in [("A", "B"), ("B", "A"), ("A", "B")]:
        db.execute(
            insert(SkuCodeHistory).values(
                tenant_id=tenant, sku_id=sku, old_code=old, new_code=new, actor_ref="admin_1"
            )
        )
    padded = insert(SkuCodeHistory).values(
        tenant_id=tenant, sku_id=sku, old_code="B", new_code=" C", actor_ref="admin_1"
    )
    assert_rejected_by(db, padded, "ck_sku_code_history_new_code_trimmed")


def test_code_claim_must_point_to_a_sku_of_the_same_tenant(db: Connection):
    tenant, _ = add_tenant_with_product(db)
    other_tenant, other_product = add_tenant_with_product(db)
    foreign_sku = add_sku(db, other_tenant, other_product)
    claim = insert(SkuCodeClaim).values(tenant_id=tenant, code="X-1", sku_id=foreign_sku)
    assert_rejected_by(db, claim, "fk_sku_code_claims_tenant_id_skus")


# 批量导入（契约 2.3）


def import_insert(tenant_id: str, credential_id: str, **overrides: object) -> Executable:
    values = {
        "import_id": new_id("imp"),
        "tenant_id": tenant_id,
        "status": "previewed",
        "expires_at": EXPIRES_AT,
        "rows": [{"external_ref": "variant-1", "result": "ok"}],
        "credential_id": credential_id,
        "actor_ref": "admin_1",
        "request_id": new_id("req"),
    }
    return insert(CatalogImport).values(values | overrides)


def confirmation(credential_id: str) -> dict[str, object]:
    """确认才填的五列，都非空。"""
    return {
        "result": [{"index": 0, "outcome": "created"}],
        "confirmed_credential_id": credential_id,
        "confirmed_actor_ref": "admin_2",
        "confirmed_request_id": new_id("req"),
        "confirmed_at": CONFIRMED_AT,
    }


def add_import(db: Connection, tenant_id: str, credential_id: str, **overrides: object) -> str:
    import_id = new_id("imp")
    db.execute(import_insert(tenant_id, credential_id, import_id=import_id, **overrides))
    return import_id


def test_import_status_must_be_an_exact_known_value(db: Connection):
    tenant, _ = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    add_import(db, tenant, credential)
    add_import(db, tenant, credential, status="confirmed", **confirmation(credential))
    # expired 由 expires_at 推出，不存。
    for status in ["PREVIEWED", "Previewed", "expired", ""]:
        assert_rejected_by(
            db, import_insert(tenant, credential, status=status), "ck_catalog_imports_status"
        )
    for status in ["CONFIRMED", "Confirmed"]:
        confirmed = import_insert(tenant, credential, status=status, **confirmation(credential))
        assert_rejected_by(db, confirmed, "ck_catalog_imports_status")


def test_previewed_import_has_no_result_or_confirmation(db: Connection):
    tenant, _ = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    for column, value in confirmation(credential).items():
        assert_rejected_by(
            db,
            import_insert(tenant, credential, **{column: value}),
            "ck_catalog_imports_previewed_unconfirmed",
        )


def test_confirmed_import_has_result_and_every_confirmation_column(db: Connection):
    tenant, _ = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    for column in confirmation(credential):
        incomplete = confirmation(credential) | {column: None}
        assert_rejected_by(
            db,
            import_insert(tenant, credential, status="confirmed", **incomplete),
            "ck_catalog_imports_confirmed_complete",
        )


def test_import_rows_must_be_an_array(db: Connection):
    tenant, _ = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    add_import(db, tenant, credential, rows=[])
    for rows in [{"external_ref": "variant-1"}, "variant-1", 1]:
        assert_rejected_by(
            db, import_insert(tenant, credential, rows=rows), "ck_catalog_imports_rows"
        )


def test_import_credentials_must_belong_to_the_same_tenant(db: Connection):
    tenant, _ = add_tenant_with_product(db)
    other_tenant, _ = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    foreign_credential = add_credential(db, other_tenant)
    assert_rejected_by(
        db,
        import_insert(tenant, foreign_credential),
        "fk_catalog_imports_tenant_id_credentials",
    )
    assert_rejected_by(
        db,
        import_insert(tenant, credential, status="confirmed", **confirmation(foreign_credential)),
        "fk_catalog_imports_confirmed_credential_id_credentials",
    )
    # 确认人可以用同租户的另一个凭据。
    add_import(
        db, tenant, credential, status="confirmed", **confirmation(add_credential(db, tenant))
    )


# 目录变更记录（契约 2.7）


def change_insert(
    tenant_id: str, product_id: str, credential_id: str, **overrides: object
) -> Executable:
    values = {
        "tenant_id": tenant_id,
        "event_type": "product_created",
        "object_type": "product",
        "object_id": product_id,
        "product_id": product_id,
        "version": 1,
        "changes": {"name": {"before": None, "after": "Tee"}},
        "credential_id": credential_id,
        "actor_ref": "admin_1",
        "actor_role": "catalog_admin",
        "request_id": new_id("req"),
        "idempotency_key": f"shop:product:{product_id}:create:1",
    }
    return insert(CatalogChange).values(values | overrides)


def test_change_object_type_must_be_an_exact_known_value(db: Connection):
    tenant, product = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    for object_type in ["PRODUCT", "Sku", "variant", ""]:
        assert_rejected_by(
            db,
            change_insert(tenant, product, credential, object_type=object_type),
            "ck_catalog_changes_object_type",
        )


def test_product_change_object_id_is_its_product_id(db: Connection):
    tenant, product = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    db.execute(change_insert(tenant, product, credential))
    sku = add_sku(db, tenant, product)
    db.execute(
        change_insert(
            tenant, product, credential, event_type="sku_created", object_type="sku", object_id=sku
        )
    )
    assert_rejected_by(
        db,
        change_insert(tenant, product, credential, object_id=new_id("prd")),
        "ck_catalog_changes_product_object_id",
    )


def test_change_product_must_belong_to_the_same_tenant(db: Connection):
    tenant, _ = add_tenant_with_product(db)
    _, foreign_product = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    assert_rejected_by(
        db,
        change_insert(
            tenant,
            foreign_product,
            credential,
            event_type="sku_created",
            object_type="sku",
            object_id=new_id("sku"),
        ),
        "fk_catalog_changes_tenant_id_products",
    )


def test_change_import_must_belong_to_the_same_tenant(db: Connection):
    tenant, product = add_tenant_with_product(db)
    other_tenant, _ = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    own_import = add_import(db, tenant, credential, status="confirmed", **confirmation(credential))
    db.execute(change_insert(tenant, product, credential, import_id=own_import))
    other_credential = add_credential(db, other_tenant)
    foreign_import = add_import(
        db, other_tenant, other_credential, status="confirmed", **confirmation(other_credential)
    )
    assert_rejected_by(
        db,
        change_insert(tenant, product, credential, import_id=foreign_import),
        "fk_catalog_changes_tenant_id_catalog_imports",
    )


def test_change_credential_must_belong_to_the_same_tenant(db: Connection):
    tenant, product = add_tenant_with_product(db)
    other_tenant, _ = add_tenant_with_product(db)
    foreign_credential = add_credential(db, other_tenant)
    assert_rejected_by(
        db,
        change_insert(tenant, product, foreign_credential),
        "fk_catalog_changes_tenant_id_credentials",
    )


def test_change_version_starts_at_one(db: Connection):
    tenant, product = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    assert_rejected_by(
        db, change_insert(tenant, product, credential, version=0), "ck_catalog_changes_version"
    )


def test_change_changes_must_be_an_object(db: Connection):
    tenant, product = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    for changes in [[], [{"name": {"before": None, "after": "Tee"}}], "name", 1]:
        assert_rejected_by(
            db,
            change_insert(tenant, product, credential, changes=changes),
            "ck_catalog_changes_changes",
        )


def test_change_event_type_is_not_limited_by_the_database(db: Connection):
    # 以后加事件类型不改表，由应用校验（契约 2.7）。
    tenant, product = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    for event_type in ["sku_merged", "some_future_event"]:
        db.execute(change_insert(tenant, product, credential, event_type=event_type))


def test_change_ids_increase_in_insert_order(db: Connection):
    tenant, product = add_tenant_with_product(db)
    credential = add_credential(db, tenant)
    first = db.execute(change_insert(tenant, product, credential)).inserted_primary_key[0]
    second = db.execute(
        change_insert(tenant, product, credential, event_type="product_updated", version=2)
    ).inserted_primary_key[0]
    assert second > first
