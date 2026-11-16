"""库里的实例标记（契约 6.1）。

标记迁移（0006）按执行迁移的进程的 CORE_ENV 写入唯一一行，之后的迁移不改它。
迁移前核对与启动核对拿它与 CORE_ENV 比对，不一致就不迁移、不启动。
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, String
from sqlalchemy.dialects.mysql import TINYINT
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.catalog import EXACT_COLLATION, NOW, TABLE_OPTIONS, TIMESTAMP


class CoreInstance(Base):
    __tablename__ = "core_instance"
    __table_args__ = (
        # 主键恒为 1，所以只能有一行。
        CheckConstraint("singleton = 1", name="singleton"),
        CheckConstraint("core_env IN ('demo', 'production')", name="core_env"),
        TABLE_OPTIONS,
    )

    # MySQL 8 不允许检查约束引用自增列，关掉整数主键默认的 AUTO_INCREMENT。
    singleton: Mapped[int] = mapped_column(TINYINT, primary_key=True, autoincrement=False)
    core_env: Mapped[str] = mapped_column(String(16, collation=EXACT_COLLATION))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=NOW)
