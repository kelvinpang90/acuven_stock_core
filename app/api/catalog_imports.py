"""批量导入的预览、确认与查询接口。

契约 §2.3、§2.7、§7.2、§10 第 4 条。
预览与确认要求 catalog.write 与 catalog_admin 角色，经幂等执行器；
预览不写目录、不取目录写锁，确认取目录写锁。查询要求 catalog.read、不要求角色。
预览的正文只在第 7 步查顶层（只有 rows，1–100 个 JSON 对象），
行内字段的问题由预览判定记为该行 invalid_format，不整单拒绝。
查库与写库在 app/services/catalog_imports.py。
"""

import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import authorize, cross_tenant_not_found, get_access
from app.api.catalog import CatalogAdmin, format_timestamp
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
from app.domain.catalog_imports import summarize_preview
from app.domain.requests import Actor
from app.domain.service_auth import CATALOG_READ, CATALOG_WRITE
from app.services import catalog_imports
from app.services.catalog import ChangeAuthor
from app.services.catalog_imports import CONFIRMED, PREVIEWED, ImportRecord
from app.services.idempotency import CommandResult, Execution, run_idempotent
from app.services.security_events import CROSS_TENANT

IMPORT = "import"
IMPORT_NOT_FOUND = "IMPORT_NOT_FOUND"
IMPORT_EXPIRED = "IMPORT_EXPIRED"
# 由 previewed 且 expires_at 不晚于数据库当前时间推出，不存。
EXPIRED = "expired"

CATALOG_IMPORTS_PREVIEW = "catalog_imports.preview"
CATALOG_IMPORTS_CONFIRM = "catalog_imports.confirm"

ROWS = "rows"

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(CATALOG_READ))]
_WRITE = [Depends(authorize(CATALOG_WRITE, write=True))]


# --- 表示 ---


def import_representation(record: ImportRecord) -> dict:
    previews = catalog_imports.previews_of(record)
    summary = summarize_preview(previews)
    status = EXPIRED if record.status == PREVIEWED and record.expired else record.status
    confirmed_at = record.confirmed_at
    return {
        "import_id": record.import_id,
        "status": status,
        "expires_at": format_timestamp(record.expires_at),
        "created_at": format_timestamp(record.created_at),
        "confirmed_at": None if confirmed_at is None else format_timestamp(confirmed_at),
        "rows": [
            {
                "index": preview.index,
                "external_ref": preview.external_ref,
                "product_ref": preview.product_ref,
                "result": preview.result,
                "invalid_fields": list(preview.invalid_fields),
            }
            for preview in previews
        ],
        "summary": {
            "counts": dict(summary.counts),
            "executable_groups": summary.executable_groups,
            "executable_rows": summary.executable_rows,
        },
        "result": record.result,
    }


def _not_found() -> ApiError:
    """不存在与属于别的租户同一个错误码与 message（§8.3）。"""
    return ApiError(404, IMPORT_NOT_FOUND, f"{IMPORT} not found")


# --- 查询 ---


def _read[T](request: Request, query: Callable[[Connection], T]) -> T:
    """用本请求的连接读，读完回滚结束事务（之后写安全事件要求没有打开的事务）；出错 503。"""
    connection = request_connection(request)
    try:
        result = query(connection)
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error during a catalog import read (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None
    return result


@router.get("/v1/catalog-imports/{import_id}", dependencies=_READ)
def get_import(request: Request, import_id: str) -> JSONResponse:
    tenant_id = get_access(request).principal.tenant_id

    def lookup(connection: Connection) -> tuple[ImportRecord | None, str | None]:
        record = catalog_imports.read_import(connection, tenant_id, import_id)
        owner = None if record is not None else catalog_imports.import_owner(connection, import_id)
        return record, owner

    record, owner = _read(request, lookup)
    if record is None:
        if owner is not None:
            raise cross_tenant_not_found(
                request, object_type=IMPORT, object_id=import_id, code=IMPORT_NOT_FOUND
            )
        raise _not_found()
    return ok(request, import_representation(record))


# --- 预览 ---


class ImportRows(RequestBody):
    """预览的正文：顶层只有 rows，1–100 个 JSON 对象（超过 100 为 413）。

    行内字段不在这里校验。命令用 ParsedBody.raw 里的行（json 解析出的原值）。
    """

    batch_fields = (ROWS,)

    rows: list[dict[str, Any]] = Field(min_length=1)


import_rows_body = json_body(ImportRows)


def _preview(execution: Execution, actor: Actor, rows: list[dict]) -> CommandResult:
    connection = execution.connection
    tenant_id = execution.access.principal.tenant_id
    import_id = catalog_imports.save_preview(
        connection,
        tenant_id,
        rows,
        credential_id=execution.access.principal.credential_id,
        actor_ref=actor.ref,
        request_id=execution.request.state.request_id,
    )
    record = catalog_imports.read_import(connection, tenant_id, import_id)
    return CommandResult(import_representation(record), 201)


@router.post("/v1/catalog-imports", dependencies=_WRITE)
def preview_import(
    request: Request,
    body: Annotated[ParsedBody[ImportRows], Depends(import_rows_body)],
    actor: CatalogAdmin,
) -> Response:
    return run_idempotent(
        request,
        operation=CATALOG_IMPORTS_PREVIEW,
        body=body,
        command=lambda execution: _preview(execution, actor, body.raw[ROWS]),
    )


# --- 确认 ---


def _confirm(execution: Execution, actor: Actor, import_id: str) -> CommandResult:
    """判定顺序：404 → 已确认过原样返回 → 已过期 409 → 确认。

    不存在与属于别的租户同一个 404，后者经执行器登记 cross_tenant。
    """
    connection = execution.connection
    access = execution.access
    tenant_id = access.principal.tenant_id
    record = catalog_imports.lock_import(connection, tenant_id, import_id)
    if record is None:
        if catalog_imports.import_owner(connection, import_id) is not None:
            execution.record_security_event(CROSS_TENANT, {"object_type": IMPORT, "id": import_id})
        raise _not_found()
    if record.status == CONFIRMED:
        return CommandResult(import_representation(record))
    if record.expired:
        raise ApiError(409, IMPORT_EXPIRED, "import has expired, preview it again")
    author = ChangeAuthor(
        credential_id=access.principal.credential_id,
        actor_ref=actor.ref,
        actor_role=actor.role,
        request_id=execution.request.state.request_id,
        idempotency_key=access.idempotency_key,
        reason=None,
        import_id=import_id,
    )
    catalog_imports.confirm_import(connection, tenant_id, record, author)
    record = catalog_imports.read_import(connection, tenant_id, import_id)
    return CommandResult(import_representation(record))


# 确认没有请求体：no_body 在 authorize 之后，空请求体跳过、非空 422。
_WRITE_WITHOUT_BODY = [*_WRITE, Depends(no_body)]


@router.post("/v1/catalog-imports/{import_id}/confirm", dependencies=_WRITE_WITHOUT_BODY)
def confirm_import(request: Request, import_id: str, actor: CatalogAdmin) -> Response:
    return run_idempotent(
        request,
        operation=CATALOG_IMPORTS_CONFIRM,
        body=None,
        command=lambda execution: _confirm(execution, actor, import_id),
        catalog_lock=True,
    )
