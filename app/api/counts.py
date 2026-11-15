"""盘点单的开单、提交实数、确认、取消与查询接口。

契约第 6 节、§6.2、§6.6、§7.2、§10 第 3、4 条。
四个写命令要求 stock.write，经幂等执行器，
业务事务以 READ COMMITTED 开启（app/services/counts.py 说明原因）。
开单、提交、取消接受 stock_operator 与 stock_admin；确认只接受 stock_admin。
池授权：开单按正文的 pool_id；提交、确认与取消按盘点单所属的池，
盘点单不存在或属于别的租户 404 COUNT_NOT_FOUND（后者记 cross_tenant，都不保存）。
提交的 skus 是 1–100 个的批量数组（超过 413），sku_id 重复 422；
余额行按盘点单的池与正文的 SKU 预建。确认的正文是 reason_code（必填）与可选的 note，
不预建余额行（盘点行的余额行在提交时已建好）。取消没有请求体。
两个查询要求 stock.read、不要求角色，不做池授权，只限定在本租户。
查库与写库在 app/services/counts.py。
"""

import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, ValidationInfo, field_validator, model_validator
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
from app.api.errors import ApiError, invalid_request, ok
from app.api.requests import (
    ParsedBody,
    RequestBody,
    discard_connection,
    json_body,
    no_body,
    request_connection,
    unavailable,
)
from app.domain.catalog import InvalidField
from app.domain.requests import STOCK_ADMIN, STOCK_OPERATOR, Actor, is_reference
from app.domain.service_auth import STOCK_READ, STOCK_WRITE
from app.domain.stock import normalize_note, normalize_reason_code, parse_non_negative_quantity
from app.services import counts
from app.services.counts import COUNT, COUNT_NOT_FOUND, CountSku
from app.services.idempotency import CommandResult, Execution, run_idempotent

COUNTS_CREATE = "counts.create"
COUNTS_SUBMIT = "counts.submit"
COUNTS_CONFIRM = "counts.confirm"
COUNTS_CANCEL = "counts.cancel"

COUNT_REF = "count_ref"
SKUS = "skus"

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(STOCK_READ))]
_WRITE = [Depends(authorize(STOCK_WRITE, write=True))]
# 取消没有请求体：no_body 在 authorize 之后，空请求体跳过、非空 422。
_WRITE_WITHOUT_BODY = [*_WRITE, Depends(no_body)]

OperatorOrAdmin = Annotated[Actor, Depends(require_role(STOCK_OPERATOR, STOCK_ADMIN))]
Admin = Annotated[Actor, Depends(require_role(STOCK_ADMIN))]


# --- 请求体（§6.2 第 5 条、§6.6） ---


class CountOpening(RequestBody):
    """可选字段的默认 None 只表示没带；note 给 null 与没带相同。"""

    pool_id: str
    count_ref: str
    note: str | None = None

    @field_validator("count_ref")
    @classmethod
    def check_count_ref(cls, value: str) -> str:
        if not is_reference(value):
            raise InvalidField(COUNT_REF)
        return value

    @field_validator("note")
    @classmethod
    def check_note(cls, value: str | None) -> str | None:
        return normalize_note(value)


class CountedSku(RequestBody):
    sku_id: str
    shelf_count: int
    unsellable_count: int

    @field_validator("shelf_count", "unsellable_count")
    @classmethod
    def check_count(cls, value: int, info: ValidationInfo) -> int:
        return parse_non_negative_quantity(value, info.field_name)


class CountSubmission(RequestBody):
    """skus 是 1–100 个的批量数组（超过 100 为 413）。"""

    batch_fields = (SKUS,)

    skus: list[CountedSku] = Field(min_length=1)

    @model_validator(mode="after")
    def check_unique_skus(self):
        sku_ids = [sku.sku_id for sku in self.skus]
        if len(set(sku_ids)) != len(sku_ids):
            raise ValueError("duplicate sku_id in skus")
        return self

    def count_skus(self) -> list[CountSku]:
        return [CountSku(sku.sku_id, sku.shelf_count, sku.unsellable_count) for sku in self.skus]


class CountConfirmation(RequestBody):
    """note 的默认 None 只表示没带：给 null 与没带相同；reason_code 给 null 照样 422。"""

    reason_code: str
    note: str | None = None

    @field_validator("reason_code")
    @classmethod
    def check_reason_code(cls, value: str) -> str:
        return normalize_reason_code(value)

    @field_validator("note")
    @classmethod
    def check_note(cls, value: str | None) -> str | None:
        return normalize_note(value)


opening_body = json_body(CountOpening)
submission_body = json_body(CountSubmission)
confirmation_body = json_body(CountConfirmation)
OpeningBody = Annotated[ParsedBody[CountOpening], Depends(opening_body)]
SubmissionBody = Annotated[ParsedBody[CountSubmission], Depends(submission_body)]
ConfirmationBody = Annotated[ParsedBody[CountConfirmation], Depends(confirmation_body)]


def _opening_pool(body: OpeningBody) -> str:
    return body.model.pool_id


def _path_count_id(count_id: str) -> str:
    return count_id


_count_pool = require_object_pool(
    _path_count_id,
    counts.find_count_pool,
    object_type=COUNT,
    not_found_code=COUNT_NOT_FOUND,
)


# --- 开单 ---


@router.post("/v1/counts", dependencies=_WRITE)
def create_count(
    request: Request,
    body: OpeningBody,
    actor: OperatorOrAdmin,
    pool_id: Annotated[str, Depends(require_pool(_opening_pool))],
) -> Response:
    model = body.model

    def command(execution: Execution) -> CommandResult:
        return counts.create_count(execution, pool_id, model.count_ref, model.note)

    return run_idempotent(
        request, operation=COUNTS_CREATE, body=body, command=command, read_committed=True
    )


# --- 提交实数 ---


@router.post("/v1/counts/{count_id}/submit", dependencies=_WRITE)
def submit_count(
    request: Request,
    count_id: str,
    body: SubmissionBody,
    actor: OperatorOrAdmin,
    pool_id: Annotated[str, Depends(_count_pool)],
) -> Response:
    skus = body.model.count_skus()

    def balance_rows(connection: Connection) -> list[tuple[str, str]]:
        return [(pool_id, sku.sku_id) for sku in skus]

    def command(execution: Execution) -> CommandResult:
        return counts.submit_count(execution, count_id, skus)

    return run_idempotent(
        request,
        operation=COUNTS_SUBMIT,
        body=body,
        command=command,
        balance_rows=balance_rows,
        read_committed=True,
    )


# --- 确认 ---


@router.post("/v1/counts/{count_id}/confirm", dependencies=_WRITE)
def confirm_count(
    request: Request,
    count_id: str,
    body: ConfirmationBody,
    actor: Admin,
    pool_id: Annotated[str, Depends(_count_pool)],
) -> Response:
    model = body.model

    def command(execution: Execution) -> CommandResult:
        return counts.confirm_count(execution, count_id, model.reason_code, model.note)

    return run_idempotent(
        request, operation=COUNTS_CONFIRM, body=body, command=command, read_committed=True
    )


# --- 取消 ---


@router.post("/v1/counts/{count_id}/cancel", dependencies=_WRITE_WITHOUT_BODY)
def cancel_count(
    request: Request,
    count_id: str,
    actor: OperatorOrAdmin,
    pool_id: Annotated[str, Depends(_count_pool)],
) -> Response:
    def command(execution: Execution) -> CommandResult:
        return counts.cancel_count(execution, count_id)

    return run_idempotent(
        request, operation=COUNTS_CANCEL, body=None, command=command, read_committed=True
    )


# --- 查询 ---


def _read[T](request: Request, query: Callable[[Connection], T]) -> T:
    """用本请求的连接读，读完回滚结束事务（之后写安全事件要求没有打开的事务）；出错 503。"""
    connection = request_connection(request)
    try:
        result = query(connection)
        connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error during a count read (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None
    return result


@router.get("/v1/counts/{count_id}", dependencies=_READ)
def get_count(request: Request, count_id: str) -> JSONResponse:
    """不存在与属于别的租户同一个 404，后者记 cross_tenant。"""
    tenant_id = get_access(request).principal.tenant_id

    def lookup(connection: Connection) -> tuple[dict | None, str | None]:
        found = counts.current_representation(connection, tenant_id, count_id)
        owner = None if found is not None else counts.count_owner(connection, count_id)
        return found, owner

    found, owner = _read(request, lookup)
    if found is None:
        if owner is not None:
            raise cross_tenant_not_found(
                request, object_type=COUNT, object_id=count_id, code=COUNT_NOT_FOUND
            )
        raise ApiError(404, COUNT_NOT_FOUND, f"{COUNT} not found")
    return ok(request, found)


def _count_ref_parameter(request: Request) -> str:
    """count_ref 必填且只出现一次，别的查询参数都不收，取值是第 1 节的引用格式。

    不回显未知参数的名字与参数值。
    """
    names = [name for name, _ in request.query_params.multi_items()]
    if any(name != COUNT_REF for name in names):
        raise invalid_request("unknown query parameter")
    if names.count(COUNT_REF) > 1:
        raise invalid_request("query parameter given more than once", {"parameter": COUNT_REF})
    if not names:
        raise invalid_request("missing query parameter", {"parameter": COUNT_REF})
    value = request.query_params[COUNT_REF]
    if not is_reference(value):
        raise invalid_request(f"invalid {COUNT_REF} parameter", {"parameter": COUNT_REF})
    return value


@router.get("/v1/counts", dependencies=_READ)
def list_counts(request: Request) -> JSONResponse:
    """按凭据的销售入口查，counts 为 0 或 1 个。

    凭据没有销售入口或入口已停用时它不可能有自己的 count_ref，返回空数组。
    """
    count_ref = _count_ref_parameter(request)
    principal = get_access(request).principal
    if principal.channel_id is None:
        return ok(request, {"counts": []})

    def lookup(connection: Connection) -> list[dict]:
        count = counts.find_count_by_ref(
            connection, principal.tenant_id, principal.channel_id, count_ref
        )
        if count is None:
            return []
        lines = counts.read_lines(connection, principal.tenant_id, count.count_id)
        return [counts.count_representation(count, lines)]

    return ok(request, {"counts": _read(request, lookup)})
