"""收货与不可售调整的写入（契约 §6.2、§6.4、§6.5）。

两个命令都是单据命令，经 app/services/stock.py 的 with_document 运行，在幂等执行器已开的事务里、
用 execution.connection，自己不开、不提交、不回滚事务。
余额行由执行器按正文的 pool_id 与各行 sku_id 预建。

判定顺序照 §6.2 第 4 条：业务引用（with_document）→ 池为 live → SKU 不存在
→ 升序锁余额行 → SKU 已合并 → 命令自己的规则。收货没有自己的规则（只加 stock_balance）；
不可售调整逐行按锁住的账面查上限，任何一行超限整笔 409、什么也不写，details.lines 列出全部超限的行。

同一请求里 sku_id 不重复（接口在第 7 步保证），所以每行写后的账面就是这一行流水的写后值。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.api.errors import ApiError
from app.domain.ledger import (
    ADJUSTMENT_RULES,
    QUANTITY_RULES,
    AdjustmentAction,
    AdjustmentExceedsLimit,
    EntryType,
    apply_adjustment,
)
from app.domain.quantities import Quantities
from app.services.idempotency import CommandResult, Execution
from app.services.stock import (
    RECEIPT,
    UNAVAILABLE_ADJUSTMENT,
    LockedBalance,
    append_entry,
    command_now,
    format_timestamp,
    insert_document,
    lock_balances,
    new_command_id,
    new_document_id,
    require_live,
    require_not_merged,
    require_skus_exist,
    with_document,
)

_LIMIT_MESSAGES = {
    "INSUFFICIENT_STOCK": "quantity exceeds the available stock",
    "QUANTITY_EXCEEDS_UNAVAILABLE": "quantity exceeds the unavailable stock",
}


@dataclass(frozen=True)
class DocumentLine:
    """请求里的一行，已经过第 7 步校验。"""

    line_ref: str
    sku_id: str
    quantity: int


@dataclass(frozen=True)
class DocumentFields:
    """两种单据共有的正文字段；occurred_at 是换算成 UTC 的时间，没带为 None。"""

    pool_id: str
    lines: Sequence[DocumentLine]
    reason_code: str | None
    note: str | None
    occurred_at: datetime | None


def _lock_lines(execution: Execution, fields: DocumentFields) -> dict[str, LockedBalance]:
    """池为 live → SKU 存在（按请求顺序）→ 升序锁余额行 → SKU 未合并。"""
    require_live(execution, fields.pool_id)
    sku_ids = [line.sku_id for line in fields.lines]
    require_skus_exist(execution, sku_ids)
    balances = lock_balances(execution, fields.pool_id, sku_ids)
    require_not_merged(execution, sku_ids)
    return balances


def _deltas(entry_type: EntryType, quantity: int) -> tuple[int, int, int, int]:
    sb, r, u, ppd = QUANTITY_RULES[entry_type]
    return sb * quantity, r * quantity, u * quantity, ppd * quantity


def _write_lines(
    execution: Execution,
    fields: DocumentFields,
    balances: dict[str, LockedBalance],
    entry_type: EntryType,
    *,
    source_type: str,
    source_ref: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """按请求顺序每行写一条流水，返回各行的表示。同一命令的流水共用一个 command_id。"""
    command_id = new_command_id()
    represented = []
    for line in fields.lines:
        entry = append_entry(
            execution,
            balances[line.sku_id],
            entry_type,
            _deltas(entry_type, line.quantity),
            command_id,
            now=now,
            source_type=source_type,
            source_ref=source_ref,
            line_ref=line.line_ref,
            reason_code=fields.reason_code,
            note=fields.note,
            occurred_at=fields.occurred_at,
        )
        after = entry["after"]
        represented.append(
            {
                "line_ref": line.line_ref,
                "sku_id": line.sku_id,
                "quantity": line.quantity,
                "entry_id": entry["entry_id"],
                **after,
                "available": Quantities(**after).available,
                "balance_version": entry["balance_version_after"],
            }
        )
    return represented


def _header(fields: DocumentFields, document_id: str, now: datetime) -> dict[str, Any]:
    occurred_at = now if fields.occurred_at is None else fields.occurred_at
    return {
        "document_id": document_id,
        "pool_id": fields.pool_id,
        "reason_code": fields.reason_code,
        "note": fields.note,
        "occurred_at": format_timestamp(occurred_at),
        "created_at": format_timestamp(now),
    }


# --- 收货（§6.4） ---


def create_receipt(execution: Execution, receipt_ref: str, fields: DocumentFields) -> CommandResult:
    """每行写一条 RECEIPT（source_type receipt，source_ref 为 receipt_ref），写单据，201。"""

    def command(execution: Execution) -> CommandResult:
        now = command_now(execution)
        balances = _lock_lines(execution, fields)
        lines = _write_lines(
            execution,
            fields,
            balances,
            EntryType.RECEIPT,
            source_type=RECEIPT,
            source_ref=receipt_ref,
            now=now,
        )
        document_id = new_document_id(RECEIPT)
        header = _header(fields, document_id, now)
        result = {"document_id": document_id, "receipt_ref": receipt_ref} | header
        result["lines"] = lines
        insert_document(execution, RECEIPT, receipt_ref, document_id, now, result)
        return CommandResult(result, 201)

    return with_document(execution, RECEIPT, receipt_ref, command)


# --- 不可售调整（§6.5） ---


def _check_limits(
    action: AdjustmentAction, fields: DocumentFields, balances: dict[str, LockedBalance]
) -> None:
    """逐行按当前账面查上限；有超限的行时 409（错误码按动作），details.lines 按请求顺序列全。"""
    exceeded = []
    code = None
    for line in fields.lines:
        try:
            apply_adjustment(action, balances[line.sku_id].quantities, line.quantity)
        except AdjustmentExceedsLimit as exc:
            code = exc.code
            exceeded.append(
                {
                    "line_ref": line.line_ref,
                    "sku_id": line.sku_id,
                    "quantity": line.quantity,
                    "limit": exc.limit,
                }
            )
    if exceeded:
        raise ApiError(409, code, _LIMIT_MESSAGES[code], details={"lines": exceeded})


def create_adjustment(
    execution: Execution, adjustment_ref: str, action: AdjustmentAction, fields: DocumentFields
) -> CommandResult:
    """全部行都不超限时每行写一条对应类型的流水（source_type unavailable_adjustment，
    source_ref 为 adjustment_ref），写单据，201。
    """
    action = AdjustmentAction(action)
    entry_type, _ = ADJUSTMENT_RULES[action]

    def command(execution: Execution) -> CommandResult:
        now = command_now(execution)
        balances = _lock_lines(execution, fields)
        _check_limits(action, fields, balances)
        lines = _write_lines(
            execution,
            fields,
            balances,
            entry_type,
            source_type=UNAVAILABLE_ADJUSTMENT,
            source_ref=adjustment_ref,
            now=now,
        )
        document_id = new_document_id(UNAVAILABLE_ADJUSTMENT)
        header = _header(fields, document_id, now)
        result = {
            "document_id": document_id,
            "adjustment_ref": adjustment_ref,
            "action": action.value,
        } | header
        result["lines"] = lines
        insert_document(execution, UNAVAILABLE_ADJUSTMENT, adjustment_ref, document_id, now, result)
        return CommandResult(result, 201)

    return with_document(execution, UNAVAILABLE_ADJUSTMENT, adjustment_ref, command)
