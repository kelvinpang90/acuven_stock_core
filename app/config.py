from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.environment import parse_core_env


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CORE_")

    # 必填、无默认值：没写 CORE_ENV 就启动失败，不会默认成某个环境。
    env: str
    # 镜像构建时由 Dockerfile 的 GIT_SHA 构建参数写进 CORE_GIT_SHA；健康检查原样返回。
    git_sha: str = "unknown"
    # 可以不设：未设置时 /readyz 回答 503（数据库未配置），/healthz 照常。
    database_url: str = ""

    @field_validator("env")
    @classmethod
    def _check_env(cls, value: str) -> str:
        return parse_core_env(value)
