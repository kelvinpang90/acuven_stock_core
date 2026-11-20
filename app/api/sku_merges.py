"""SKU 合并的新建、录入实数、执行、完成、取消与查询接口。

契约 §2.4、§2.7、§2.8、§7.2、§10 第 3、4 条。
全部接口（含查询）要求 catalog.write 与 stock.write 两者。
五个写命令经幂等执行器，业务事务以 READ COMMITTED 开启（§2.8 通用）。
新建、执行、完成、取消只接受 stock_admin；录入实数接受 stock_operator 与 stock_admin。
新建不写目录，也取目录写锁（§2.7、§2.8 新建），与修改编码等目录命令串行；
执行写 sku_merged 变更记录，同样取目录写锁。
池授权：录入实数按正文的 pool_id，开事务之前按这个池预建合并单两个 SKU 的余额行；
执行按合并单录入实数时的池（按 ID 指向池内对象，还没录入的只查有没有销售入口），
开事务之前按池授权时读到的池预建两个余额行、先关两个 SKU 上已到期的预留，
复核多出订单行时一个请求最多做 3 遍。
录入实数、执行、完成与取消的 404 在事务里判出、作为确定结果保存（app/services/sku_merges.py）；
执行的池授权在取幂等锁之前，它的 403、404 不保存。
执行、完成与取消没有请求体。查询不要求角色，不存在或属于别的租户 404，后者记 cross_tenant。
查库与写库在 app/services/sku_merges.py。
"""

import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
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
from app.domain.catalog import REASON, InvalidField
from app.domain.requests import STOCK_ADMIN, STOCK_OPERATOR, Actor
from app.domain.service_auth import CATALOG_WRITE, STOCK_WRITE
from app.domain.sku_merges import (
    CANONICAL_SKU_ID,
    POOL_ID,
    SHELF_COUNT,
    SOURCE_SKU_ID,
    UNSELLABLE_COUNT,
    parse_count_body,
    parse_create_body,
)
from app.services import sku_merges
from app.services.idempotency import CommandResult, Execution, run_idempotent
from app.services.sku_merges import SKU_MERGE, SKU_MERGE_NOT_FOUND

SKU_MERGES_CREATE = "sku_merges.create"
SKU_MERGES_COUNT = "sku_merges.count"
SKU_MERGES_EXECUTE = "sku_merges.execute"
SKU_MERGES_COMPLETE = "sku_merges.complete"
SKU_MERGES_CANCEL = "sku_merges.cancel"

SKU_MERGES = "/v1/sku-merges"

_log = logging.getLogger("app.api")

router = APIRouter()
_READ = [Depends(authorize(CATALOG_WRITE, STOCK_WRITE))]
_WRITE = [Depends(authorize(CATALOG_WRITE, STOCK_WRITE, write=True))]
# 执行、完成与取消没有请求体：no_body 在 authorize 之后，空请求体跳过、非空 422。
_WRITE_WITHOUT_BODY = [*_WRITE, Depends(no_body)]

OperatorOrAdmin = Annotated[Actor, Depends(require_role(STOCK_OPERATOR, STOCK_ADMIN))]
Admin = Annotated[Actor, Depends(require_role(STOCK_ADMIN))]


# --- 请求体（§2.8） ---


class MergeCreation(RequestBody):
    """只管 JSON 类型与未知字段；取值规则由 parse_create_body 判（见 checked_creation）。

    reason 的默认 None 只表示没带：给 null 与没带相同。
    """

    source_sku_id: str
    canonical_sku_id: str
    reason: str | None = None


class MergeCount(RequestBody):
    """只管 JSON 类型与未知字段；两项实数的范围由 parse_count_body 判（见 checked_count）。"""

    pool_id: str
    shelf_count: int
    unsellable_count: int


creation_body = json_body(MergeCreation)
count_body = json_body(MergeCount)


def _parsed(parse: Callable[[dict], dict], body: ParsedBody) -> dict:
    """第 7 步的最后一小步：按 app/domain/sku_merges.py 的规则判，不合规的字段按位置 422。"""
    try:
        return parse(body.raw)
    except InvalidField as exc:
        errors = [{"loc": [exc.field], "type": "value_error"}]
        raise invalid_request("request body validation failed", {"errors": errors}) from None


def checked_creation(
    body: Annotated[ParsedBody[MergeCreation], Depends(creation_body)],
) -> dict:
    """两个 ID 相同、reason 不合规：422。返回规范化后的三项。"""
    return _parsed(parse_create_body, body)


def checked_count(body: Annotated[ParsedBody[MergeCount], Depends(count_body)]) -> dict:
    """两项实数不在 0–1,000,000：422。返回三项。"""
    return _parsed(parse_count_body, body)


CreationBody = Annotated[ParsedBody[MergeCreation], Depends(creation_body)]
CountBody = Annotated[ParsedBody[MergeCount], Depends(count_body)]


def _count_pool(fields: Annotated[dict, Depends(checked_count)]) -> str:
    return fields[POOL_ID]


def _path_merge_id(merge_id: str) -> str:
    return merge_id


# 执行的池授权：按合并单录入实数时的池；还没录入（池为空）的只查有没有销售入口。
_merge_pool = require_object_pool(
    _path_merge_id,
    sku_merges.find_merge_pool,
    object_type=SKU_MERGE,
    not_found_code=SKU_MERGE_NOT_FOUND,
)


# --- 新建 ---


@router.post(SKU_MERGES, dependencies=_WRITE)
def create_merge(
    request: Request,
    body: CreationBody,
    fields: Annotated[dict, Depends(checked_creation)],
    actor: Admin,
) -> Response:
    def command(execution: Execution) -> CommandResult:
        return sku_merges.create_merge(
            execution, fields[SOURCE_SKU_ID], fields[CANONICAL_SKU_ID], fields[REASON]
        )

    return run_idempotent(
        request,
        operation=SKU_MERGES_CREATE,
        body=body,
        command=command,
        catalog_lock=True,
        read_committed=True,
    )


# --- 录入实数 ---


@router.post(SKU_MERGES + "/{merge_id}/count", dependencies=_WRITE)
def count_merge(
    request: Request,
    merge_id: str,
    body: CountBody,
    fields: Annotated[dict, Depends(checked_count)],
    actor: OperatorOrAdmin,
    pool_id: Annotated[str, Depends(require_pool(_count_pool))],
) -> Response:
    tenant_id = get_access(request).principal.tenant_id

    def balance_rows(connection: Connection) -> list[tuple[str, str]]:
        # 合并单不存在或属于别的租户时不预建，由事务里报 404。
        sku_ids = sku_merges.merge_sku_ids(connection, tenant_id, merge_id)
        return [(pool_id, sku_id) for sku_id in sku_ids]

    def command(execution: Execution) -> CommandResult:
        return sku_merges.count_merge(
            execution, merge_id, pool_id, fields[SHELF_COUNT], fields[UNSELLABLE_COUNT]
        )

    return run_idempotent(
        request,
        operation=SKU_MERGES_COUNT,
        body=body,
        command=command,
        balance_rows=balance_rows,
        read_committed=True,
    )


# --- 执行 ---


@router.post(SKU_MERGES + "/{merge_id}/execute", dependencies=_WRITE_WITHOUT_BODY)
def execute_merge(
    request: Request,
    merge_id: str,
    actor: Admin,
    pool_id: Annotated[str | None, Depends(_merge_pool)],
) -> Response:
    tenant_id = get_access(request).principal.tenant_id
    # 池授权时读到的池；为 None 时开事务前的预建与先关到期都跳过。
    skus = sku_merges.ExecutionSkus(tenant_id, merge_id, pool_id)

    def command(execution: Execution) -> CommandResult:
        return sku_merges.execute_merge(execution, merge_id)

    return run_idempotent(
        request,
        operation=SKU_MERGES_EXECUTE,
        body=None,
        command=command,
        catalog_lock=True,
        balance_rows=skus.balance_rows,
        read_committed=True,
        before_transaction=skus.close_due,
        attempts=sku_merges.EXECUTE_ATTEMPTS,
    )


# --- 完成与取消 ---


@router.post(SKU_MERGES + "/{merge_id}/complete", dependencies=_WRITE_WITHOUT_BODY)
def complete_merge(request: Request, merge_id: str, actor: Admin) -> Response:
    def command(execution: Execution) -> CommandResult:
        return sku_merges.complete_merge(execution, merge_id)

    return run_idempotent(
        request, operation=SKU_MERGES_COMPLETE, body=None, command=command, read_committed=True
    )


@router.post(SKU_MERGES + "/{merge_id}/cancel", dependencies=_WRITE_WITHOUT_BODY)
def cancel_merge(request: Request, merge_id: str, actor: Admin) -> Response:
    def command(execution: Execution) -> CommandResult:
        return sku_merges.cancel_merge(execution, merge_id)

    return run_idempotent(
        request, operation=SKU_MERGES_CANCEL, body=None, command=command, read_committed=True
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
            "database error during a sku merge read (request_id=%s)",
            request.state.request_id,
            exc_info=True,
        )
        discard_connection(request)
        raise unavailable() from None
    return result


@router.get(SKU_MERGES + "/{merge_id}", dependencies=_READ)
def get_merge(request: Request, merge_id: str) -> JSONResponse:
    """不存在与属于别的租户同一个 404，后者记 cross_tenant。"""
    tenant_id = get_access(request).principal.tenant_id

    def lookup(connection: Connection) -> tuple[dict | None, str | None]:
        found = sku_merges.current_representation(connection, tenant_id, merge_id)
        owner = None if found is not None else sku_merges.merge_owner(connection, merge_id)
        return found, owner

    found, owner = _read(request, lookup)
    if found is None:
        if owner is not None:
            raise cross_tenant_not_found(
                request, object_type=SKU_MERGE, object_id=merge_id, code=SKU_MERGE_NOT_FOUND
            )
        raise ApiError(404, SKU_MERGE_NOT_FOUND, f"{SKU_MERGE} not found")
    return ok(request, found)
