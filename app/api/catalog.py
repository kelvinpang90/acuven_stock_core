"""商品与 SKU 的查询、新建、修改、停用与重新启用接口。

契约 §2.1、§2.2、§2.4、§2.6、§2.7、§7.2。
读接口要求 catalog.read、不要求角色；带了操作者头照样校验格式（authorize 里做）。
写命令要求 catalog.write 与 catalog_admin 角色，经幂等执行器并取目录写锁；
字段取值、请求内的编码与条码重复、修改没带可改字段在第 7 步拒绝（取锁之前，不保存）。
查库与写库在 app/services/catalog.py。
product_representation、sku_representation 与 format_timestamp
供之后的目录写命令、批量导入与变更流共用。
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, field_validator
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import authorize, cross_tenant_not_found, get_access, require_role
from app.api.errors import ApiError, invalid_request, ok
from app.api.requests import (
    MAX_BATCH_ITEMS,
    ParsedBody,
    RequestBody,
    discard_connection,
    json_body,
    no_body,
    request_connection,
    unavailable,
)
from app.domain.catalog import (
    ACTIVE,
    BARCODE,
    INACTIVE,
    PRODUCT,
    PRODUCT_DEACTIVATED,
    PRODUCT_REACTIVATED,
    PRODUCT_UPDATED,
    SKU,
    SKU_CODE,
    SKU_DEACTIVATED,
    SKU_REACTIVATED,
    SKU_UPDATED,
    InvalidField,
    NothingToUpdate,
    code_key,
    conflict_code,
    diff_fields,
    merged_sku_update_allowed,
    normalize_barcode,
    normalize_name,
    normalize_reason,
    normalize_reference_price,
    normalize_sku_code,
    normalize_update,
    sort_conflicts,
    status_changes,
)
from app.domain.requests import CATALOG_ADMIN, Actor
from app.domain.service_auth import CATALOG_READ, CATALOG_WRITE
from app.services import catalog
from app.services.catalog import (
    CatalogConflict,
    ChangeAuthor,
    LockedProduct,
    LockedSku,
    ProductRecord,
    SkuFields,
    SkuRecord,
)
from app.services.idempotency import CommandResult, Execution, run_idempotent
from app.services.security_events import CROSS_TENANT

PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
SKU_NOT_FOUND = "SKU_NOT_FOUND"
VERSION_CONFLICT = "VERSION_CONFLICT"
SKU_MERGED = "SKU_MERGED"

PRODUCTS_CREATE = "products.create"
SKUS_CREATE = "skus.create"
PRODUCTS_UPDATE = "products.update"
SKUS_UPDATE = "skus.update"
PRODUCTS_DEACTIVATE = "products.deactivate"
PRODUCTS_REACTIVATE = "products.reactivate"
SKUS_DEACTIVATE = "skus.deactivate"
SKUS_REACTIVATE = "skus.reactivate"

IDS = "ids"
LIST_PARAMETERS = (SKU_CODE, BARCODE, IDS)
SKUS = "skus"

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(CATALOG_READ))]
_WRITE = [Depends(authorize(CATALOG_WRITE, write=True))]


# --- 表示（§2.6 第 7 条） ---


def format_timestamp(value: datetime) -> str:
    """UTC 的 RFC 3339 字符串，微秒精度，以 Z 结尾，如 2026-11-14T09:30:00.000000Z。

    数据库的 DATETIME(6) 读出来是不带时区的 UTC 时间；带时区的先换成 UTC。
    """
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds") + "Z"


def sku_representation(sku: SkuRecord) -> dict:
    return {
        "sku_id": sku.sku_id,
        "product_id": sku.product_id,
        "sku_code": sku.sku_code,
        "barcode": sku.barcode,
        "name": sku.name,
        "reference_price": None if sku.reference_price is None else dict(sku.reference_price),
        "status": sku.status,
        "product_status": sku.product_status,
        "merged_into_sku_id": sku.merged_into_sku_id,
        "version": sku.version,
        "created_at": format_timestamp(sku.created_at),
        "updated_at": format_timestamp(sku.updated_at),
    }


def product_representation(product: ProductRecord) -> dict:
    return {
        "product_id": product.product_id,
        "name": product.name,
        "status": product.status,
        "version": product.version,
        "created_at": format_timestamp(product.created_at),
        "updated_at": format_timestamp(product.updated_at),
        "skus": [sku_representation(sku) for sku in product.skus],
    }


# --- 查库 ---


def _read[T](request: Request, query: Callable[[Connection], T]) -> T:
    """用本请求的连接读，读完回滚结束事务（之后写安全事件要求没有打开的事务）；出错 503。"""
    connection = request_connection(request)
    try:
        result = query(connection)
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error during a catalog read (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None
    return result


def _tenant_id(request: Request) -> str:
    return get_access(request).principal.tenant_id


def _not_found(
    request: Request, *, object_type: str, object_id: str, code: str, owner: str | None
) -> ApiError:
    """按路径 ID 找不到：属于别的租户时另记 cross_tenant，对外与不存在完全相同（§8.3）。"""
    if owner is not None:
        return cross_tenant_not_found(
            request, object_type=object_type, object_id=object_id, code=code
        )
    return ApiError(404, code, f"{object_type} not found")


# --- 按 ID 查询 ---


@router.get("/v1/products/{product_id}", dependencies=_READ)
def get_product(request: Request, product_id: str) -> JSONResponse:
    tenant_id = _tenant_id(request)

    def lookup(connection: Connection) -> tuple[ProductRecord | None, str | None]:
        product = catalog.read_product(connection, tenant_id, product_id)
        owner = None if product is not None else catalog.product_owner(connection, product_id)
        return product, owner

    product, owner = _read(request, lookup)
    if product is None:
        raise _not_found(
            request, object_type=PRODUCT, object_id=product_id, code=PRODUCT_NOT_FOUND, owner=owner
        )
    return ok(request, product_representation(product))


@router.get("/v1/skus/{sku_id}", dependencies=_READ)
def get_sku(request: Request, sku_id: str) -> JSONResponse:
    """已合并的 SKU 照常返回，带 merged_into_sku_id（§2.4）。"""
    tenant_id = _tenant_id(request)

    def lookup(connection: Connection) -> tuple[SkuRecord | None, str | None]:
        sku = catalog.read_sku(connection, tenant_id, sku_id)
        owner = None if sku is not None else catalog.sku_owner(connection, sku_id)
        return sku, owner

    sku, owner = _read(request, lookup)
    if sku is None:
        raise _not_found(
            request, object_type=SKU, object_id=sku_id, code=SKU_NOT_FOUND, owner=owner
        )
    return ok(request, sku_representation(sku))


# --- 列表查询 ---


def _list_parameter(request: Request) -> tuple[str, str]:
    """sku_code、barcode、ids 恰好带一个且只出现一次，别的查询参数都不收；返回参数名与原值。

    不回显未知参数的名字与参数值。
    """
    items = request.query_params.multi_items()
    names = [name for name, _ in items]
    if any(name not in LIST_PARAMETERS for name in names):
        raise invalid_request("unknown query parameter")
    for name in LIST_PARAMETERS:
        if names.count(name) > 1:
            raise invalid_request("query parameter given more than once", {"parameter": name})
    if len(items) != 1:
        raise invalid_request("exactly one of sku_code, barcode and ids is required")
    return items[0]


def _code_parameter(name: str, value: str) -> str:
    """按 §2.6 第 1 条规范化；barcode 去首尾空白后为空串时也不合格（不当作查空条码）。"""
    try:
        if name == BARCODE:
            normalized = normalize_barcode(value)
            if normalized is None:
                raise InvalidField(BARCODE)
            return normalized
        return normalize_sku_code(value)
    except InvalidField:
        raise invalid_request(f"invalid {name} parameter", {"parameter": name}) from None


def _ids_parameter(value: str) -> list[str]:
    """逗号分隔的 1–100 个 ID，不能有空项或重复；不校验格式（查不到即不出现）。

    超过 100 个也是 422：查询参数不是请求体，不用 413。
    """
    ids = value.split(",")
    if len(ids) > MAX_BATCH_ITEMS:
        raise invalid_request(f"at most {MAX_BATCH_ITEMS} ids", {"parameter": IDS})
    if "" in ids:
        raise invalid_request("empty id in ids", {"parameter": IDS})
    if len(set(ids)) != len(ids):
        raise invalid_request("duplicate id in ids", {"parameter": IDS})
    return ids


@router.get("/v1/skus", dependencies=_READ)
def list_skus(request: Request) -> JSONResponse:
    """结果按 sku_id 升序、不分页。

    查询本身限定在本租户：查不到的、属于别的租户的都不出现，不报错、不记 cross_tenant。
    """
    tenant_id = _tenant_id(request)
    name, value = _list_parameter(request)
    if name == IDS:
        ids = _ids_parameter(value)
        skus = _read(request, lambda connection: catalog.read_skus(connection, tenant_id, ids))
    else:
        code = _code_parameter(name, value)
        find = catalog.find_sku_by_code if name == SKU_CODE else catalog.find_sku_by_barcode
        found = _read(request, lambda connection: find(connection, tenant_id, code))
        skus = [] if found is None else [found]
    return ok(request, {"skus": [sku_representation(sku) for sku in skus]})


# --- 新建的请求体（§2.6 第 1–3 条） ---


class NewSku(RequestBody):
    """一个 SKU 对象；校验时按 §2.6 第 1 条规范化，不合规的字段按位置 422。"""

    sku_code: str
    name: str
    barcode: str | None = None
    reference_price: dict | None = None

    @field_validator("sku_code")
    @classmethod
    def check_sku_code(cls, value: str) -> str:
        return normalize_sku_code(value)

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return normalize_name(value)

    @field_validator("barcode")
    @classmethod
    def check_barcode(cls, value: str | None) -> str | None:
        return normalize_barcode(value)

    @field_validator("reference_price")
    @classmethod
    def check_reference_price(cls, value: dict | None) -> dict | None:
        return normalize_reference_price(value)


class AddedSku(NewSku):
    """加规格的正文：一个 SKU 对象，另可带 reason。"""

    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def check_reason(cls, value: str | None) -> str | None:
        return normalize_reason(value)


class NewProduct(RequestBody):
    """新建商品的正文；skus 是 1–100 个的批量数组（超过 100 为 413）。"""

    batch_fields = (SKUS,)

    name: str
    skus: list[NewSku] = Field(min_length=1)
    reason: str | None = None

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return normalize_name(value)

    @field_validator("reason")
    @classmethod
    def check_reason(cls, value: str | None) -> str | None:
        return normalize_reason(value)


new_product_body = json_body(NewProduct)
added_sku_body = json_body(AddedSku)


def checked_new_product(
    body: Annotated[ParsedBody[NewProduct], Depends(new_product_body)],
) -> ParsedBody[NewProduct]:
    """第 7 步的最后一小步：同一请求里 sku_code 或非空 barcode 的比较键重复 422。

    每个与前面某项重复的位置列一条，取锁之前拒绝、不保存。
    """
    errors = []
    for field in (SKU_CODE, BARCODE):
        seen = set()
        for index, sku in enumerate(body.model.skus):
            value = getattr(sku, field)
            if value is None:
                continue
            key = code_key(value)
            if key in seen:
                errors.append({"loc": [SKUS, index, field], "type": "duplicate"})
            seen.add(key)
    if errors:
        raise invalid_request("duplicate sku_code or barcode in skus", {"errors": errors})
    return body


# --- 修改的请求体（§2.6 第 4 条） ---


class _Update(RequestBody):
    """修改的正文都有：expected_version（必填，正整数）与可选的 reason。

    子类的可改字段默认 None 只表示没带：校验器只对带了的字段运行，
    所以 name、sku_code 给 null 照样 422，barcode、reference_price 给 null 是清空。
    """

    expected_version: int = Field(ge=1)
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def check_reason(cls, value: str | None) -> str | None:
        return normalize_reason(value)


class ProductUpdate(_Update):
    name: str | None = None

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str | None) -> str:
        return normalize_name(value)


class SkuUpdate(_Update):
    sku_code: str | None = None
    barcode: str | None = None
    name: str | None = None
    reference_price: dict | None = None

    @field_validator("sku_code")
    @classmethod
    def check_sku_code(cls, value: str | None) -> str:
        return normalize_sku_code(value)

    @field_validator("barcode")
    @classmethod
    def check_barcode(cls, value: str | None) -> str | None:
        return normalize_barcode(value)

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str | None) -> str:
        return normalize_name(value)

    @field_validator("reference_price")
    @classmethod
    def check_reference_price(cls, value: dict | None) -> dict | None:
        return normalize_reference_price(value)


product_update_body = json_body(ProductUpdate)
sku_update_body = json_body(SkuUpdate)


def _update_fields(object_type: str, body: ParsedBody) -> dict:
    """第 7 步的最后一小步：正文里带了的可改字段，规范化后的值（不含 expected_version、reason）。

    除 expected_version、reason 外一个都没带 422，取锁之前拒绝、不保存。
    """
    try:
        return normalize_update(object_type, body.raw)
    except NothingToUpdate:
        raise invalid_request("at least one field to update is required") from None


def product_update_fields(
    body: Annotated[ParsedBody[ProductUpdate], Depends(product_update_body)],
) -> dict:
    return _update_fields(PRODUCT, body)


def sku_update_fields(body: Annotated[ParsedBody[SkuUpdate], Depends(sku_update_body)]) -> dict:
    return _update_fields(SKU, body)


CatalogAdmin = Annotated[Actor, Depends(require_role(CATALOG_ADMIN))]


# --- 新建的命令（在幂等执行器的事务里运行） ---


def _sku_fields(sku: NewSku) -> SkuFields:
    return SkuFields(
        sku_code=sku.sku_code,
        barcode=sku.barcode,
        name=sku.name,
        reference_price=sku.reference_price,
    )


def _author(execution: Execution, actor: Actor, reason: str | None) -> ChangeAuthor:
    """变更记录的凭据、操作者、角色、请求 ID 与幂等键取自本请求，原因取正文。"""
    access = execution.access
    return ChangeAuthor(
        credential_id=access.principal.credential_id,
        actor_ref=actor.ref,
        actor_role=actor.role,
        request_id=execution.request.state.request_id,
        idempotency_key=access.idempotency_key,
        reason=reason,
    )


def _conflict(error: CatalogConflict) -> ApiError:
    """409 的顶层错误码与 details.conflicts（§2.6 第 2 条）。"""
    conflicts = sort_conflicts(error.conflicts)
    details = {"conflicts": [conflict.as_dict() for conflict in conflicts]}
    message = "conflicts with the existing catalog"
    return ApiError(409, conflict_code(conflicts), message, details=details)


def _create_product(execution: Execution, actor: Actor, body: NewProduct) -> CommandResult:
    connection = execution.connection
    tenant_id = execution.access.principal.tenant_id
    skus = [_sku_fields(sku) for sku in body.skus]
    author = _author(execution, actor, body.reason)
    try:
        product_id = catalog.create_product(connection, tenant_id, body.name, skus, author)
    except CatalogConflict as error:
        raise _conflict(error) from None
    product = catalog.read_product(connection, tenant_id, product_id)
    return CommandResult(product_representation(product), 201)


def _missing(
    execution: Execution, *, object_type: str, object_id: str, code: str, owner: str | None
) -> ApiError:
    """命令里按路径 ID 找不到：属于别的租户时经执行器登记 cross_tenant，对外与不存在完全相同。"""
    if owner is not None:
        details = {"object_type": object_type, "id": object_id}
        execution.record_security_event(CROSS_TENANT, details)
    return ApiError(404, code, f"{object_type} not found")


def _add_sku(execution: Execution, actor: Actor, product_id: str, body: AddedSku) -> CommandResult:
    """商品不存在与属于别的租户同一个 404，后者另登记 cross_tenant。

    inactive 的商品照样能加。
    """
    connection = execution.connection
    tenant_id = execution.access.principal.tenant_id
    owner = catalog.product_owner(connection, product_id)
    if owner != tenant_id:
        raise _missing(
            execution,
            object_type=PRODUCT,
            object_id=product_id,
            code=PRODUCT_NOT_FOUND,
            owner=owner,
        )
    author = _author(execution, actor, body.reason)
    try:
        sku_id = catalog.add_sku(connection, tenant_id, product_id, _sku_fields(body), author)
    except CatalogConflict as error:
        raise _conflict(error) from None
    sku = catalog.read_sku(connection, tenant_id, sku_id)
    return CommandResult(sku_representation(sku), 201)


@router.post("/v1/products", dependencies=_WRITE)
def create_product(
    request: Request,
    body: Annotated[ParsedBody[NewProduct], Depends(checked_new_product)],
    actor: CatalogAdmin,
) -> Response:
    return run_idempotent(
        request,
        operation=PRODUCTS_CREATE,
        body=body,
        command=lambda execution: _create_product(execution, actor, body.model),
        catalog_lock=True,
    )


@router.post("/v1/products/{product_id}/skus", dependencies=_WRITE)
def create_sku(
    request: Request,
    product_id: str,
    body: Annotated[ParsedBody[AddedSku], Depends(added_sku_body)],
    actor: CatalogAdmin,
) -> Response:
    return run_idempotent(
        request,
        operation=SKUS_CREATE,
        body=body,
        command=lambda execution: _add_sku(execution, actor, product_id, body.model),
        catalog_lock=True,
    )


# --- 修改、停用与重新启用的命令（§2.6 第 4、5 条，在幂等执行器的事务里运行） ---


def _locked_product(execution: Execution, product_id: str) -> LockedProduct:
    """SELECT … FOR UPDATE 读要改的商品；不存在与属于别的租户同一个 404。"""
    connection = execution.connection
    product = catalog.lock_product(connection, execution.access.principal.tenant_id, product_id)
    if product is None:
        raise _missing(
            execution,
            object_type=PRODUCT,
            object_id=product_id,
            code=PRODUCT_NOT_FOUND,
            owner=catalog.product_owner(connection, product_id),
        )
    return product


def _locked_sku(execution: Execution, sku_id: str) -> LockedSku:
    """同 _locked_product，读要改的 SKU（已合并的照常读到）。"""
    connection = execution.connection
    sku = catalog.lock_sku(connection, execution.access.principal.tenant_id, sku_id)
    if sku is None:
        raise _missing(
            execution,
            object_type=SKU,
            object_id=sku_id,
            code=SKU_NOT_FOUND,
            owner=catalog.sku_owner(connection, sku_id),
        )
    return sku


def _check_version(expected_version: int, current_version: int) -> None:
    if expected_version != current_version:
        raise ApiError(
            409,
            VERSION_CONFLICT,
            "expected_version does not match the current version",
            details={"current_version": current_version},
        )


def _sku_merged(sku: LockedSku) -> ApiError:
    """已合并的 SKU 不能重新启用，也只能清空条码（§2.4）；附规范 SKU 的 ID。"""
    details = {"merged_into_sku_id": sku.merged_into_sku_id}
    return ApiError(409, SKU_MERGED, "sku has been merged", details=details)


def _product_result(execution: Execution, product_id: str) -> CommandResult:
    tenant_id = execution.access.principal.tenant_id
    product = catalog.read_product(execution.connection, tenant_id, product_id)
    return CommandResult(product_representation(product))


def _sku_result(execution: Execution, sku_id: str) -> CommandResult:
    tenant_id = execution.access.principal.tenant_id
    sku = catalog.read_sku(execution.connection, tenant_id, sku_id)
    return CommandResult(sku_representation(sku))


def _update_product(
    execution: Execution, actor: Actor, product_id: str, body: ProductUpdate, requested: dict
) -> CommandResult:
    """判定顺序：404 → 版本不符 409 → 与当前值全都相同时原样返回 → 写入。

    requested 是正文里带了的可改字段；inactive 的商品也可以改。
    """
    product = _locked_product(execution, product_id)
    _check_version(body.expected_version, product.version)
    changes = diff_fields(product.current(), requested)
    if changes:
        tenant_id = execution.access.principal.tenant_id
        author = _author(execution, actor, body.reason)
        catalog.update_product(
            execution.connection, tenant_id, product, changes, author, PRODUCT_UPDATED
        )
    return _product_result(execution, product_id)


def _update_sku(
    execution: Execution, actor: Actor, sku_id: str, body: SkuUpdate, requested: dict
) -> CommandResult:
    """已合并的 SKU 只能清空条码，否则 409 SKU_MERGED（在 404 之后、版本之前判定）。

    其余同 _update_product，写入之前另查编码与条码冲突（409，见 catalog.update_sku）。
    inactive 的 SKU 也可以改；改了 sku_code 时登记归属并写编码历史。
    """
    sku = _locked_sku(execution, sku_id)
    if sku.merged_into_sku_id is not None and not merged_sku_update_allowed(requested):
        raise _sku_merged(sku)
    _check_version(body.expected_version, sku.version)
    changes = diff_fields(sku.current(), requested)
    if changes:
        tenant_id = execution.access.principal.tenant_id
        author = _author(execution, actor, body.reason)
        try:
            catalog.update_sku(execution.connection, tenant_id, sku, changes, author, SKU_UPDATED)
        except CatalogConflict as error:
            raise _conflict(error) from None
    return _sku_result(execution, sku_id)


def _set_product_status(
    execution: Execution, actor: Actor, product_id: str, target: str, event_type: str
) -> CommandResult:
    """已是目标状态时原样返回；不改它的 SKU 的状态，不检查库存与未结订单。

    没有请求体，变更记录的 reason 为空。
    """
    product = _locked_product(execution, product_id)
    changes = status_changes(product.status, target)
    if changes:
        tenant_id = execution.access.principal.tenant_id
        author = _author(execution, actor, None)
        catalog.update_product(
            execution.connection, tenant_id, product, changes, author, event_type
        )
    return _product_result(execution, product_id)


def _set_sku_status(
    execution: Execution, actor: Actor, sku_id: str, target: str, event_type: str
) -> CommandResult:
    """重新启用已合并的 SKU 409 SKU_MERGED；已是目标状态时原样返回（停用已合并的 SKU 也是）。

    所属商品 inactive 时照样可以重新启用。没有请求体，变更记录的 reason 为空。
    """
    sku = _locked_sku(execution, sku_id)
    if sku.merged_into_sku_id is not None and target == ACTIVE:
        raise _sku_merged(sku)
    changes = status_changes(sku.status, target)
    if changes:
        tenant_id = execution.access.principal.tenant_id
        author = _author(execution, actor, None)
        catalog.update_sku(execution.connection, tenant_id, sku, changes, author, event_type)
    return _sku_result(execution, sku_id)


@router.patch("/v1/products/{product_id}", dependencies=_WRITE)
def update_product(
    request: Request,
    product_id: str,
    body: Annotated[ParsedBody[ProductUpdate], Depends(product_update_body)],
    requested: Annotated[dict, Depends(product_update_fields)],
    actor: CatalogAdmin,
) -> Response:
    return run_idempotent(
        request,
        operation=PRODUCTS_UPDATE,
        body=body,
        command=lambda execution: _update_product(
            execution, actor, product_id, body.model, requested
        ),
        catalog_lock=True,
    )


@router.patch("/v1/skus/{sku_id}", dependencies=_WRITE)
def update_sku(
    request: Request,
    sku_id: str,
    body: Annotated[ParsedBody[SkuUpdate], Depends(sku_update_body)],
    requested: Annotated[dict, Depends(sku_update_fields)],
    actor: CatalogAdmin,
) -> Response:
    return run_idempotent(
        request,
        operation=SKUS_UPDATE,
        body=body,
        command=lambda execution: _update_sku(execution, actor, sku_id, body.model, requested),
        catalog_lock=True,
    )


# 停用与重新启用没有请求体：no_body 在 authorize 之后，空请求体跳过、非空 422。
_WRITE_WITHOUT_BODY = [*_WRITE, Depends(no_body)]


@router.post("/v1/products/{product_id}/deactivate", dependencies=_WRITE_WITHOUT_BODY)
def deactivate_product(request: Request, product_id: str, actor: CatalogAdmin) -> Response:
    return run_idempotent(
        request,
        operation=PRODUCTS_DEACTIVATE,
        body=None,
        command=lambda execution: _set_product_status(
            execution, actor, product_id, INACTIVE, PRODUCT_DEACTIVATED
        ),
        catalog_lock=True,
    )


@router.post("/v1/products/{product_id}/reactivate", dependencies=_WRITE_WITHOUT_BODY)
def reactivate_product(request: Request, product_id: str, actor: CatalogAdmin) -> Response:
    return run_idempotent(
        request,
        operation=PRODUCTS_REACTIVATE,
        body=None,
        command=lambda execution: _set_product_status(
            execution, actor, product_id, ACTIVE, PRODUCT_REACTIVATED
        ),
        catalog_lock=True,
    )


@router.post("/v1/skus/{sku_id}/deactivate", dependencies=_WRITE_WITHOUT_BODY)
def deactivate_sku(request: Request, sku_id: str, actor: CatalogAdmin) -> Response:
    return run_idempotent(
        request,
        operation=SKUS_DEACTIVATE,
        body=None,
        command=lambda execution: _set_sku_status(
            execution, actor, sku_id, INACTIVE, SKU_DEACTIVATED
        ),
        catalog_lock=True,
    )


@router.post("/v1/skus/{sku_id}/reactivate", dependencies=_WRITE_WITHOUT_BODY)
def reactivate_sku(request: Request, sku_id: str, actor: CatalogAdmin) -> Response:
    return run_idempotent(
        request,
        operation=SKUS_REACTIVATE,
        body=None,
        command=lambda execution: _set_sku_status(
            execution, actor, sku_id, ACTIVE, SKU_REACTIVATED
        ),
        catalog_lock=True,
    )
