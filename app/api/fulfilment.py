"""发货与退款回库的接口（契约 §5.3、§5.8、§7.1、§8.1、§10 第 3、4 条）。

两个命令都要求 orders.write、不要求角色（操作者头带了照常校验并记录），经幂等执行器，
业务事务以 READ COMMITTED 开启；不预建余额行、没有开事务前的钩子。
路径的 order_ref 是第 1 节的引用格式，不合格在第 7 步 422。
正文是本类型的引用（shipment_ref 或 refund_ref，第 1 节的引用格式）与 lines
（1–100 行，超过 413；每行 line_ref 与 1–1,000,000 的 JSON 整数 quantity，line_ref 重复 422），
加可选的 reason_code、note、occurred_at（规则同收货），不接受别的字段。
池是凭据销售入口绑定的池：凭据没有销售入口或入口已停用 403（第 9 步，不保存）。
判定与写库在 app/services/fulfilment.py。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import Field, field_validator

from app.api.auth import authorize, require_sales_pool
from app.api.errors import invalid_request
from app.api.requests import ParsedBody, RequestBody, json_body
from app.domain.catalog import InvalidField
from app.domain.fulfilment import DISPATCH, REFUND_RESTOCK, QuantityLine
from app.domain.requests import is_reference
from app.domain.service_auth import ORDERS_WRITE
from app.domain.stock import (
    normalize_note,
    normalize_reason_code,
    parse_occurred_at,
    parse_positive_quantity,
)
from app.services import fulfilment
from app.services.fulfilment import CommonFields
from app.services.idempotency import CommandResult, Execution, run_idempotent

DISPATCHES_CREATE = "dispatches.create"
REFUND_RESTOCKS_CREATE = "refund_restocks.create"

ORDER_REF = "order_ref"
LINES = "lines"

router = APIRouter()
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


class _Fulfilment(RequestBody):
    """两个命令共有的字段。lines 是 1–100 行的批量数组（超过 100 为 413）。

    line_ref 的重复由 json_body 查（app/api/requests.py 的 _validate，422），这里不再重复。

    可选字段的默认 None 只表示没带，规则同收货：校验器只对带了的字段运行，
    所以 reason_code、occurred_at 给 null 照样 422，note 给 null 与没带相同。
    """

    batch_fields = (LINES,)

    lines: list[Line] = Field(min_length=1)
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

    def quantity_lines(self) -> list[QuantityLine]:
        return [QuantityLine(line.line_ref, line.quantity) for line in self.lines]

    def common_fields(self) -> CommonFields:
        return CommonFields(
            reason_code=self.reason_code,
            note=self.note,
            occurred_at=None if self.occurred_at is None else parse_occurred_at(self.occurred_at),
        )


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


dispatch_body = json_body(Dispatch)
refund_restock_body = json_body(RefundRestock)
DispatchBody = Annotated[ParsedBody[Dispatch], Depends(dispatch_body)]
RefundRestockBody = Annotated[ParsedBody[RefundRestock], Depends(refund_restock_body)]


def _path_order_ref(order_ref: str) -> str:
    """路径的 order_ref 是第 1 节的引用格式，不合格 422（排在请求体之后，同属第 7 步）。"""
    if not is_reference(order_ref):
        raise invalid_request(f"invalid {ORDER_REF} path parameter", {"parameter": ORDER_REF})
    return order_ref


def _run(
    request: Request,
    operation: str,
    body: ParsedBody[_Fulfilment],
    document_type: str,
    order_ref: str,
    document_ref: str,
) -> Response:
    # 路由上的 pool_id 只用来在第 9 步拦没有销售入口的凭据（403）：
    # 余额行用预留行的 pool_id，预留行按凭据的渠道查到，二者一致。
    lines = body.model.quantity_lines()
    fields = body.model.common_fields()

    def command(execution: Execution) -> CommandResult:
        return fulfilment.create_document(
            execution, document_type, order_ref, document_ref, lines, fields
        )

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
    return _run(request, DISPATCHES_CREATE, body, DISPATCH, order_ref, shipment_ref)


# --- 退款回库 ---


@router.post("/v1/orders/{order_ref}/refund-restocks", dependencies=_WRITE)
def create_refund_restock(
    request: Request,
    body: RefundRestockBody,
    order_ref: Annotated[str, Depends(_path_order_ref)],
    pool_id: Annotated[str, Depends(require_sales_pool)],
) -> Response:
    refund_ref = body.model.refund_ref
    return _run(request, REFUND_RESTOCKS_CREATE, body, REFUND_RESTOCK, order_ref, refund_ref)
