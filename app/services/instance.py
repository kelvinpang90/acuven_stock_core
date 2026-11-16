"""库里的实例标记与启动核对（契约 6.1）。

API（app.main.create_app）与运维命令行（app.cli.run_command）在开始处理请求或命令之前核对。
"""

from sqlalchemy import Connection, Engine, select

from app.domain.environment import InstanceMismatch, check_instance_marker
from app.models.instance import CoreInstance


def read_instance_marker(connection: Connection) -> str | None:
    """core_instance 那一行的 core_env；表存在而没有行时为 None。

    表不存在、连不上等数据库错误照常抛出，由调用方当作核对失败处理。
    """
    return connection.execute(select(CoreInstance.core_env)).scalar_one_or_none()


def instance_check_error(engine: Engine, core_env: str) -> str | None:
    """启动核对：通过时为 None，否则是一行原因。

    原因只有固定文本与环境名：连接串与异常文本都可能带密码，不写进去。
    """
    try:
        with engine.connect() as connection:
            marker = read_instance_marker(connection)
    except Exception:
        return f"cannot read core_instance; CORE_ENV is {core_env}"
    try:
        check_instance_marker(marker, core_env)
    except InstanceMismatch as mismatch:
        return str(mismatch)
    return None
