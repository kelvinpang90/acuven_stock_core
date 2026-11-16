"""Demo 每日重置的写入（契约 §6.1、§6.2、§7.1）。

命令在幂等执行器已开的事务里、用 execution.connection 运行，自己不开、不提交、不回滚事务。
余额行由执行器按正文的 pool_id 与各 sku_id 预建。

判定顺序照 §6.1 的九步：业务引用（普通读查 demo_resets）→ 池为 live → SKU 不存在
→ 升序锁余额行 → SKU 已合并 → 逐 SKU 按 apply_demo_reset 落账 → 写 demo_resets。
增量不为 0 写一条 DEMO_RESET（判定列由 append_entry 设为写后的版本），
增量为 0 不写流水、调 mark_verified（判定列设为当前版本）。
重置不会违反不变量，没有数量上的拒绝。

业务引用 (tenant_id, pool_id, business_date) 不在 stock_documents 里，
并发的处理照 app/services/stock.py 的 with_document 在这里实现：
命令的业务 4xx 与写 demo_resets 撞唯一约束都改抛 BusinessRefTaken，
执行器在新事务里重新查 demo_resets，有则按摘要回答，没有则再运行一次命令，
这一次的结果就是确定结果。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from app.api.errors import ApiError
from app.domain.ids import new_id
from app.domain.ledger import EntryType, apply_demo_reset
from app.models.inventory import QUANTITIES, DemoReset
from app.services.idempotency import BusinessRefTaken, CommandResult, Execution
from app.services.stock import (
    BUSINESS_REF_CONFLICT,
    BUSINESS_REF_CONFLICT_EVENT,
    LockedBalance,
    append_entry,
    command_now,
    format_timestamp,
    lock_balances,
    mark_verified,
    new_command_id,
    require_live,
    require_not_merged,
    require_skus_exist,
)

# 流水的 source_type 与冲突 details 的 ref_type（§6.1）。
DEMO_RESET = "demo_reset"
RESET_ID_PREFIX = "drs"

_DUPLICATE_ENTRY = 1062  # MySQL ER_DUP_ENTRY


@dataclass(frozen=True)
class ResetSku:
    """请求里的一个 SKU，已经过第 7 步校验。"""

    sku_id: str
    target: int


@dataclass(frozen=True)
class ResetFields:
    """重置的正文字段；business_date 已换成 date。"""

    pool_id: str
    business_date: date
    skus: Sequence[ResetSku]


@dataclass(frozen=True)
class _Existing:
    """demo_resets 里已生效的一行。"""

    request_hash: str
    result: dict[str, Any]


def _find_reset(execution: Execution, fields: ResetFields) -> _Existing | None:
    """按 (tenant_id, pool_id, business_date) 用普通读查。"""
    statement = select(DemoReset.request_hash, DemoReset.result).where(
        DemoReset.tenant_id == execution.access.principal.tenant_id,
        DemoReset.pool_id == fields.pool_id,
        DemoReset.business_date == fields.business_date,
    )
    row = execution.connection.execute(statement).mappings().first()
    return None if row is None else _Existing(**row)


def _answer_existing(
    execution: Execution, fields: ResetFields, existing: _Existing
) -> CommandResult:
    """换幂等键重报：摘要相同返回 200 与 result；
    不同时登记 business_ref_conflict，409 BUSINESS_REF_CONFLICT。
    """
    if existing.request_hash == execution.request_digest:
        return CommandResult(existing.result, 200)
    details = {
        "ref_type": DEMO_RESET,
        "ref": fields.business_date.isoformat(),
        "pool_id": fields.pool_id,
    }
    execution.record_security_event(BUSINESS_REF_CONFLICT_EVENT, details)
    raise ApiError(
        409,
        BUSINESS_REF_CONFLICT,
        "the business reference was used with different content",
        details=details,
    )


def _outside_create_reset(execution: Execution) -> CommandResult:
    """_insert_reset 抛的 BusinessRefTaken 由 create_reset 换上真正的重新判定。"""
    raise RuntimeError("a demo reset was taken outside create_reset")


def _insert_reset(
    execution: Execution,
    fields: ResetFields,
    reset_id: str,
    created_at: datetime,
    result: dict[str, Any],
) -> None:
    """写 demo_resets 一行；channel_id 取凭据的销售入口。撞唯一约束时抛 BusinessRefTaken。"""
    access = execution.access
    principal = access.principal
    values = {
        "reset_id": reset_id,
        "tenant_id": principal.tenant_id,
        "channel_id": principal.channel_id,
        "pool_id": fields.pool_id,
        "business_date": fields.business_date,
        "request_hash": execution.request_digest,
        "result": result,
        "credential_id": principal.credential_id,
        "actor_ref": None if access.actor is None else access.actor.ref,
        "actor_role": None if access.actor is None else access.actor.role,
        "request_id": execution.request.state.request_id,
        "created_at": created_at,
    }
    try:
        execution.connection.execute(insert(DemoReset).values(values))
    except IntegrityError as exc:
        if getattr(exc.orig, "args", ())[:1] == (_DUPLICATE_ENTRY,):
            raise BusinessRefTaken(_outside_create_reset) from None
        raise


def _reset_sku(
    execution: Execution,
    balance: LockedBalance,
    sku: ResetSku,
    *,
    reset_id: str,
    command_id: str,
    now: datetime,
) -> dict[str, Any]:
    """§6.1 第 6–8 步：一个 SKU 落账（或核实），返回它在结果里的表示。"""
    after = apply_demo_reset(balance.quantities, sku.target)
    delta = after.stock_balance - balance.stock_balance
    entry_id = None
    if delta != 0:
        entry = append_entry(
            execution,
            balance,
            EntryType.DEMO_RESET,
            (delta, 0, 0, 0),
            command_id,
            now=now,
            source_type=DEMO_RESET,
            source_ref=reset_id,
        )
        entry_id = entry["entry_id"]
    else:
        mark_verified(execution, balance)
    quantities = balance.quantities
    return {
        "sku_id": sku.sku_id,
        "target": sku.target,
        "delta": delta,
        "entry_id": entry_id,
        **{name: getattr(quantities, name) for name in QUANTITIES},
        "available": quantities.available,
        "balance_version": balance.balance_version,
    }


def create_reset(execution: Execution, fields: ResetFields) -> CommandResult:
    """同一池同一天第一次生效的重置：逐 SKU 落账，写 demo_resets，201。"""

    def command(execution: Execution) -> CommandResult:
        now = command_now(execution)
        reset_id = new_id(RESET_ID_PREFIX)
        require_live(execution, fields.pool_id)
        sku_ids = [sku.sku_id for sku in fields.skus]
        require_skus_exist(execution, sku_ids)
        balances = lock_balances(execution, fields.pool_id, sku_ids)
        require_not_merged(execution, sku_ids)
        command_id = new_command_id()
        skus = [
            _reset_sku(
                execution,
                balances[sku.sku_id],
                sku,
                reset_id=reset_id,
                command_id=command_id,
                now=now,
            )
            for sku in fields.skus
        ]
        result = {
            "reset_id": reset_id,
            "pool_id": fields.pool_id,
            "business_date": fields.business_date.isoformat(),
            "created_at": format_timestamp(now),
            "skus": skus,
        }
        _insert_reset(execution, fields, reset_id, now, result)
        return CommandResult(result, 201)

    return _with_reset(execution, fields, command)


def _with_reset(
    execution: Execution,
    fields: ResetFields,
    command: Callable[[Execution], CommandResult],
) -> CommandResult:
    """已有重置就按 _answer_existing 回答，没有就运行 command（规则同 with_document）。

    command 的业务拒绝（ApiError 4xx）与写 demo_resets 撞唯一约束都改抛 BusinessRefTaken：
    执行器在新事务里调用 judge，重新查 demo_resets，有则回答，没有则再运行一次 command，
    这一次的结果（含拒绝）就是确定结果。
    """

    def judge(execution: Execution) -> CommandResult:
        existing = _find_reset(execution, fields)
        if existing is not None:
            return _answer_existing(execution, fields, existing)
        return command(execution)

    existing = _find_reset(execution, fields)
    if existing is not None:
        return _answer_existing(execution, fields, existing)
    try:
        return command(execution)
    except ApiError as error:
        if not 400 <= error.status <= 499:
            raise
        raise BusinessRefTaken(judge) from None
    except BusinessRefTaken:
        raise BusinessRefTaken(judge) from None
