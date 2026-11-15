"""商品批量导入的预览、确认与查询接口（契约 §2.3、§2.7、§7.2）。

预览与确认要求 catalog.write 与 catalog_admin 角色，经幂等执行器；
预览不写目录、不取目录写锁，确认取目录写锁。查询要求 catalog.read、不要求角色。
预览的正文顶层只有 rows（1–100 个 JSON 对象，超过 100 为 413），其余整单 422；
行内字段的问题不整单拒绝，由 app/domain/catalog_imports.py 记为该行 invalid_format。
逐行判定、汇总与确认计划用 app/domain/catalog_imports.py 的纯函数；
查库与写库在 app/services/catalog_imports.py，建商品与 SKU 用 app/services/catalog.py。

表示里不出现调用方的任意值：external_ref、product_ref 只在行里是字符串时回显，
其余字段都是 Core 生成或规范化后的值（响应体也存进幂等记录的 JSON 列）。
"""

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import authorize, cross_tenant_not_found, get_access, require_role
from app.api.catalog import format_timestamp
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
from app.domain.catalog import InvalidField, normalize_barcode, normalize_sku_code
from app.domain.catalog_imports import (
    BARCODE,
    CREATED,
    FAILED,
    OK,
    SKIPPED,
    SKU_CODE,
    RowOutcome,
    plan_confirm,
    preview_import,
    recheck_conflicts,
    summarize_preview,
)
from app.domain.requests import CATALOG_ADMIN, Actor
from app.domain.service_auth import CATALOG_READ, CATALOG_WRITE
from app.services import catalog, catalog_imports
from app.services.catalog import ChangeAuthor, SkuFields
from app.services.catalog_imports import CONFIRMED, ImportRecord, echo
from app.services.idempotency import CommandResult, Execution, run_idempotent
from app.services.security_events import CROSS_TENANT

IMPORT_NOT_FOUND = "IMPORT_NOT_FOUND"
IMPORT_EXPIRED = "IMPORT_EXPIRED"

CATALOG_IMPORTS_PREVIEW = "catalog_imports.preview"
CATALOG_IMPORTS_CONFIRM = "catalog_imports.confirm"

IMPORT = "import"
ROWS = "rows"

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(CATALOG_READ))]
_WRITE = [Depends(authorize(CATALOG_WRITE, write=True))]
# 确认没有请求体：no_body 在 authorize 之后，空请求体跳过、非空 422。
_WRITE_WITHOUT_BODY = [*_WRITE, Depends(no_body)]

CatalogAdmin = Annotated[Actor, Depends(require_role(CATALOG_ADMIN))]


# --- 表示 ---


def _outcome(row: RowOutcome) -> dict:
    """确认的一行；键都在，不适用的为 None。"""
    return {
        "index": row.index,
        "external_ref": echo(row.external_ref),
        "product_ref": echo(row.product_ref),
        "outcome": row.outcome,
        "product_id": row.product_id,
        "sku_id": row.sku_id,
        "preview_result": row.result if row.outcome == SKIPPED else None,
        "failure": row.result if row.outcome == FAILED else None,
    }


def import_representation(record: ImportRecord) -> dict:
    previews = catalog_imports.row_previews(record.rows)
    summary = summarize_preview(previews)
    confirmed_at = record.confirmed_at
    return {
        "import_id": record.import_id,
        "status": record.current_status,
        "expires_at": format_timestamp(record.expires_at),
        "created_at": format_timestamp(record.created_at),
        "confirmed_at": None if confirmed_at is None else format_timestamp(confirmed_at),
        "rows": [
            {
                "index": preview.index,
                "external_ref": echo(preview.external_ref),
                "product_ref": echo(preview.product_ref),
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
        # 逐行结果在确认时已按 _outcome 的格式存放。
        "result": None if record.result is None else record.result["rows"],
    }


def _not_found() -> ApiError:
    """不存在与属于别的租户同一个错误体（与 cross_tenant_not_found 的相同）。"""
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
    """过期的预览照常返回，status 为 expired。

    属于别的租户的与不存在的同一个 404，前者另记 cross_tenant。
    """
    tenant_id = get_access(request).principal.tenant_id

    def lookup(connection: Connection) -> tuple[ImportRecord | None, str | None]:
        record = catalog_imports.read_import(connection, tenant_id, import_id)
        if record is not None:
            return record, None
        return None, catalog_imports.import_owner(connection, import_id)

    record, owner = _read(request, lookup)
    if record is None:
        if owner is not None:
            raise cross_tenant_not_found(
                request, object_type=IMPORT, object_id=import_id, code=IMPORT_NOT_FOUND
            )
        raise _not_found()
    return ok(request, import_representation(record))


# --- 预览 ---


class ImportPreview(RequestBody):
    """顶层只有 rows：1–100 个 JSON 对象。行内字段不在这里校验。"""

    batch_fields = (ROWS,)

    rows: list[dict] = Field(min_length=1)


import_preview_body = json_body(ImportPreview)


def _requested_codes(rows: Iterable[Mapping]) -> tuple[set[str], set[str]]:
    """本次导入里规范化得了的 sku_code 与非空 barcode，用来查现有目录。

    不合规的值不查：那些行是 invalid_format，不参加 conflicts_existing 检查。
    """
    sku_codes, barcodes = set(), set()
    for row in rows:
        try:
            sku_codes.add(normalize_sku_code(row.get(SKU_CODE)))
        except InvalidField:
            pass
        try:
            barcode = normalize_barcode(row.get(BARCODE))
        except InvalidField:
            continue
        if barcode is not None:
            barcodes.add(barcode)
    return sku_codes, barcodes


def _preview(execution: Execution, actor: Actor, rows: list[dict]) -> CommandResult:
    """rows 是调用方发来的原值（JSON 解析结果），各行原文照它存。"""
    connection = execution.connection
    tenant_id = execution.access.principal.tenant_id
    sku_codes, barcodes = _requested_codes(rows)
    claimed, taken = catalog_imports.existing_code_keys(connection, tenant_id, sku_codes, barcodes)
    previews = preview_import(rows, claimed, taken)
    import_id = catalog_imports.insert_import(
        connection,
        tenant_id,
        catalog_imports.stored_rows(rows, previews),
        credential_id=execution.access.principal.credential_id,
        actor_ref=actor.ref,
        request_id=execution.request.state.request_id,
    )
    record = catalog_imports.read_import(connection, tenant_id, import_id)
    return CommandResult(import_representation(record), 201)


@router.post("/v1/catalog-imports", dependencies=_WRITE)
def preview_catalog_import(
    request: Request,
    body: Annotated[ParsedBody[ImportPreview], Depends(import_preview_body)],
    actor: CatalogAdmin,
) -> Response:
    return run_idempotent(
        request,
        operation=CATALOG_IMPORTS_PREVIEW,
        body=body,
        command=lambda execution: _preview(execution, actor, body.raw[ROWS]),
    )


# --- 确认 ---


def _create_groups(
    execution: Execution, actor: Actor, import_id: str, record: ImportRecord
) -> list[dict]:
    """按当前目录重查预览为 ok 的行，照确认计划建商品与 SKU。

    返回逐行结果（_outcome 的格式）。目录写锁保证重查到写入之间
    没有别的目录写入，所以建商品时不会再报冲突；万一报了，
    CatalogConflict 不是业务拒绝，执行器整笔回滚并返回 500。
    """
    connection = execution.connection
    access = execution.access
    tenant_id = access.principal.tenant_id
    previews = catalog_imports.row_previews(record.rows)
    ok_values = [preview.values for preview in previews if preview.result == OK]
    claimed, taken = catalog_imports.existing_code_keys(
        connection,
        tenant_id,
        {values.sku_code for values in ok_values},
        {values.barcode for values in ok_values if values.barcode is not None},
    )
    plan = plan_confirm(previews, recheck_conflicts(previews, claimed, taken))

    author = ChangeAuthor(
        credential_id=access.principal.credential_id,
        actor_ref=actor.ref,
        actor_role=actor.role,
        request_id=execution.request.state.request_id,
        idempotency_key=access.idempotency_key,
        reason=None,
        import_id=import_id,
    )
    created = {}
    for product in plan.products:
        skus = [
            SkuFields(
                sku_code=sku.sku_code,
                barcode=sku.barcode,
                name=sku.name,
                reference_price=sku.reference_price,
            )
            for sku in product.skus
        ]
        product_id, sku_ids = catalog.create_product_with_skus(
            connection, tenant_id, product.name, skus, author
        )
        for sku, sku_id in zip(product.skus, sku_ids, strict=True):
            created[sku.index] = (product_id, sku_id)
    return [
        _outcome(row.with_ids(*created[row.index]) if row.outcome == CREATED else row)
        for row in plan.rows
    ]


def _confirm(execution: Execution, actor: Actor, import_id: str) -> CommandResult:
    """依次判定：不存在或属于别的租户 404，已确认过原样返回，已过有效期 409；

    否则按组建，一组都没建成也算确认完成。
    """
    connection = execution.connection
    access = execution.access
    tenant_id = access.principal.tenant_id
    record = catalog_imports.lock_import(connection, tenant_id, import_id)
    if record is None:
        if catalog_imports.import_owner(connection, import_id) is not None:
            details = {"object_type": IMPORT, "id": import_id}
            execution.record_security_event(CROSS_TENANT, details)
        raise _not_found()
    if record.status == CONFIRMED:
        return CommandResult(import_representation(record))
    if record.expired:
        raise ApiError(409, IMPORT_EXPIRED, "import has expired, preview it again")

    result_rows = _create_groups(execution, actor, import_id, record)
    catalog_imports.confirm_import(
        connection,
        tenant_id,
        import_id,
        result_rows,
        credential_id=access.principal.credential_id,
        actor_ref=actor.ref,
        request_id=execution.request.state.request_id,
    )
    confirmed = catalog_imports.read_import(connection, tenant_id, import_id)
    return CommandResult(import_representation(confirmed))


@router.post("/v1/catalog-imports/{import_id}/confirm", dependencies=_WRITE_WITHOUT_BODY)
def confirm_catalog_import(request: Request, import_id: str, actor: CatalogAdmin) -> Response:
    return run_idempotent(
        request,
        operation=CATALOG_IMPORTS_CONFIRM,
        body=None,
        command=lambda execution: _confirm(execution, actor, import_id),
        catalog_lock=True,
    )
