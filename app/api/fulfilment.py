"""发货、退款回库与退货验收的接口（契约 §5.3、§5.8、§7.1、§8.1、§10 第 3、4 条）。

三个命令都要求 orders.write、不要求角色（操作者头带了照常校验并记录），经幂等执行器，
业务事务以 READ COMMITTED 开启；不预建余额行、没有开事务前的钩子。
路径的 order_ref 是第 1 节的引用格式，不合格在第 7 步 422。
正文是本类型的引用（shipment_ref、refund_ref 或 return_ref，第 1 节的引用格式）与 lines
（1–100 行，超过 413；line_ref 重复 422），加可选的 reason_code、note、occurred_at（规则同收货），
不接受别的字段。发货与退款回库每行 line_ref 与 1–1,000,000 的 JSON 整数 quantity；
退货验收每行 line_ref、sellable_quantity 与 unsellable_quantity，两个数都必填，
按 parse_return_quantities 校验（各 0–1,000,000 的 JSON 整数，和至少 1）。
池是凭据销售入口绑定的池：凭据没有销售入口或入口已停用 403（第 9 步，不保存）。
判定与写库在 app/services/fulfilment.py（退货验收为 create_return_receipt）。

订单库存查询 GET /v1/orders/{order_ref}/stock 要求 orders.read、不要求角色，不经幂等执行器。
查询参数只收 after 与 limit，各最多一次（规则同流水查询）。判定顺序：路径 order_ref、limit、
after 的格式不对 422；凭据没有销售入口或查不到预留 404 RESERVATION_NOT_FOUND（不记事件）；
游标不属于本订单 422。读库在 app/services/fulfilment.py 的 read_order_stock。
"""

import logging
from collections.abc import Callable, Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, field_validator, model_validator
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import authorize, get_access, require_sales_pool
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
from app.domain.fulfilment import (
    DISPATCH,
    REFUND_RESTOCK,
    QuantityLine,
    ReturnLine,
    parse_order_stock_cursor,
    parse_return_quantities,
)
from app.domain.requests import is_reference
from app.domain.service_auth import ORDERS_READ, ORDERS_WRITE
from app.domain.stock import (
    normalize_note,
    normalize_reason_code,
    parse_ledger_limit,
    parse_occurred_at,
    parse_positive_quantity,
)
from app.services import fulfilment
from app.services.fulfilment import CommonFields
from app.services.idempotency import CommandResult, Execution, run_idempotent
from app.services.reservations import RESERVATION_NOT_FOUND

DISPATCHES_CREATE = "dispatches.create"
REFUND_RESTOCKS_CREATE = "refund_restocks.create"
RETURN_RECEIPTS_CREATE = "return_receipts.create"

ORDER_REF = "order_ref"
LINES = "lines"
ORDER_STOCK_PARAMETERS = (AFTER, LIMIT)

# 一条订单单据命令：(execution, order_ref, 单据引用, lines, 公共字段)。
Create = Callable[
    [Execution, str, str, Sequence[QuantityLine | ReturnLine], CommonFields], CommandResult
]

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(ORDERS_READ))]
_WRITE = [Depends(authorize(ORDERS_WRITE, write=True))]


def _reference(value: str, field: str) -> str:
    """第 1 节的引用格式，原样返回。"""
    if not is_reference(value):
        raise InvalidField(field)
    return value


# --- 请求体（§5.8） ---


class Line(RequestBody):
    """只按 line_ref 指订单行，不带 sku_id（Q-66）。"""

    line_ref: str
    quantity: int

    @field_validator("line_ref")
    @classmethod
    def check_line_ref(cls, value: str) -> str:
        return _reference(value, "line_ref")

    @field_validator("quantity")
    @classmethod
    def check_quantity(cls, value: int) -> int:
        return parse_positive_quantity(value)


class ReturnReceiptLine(RequestBody):
    """退货验收的一行：两个数都必填，同一行可以既有可售也有不可售的件（Q-63）。"""

    line_ref: str
    sellable_quantity: int
    unsellable_quantity: int

    @field_validator("line_ref")
    @classmethod
    def check_line_ref(cls, value: str) -> str:
        return _reference(value, "line_ref")

    @model_validator(mode="after")
    def check_quantities(self):
        """各 0–1,000,000 的整数，和至少 1。"""
        parse_return_quantities(self.sellable_quantity, self.unsellable_quantity)
        return self


class _Common(RequestBody):
    """三个命令共有的字段。子类定义 lines（1–100 行的批量数组，超过 100 为 413）。

    line_ref 的重复由 json_body 查（app/api/requests.py 的 _validate，422），这里不再重复。

    可选字段的默认 None 只表示没带，规则同收货：校验器只对带了的字段运行，
    所以 reason_code、occurred_at 给 null 照样 422，note 给 null 与没带相同。
    """

    batch_fields = (LINES,)

    reason_code: str | None = None
    note: str | None = None
    occurred_at: str | None = None

    @field_validator("reason_code")
    @classmethod
    def check_reason_code(cls, value: str | None) -> str:
        return normalize_reason_code(value)

    @field_validator("note")
    @classmethod
    def check_note(cls, value: str | None) -> str | None:
        return normalize_note(value)

    @field_validator("occurred_at")
    @classmethod
    def check_occurred_at(cls, value: str | None) -> str:
        parse_occurred_at(value)
        return value

    def request_lines(self) -> list[QuantityLine | ReturnLine]:
        raise NotImplementedError

    def common_fields(self) -> CommonFields:
        return CommonFields(
            reason_code=self.reason_code,
            note=self.note,
            occurred_at=None if self.occurred_at is None else parse_occurred_at(self.occurred_at),
        )


class _Fulfilment(_Common):
    """发货与退款回库：每行 line_ref 与 quantity。"""

    lines: list[Line] = Field(min_length=1)

    def request_lines(self) -> list[QuantityLine | ReturnLine]:
        return [QuantityLine(line.line_ref, line.quantity) for line in self.lines]


class Dispatch(_Fulfilment):
    shipment_ref: str

    @field_validator("shipment_ref")
    @classmethod
    def check_shipment_ref(cls, value: str) -> str:
        return _reference(value, "shipment_ref")


class RefundRestock(_Fulfilment):
    refund_ref: str

    @field_validator("refund_ref")
    @classmethod
    def check_refund_ref(cls, value: str) -> str:
        return _reference(value, "refund_ref")


class ReturnReceipt(_Common):
    return_ref: str
    lines: list[ReturnReceiptLine] = Field(min_length=1)

    @field_validator("return_ref")
    @classmethod
    def check_return_ref(cls, value: str) -> str:
        return _reference(value, "return_ref")

    def request_lines(self) -> list[QuantityLine | ReturnLine]:
        return [
            ReturnLine(line.line_ref, line.sellable_quantity, line.unsellable_quantity)
            for line in self.lines
        ]


dispatch_body = json_body(Dispatch)
refund_restock_body = json_body(RefundRestock)
return_receipt_body = json_body(ReturnReceipt)
DispatchBody = Annotated[ParsedBody[Dispatch], Depends(dispatch_body)]
RefundRestockBody = Annotated[ParsedBody[RefundRestock], Depends(refund_restock_body)]
ReturnReceiptBody = Annotated[ParsedBody[ReturnReceipt], Depends(return_receipt_body)]


def _path_order_ref(order_ref: str) -> str:
    """路径的 order_ref 是第 1 节的引用格式，不合格 422（排在请求体之后，同属第 7 步）。"""
    if not is_reference(order_ref):
        raise invalid_request(f"invalid {ORDER_REF} path parameter", {"parameter": ORDER_REF})
    return order_ref


def _create_document(document_type: str) -> Create:
    """发货与退款回库：按单据类型走 create_document。"""

    def create(
        execution: Execution,
        order_ref: str,
        document_ref: str,
        lines: Sequence[QuantityLine | ReturnLine],
        fields: CommonFields,
    ) -> CommandResult:
        return fulfilment.create_document(
            execution, document_type, order_ref, document_ref, lines, fields
        )

    return create


def _run(
    request: Request,
    operation: str,
    body: ParsedBody[_Common],
    create: Create,
    order_ref: str,
    document_ref: str,
) -> Response:
    # 路由上的 pool_id 只用来在第 9 步拦没有销售入口的凭据（403）：
    # 余额行用预留行的 pool_id，预留行按凭据的渠道查到，二者一致。
    lines = body.model.request_lines()
    fields = body.model.common_fields()

    def command(execution: Execution) -> CommandResult:
        return create(execution, order_ref, document_ref, lines, fields)

    return run_idempotent(
        request,
        operation=operation,
        body=body,
        command=command,
        read_committed=True,
    )


# --- 发货 ---


@router.post("/v1/orders/{order_ref}/dispatches", dependencies=_WRITE)
def create_dispatch(
    request: Request,
    body: DispatchBody,
    order_ref: Annotated[str, Depends(_path_order_ref)],
    pool_id: Annotated[str, Depends(require_sales_pool)],
) -> Response:
    shipment_ref = body.model.shipment_ref
    create = _create_document(DISPATCH)
    return _run(request, DISPATCHES_CREATE, body, create, order_ref, shipment_ref)


# --- 退款回库 ---


@router.post("/v1/orders/{order_ref}/refund-restocks", dependencies=_WRITE)
def create_refund_restock(
    request: Request,
    body: RefundRestockBody,
    order_ref: Annotated[str, Depends(_path_order_ref)],
    pool_id: Annotated[str, Depends(require_sales_pool)],
) -> Response:
    refund_ref = body.model.refund_ref
    create = _create_document(REFUND_RESTOCK)
    return _run(request, REFUND_RESTOCKS_CREATE, body, create, order_ref, refund_ref)


# --- 退货验收 ---


@router.post("/v1/orders/{order_ref}/return-receipts", dependencies=_WRITE)
def create_return_receipt(
    request: Request,
    body: ReturnReceiptBody,
    order_ref: Annotated[str, Depends(_path_order_ref)],
    pool_id: Annotated[str, Depends(require_sales_pool)],
) -> Response:
    return_ref = body.model.return_ref
    create = fulfilment.create_return_receipt
    return _run(request, RETURN_RECEIPTS_CREATE, body, create, order_ref, return_ref)


# --- 订单库存查询 ---


def _read[T](request: Request, query: Callable[[Connection], T]) -> T:
    """用本请求的连接读，读完（也包括查询报 404、422）回滚结束事务；数据库出错 503。"""
    connection = request_connection(request)
    try:
        try:
            return query(connection)
        finally:
            connection.rollback()
    except SQLAlchemyError:
        _log.warning(
            "database error during an order stock read (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None


@router.get("/v1/orders/{order_ref}/stock", dependencies=_READ)
def get_order_stock(
    request: Request,
    order_ref: Annotated[str, Depends(_path_order_ref)],
) -> JSONResponse:
    """预留、每行累计量与按 document_seq 分页的单据；三部分出自同一个读快照。

    next_cursor 为本页最后一张单据的游标；没有新单据时原样返回请求里的 after，没带则为 null。
    """
    values = _parameters(request, (), ORDER_STOCK_PARAMETERS)
    after = values.get(AFTER)
    try:
        limit = parse_ledger_limit(values.get(LIMIT))
        cursor = None if after is None else parse_order_stock_cursor(after)
    except InvalidParameter as error:
        raise _invalid_parameter(error) from None
    principal = get_access(request).principal
    if principal.channel_id is None:
        # 查找限定在本入口：没有入口就没有自己的订单（不记事件）。
        raise ApiError(404, RESERVATION_NOT_FOUND, "reservation not found")
    data = _read(
        request,
        lambda connection: fulfilment.read_order_stock(
            connection,
            principal.tenant_id,
            principal.channel_id,
            order_ref,
            after,
            cursor,
            limit,
        ),
    )
    return ok(request, data)
