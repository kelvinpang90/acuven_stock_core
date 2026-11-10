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


def health_payload(commit: str, env: str) -> dict[str, str]:
    """健康检查的响应体：部署工作流靠 commit 确认线上就是本次提交（D3），网店靠 env 核对实例。"""
    return {"status": "ok", "commit": commit or "unknown", "env": env}
