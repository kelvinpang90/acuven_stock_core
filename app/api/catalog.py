"""商品与 SKU 的查询与新建接口（契约 §2.1、§2.4、§2.6 第 1–3、6、7 条、§2.7、§7.2）。

读接口要求 catalog.read、不要求角色；带了操作者头照样校验格式（authorize 里做）。
写命令要求 catalog.write 与 catalog_admin 角色，经幂等执行器并取目录写锁；
字段取值与请求内的编码、条码重复在第 7 步拒绝（取锁之前，不保存）。
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
    request_connection,
    unavailable,
)
from app.domain.catalog import (
    BARCODE,
    PRODUCT,
    SKU,
    SKU_CODE,
    InvalidField,
    code_key,
    conflict_code,
    normalize_barcode,
    normalize_name,
    normalize_reason,
    normalize_reference_price,
    normalize_sku_code,
    sort_conflicts,
)
from app.domain.requests import CATALOG_ADMIN, Actor
from app.domain.service_auth import CATALOG_READ, CATALOG_WRITE
from app.services import catalog
from app.services.catalog import (
    CatalogConflict,
    ChangeAuthor,
    ProductRecord,
    SkuFields,
    SkuRecord,
)
from app.services.idempotency import CommandResult, Execution, run_idempotent
from app.services.security_events import CROSS_TENANT

PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
SKU_NOT_FOUND = "SKU_NOT_FOUND"

PRODUCTS_CREATE = "products.create"
SKUS_CREATE = "skus.create"

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


def _add_sku(execution: Execution, actor: Actor, product_id: str, body: AddedSku) -> CommandResult:
    """商品不存在与属于别的租户同一个 404，后者另登记 cross_tenant。

    inactive 的商品照样能加。
    """
    connection = execution.connection
    tenant_id = execution.access.principal.tenant_id
    owner = catalog.product_owner(connection, product_id)
    if owner != tenant_id:
        if owner is not None:
            details = {"object_type": PRODUCT, "id": product_id}
            execution.record_security_event(CROSS_TENANT, details)
        raise ApiError(404, PRODUCT_NOT_FOUND, f"{PRODUCT} not found")
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
