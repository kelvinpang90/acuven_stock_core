"""Demo 每日重置接口（契约 §6.1、§6.2、§7.1、§8.1、§10 第 3、4 条）。

只在 CORE_ENV 为 demo 时由 app/main.py 挂上；
生产实例不挂，该路径按 §8.1 第 1 步 404 NOT_FOUND、不鉴权。
要求 demo.reset，不要求角色（操作者头带了照常校验并记录），经幂等执行器。
池授权按正文的 pool_id，余额行按正文的 pool_id 与各 sku_id 预建。
未知字段、business_date 不合格、target 不是 0–1,000,000 的整数、sku_id 重复在第 7 步 422
（取锁之前，不保存）；skus 超过 100 个 413。
不另设查询接口：结果未知时用原内容重报取回（§6.1）。
写库在 app/services/demo_resets.py。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import Field, field_validator, model_validator
from sqlalchemy import Connection

from app.api.auth import authorize, require_pool
from app.api.requests import ParsedBody, RequestBody, json_body
from app.domain.service_auth import DEMO_RESET
from app.domain.stock import parse_business_date, parse_non_negative_quantity
from app.services import demo_resets
from app.services.demo_resets import ResetFields, ResetSku
from app.services.idempotency import CommandResult, Execution, run_idempotent

DEMO_RESETS_CREATE = "demo_resets.create"

SKUS = "skus"
TARGET = "target"

router = APIRouter()


# --- 请求体（§6.1） ---


class SkuTarget(RequestBody):
    sku_id: str
    target: int

    @field_validator("target")
    @classmethod
    def check_target(cls, value: int) -> int:
        return parse_non_negative_quantity(value, TARGET)


class Reset(RequestBody):
    """skus 是 1–100 个的批量数组（超过 100 为 413）。"""

    batch_fields = (SKUS,)

    pool_id: str
    business_date: str
    skus: list[SkuTarget] = Field(min_length=1)

    @field_validator("business_date")
    @classmethod
    def check_business_date(cls, value: str) -> str:
        parse_business_date(value)
        return value

    @model_validator(mode="after")
    def check_unique_skus(self):
        """同一请求里 sku_id 不能重复。"""
        sku_ids = [sku.sku_id for sku in self.skus]
        if len(set(sku_ids)) != len(sku_ids):
            raise ValueError("duplicate sku_id in skus")
        return self

    def reset_fields(self) -> ResetFields:
        return ResetFields(
            pool_id=self.pool_id,
            business_date=parse_business_date(self.business_date),
            skus=[ResetSku(sku.sku_id, sku.target) for sku in self.skus],
        )


reset_body = json_body(Reset)
ResetBody = Annotated[ParsedBody[Reset], Depends(reset_body)]


def _reset_pool(body: ResetBody) -> str:
    return body.model.pool_id


def _balance_rows(fields: ResetFields):
    def balance_rows(connection: Connection) -> list[tuple[str, str]]:
        return [(fields.pool_id, sku.sku_id) for sku in fields.skus]

    return balance_rows


@router.post("/v1/demo/resets", dependencies=[Depends(authorize(DEMO_RESET, write=True))])
def create_reset(
    request: Request,
    body: ResetBody,
    pool_id: Annotated[str, Depends(require_pool(_reset_pool))],
) -> Response:
    fields = body.model.reset_fields()

    def command(execution: Execution) -> CommandResult:
        return demo_resets.create_reset(execution, fields)

    return run_idempotent(
        request,
        operation=DEMO_RESETS_CREATE,
        body=body,
        command=command,
        balance_rows=_balance_rows(fields),
    )
