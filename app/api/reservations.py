"""整单预留、释放与预留的查询接口。

契约 §5.2、§5.6、§7.1、§8.1、§10 第 3、4 条。
预留要求 orders.write、不要求角色（操作者头带了照常校验并记录），经幂等执行器，
业务事务以 READ COMMITTED 开启（§5.6 隔离级别）。池是凭据销售入口绑定的池：
凭据没有销售入口或入口已停用 403（第 9 步，不保存）；余额行按这个池与各行 sku_id 预建。
开业务事务之前先关掉所列 SKU 上已到期的预留（执行器的 before_transaction 钩子）；
关单的连接作废、回滚出错或断开时 503，什么也不保存。
正文只有 order_ref 与 lines：lines 是 1–100 行的批量数组（超过 413），
未知字段、引用格式、quantity 不是 1–1,000,000 的 JSON 整数、
line_ref 或 sku_id 重复在第 7 步 422。
有效期取启动时读的配置（app.state.reservation_ttl_seconds），网店不能在请求里指定。
释放要求 orders.write、不要求角色，经幂等执行器，以 READ COMMITTED 开启；
池授权按预留所属的池（预留不存在或属于别的租户 404 RESERVATION_NOT_FOUND，后者记 cross_tenant，
都不保存）。正文只有 reason（三个取值之一），其他 422。不预建余额行（预留时已建好）。
两个查询要求 orders.read、不要求角色：按 ID 查只限定在本租户；按 order_ref 查按凭据的销售入口。
查库与写库在 app/services/reservations.py。
GET /v1/reservations 也是待审列表（§5.9）：order_ref 与 status 必须且只能带一个；
带 status 时只收 status、after、limit，按凭据的销售入口、reservation_id 升序分页，
读库在 app/services/reconciliation.py。
"""

import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, field_validator, model_validator
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import (
    authorize,
    cross_tenant_not_found,
    get_access,
    require_object_pool,
    require_sales_pool,
)
from app.api.errors import ApiError, invalid_request, ok
from app.api.requests import (
    ParsedBody,
    RequestBody,
    discard_connection,
    json_body,
    request_connection,
    unavailable,
)
from app.api.stock_queries import _invalid_parameter, _parameters
from app.domain.catalog import AFTER, LIMIT, InvalidField, InvalidParameter
from app.domain.reconciliation import (
    STATUS,
    format_review_cursor,
    match_review_cursor,
    parse_page_limit,
    parse_review_cursor,
    parse_review_status,
)
from app.domain.requests import is_reference
from app.domain.reservations import RELEASE_REASONS
from app.domain.service_auth import ORDERS_READ, ORDERS_WRITE
from app.domain.stock import parse_positive_quantity
from app.services import reconciliation, reservations
from app.services.idempotency import CommandResult, Execution, run_idempotent
from app.services.reservations import RESERVATION, RESERVATION_NOT_FOUND, ReservationLine

RESERVATIONS_CREATE = "reservations.create"
RESERVATIONS_RELEASE = "reservations.release"

ORDER_REF = "order_ref"
LINES = "lines"
REVIEW_LIST_OPTIONAL = (AFTER, LIMIT)

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(ORDERS_READ))]
_WRITE = [Depends(authorize(ORDERS_WRITE, write=True))]


def _reference(value: str, field: str) -> str:
    """第 1 节的引用格式，原样返回。"""
    if not is_reference(value):
        raise InvalidField(field)
    return value


# --- 请求体（§5.6 预留） ---


class Line(RequestBody):
    line_ref: str
    sku_id: str
    quantity: int

    @field_validator("line_ref")
    @classmethod
    def check_line_ref(cls, value: str) -> str:
        return _reference(value, "line_ref")

    @field_validator("quantity")
    @classmethod
    def check_quantity(cls, value: int) -> int:
        return parse_positive_quantity(value)


class ReservationRequest(RequestBody):
    """lines 是 1–100 行的批量数组（超过 100 为 413）。"""

    batch_fields = (LINES,)

    order_ref: str
    lines: list[Line] = Field(min_length=1)

    @field_validator("order_ref")
    @classmethod
    def check_order_ref(cls, value: str) -> str:
        return _reference(value, ORDER_REF)

    @model_validator(mode="after")
    def check_unique_skus(self):
        """一个订单里同一个 SKU 只占一行（Q-56；line_ref 的重复由 json_body 查）。"""
        sku_ids = [line.sku_id for line in self.lines]
        if len(set(sku_ids)) != len(sku_ids):
            raise ValueError("duplicate sku_id in lines")
        return self

    def reservation_lines(self) -> list[ReservationLine]:
        return [ReservationLine(line.line_ref, line.sku_id, line.quantity) for line in self.lines]


class ReleaseRequest(RequestBody):
    """§5.6 释放：reason 必填，取值 cancelled、payment_failed 或 checkout_replaced。"""

    reason: str

    @field_validator("reason")
    @classmethod
    def check_reason(cls, value: str) -> str:
        if value not in RELEASE_REASONS:
            raise ValueError("unknown release reason")
        return value


reservation_body = json_body(ReservationRequest)
ReservationBody = Annotated[ParsedBody[ReservationRequest], Depends(reservation_body)]
release_body = json_body(ReleaseRequest)
ReleaseBody = Annotated[ParsedBody[ReleaseRequest], Depends(release_body)]


def _path_reservation_id(reservation_id: str) -> str:
    return reservation_id


_reservation_pool = require_object_pool(
    _path_reservation_id,
    reservations.find_reservation_pool,
    object_type=RESERVATION,
    not_found_code=RESERVATION_NOT_FOUND,
)


# --- 预留 ---


@router.post("/v1/reservations", dependencies=_WRITE)
def create_reservation(
    request: Request,
    body: ReservationBody,
    pool_id: Annotated[str, Depends(require_sales_pool)],
) -> Response:
    order_ref = body.model.order_ref
    lines = body.model.reservation_lines()
    ttl_seconds = request.app.state.reservation_ttl_seconds
    tenant_id = get_access(request).principal.tenant_id
    sku_ids = [line.sku_id for line in lines]

    def balance_rows(connection: Connection) -> list[tuple[str, str]]:
        return [(pool_id, sku_id) for sku_id in sku_ids]

    def close_due(connection: Connection) -> None:
        """先关到期（§5.6）：只关凭据的租户、入口的池里所列 SKU 上的。"""
        reservations.close_due_before_reserving(connection, tenant_id, pool_id, sku_ids)

    def command(execution: Execution) -> CommandResult:
        return reservations.create_reservation(execution, pool_id, order_ref, lines, ttl_seconds)

    return run_idempotent(
        request,
        operation=RESERVATIONS_CREATE,
        body=body,
        command=command,
        balance_rows=balance_rows,
        read_committed=True,
        before_transaction=close_due,
    )


# --- 释放 ---


@router.post("/v1/reservations/{reservation_id}/release", dependencies=_WRITE)
def release_reservation(
    request: Request,
    reservation_id: str,
    body: ReleaseBody,
    pool_id: Annotated[str, Depends(_reservation_pool)],
) -> Response:
    reason = body.model.reason

    def command(execution: Execution) -> CommandResult:
        return reservations.release_reservation(execution, reservation_id, reason)

    return run_idempotent(
        request,
        operation=RESERVATIONS_RELEASE,
        body=body,
        command=command,
        read_committed=True,
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
            "database error during a reservation read (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None
    return result


@router.get("/v1/reservations/{reservation_id}", dependencies=_READ)
def get_reservation(request: Request, reservation_id: str) -> JSONResponse:
    """不存在与属于别的租户同一个 404，后者记 cross_tenant。"""
    tenant_id = get_access(request).principal.tenant_id

    def lookup(connection: Connection) -> tuple[dict | None, str | None]:
        found = reservations.read_reservation(connection, tenant_id, reservation_id)
        if found is not None:
            return reservations.current_representation(connection, tenant_id, found), None
        return None, reservations.reservation_owner(connection, reservation_id)

    found, owner = _read(request, lookup)
    if found is None:
        if owner is not None:
            raise cross_tenant_not_found(
                request,
                object_type=RESERVATION,
                object_id=reservation_id,
                code=RESERVATION_NOT_FOUND,
            )
        raise ApiError(404, RESERVATION_NOT_FOUND, f"{RESERVATION} not found")
    return ok(request, found)


def _order_ref_parameter(request: Request) -> str:
    """order_ref 必填且只出现一次，别的查询参数都不收，取值是第 1 节的引用格式。

    不回显未知参数的名字与参数值。
    """
    names = [name for name, _ in request.query_params.multi_items()]
    if any(name != ORDER_REF for name in names):
        raise invalid_request("unknown query parameter")
    if names.count(ORDER_REF) > 1:
        raise invalid_request("query parameter given more than once", {"parameter": ORDER_REF})
    if not names:
        raise invalid_request("missing query parameter", {"parameter": ORDER_REF})
    value = request.query_params[ORDER_REF]
    if not is_reference(value):
        raise invalid_request(f"invalid {ORDER_REF} parameter", {"parameter": ORDER_REF})
    return value


@router.get("/v1/reservations", dependencies=_READ)
def list_reservations(request: Request) -> JSONResponse:
    """order_ref 与 status 必须且只能带一个（§5.9）；不带 status 时照 §5.6 的 order_ref 查询。"""
    names = {name for name, _ in request.query_params.multi_items()}
    if STATUS in names:
        if ORDER_REF in names:
            raise invalid_request("give exactly one of order_ref and status")
        return _list_by_status(request)
    return _list_by_order_ref(request)


def _list_by_order_ref(request: Request) -> JSONResponse:
    """按凭据的销售入口查，reservations 为 0 或 1 个。

    凭据没有销售入口或入口已停用时它不可能有自己的 order_ref，返回空数组。
    """
    order_ref = _order_ref_parameter(request)
    principal = get_access(request).principal
    if principal.channel_id is None:
        return ok(request, {"reservations": []})

    def lookup(connection: Connection) -> list[dict]:
        found = reservations.find_reservation_by_ref(
            connection, principal.tenant_id, principal.channel_id, order_ref
        )
        if found is None:
            return []
        return [reservations.current_representation(connection, principal.tenant_id, found)]

    return ok(request, {"reservations": _read(request, lookup)})


def _list_by_status(request: Request) -> JSONResponse:
    """待审列表：只收 status、after、limit，各最多一次（规则同流水查询）。

    判定顺序：status、limit、游标的格式不对 422，再游标与 status 不符 422；
    凭据没有销售入口或入口已停用时在这之后返回空数组。普通读，不加锁。
    next_cursor 为本页最后一个的游标；没有新记录时原样返回请求里的 after，没带则为 null。
    """
    values = _parameters(request, (STATUS,), REVIEW_LIST_OPTIONAL)
    after = values.get(AFTER)
    try:
        status = parse_review_status(values[STATUS])
        limit = parse_page_limit(values.get(LIMIT))
        cursor = match_review_cursor(parse_review_cursor(after), status)
    except InvalidParameter as error:
        raise _invalid_parameter(error) from None
    principal = get_access(request).principal
    if principal.channel_id is None:
        return ok(request, {"reservations": [], "next_cursor": after})

    found = _read(
        request,
        lambda connection: reconciliation.read_review_reservations(
            connection, principal.tenant_id, principal.channel_id, status, cursor, limit
        ),
    )
    next_cursor = after
    if found:
        next_cursor = format_review_cursor(found[-1]["reservation_id"], status)
    return ok(request, {"reservations": found, "next_cursor": next_cursor})
