from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.environment import parse_admin_enabled, parse_core_env
from app.domain.reservations import TTL_DEFAULT, parse_ttl_seconds


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CORE_")

    # 必填、无默认值：没写 CORE_ENV 就启动失败，不会默认成某个环境。
    env: str
    # 镜像构建时由 Dockerfile 的 GIT_SHA 构建参数写进 CORE_GIT_SHA；健康检查原样返回。
    git_sha: str = "unknown"
    # API 必填（契约 6.1 启动核对）：为空时 create_app 拒绝启动。默认值留空，
    # 是为了运维命令行的 --help 与参数错误不需要它；连库的命令同样拒绝空值。
    database_url: str = ""
    # 预留有效期（秒，契约 5.6，Q-53）：60–3600 的整数，不设为 900；
    # 不合格时构造失败，API 与命令行都拒绝启动。只在启动时读。
    reservation_ttl_seconds: int = TTL_DEFAULT
    # 后台开关（契约 6.1，Q-87）：只认 true 与 false，不设为 false；不合格时构造失败，
    # API 与命令行都拒绝启动。只在启动时读；Demo 实例上为 true 时拒绝启动。
    admin_enabled: bool = False

    @field_validator("env")
    @classmethod
    def _check_env(cls, value: str) -> str:
        return parse_core_env(value)

    @field_validator("reservation_ttl_seconds", mode="before")
    @classmethod
    def _check_reservation_ttl_seconds(cls, value):
        # 环境变量读进来是字符串：只收十进制数字，不收符号、空白与小数。
        if isinstance(value, str):
            if not (value.isascii() and value.isdigit()):
                raise ValueError("CORE_RESERVATION_TTL_SECONDS must be an integer")
            value = int(value)
        return parse_ttl_seconds(value)

    @field_validator("admin_enabled", mode="before")
    @classmethod
    def _check_admin_enabled(cls, value):
        # 环境变量读进来是字符串，严格按 true 与 false 判；代码里直接传布尔值时原样收下。
        if isinstance(value, bool):
            return value
        if not isinstance(value, str):
            raise ValueError("CORE_ADMIN_ENABLED must be true or false")
        return parse_admin_enabled(value)
