"""目录变更流的拉取接口。

契约 §2.5、§2.7。要求 catalog.read、不要求角色；带了操作者头照样校验格式（authorize 里做）。
只查本租户 id 大于 after 的记录，按 id 升序，最多 limit 条。
after 格式合格但对应的记录不存在（如整机恢复后）时照常返回其后的记录，不查它存不存在。
不取目录写锁：写入方已按提交顺序串行，id 按提交顺序递增（§2.7）。
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, select
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import authorize, get_access
from app.api.catalog import format_timestamp
from app.api.errors import invalid_request, ok
from app.api.requests import discard_connection, request_connection, unavailable
from app.domain.catalog import (
    AFTER,
    LIMIT,
    InvalidParameter,
    format_cursor,
    parse_cursor,
    parse_limit,
)
from app.domain.service_auth import CATALOG_READ
from app.models.catalog import CatalogChange

PARAMETERS = (AFTER, LIMIT)

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(CATALOG_READ))]

# 对外输出的列；credential_id、actor_role 与 idempotency_key 不输出。
_COLUMNS = (
    CatalogChange.id,
    CatalogChange.event_type,
    CatalogChange.object_type,
    CatalogChange.object_id,
    CatalogChange.product_id,
    CatalogChange.version,
    CatalogChange.changes,
    CatalogChange.actor_ref,
    CatalogChange.reason,
    CatalogChange.import_id,
    CatalogChange.request_id,
    CatalogChange.created_at,
)


def _parameters(request: Request) -> tuple[str | None, int | None, int]:
    """after 与 limit 各最多出现一次，别的查询参数都不收。

    返回 after 的原值（没带为 None）、它对应的 id 与 limit。不回显未知参数的名字与参数值。
    """
    items = request.query_params.multi_items()
    names = [name for name, _ in items]
    if any(name not in PARAMETERS for name in names):
        raise invalid_request("unknown query parameter")
    for name in PARAMETERS:
        if names.count(name) > 1:
            raise invalid_request("query parameter given more than once", {"parameter": name})
    values = dict(items)
    after = values.get(AFTER)
    try:
        return after, parse_cursor(after), parse_limit(values.get(LIMIT))
    except InvalidParameter as error:
        message = f"invalid {error.parameter} parameter"
        raise invalid_request(message, {"parameter": error.parameter}) from None


def _change(row) -> dict:
    return {
        "cursor": format_cursor(row.id),
        "event_type": row.event_type,
        "object_type": row.object_type,
        "object_id": row.object_id,
        "product_id": row.product_id,
        "version": row.version,
        "changes": row.changes,
        "actor_ref": row.actor_ref,
        "reason": row.reason,
        "import_id": row.import_id,
        "request_id": row.request_id,
        "occurred_at": format_timestamp(row.created_at),
    }


def _read_changes(
    connection: Connection, tenant_id: str, after_id: int | None, limit: int
) -> list[dict]:
    statement = select(*_COLUMNS).where(CatalogChange.tenant_id == tenant_id)
    if after_id is not None:
        statement = statement.where(CatalogChange.id > after_id)
    statement = statement.order_by(CatalogChange.id).limit(limit)
    return [_change(row) for row in connection.execute(statement)]


@router.get("/v1/catalog-changes", dependencies=_READ)
def list_catalog_changes(request: Request) -> JSONResponse:
    """next_cursor 为最后一条的游标；没有新记录时原样返回请求里的 after，没带则为 null。"""
    tenant_id = get_access(request).principal.tenant_id
    after, after_id, limit = _parameters(request)
    connection = request_connection(request)
    try:
        changes = _read_changes(connection, tenant_id, after_id, limit)
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error during a catalog change read (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None
    next_cursor = changes[-1]["cursor"] if changes else after
    return ok(request, {"changes": changes, "next_cursor": next_cursor})
