# Acuven Stock Core

`acuven_stock_core` 是商品身份目录与基础库存账的**独立服务项目**。需求见 [REQUIREMENTS.md](REQUIREMENTS.md)，契约 [docs/CONTRACT_V1.md](docs/CONTRACT_V1.md) 已于 2026-11-10 批准。目前只有从项目模板起步的最小服务骨架（健康检查、CI、部署工作流、OpenClaw 契约），还没有业务功能；没有 GitHub 远端仓库，也没有部署。

服务范围：统一 SKU、条码、基本商品名称与参考价；记录库存地点、预留、出入库与可追溯流水。`acuven_inventory` 另负责跨销售入口的库存分配、外部平台同步与对账。

项目名确定为 **Acuven Stock Core**，部署边界确定为**独立常驻服务及独立持久化库**。V1 先接公开网店 Demo（独立的 Demo Core 实例），单一库存池；真实网店何时接入待定，Inventory、ERP、POS 后续接入。恢复目标：RPO≤5 分钟、RTO≤4 小时（2026-11-10 由 RPO=0 放宽）。不同套餐的装配方式见 [部署包装讨论](../acuven_hub/STOCK_CORE_DEPLOYMENT_PACKAGING_2026-10-08.md)；原方案比较见 [商品与库存关系](../acuven_hub/REVISED_PRODUCT_STOCK_ARCHITECTURE_2026-10-07.md)及[开发优先顺序](../acuven_hub/DEVELOPMENT_PRIORITY_2026-10-08.md)。

## 阶段 1 设计文档（2026-11-10，契约已由 Kelvin 批准）

| 文档 | 内容 |
| --- | --- |
| [docs/SHOP_STOCK_INVENTORY.md](docs/SHOP_STOCK_INVENTORY.md) | 网店现有商品与库存代码盘点，及将来换成调用 Core 的每个调用点 |
| [docs/CONTRACT_V1.md](docs/CONTRACT_V1.md) | Core V1 契约（0.4，2026-11-10 批准）：商品身份、库存池、数量模型与流水、预留→确认→释放、幂等与审计、`/v1` API、`StockGateway`、迁移与回退样例、租户与鉴权、恢复目标（RPO≤5 分钟、RTO≤4h）的设计约束 |
| [docs/TEMPLATE_BOOTSTRAP_PLAN.md](docs/TEMPLATE_BOOTSTRAP_PLAN.md) | 从项目模板起步的方案（2026-11-10 已按它起步，推送与登记待做） |
| [docs/ESTIMATE_BREAKDOWN.md](docs/ESTIMATE_BREAKDOWN.md) | 工期重估（I-04）用的工作拆分，不是排期 |
| [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md) | 设计问题与 Kelvin 的答复（Q-24 暂缓） |

开发任务清单与 OpenClaw 计划见 [docs/TODO.md](docs/TODO.md)；项目规则见 [CLAUDE.md](CLAUDE.md)（与 `AGENTS.md` 同一内容）。

## 网店单卖与加购 Inventory 的建议连接方式

| 购买组合 | 网店的商品身份读取 | 网店的库存操作 | 数量权威 |
| --- | --- | --- | --- |
| 只有网店 | 使用随附的 Core 商品目录 | 网店后端通过稳定的库存接口直连 Core，查可售量、预留、确认销售及释放 | Core 的一份基础库存账 |
| 后加 Inventory 并与其他入口共卖同一批货 | 仍使用原 Core 商品目录；图片、文案与渠道售价留在网店 | 网店后端的库存适配器改接 Inventory；Inventory 处理入口映射、份额、同步与异常，再向同一 Core 提交唯一的库存命令 | **同一个** Core 库存账，不在 Inventory 另建余额账 |

Inventory 只在**库存命令路径**上处于网店与 Core 之间，不代理网店所有页面、订单或付款。Core 在单一数量交易内校验已生效的入口额度与预留，Inventory 不可先在自己数据库扣数、再要求 Core 扣第二次。Shop 与 Inventory 都不得同时对 Core 提交同一笔销售事实。

为了日后加购，网店业务代码从第一版真实经营起只依赖稳定的 `StockGateway` 契约；单卖时由 Core 适配器实现，加购时由 Inventory 适配器实现。切换前须核对 SKU／库存池映射、未结预留和期初数量，停止旧路径写入，再启用新路径并对账。即使 Core 已独立运行，加购 Inventory 仍需要适配器切换与业务验收，不能承诺零改造。

这是后续加购 Inventory 时的**架构建议**，不是已实现的接口。首版需求已确定 Core 独立运行；现有 [Inventory 网店 API 契约](../acuven_inventory/docs/API_CONTRACT.md)仍是草案，正式接入前要确定两种适配器的共同语义与切换验收。
