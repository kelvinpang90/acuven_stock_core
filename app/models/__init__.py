"""数据库模型。每个模型模块都要在 alembic/env.py 与这里 import，登记到 Base.metadata。

instance 是库里的实例标记（契约 6.1）。
"""

from app.models import catalog, instance, inventory, merges, orders, platform, service_auth

__all__ = ["catalog", "instance", "inventory", "merges", "orders", "platform", "service_auth"]
