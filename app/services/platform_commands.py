"""平台事件执行器调 Core 命令体的动作（契约 §5.10 派生的命令正文、第 5、6 步与命令体共用）。

现在只有 ready_to_ship：reservations.create 紧接 sale_commits.create，共用一个保存点。
这里建每个命令的 Execution、派生并校验正文、开保存点、调命令体、按结果回滚到保存点；
锁序里命令体之前的几层、平台订单与配额的改写由 app/services/platform_events.py 做，
它按动作分派到这里。

命令执行（每个 Core 命令一个 Execution，CORE-TASK-079 之后它不持 HTTP 请求）：
- 上下文：租户、入口与池取平台订单的入口，credential_id 为这个租户的 system 主体，
  操作者与幂等键为空，request_id 为收件箱事件的 event_row_id（流水与预留由它追到事件），
  预留有效期取 process_order 传入的，HTTP 三项为空；
- operation 用 /v1 对应接口的操作类型名，摘要为 request_digest(路径参数, 派生正文)；
- 派生正文先用 /v1 对应接口的请求模型校验（同 HTTP，按 JSON 校验）；HTTP 在
  app/api/requests.py 里判的批量上限（413）与 line_ref 不重复（422）这里自己判。
  不过都抛 InvalidDerivedBody，执行器按 INVALID_REQUEST 设 held、不调 Core。

一个保存点（reserve_and_commit）：
- SAVEPOINT → create_reservation → 用它返回的 reservation_id 派生并调 create_sale_commit
  → RELEASE SAVEPOINT；
- 命令以业务拒绝（ApiError 4xx）结束：ROLLBACK TO SAVEPOINT，再执行这个命令登记的
  被拒仍保留的写入；登记的安全事件随拒绝保留；
- create_reservation 抛 BusinessRefTaken：ROLLBACK TO SAVEPOINT，丢弃这次登记的安全事件与
  被拒仍保留的写入，新开一个保存点、用新的 Execution 调 rejudge，之后照常（成功就接着确认销售）；
  有按订单的锁，本不该发生，所以不论 rejudge 的结果都带缺陷标记；
- 预留或确认销售按业务引用返回原结果（Execution.answered_by_reference）的成功带缺陷标记：
  订单还在扣数之前，Core 里却已有这个订单，说明状态记录有缺陷；
- 预留按业务引用返回原结果（已释放或已到期的预留）、接着确认销售被拒：照业务拒绝、带缺陷标记，
  确认销售登记的被拒仍保留的写入（例如晚到付款转待审）不执行、丢弃——平台订单不走晚到与
  故障补交（§5.10）；
- 错误码是 BUSINESS_REF_CONFLICT、ORDER_REFERENCE_CONFLICT、RESERVATION_MISMATCH 的拒绝
  带缺陷标记（第 6 步“另标为缺陷”）；
- RedoCommand、不是 4xx 的 ApiError 与别的异常原样抛出，由执行器回滚整笔事务。
安全事件不在这里写：结果里带回各命令登记的，执行器在事务提交之后写（同 HTTP 执行器）。

rejudge 这条路上预留已存在，确认销售在余额行之后才锁预留行，与 §5.10 第 4 步
“预留行在余额行之前”的锁序相反，可能与到期关单互锁；InnoDB 选中牺牲者回滚，
执行器按第 8 步下一轮重来。生产的池里平时没有 active 的预留，接受。
"""

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection

from app.api.errors import ApiError
from app.api.requests import MAX_BATCH_ITEMS, RequestBody
from app.api.reservations import RESERVATIONS_CREATE, ReservationRequest
from app.api.sale_commits import SALE_COMMITS_CREATE, SaleCommitRequest
from app.domain.idempotency import request_digest
from app.domain.ids import new_id
from app.domain.platform_events import CoreLine, reservation_body, sale_commit_body
from app.domain.reservations import RESERVATION_ID_PREFIX
from app.services.command_context import CommandContext
from app.services.idempotency import BusinessRefTaken, CommandResult, Execution
from app.services.reservations import ORDER_REFERENCE_CONFLICT, create_reservation
from app.services.sale_commits import RESERVATION_MISMATCH, CommitSkus, create_sale_commit
from app.services.security_events import SecurityEventRecord
from app.services.stock import BUSINESS_REF_CONFLICT

# 说明引用派生或状态记录有缺陷的拒绝（§5.10 第 6 步）。
DEFECT_CODES = frozenset({BUSINESS_REF_CONFLICT, ORDER_REFERENCE_CONFLICT, RESERVATION_MISMATCH})


class InvalidDerivedBody(ValueError):
    """派生正文过不了 /v1 的请求校验：不调 Core，执行器按 INVALID_REQUEST 处理。"""


# --- 命令上下文 ---


def command_context(
    *,
    tenant_id: str,
    channel_id: str,
    pool_id: str,
    credential_id: str,
    event_row_id: str,
    reservation_ttl_seconds: int,
) -> CommandContext:
    """平台事件执行器的命令上下文：操作者、幂等键与 HTTP 三项为空。"""
    return CommandContext(
        tenant_id=tenant_id,
        channel_id=channel_id,
        pool_id=pool_id,
        credential_id=credential_id,
        actor=None,
        request_id=event_row_id,
        idempotency_key=None,
        reservation_ttl_seconds=reservation_ttl_seconds,
    )


# --- 派生的正文（§5.10，Q-100） ---


@dataclass(frozen=True)
class DerivedCommand[M: RequestBody]:
    """一个 Core 命令的路径参数、派生正文、校验过的请求模型与请求摘要。"""

    operation: str
    path: dict[str, str]
    body: dict
    model: M
    digest: str


def _derive[M: RequestBody](
    operation: str, model_type: type[M], derived: tuple[dict, dict]
) -> DerivedCommand[M]:
    """按 /v1 的规则校验派生正文；不过抛 InvalidDerivedBody。"""
    path, body = derived
    lines = body.get("lines")
    if isinstance(lines, list):
        # HTTP 在 app/api/requests.py 里判的两条：超过 100 行 413、line_ref 重复 422。
        if len(lines) > MAX_BATCH_ITEMS:
            raise InvalidDerivedBody(f"{operation}: more than {MAX_BATCH_ITEMS} lines")
        refs = [line.get("line_ref") for line in lines]
        if len(set(refs)) != len(refs):
            raise InvalidDerivedBody(f"{operation}: duplicate line_ref")
    try:
        # 同 HTTP 按 JSON 校验：严格模式下的类型按 JSON 的类型匹配。
        model = model_type.model_validate_json(json.dumps(body))
    except ValueError:
        raise InvalidDerivedBody(f"{operation}: the derived body is invalid") from None
    return DerivedCommand(operation, path, body, model, request_digest(path, body))


@dataclass(frozen=True)
class ReadyCommands:
    """ready_to_ship 的两个命令：预留已派生并校验；确认销售要等预留得到 reservation_id。"""

    order_ref: str
    ready_at: datetime  # 截到整秒
    lines: tuple[CoreLine, ...]
    reservation: DerivedCommand[ReservationRequest]

    def sale_commit(self, reservation_id: str) -> DerivedCommand[SaleCommitRequest]:
        """sale_commits.create：paid_at 取 ready_at，lines 同预留。"""
        derived = sale_commit_body(self.order_ref, reservation_id, self.ready_at, self.lines)
        if derived is None:
            raise RuntimeError("a sale commit was derived without lines")
        return _derive(SALE_COMMITS_CREATE, SaleCommitRequest, derived)


def ready_commands(
    order_ref: str, lines: tuple[CoreLine, ...], ready_at: datetime
) -> ReadyCommands | None:
    """派生并校验 ready_to_ship 的命令正文；整理后一行不剩为 None，不合格抛 InvalidDerivedBody。

    确认销售的正文与预留只差 reservation_id（1–32 个字符）：先用一个预留 ID 形状的占位校验，
    不合格的在调 Core 之前就判出。
    """
    derived = reservation_body(order_ref, lines)
    if derived is None:
        return None
    commands = ReadyCommands(
        order_ref=order_ref,
        ready_at=ready_at,
        lines=tuple(lines),
        reservation=_derive(RESERVATIONS_CREATE, ReservationRequest, derived),
    )
    commands.sale_commit(new_id(RESERVATION_ID_PREFIX))
    return commands


# --- 一个保存点（§5.10 第 5、6 步） ---


@dataclass(frozen=True)
class CommandsOutcome:
    """保存点里命令的结果：rejection 为业务拒绝（成功为 None），
    defect 为要另标缺陷，events 为要在事务提交之后写的安全事件。
    """

    rejection: ApiError | None
    defect: bool
    events: tuple[SecurityEventRecord, ...]


def _execution(
    connection: Connection, context: CommandContext, command: DerivedCommand
) -> Execution:
    return Execution(context, connection, command.operation, command.digest)


def _succeeded(result) -> CommandResult:
    if not isinstance(result, CommandResult) or not 200 <= result.status <= 299:
        raise TypeError(f"a command must return a 2xx CommandResult, got {result!r}")
    return result


def _events(*executions: Execution) -> tuple[SecurityEventRecord, ...]:
    return tuple(event for execution in executions for event in execution.events)


def _rejected(
    savepoint,
    error: ApiError,
    executions: tuple[Execution, ...],
    kept: Execution | None,
    defect: bool,
) -> CommandsOutcome:
    """业务拒绝：回滚到保存点，执行 kept 登记的被拒仍保留的写入（kept 为 None 时丢弃）。

    不是 4xx 的 ApiError 不算业务拒绝，原样抛出。
    """
    if not 400 <= error.status <= 499:
        raise error
    savepoint.rollback()
    if kept is not None:
        for write in kept.kept_writes:
            write(kept.connection)
    return CommandsOutcome(error, defect or error.code in DEFECT_CODES, _events(*executions))


def reserve_and_commit(
    connection: Connection, context: CommandContext, skus: CommitSkus, commands: ReadyCommands
) -> CommandsOutcome:
    """在执行器已开的事务里开一个保存点，预留并确认销售。

    skus 是第 3 步预建余额行时算出的 SKU 及其合并去向（同 HTTP 接口的 CommitSkus）。
    调用前执行器已按锁序锁住池行、余额行、配额行与平台商品。
    """
    order_ref = commands.order_ref
    lines = commands.reservation.model.reservation_lines()
    ttl_seconds = context.reservation_ttl_seconds
    defect = False

    reserving = _execution(connection, context, commands.reservation)
    savepoint = connection.begin_nested()
    try:
        reserved = create_reservation(reserving, skus.pool_id, order_ref, lines, ttl_seconds)
    except BusinessRefTaken as taken:
        # 撤销这一次的写入，丢弃它登记的事件与被拒仍保留的写入，在新保存点里重新判定。
        savepoint.rollback()
        defect = True
        reserving = _execution(connection, context, commands.reservation)
        savepoint = connection.begin_nested()
        try:
            reserved = taken.rejudge(reserving)
        except ApiError as error:
            return _rejected(savepoint, error, (reserving,), reserving, defect)
    except ApiError as error:
        return _rejected(savepoint, error, (reserving,), reserving, defect)
    reserved = _succeeded(reserved)
    by_reference = reserving.answered_by_reference

    sale_commit = commands.sale_commit(reserved.data["reservation_id"])
    committing = _execution(connection, context, sale_commit)
    try:
        _succeeded(create_sale_commit(committing, skus, order_ref, sale_commit.model.sale_commit()))
    except ApiError as error:
        # 按引用拿回的预留再确认被拒：被拒仍保留的写入不执行（平台订单不走晚到与故障补交）。
        kept = None if by_reference else committing
        return _rejected(savepoint, error, (reserving, committing), kept, defect or by_reference)
    savepoint.commit()
    defect = defect or by_reference or committing.answered_by_reference
    return CommandsOutcome(None, defect, _events(reserving, committing))
