"""商品与 SKU 的查询接口（契约 §2.1、§2.4、§2.6 第 6、7 条、§7.2）。

读接口要求 catalog.read、不要求角色；带了操作者头照样校验格式（authorize 里做）。
查库在 app/services/catalog.py。product_representation、sku_representation 与 format_timestamp
供之后的目录写命令、批量导入与变更流共用。
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import authorize, cross_tenant_not_found, get_access
from app.api.errors import ApiError, invalid_request, ok
from app.api.requests import MAX_BATCH_ITEMS, discard_connection, request_connection, unavailable
from app.domain.catalog import (
    BARCODE,
    PRODUCT,
    SKU,
    SKU_CODE,
    InvalidField,
    normalize_barcode,
    normalize_sku_code,
)
from app.domain.service_auth import CATALOG_READ
from app.services import catalog
from app.services.catalog import ProductRecord, SkuRecord

PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
SKU_NOT_FOUND = "SKU_NOT_FOUND"

IDS = "ids"
LIST_PARAMETERS = (SKU_CODE, BARCODE, IDS)

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(CATALOG_READ))]


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
