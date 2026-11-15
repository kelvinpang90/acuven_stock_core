"""池、余额与流水的查询接口。

契约 §6.2 第 1 条、§6.3 池的查询、§6.8、§7.2。三个接口都要求 stock.read、不要求角色，
不做池授权，只限定在本租户；带了操作者头照样校验格式（authorize 里做）。
查询参数先校验（不合规 422），再查池与 SKU：路径或必填参数指向的池、SKU 不存在与属于别的租户
同一个 404，后者记 cross_tenant。余额里查不到的 SKU 不出现、不报错、不记事件。
查库在 app/services/stock_queries.py。
"""

import logging
from collections.abc import Callable, Sequence

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import POOL_NOT_FOUND, authorize, cross_tenant_not_found, get_access
from app.api.errors import ApiError, invalid_request, ok
from app.api.requests import discard_connection, request_connection, unavailable
from app.domain.catalog import AFTER, LIMIT, InvalidParameter
from app.domain.quantities import Quantities
from app.domain.service_auth import STOCK_READ
from app.domain.stock import (
    FROM,
    SKU_IDS,
    TO,
    format_ledger_cursor,
    parse_ledger_cursor,
    parse_ledger_limit,
    parse_ledger_range,
    parse_sku_ids,
)
from app.services import catalog, stock_queries
from app.services.stock import SKU, SKU_NOT_FOUND, entry_representation, format_timestamp
from app.services.stock_queries import BalanceRecord, PoolRecord

POOL = "pool"
POOL_ID = "pool_id"
SKU_ID = "sku_id"

BALANCE_PARAMETERS = (POOL_ID, SKU_IDS)
LEDGER_REQUIRED = (POOL_ID, SKU_ID)
LEDGER_OPTIONAL = (FROM, TO, AFTER, LIMIT)

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(STOCK_READ))]


# --- 查库 ---


def _read[T](request: Request, query: Callable[[Connection], T]) -> T:
    """用本请求的连接读，读完回滚结束事务（之后写安全事件要求没有打开的事务）；出错 503。"""
    connection = request_connection(request)
    try:
        result = query(connection)
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error during a stock read (request_id=%s)",
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
    """属于别的租户时另记 cross_tenant，对外与不存在完全相同（§8.3）。"""
    if owner is not None:
        return cross_tenant_not_found(
            request, object_type=object_type, object_id=object_id, code=code
        )
    return ApiError(404, code, f"{object_type} not found")


def _require_pool(request: Request, pool_id: str) -> None:
    """必填参数指向的池不属于本租户：404 POOL_NOT_FOUND。"""
    owner = _read(request, lambda connection: stock_queries.pool_owner(connection, pool_id))
    if owner != _tenant_id(request):
        raise _not_found(
            request, object_type=POOL, object_id=pool_id, code=POOL_NOT_FOUND, owner=owner
        )


def _require_sku(request: Request, sku_id: str) -> None:
    """必填参数指向的 SKU 不属于本租户：404 SKU_NOT_FOUND；已合并的照常通过。"""
    owner = _read(request, lambda connection: catalog.sku_owner(connection, sku_id))
    if owner != _tenant_id(request):
        raise _not_found(
            request, object_type=SKU, object_id=sku_id, code=SKU_NOT_FOUND, owner=owner
        )


# --- 查询参数 ---


def _parameters(
    request: Request, required: Sequence[str], optional: Sequence[str] = ()
) -> dict[str, str]:
    """各参数最多出现一次，required 都要带，别的查询参数都不收；返回参数名到原值。

    不回显未知参数的名字与参数值。
    """
    items = request.query_params.multi_items()
    names = [name for name, _ in items]
    known = (*required, *optional)
    if any(name not in known for name in names):
        raise invalid_request("unknown query parameter")
    for name in known:
        if names.count(name) > 1:
            raise invalid_request("query parameter given more than once", {"parameter": name})
    for name in required:
        if name not in names:
            raise invalid_request("missing query parameter", {"parameter": name})
    return dict(items)


def _invalid_parameter(error: InvalidParameter) -> ApiError:
    message = f"invalid {error.parameter} parameter"
    return invalid_request(message, {"parameter": error.parameter})


# --- 表示 ---


def pool_representation(pool: PoolRecord) -> dict:
    return {
        "pool_id": pool.pool_id,
        "location_id": pool.location_id,
        "name": pool.name,
        "status": pool.status,
        "went_live_at": None if pool.went_live_at is None else format_timestamp(pool.went_live_at),
        "created_at": format_timestamp(pool.created_at),
    }


def balance_representation(balance: BalanceRecord) -> dict:
    """四个量、派生的 available、balance_version 与 merged_into_sku_id（§6.8）。"""
    quantities = Quantities(
        balance.stock_balance, balance.reserved, balance.unavailable, balance.paid_pending_dispatch
    )
    return {
        "sku_id": balance.sku_id,
        "stock_balance": quantities.stock_balance,
        "reserved": quantities.reserved,
        "unavailable": quantities.unavailable,
        "paid_pending_dispatch": quantities.paid_pending_dispatch,
        "available": quantities.available,
        "balance_version": balance.balance_version,
        "merged_into_sku_id": balance.merged_into_sku_id,
    }


# --- 接口 ---


@router.get("/v1/pools/{pool_id}", dependencies=_READ)
def get_pool(request: Request, pool_id: str) -> JSONResponse:
    tenant_id = _tenant_id(request)

    def lookup(connection: Connection) -> tuple[PoolRecord | None, str | None]:
        pool = stock_queries.read_pool(connection, tenant_id, pool_id)
        owner = None if pool is not None else stock_queries.pool_owner(connection, pool_id)
        return pool, owner

    pool, owner = _read(request, lookup)
    if pool is None:
        raise _not_found(
            request, object_type=POOL, object_id=pool_id, code=POOL_NOT_FOUND, owner=owner
        )
    return ok(request, pool_representation(pool))


@router.get("/v1/balances", dependencies=_READ)
def list_balances(request: Request) -> JSONResponse:
    """结果按 sku_id 升序、不分页；本租户的 SKU 还没有余额行时四个量与版本都为 0。"""
    tenant_id = _tenant_id(request)
    values = _parameters(request, BALANCE_PARAMETERS)
    try:
        sku_ids = parse_sku_ids(values[SKU_IDS])
    except InvalidParameter as error:
        raise _invalid_parameter(error) from None
    pool_id = values[POOL_ID]
    _require_pool(request, pool_id)
    balances = _read(
        request,
        lambda connection: stock_queries.read_balances(connection, tenant_id, pool_id, sku_ids),
    )
    return ok(request, {"balances": [balance_representation(b) for b in balances]})


@router.get("/v1/ledger", dependencies=_READ)
def list_ledger(request: Request) -> JSONResponse:
    """按 balance_version_after 升序；已合并的 SKU 返回它自己的流水（§2.4）。

    next_cursor 为最后一条的游标；没有记录时原样返回请求里的 after，没带则为 null。
    """
    tenant_id = _tenant_id(request)
    values = _parameters(request, LEDGER_REQUIRED, LEDGER_OPTIONAL)
    pool_id, sku_id, after = values[POOL_ID], values[SKU_ID], values.get(AFTER)
    try:
        start, end = parse_ledger_range(values.get(FROM), values.get(TO))
        after_version = parse_ledger_cursor(after, pool_id, sku_id, start, end)
        limit = parse_ledger_limit(values.get(LIMIT))
    except InvalidParameter as error:
        raise _invalid_parameter(error) from None
    _require_pool(request, pool_id)
    _require_sku(request, sku_id)
    rows = _read(
        request,
        lambda connection: stock_queries.read_ledger(
            connection,
            tenant_id,
            pool_id,
            sku_id,
            start=start,
            end=end,
            after_version=after_version,
            limit=limit,
        ),
    )
    entries = [entry_representation(row) for row in rows]
    next_cursor = after
    if rows:
        last = rows[-1]["balance_version_after"]
        next_cursor = format_ledger_cursor(last, pool_id, sku_id, start, end)
    return ok(request, {"entries": entries, "next_cursor": next_cursor})
