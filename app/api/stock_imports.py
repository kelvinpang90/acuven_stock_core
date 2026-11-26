"""期初导入的预览、确认与查询接口，以及池切 live。

契约 §6.2、§6.3、§7.2、§10 第 3、4 条。
预览、确认与池切 live 要求 stock.write 与 stock_admin 角色，经幂等执行器；
查询要求 stock.read、不要求角色，不做池授权，只限定在本租户。
池授权：预览按正文的 pool_id，确认按导入所属的池，池切 live 按路径的 pool_id。
预览的正文只在第 7 步查顶层：只有 pool_id 与 rows，pool_id 是字符串，
rows 是 1–100 个 JSON 对象；行内字段的问题由预览判定记在该行的结果里，不整单拒绝。
查库与写库在 app/services/stock_imports.py。
"""

import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import (
    authorize,
    cross_tenant_not_found,
    get_access,
    require_object_pool,
    require_pool,
    require_role,
)
from app.api.errors import ApiError, ok
from app.api.requests import (
    ParsedBody,
    RequestBody,
    discard_connection,
    json_body,
    no_body,
    request_connection,
    unavailable,
)
from app.api.stock_queries import pool_representation
from app.domain.requests import STOCK_ADMIN, Actor
from app.domain.service_auth import STOCK_READ, STOCK_WRITE
from app.domain.stock_imports import summarize
from app.services import stock_imports
from app.services.idempotency import CommandResult, Execution, run_idempotent
from app.services.stock import format_timestamp, require_initializing
from app.services.stock_imports import CONFIRMED, PREVIEWED, ImportRecord

STOCK_IMPORT = "stock_import"
STOCK_IMPORT_NOT_FOUND = "STOCK_IMPORT_NOT_FOUND"
IMPORT_EXPIRED = "IMPORT_EXPIRED"
# 由 previewed 且 expires_at 不晚于数据库当前时间推出，不存。
EXPIRED = "expired"

STOCK_IMPORTS_PREVIEW = "stock_imports.preview"
STOCK_IMPORTS_CONFIRM = "stock_imports.confirm"
POOLS_GO_LIVE = "pools.go_live"

ROWS = "rows"

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(STOCK_READ))]
_WRITE = [Depends(authorize(STOCK_WRITE, write=True))]
# 确认与池切 live 没有请求体：no_body 在 authorize 之后，空请求体跳过、非空 422。
_WRITE_WITHOUT_BODY = [*_WRITE, Depends(no_body)]

StockAdmin = Annotated[Actor, Depends(require_role(STOCK_ADMIN))]


# --- 表示 ---


def import_representation(record: ImportRecord) -> dict:
    previews = stock_imports.previews_of(record)
    status = EXPIRED if record.status == PREVIEWED and record.expired else record.status
    confirmed_at = record.confirmed_at
    return {
        "import_id": record.import_id,
        "pool_id": record.pool_id,
        "status": status,
        "expires_at": format_timestamp(record.expires_at),
        "created_at": format_timestamp(record.created_at),
        "confirmed_at": None if confirmed_at is None else format_timestamp(confirmed_at),
        "rows": previews,
        "summary": summarize(previews),
        "result": record.result,
    }


# --- 查询 ---


def _read[T](request: Request, query: Callable[[Connection], T]) -> T:
    """用本请求的连接读，读完回滚结束事务（之后写安全事件要求没有打开的事务）；出错 503。"""
    connection = request_connection(request)
    try:
        result = query(connection)
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error during a stock import read (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None
    return result


@router.get("/v1/stock-imports/{import_id}", dependencies=_READ)
def get_import(request: Request, import_id: str) -> JSONResponse:
    """不存在与属于别的租户同一个 404，后者记 cross_tenant。"""
    tenant_id = get_access(request).principal.tenant_id

    def lookup(connection: Connection) -> tuple[ImportRecord | None, str | None]:
        record = stock_imports.read_import(connection, tenant_id, import_id)
        owner = None if record is not None else stock_imports.import_owner(connection, import_id)
        return record, owner

    record, owner = _read(request, lookup)
    if record is None:
        if owner is not None:
            raise cross_tenant_not_found(
                request,
                object_type=STOCK_IMPORT,
                object_id=import_id,
                code=STOCK_IMPORT_NOT_FOUND,
            )
        raise ApiError(404, STOCK_IMPORT_NOT_FOUND, f"{STOCK_IMPORT} not found")
    return ok(request, import_representation(record))


# --- 预览 ---


class ImportRows(RequestBody):
    """预览的正文：顶层只有 pool_id 与 rows；rows 是 1–100 个 JSON 对象（超过 100 为 413）。

    行内字段不在这里校验。命令用 ParsedBody.raw 里的行（json 解析出的原值）。
    """

    batch_fields = (ROWS,)

    pool_id: str
    rows: list[dict[str, Any]] = Field(min_length=1)


import_rows_body = json_body(ImportRows)
ImportRowsBody = Annotated[ParsedBody[ImportRows], Depends(import_rows_body)]


def _body_pool(body: ImportRowsBody) -> str:
    return body.model.pool_id


def _preview(execution: Execution, actor: Actor, pool_id: str, rows: list[dict]) -> CommandResult:
    """池已 live：409 POOL_ALREADY_LIVE（保存）；否则逐行判定、保存导入，201。"""
    require_initializing(execution, pool_id)
    connection = execution.connection
    context = execution.context
    import_id = stock_imports.save_preview(
        connection,
        context.tenant_id,
        pool_id,
        rows,
        credential_id=context.credential_id,
        actor_ref=actor.ref,
        request_id=context.request_id,
    )
    record = stock_imports.read_import(connection, context.tenant_id, import_id)
    return CommandResult(import_representation(record), 201)


@router.post("/v1/stock-imports", dependencies=_WRITE)
def preview_import(
    request: Request,
    body: ImportRowsBody,
    actor: StockAdmin,
    pool_id: Annotated[str, Depends(require_pool(_body_pool))],
) -> Response:
    return run_idempotent(
        request,
        operation=STOCK_IMPORTS_PREVIEW,
        body=body,
        command=lambda execution: _preview(execution, actor, pool_id, body.raw[ROWS]),
    )


# --- 确认 ---


def _path_import_id(import_id: str) -> str:
    return import_id


_import_pool = require_object_pool(
    _path_import_id,
    stock_imports.find_import_pool,
    object_type=STOCK_IMPORT,
    not_found_code=STOCK_IMPORT_NOT_FOUND,
)


def _confirm(execution: Execution, actor: Actor, import_id: str) -> CommandResult:
    """判定顺序照 §6.3：已确认过原样返回 → 已过期 409 → 池已 live 409 → 逐行确认。"""
    connection = execution.connection
    tenant_id = execution.context.tenant_id
    record = stock_imports.lock_import(connection, tenant_id, import_id)
    if record is None:
        # 池授权已确认导入属于本租户，导入又不删除。
        raise RuntimeError(f"stock import {import_id} is not in the tenant")
    if record.status == CONFIRMED:
        return CommandResult(import_representation(record))
    if record.expired:
        raise ApiError(409, IMPORT_EXPIRED, "import has expired, preview it again")
    stock_imports.confirm_import(execution, record, actor)
    record = stock_imports.read_import(connection, tenant_id, import_id)
    return CommandResult(import_representation(record))


@router.post("/v1/stock-imports/{import_id}/confirm", dependencies=_WRITE_WITHOUT_BODY)
def confirm_import(
    request: Request,
    import_id: str,
    actor: StockAdmin,
    pool_id: Annotated[str, Depends(_import_pool)],
) -> Response:
    tenant_id = get_access(request).principal.tenant_id

    def balance_rows(connection: Connection) -> list[tuple[str, str]]:
        return stock_imports.opening_rows(connection, tenant_id, import_id)

    return run_idempotent(
        request,
        operation=STOCK_IMPORTS_CONFIRM,
        body=None,
        command=lambda execution: _confirm(execution, actor, import_id),
        balance_rows=balance_rows,
    )


# --- 池切 live ---


def _path_pool_id(pool_id: str) -> str:
    return pool_id


@router.post("/v1/pools/{pool_id}/go-live", dependencies=_WRITE_WITHOUT_BODY)
def go_live(
    request: Request,
    actor: StockAdmin,
    pool_id: Annotated[str, Depends(require_pool(_path_pool_id))],
) -> Response:
    """已 live 时 200 与池的当前表示，不再改；否则切到 live，200 与池的表示。"""

    def command(execution: Execution) -> CommandResult:
        return CommandResult(pool_representation(stock_imports.go_live(execution, pool_id, actor)))

    return run_idempotent(request, operation=POOLS_GO_LIVE, body=None, command=command)
