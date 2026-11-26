"""运行环境标记（契约 §6.1、§9.2）。

Core 启动时读一次 `CORE_ENV`，运行中不能改。Demo 专用能力（如每日重置）只在 demo 实例上存在；
健康检查返回这个标记，网店据此核对自己没有连错实例。
"""

DEMO = "demo"
PRODUCTION = "production"
ENVIRONMENTS = (DEMO, PRODUCTION)


def parse_core_env(value: str) -> str:
    """只接受 demo 或 production（区分大小写、不去空格）：配置写错时启动失败，而不是猜一个环境。"""
    if value not in ENVIRONMENTS:
        raise ValueError(f"CORE_ENV must be one of {ENVIRONMENTS}, got {value!r}")
    return value


def parse_admin_enabled(value: str) -> bool:
    """后台开关 CORE_ADMIN_ENABLED（契约 6.1，Q-87）：只认 true 与 false。

    区分大小写、不去空格、不收空串：写错时启动失败，而不是猜一个值。
    """
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("CORE_ADMIN_ENABLED must be true or false")


def admin_enabled_error(core_env: str, admin_enabled: bool) -> str | None:
    """Demo 实例不开后台（契约 6.1）：开关为 true 时返回拒绝启动的一行原因，否则为 None。

    消息只含键名与环境名，不带连接串。
    """
    if core_env == DEMO and admin_enabled:
        return f"CORE_ADMIN_ENABLED is true; CORE_ENV is {core_env}"
    return None


class InstanceMismatch(ValueError):
    """库里的实例标记与执行进程的 CORE_ENV 不一致，或库里没有标记（契约 6.1）。"""


def check_instance_marker(marker: str | None, core_env: str) -> None:
    """迁移前核对与启动核对共用：marker 是库里 core_instance 的 core_env，表在而没有行时为 None。

    消息只有一行、只含两个环境名，不带连接串：它会出现在部署日志里。
    """
    if marker is None:
        raise InstanceMismatch(f"core_instance has no row; CORE_ENV is {core_env}")
    if marker != core_env:
        raise InstanceMismatch(f"database is marked {marker}; CORE_ENV is {core_env}")


def health_payload(commit: str, env: str) -> dict[str, str]:
    """健康检查的响应体：部署工作流靠 commit 确认线上就是本次提交（D3），网店靠 env 核对实例。"""
    return {"status": "ok", "commit": commit or "unknown", "env": env}
