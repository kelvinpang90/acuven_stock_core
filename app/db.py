from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase

# 约束与索引名固定下来：迁移里按名字改约束时，MySQL 上与模型一致。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def make_engine(database_url: str) -> Engine:
    # 会话时区固定为 UTC：DATETIME 列的数据库默认值与 NOW() 都按 UTC 记（契约 2.2 数据库列约定）。
    # hide_parameters：SQLAlchemy 的异常消息不带绑定参数（密码哈希、令牌摘要等，
    # docs/ADMIN_AUTH.md 第 3 节）；数据库自己的错误文本仍可能含列值。
    return create_engine(
        database_url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"init_command": "SET time_zone = '+00:00'"},
    )
