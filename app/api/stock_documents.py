"""收货与不可售调整接口。

契约 §6.2、§6.4、§6.5、§7.2、§10 第 3、4 条。
两个命令都要求 stock.write，经幂等执行器；池授权按正文的 pool_id，
余额行按正文的 pool_id 与各行 sku_id 预建。
收货接受 stock_operator 与 stock_admin；不可售调整按 action 定角色（第 8 步，在请求体之后）：
write_off 只接受 stock_admin，另两种接受 stock_operator 与 stock_admin。
未知字段、字段取值、line_ref 或 sku_id 重复在第 7 步 422（取锁之前，不保存）；
lines 超过 100 行 413。
不另设查询接口：结果未知时用原内容重报取回（§6.4）。
写库在 app/services/stock_documents.py。
"""

from collections.abc import Collection
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import Field, field_validator, model_validator
from sqlalchemy import Connection

from app.api.auth import authorize, require_pool, require_role, require_role_by_body
from app.api.requests import ParsedBody, RequestBody, json_body
from app.domain.catalog import InvalidField
from app.domain.ledger import AdjustmentAction
from app.domain.requests import STOCK_ADMIN, STOCK_OPERATOR, Actor, is_reference
from app.domain.service_auth import STOCK_WRITE
from app.domain.stock import (
    normalize_note,
    normalize_reason_code,
    parse_occurred_at,
    parse_positive_quantity,
)
from app.services import stock_documents
from app.services.idempotency import CommandResult, Execution, run_idempotent
from app.services.stock_documents import DocumentFields, DocumentLine

RECEIPTS_CREATE = "receipts.create"
UNAVAILABLE_ADJUSTMENTS_CREATE = "unavailable_adjustments.create"

LINES = "lines"

router = APIRouter()
_WRITE = [Depends(authorize(STOCK_WRITE, write=True))]

_OPERATOR_OR_ADMIN = (STOCK_OPERATOR, STOCK_ADMIN)


def _reference(value: str, field: str) -> str:
    """第 1 节的引用格式，原样返回。"""
    if not is_reference(value):
        raise InvalidField(field)
    return value


# --- 请求体（§6.2 第 5、6 条，§6.4，§6.5） ---


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


class _Document(RequestBody):
    """两种单据共有的字段。lines 是 1–100 行的批量数组（超过 100 为 413）。

    可选字段的默认 None 只表示没带：校验器只对带了的字段运行，
    所以 reason_code、occurred_at 给 null 照样 422，note 给 null 与没带相同。
    """

    batch_fields = (LINES,)

    pool_id: str
    lines: list[Line] = Field(min_length=1)
    note: str | None = None
    occurred_at: str | None = None

    @field_validator("note")
    @classmethod
    def check_note(cls, value: str | None) -> str | None:
        return normalize_note(value)

    @field_validator("occurred_at")
    @classmethod
    def check_occurred_at(cls, value: str | None) -> str:
        parse_occurred_at(value)
        return value

    @model_validator(mode="after")
    def check_unique_skus(self):
        """同一请求里 sku_id 不能重复（line_ref 的重复由 json_body 查）。"""
        sku_ids = [line.sku_id for line in self.lines]
        if len(set(sku_ids)) != len(sku_ids):
            raise ValueError("duplicate sku_id in lines")
        return self

    def document_fields(self, reason_code: str | None) -> DocumentFields:
        return DocumentFields(
            pool_id=self.pool_id,
            lines=[DocumentLine(line.line_ref, line.sku_id, line.quantity) for line in self.lines],
            reason_code=reason_code,
            note=self.note,
            occurred_at=None if self.occurred_at is None else parse_occurred_at(self.occurred_at),
        )


class Receipt(_Document):
    receipt_ref: str
    reason_code: str | None = None

    @field_validator("receipt_ref")
    @classmethod
    def check_receipt_ref(cls, value: str) -> str:
        return _reference(value, "receipt_ref")

    @field_validator("reason_code")
    @classmethod
    def check_reason_code(cls, value: str | None) -> str:
        return normalize_reason_code(value)


class Adjustment(_Document):
    adjustment_ref: str
    action: AdjustmentAction
    reason_code: str

    @field_validator("adjustment_ref")
    @classmethod
    def check_adjustment_ref(cls, value: str) -> str:
        return _reference(value, "adjustment_ref")

    @field_validator("reason_code")
    @classmethod
    def check_reason_code(cls, value: str) -> str:
        return normalize_reason_code(value)


receipt_body = json_body(Receipt)
adjustment_body = json_body(Adjustment)
ReceiptBody = Annotated[ParsedBody[Receipt], Depends(receipt_body)]
AdjustmentBody = Annotated[ParsedBody[Adjustment], Depends(adjustment_body)]


def _receipt_pool(body: ReceiptBody) -> str:
    return body.model.pool_id


def _adjustment_pool(body: AdjustmentBody) -> str:
    return body.model.pool_id


def _adjustment_roles(body: ParsedBody[Adjustment]) -> Collection[str]:
    """报损只接受 stock_admin（§10 第 3 条）。"""
    if body.model.action is AdjustmentAction.WRITE_OFF:
        return (STOCK_ADMIN,)
    return _OPERATOR_OR_ADMIN


def _balance_rows(fields: DocumentFields):
    def balance_rows(connection: Connection) -> list[tuple[str, str]]:
        return [(fields.pool_id, line.sku_id) for line in fields.lines]

    return balance_rows


# --- 收货（§6.4） ---


@router.post("/v1/receipts", dependencies=_WRITE)
def create_receipt(
    request: Request,
    body: ReceiptBody,
    actor: Annotated[Actor, Depends(require_role(*_OPERATOR_OR_ADMIN))],
    pool_id: Annotated[str, Depends(require_pool(_receipt_pool))],
) -> Response:
    model = body.model
    fields = model.document_fields(model.reason_code)

    def command(execution: Execution) -> CommandResult:
        return stock_documents.create_receipt(execution, model.receipt_ref, fields)

    return run_idempotent(
        request,
        operation=RECEIPTS_CREATE,
        body=body,
        command=command,
        balance_rows=_balance_rows(fields),
    )


# --- 不可售调整（§6.5） ---


@router.post("/v1/unavailable-adjustments", dependencies=_WRITE)
def create_adjustment(
    request: Request,
    body: AdjustmentBody,
    actor: Annotated[Actor, Depends(require_role_by_body(adjustment_body, _adjustment_roles))],
    pool_id: Annotated[str, Depends(require_pool(_adjustment_pool))],
) -> Response:
    model = body.model
    fields = model.document_fields(model.reason_code)

    def command(execution: Execution) -> CommandResult:
        return stock_documents.create_adjustment(
            execution, model.adjustment_ref, model.action, fields
        )

    return run_idempotent(
        request,
        operation=UNAVAILABLE_ADJUSTMENTS_CREATE,
        body=body,
        command=command,
        balance_rows=_balance_rows(fields),
    )
