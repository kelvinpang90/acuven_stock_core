"""命令上下文：交给命令体、与 HTTP 无关的身份与请求信息（契约 §5.10“命令体共用”，Q-84）。

HTTP 执行器（app/services/idempotency.py 的 run_idempotent）
从鉴权结果、请求与 app.state 组出它；后台执行器与平台事件执行器按自己的来源组。
本模块不 import FastAPI 与 Starlette。
"""

from dataclasses import dataclass

from app.domain.requests import Actor


@dataclass(frozen=True)
class CommandContext:
    """一次命令的身份与请求信息。

    幂等键只有写幂等记录的执行器有；目录变更记录与 SKU 合并也存它（§2.7、§2.8）。
    http_method、path（不含查询串）与 user_agent 只用来登记安全事件，非 HTTP 的执行器给 None。
    """

    tenant_id: str
    channel_id: str | None  # 入口；没有（有效的）入口时为 None
    pool_id: str | None  # 入口绑定的池
    credential_id: str  # 操作主体
    actor: Actor | None  # 操作者（ref 与 role）
    request_id: str
    idempotency_key: str | None
    reservation_ttl_seconds: int  # 预留有效期（§5.6）
    http_method: str | None = None
    path: str | None = None
    user_agent: str | None = None
