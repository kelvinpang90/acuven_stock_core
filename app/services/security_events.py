"""写安全事件（契约 7.2）。

接口层的 401、403、跨租户 404，之后的幂等执行器、业务引用冲突与命令行
都用 record_security_event。
安全事件总在业务事务之外另开短事务写入，不随业务事务回滚；
写入失败只记进程日志，不改变对调用方的响应。
不记 Authorization 头、令牌与请求体：调用方只把契约 7.2 列出的内容放进 details。
"""

import logging
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import Connection, Engine, insert

from app.domain.ids import new_id
from app.domain.requests import truncate_user_agent
from app.models.service_auth import SecurityEvent

INVALID_CREDENTIAL = "invalid_credential"
FORBIDDEN = "forbidden"
CROSS_TENANT = "cross_tenant"

PATH_MAX_LENGTH = 512

_log = logging.getLogger(__name__)


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
