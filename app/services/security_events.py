"""写安全事件（契约 7.2）。

接口层的 401、403、跨租户 404，之后的幂等执行器、业务引用冲突与命令行
都用 record_security_event。
安全事件总在业务事务之外另开短事务写入，不随业务事务回滚；
写入失败只记进程日志，不改变对调用方的响应。
不记 Authorization 头、令牌与请求体：调用方只把契约 7.2 列出的内容放进 details。

满 1 年的事件由 purge_expired_security_events 整批删除（契约 7.1 保留期、7.2），
定时任务在 app/main.py 接入。
"""

import logging
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import Connection, Engine, insert, text

from app.domain.ids import new_id
from app.domain.requests import truncate_user_agent
from app.models.service_auth import SecurityEvent

INVALID_CREDENTIAL = "invalid_credential"
FORBIDDEN = "forbidden"
CROSS_TENANT = "cross_tenant"

PATH_MAX_LENGTH = 512
PURGE_BATCH_SIZE = 1000

_log = logging.getLogger(__name__)
# 清理结果写到 app.api 的子日志器：app/api/requests.py 给 app.api 挂了输出、级别 INFO。
# 不给本模块的日志器挂处理器：命令行也 import 本模块，挂上会把写入失败的堆栈打到标准错误。
_purge_log = logging.getLogger("app.api.retention")

# 按数据库时间：恰好满 1 年的也删。只删 security_events。
_PURGE_BATCH = text(
    "DELETE FROM security_events"
    " WHERE occurred_at <= CURRENT_TIMESTAMP(6) - INTERVAL 1 YEAR"
    " ORDER BY occurred_at"
    " LIMIT :batch_size"
)


@dataclass(frozen=True)
class SecurityEventRecord:
    """一条安全事件；发生时间取数据库时间（列默认值）。

    path 不含查询串，超过 512 字符截断；user_agent 截断到 200 字符；两者都在写入时截断。
    tenant_id 与 credential_id 认出来了才有；actor_ref 只在两个操作者头都带且都合格时记。
    """

    event_type: str
    request_id: str | None = None
    tenant_id: str | None = None
    credential_id: str | None = None
    actor_ref: str | None = None
    http_method: str | None = None
    path: str | None = None
    user_agent: str | None = None
    details: dict[str, Any] | None = None


def _insert(connection: Connection, values: dict[str, Any]) -> None:
    with connection.begin():
        connection.execute(insert(SecurityEvent).values(values))


def record_security_event(
    event: SecurityEventRecord, *, engine: Engine | None, connection: Connection | None = None
) -> bool:
    """另开一个短事务写一条事件，返回是否写入。

    connection 是处理这个请求的连接：传入时其上不能有打开的事务
    （业务事务先结束、命名锁先释放）。没传、已关闭或已作废时从 engine 的连接池另取一个。
    """
    values = asdict(event) | {
        "event_id": new_id("sev"),
        "path": None if event.path is None else event.path[:PATH_MAX_LENGTH],
        "user_agent": truncate_user_agent(event.user_agent),
    }
    try:
        if connection is not None and not connection.closed and not connection.invalidated:
            _insert(connection, values)
        elif engine is None:
            raise RuntimeError("database is not configured")
        else:
            with engine.connect() as pooled:
                _insert(pooled, values)
    except Exception:
        _log.exception(
            "security event not written (type=%s, request_id=%s)",
            event.event_type,
            event.request_id,
        )
        return False
    return True


def purge_expired_security_events(connection: Connection) -> int:
    """删除 occurred_at 不晚于数据库当前时间减 1 年的事件，返回删除总数。

    每批最多 PURGE_BATCH_SIZE 行、每批一个短事务，某批不足一批为止；各批都在 connection 上执行。
    传入时其上不能有打开的事务（与 record_security_event 的约定一致）。
    多个进程同时清理只是重复删除同一批，结果不变。
    """
    total = 0
    while True:
        with connection.begin():
            deleted = connection.execute(_PURGE_BATCH, {"batch_size": PURGE_BATCH_SIZE}).rowcount
        total += deleted
        if deleted < PURGE_BATCH_SIZE:
            break
    _purge_log.info("purged %d security events older than 1 year", total)
    return total
