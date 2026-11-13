"""目录表的约束在真 MySQL 上生效（契约 2.2、2.4 与第 10 节第 1 条）。

这些规则由数据库兜底，服务层的 bug 也绕不过去。连接夹具 db 与 assert_rejected_by 见 conftest。
"""

from sqlalchemy import Connection, insert, update
from sqlalchemy.sql.expression import Executable

from app.models.catalog import Product, Sku, SkuCodeClaim, SkuCodeHistory, Tenant
from tests.integration.conftest import assert_rejected_by, new_id


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
