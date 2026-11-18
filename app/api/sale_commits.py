"""确认销售的接口（契约 §5.2 第 5–7 条、§5.7、§7.1、§8.1、§10 第 3、4 条）。

要求 orders.write、不要求角色（操作者头带了照常校验并记录），经幂等执行器，
业务事务以 READ COMMITTED 开启。路径的 order_ref 是第 1 节的引用格式，不合格在第 7 步 422。
正文四个字段都必填、不接受别的字段：reservation_id 是 1–32 个字符的字符串或 null，
payment_ref 是第 1 节的引用格式，paid_at 是带时区的 RFC 3339 时间（存为 UTC），
lines 的规则同预留（1–100 行，超过 413；line_ref 或 sku_id 重复 422）。
池是凭据销售入口绑定的池：凭据没有销售入口或入口已停用 403（第 9 步，不保存）。
余额行预建与开事务前的先关到期由 app/services/sale_commits.py 的 CommitSkus 做，
这里只把它们交给执行器；判定与写库也在那里。

待审结案同样要求 orders.write、不要求角色、经幂等执行器以 READ COMMITTED 开事务；
没有请求体（非空 422，第 7 步，排在路径的 order_ref 之前），池同上，不预建余额行。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import Field, field_validator, model_validator

from app.api.auth import authorize, get_access, require_sales_pool
from app.api.errors import invalid_request
from app.api.requests import ParsedBody, RequestBody, json_body, no_body
from app.api.reservations import LINES, ORDER_REF, Line
from app.domain.catalog import InvalidField
from app.domain.requests import is_reference
from app.domain.service_auth import ORDERS_WRITE
from app.domain.stock import parse_occurred_at
from app.services import sale_commits
from app.services.idempotency import CommandResult, Execution, run_idempotent
from app.services.reservations import ReservationLine
from app.services.sale_commits import CommitSkus, SaleCommit

SALE_COMMITS_CREATE = "sale_commits.create"
LATE_PAYMENT_REVIEWS_CLOSE = "late_payment_reviews.close"

RESERVATION_ID = "reservation_id"
PAYMENT_REF = "payment_ref"
PAID_AT = "paid_at"
# reservation_id 不另校验前缀与格式，只限长度（§5.7）。
RESERVATION_ID_MAX_LENGTH = 32

router = APIRouter()
_WRITE = [Depends(authorize(ORDERS_WRITE, write=True))]
# 结案没有请求体：no_body 在 authorize 之后，空请求体跳过、非空 422。
_WRITE_WITHOUT_BODY = [*_WRITE, Depends(no_body)]


# --- 请求体（§5.7 确认销售） ---


class SaleCommitRequest(RequestBody):
    """四个字段都必填，reservation_id 可为 null。

    lines 是 1–100 行的批量数组（超过 100 为 413）。
    """

    batch_fields = (LINES,)

    reservation_id: str | None
    payment_ref: str
    paid_at: str
    lines: list[Line] = Field(min_length=1)

    @field_validator("reservation_id")
    @classmethod
    def check_reservation_id(cls, value: str | None) -> str | None:
        if value is not None and not 1 <= len(value) <= RESERVATION_ID_MAX_LENGTH:
            raise InvalidField(RESERVATION_ID)
        return value

    @field_validator("payment_ref")
    @classmethod
    def check_payment_ref(cls, value: str) -> str:
        if not is_reference(value):
            raise InvalidField(PAYMENT_REF)
        return value

    @field_validator("paid_at")
    @classmethod
    def check_paid_at(cls, value: str) -> str:
        try:
            parse_occurred_at(value)
        except InvalidField:
            raise InvalidField(PAID_AT) from None
        return value

    @model_validator(mode="after")
    def check_unique_skus(self):
        """同预留：一个订单里同一个 SKU 只占一行（line_ref 的重复由 json_body 查）。"""
        sku_ids = [line.sku_id for line in self.lines]
        if len(set(sku_ids)) != len(sku_ids):
            raise ValueError("duplicate sku_id in lines")
        return self

    def sale_commit(self) -> SaleCommit:
        return SaleCommit(
            reservation_id=self.reservation_id,
            payment_ref=self.payment_ref,
            paid_at=parse_occurred_at(self.paid_at),
            lines=tuple(
                ReservationLine(line.line_ref, line.sku_id, line.quantity) for line in self.lines
            ),
        )


sale_commit_body = json_body(SaleCommitRequest)
SaleCommitBody = Annotated[ParsedBody[SaleCommitRequest], Depends(sale_commit_body)]


def _path_order_ref(order_ref: str) -> str:
    """路径的 order_ref 是第 1 节的引用格式，不合格 422（排在请求体之后，同属第 7 步）。"""
    if not is_reference(order_ref):
        raise invalid_request(f"invalid {ORDER_REF} path parameter", {"parameter": ORDER_REF})
    return order_ref


# --- 确认销售 ---


@router.post("/v1/orders/{order_ref}/sale-commits", dependencies=_WRITE)
def create_sale_commit(
    request: Request,
    body: SaleCommitBody,
    order_ref: Annotated[str, Depends(_path_order_ref)],
    pool_id: Annotated[str, Depends(require_sales_pool)],
) -> Response:
    commit = body.model.sale_commit()
    tenant_id = get_access(request).principal.tenant_id
    skus = CommitSkus(tenant_id, pool_id, [line.sku_id for line in commit.lines])

    def command(execution: Execution) -> CommandResult:
        return sale_commits.create_sale_commit(execution, skus, order_ref, commit)

    return run_idempotent(
        request,
        operation=SALE_COMMITS_CREATE,
        body=body,
        command=command,
        balance_rows=skus.balance_rows,
        read_committed=True,
        before_transaction=skus.close_due,
    )


# --- 待审结案 ---


@router.post("/v1/orders/{order_ref}/late-payment-review/close", dependencies=_WRITE_WITHOUT_BODY)
def close_late_payment_review(
    request: Request,
    order_ref: Annotated[str, Depends(_path_order_ref)],
    pool_id: Annotated[str, Depends(require_sales_pool)],
) -> Response:
    def command(execution: Execution) -> CommandResult:
        return sale_commits.close_late_payment_review(execution, order_ref)

    return run_idempotent(
        request,
        operation=LATE_PAYMENT_REVIEWS_CLOSE,
        body=None,
        command=command,
        read_committed=True,
    )
