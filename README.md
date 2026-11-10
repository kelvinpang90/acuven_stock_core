# Acuven Stock Core

`acuven_stock_core` 是商品身份目录与基础库存账的**独立服务项目**。首版需求已确定，但目前没有应用代码、GitHub 远端仓库或已部署服务（本地已 `git init`，只含文档）。详见 [REQUIREMENTS.md](REQUIREMENTS.md)。

服务范围：统一 SKU、条码、基本商品名称与参考价；记录库存地点、预留、出入库与可追溯流水。`acuven_inventory` 另负责跨销售入口的库存分配、外部平台同步与对账。

项目名确定为 **Acuven Stock Core**，部署边界确定为**独立常驻服务及独立持久化库**。首版先接 Acuven 自用网店，支持单一库存池的真实经营；Inventory、ERP、POS 后续接入。生产上线前还需按需求验证独立故障域的持久化与恢复：已确认写入 RPO=0、RTO≤4 小时。不同套餐的装配方式见 [部署包装讨论](../acuven_hub/STOCK_CORE_DEPLOYMENT_PACKAGING_2026-10-08.md)；原方案比较见 [商品与库存关系](../acuven_hub/REVISED_PRODUCT_STOCK_ARCHITECTURE_2026-10-07.md)及[开发优先顺序](../acuven_hub/DEVELOPMENT_PRIORITY_2026-10-08.md)。

## 阶段 1 设计文档（2026-11-10，契约已由 Kelvin 批准）

| 文档 | 内容 |
| --- | --- |
| [docs/SHOP_STOCK_INVENTORY.md](docs/SHOP_STOCK_INVENTORY.md) | 网店现有商品与库存代码盘点，及将来换成调用 Core 的每个调用点 |
| [docs/CONTRACT_V1.md](docs/CONTRACT_V1.md) | Core V1 契约（0.2，2026-11-10 批准）：商品身份、库存池、数量模型与流水、预留→确认→释放、幂等与审计、`/v1` API、`StockGateway`、迁移与回退样例、租户与鉴权、RPO=0/RTO≤4h 的设计约束 |
| [docs/TEMPLATE_BOOTSTRAP_PLAN.md](docs/TEMPLATE_BOOTSTRAP_PLAN.md) | 从项目模板起步的方案（未执行） |
| [docs/ESTIMATE_BREAKDOWN.md](docs/ESTIMATE_BREAKDOWN.md) | 工期重估（I-04）用的工作拆分，不是排期 |
| [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md) | 待拍板的问题与文档冲突 |

以上都是设计草案，仍没有应用代码、GitHub 仓库或已部署服务。

## 网店单卖与加购 Inventory 的建议连接方式

| 购买组合 | 网店的商品身份读取 | 网店的库存操作 | 数量权威 |
| --- | --- | --- | --- |
| 只有网店 | 使用随附的 Core 商品目录 | 网店后端通过稳定的库存接口直连 Core，查可售量、预留、确认销售及释放 | Core 的一份基础库存账 |
| 后加 Inventory 并与其他入口共卖同一批货 | 仍使用原 Core 商品目录；图片、文案与渠道售价留在网店 | 网店后端的库存适配器改接 Inventory；Inventory 处理入口映射、份额、同步与异常，再向同一 Core 提交唯一的库存命令 | **同一个** Core 库存账，不在 Inventory 另建余额账 |

Inventory 只在**库存命令路径**上处于网店与 Core 之间，不代理网店所有页面、订单或付款。Core 在单一数量交易内校验已生效的入口额度与预留，Inventory 不可先在自己数据库扣数、再要求 Core 扣第二次。Shop 与 Inventory 都不得同时对 Core 提交同一笔销售事实。

为了日后加购，网店业务代码从第一版真实经营起只依赖稳定的 `StockGateway` 契约；单卖时由 Core 适配器实现，加购时由 Inventory 适配器实现。切换前须核对 SKU／库存池映射、未结预留和期初数量，停止旧路径写入，再启用新路径并对账。即使 Core 已独立运行，加购 Inventory 仍需要适配器切换与业务验收，不能承诺零改造。

这是后续加购 Inventory 时的**架构建议**，不是已实现的接口。首版需求已确定 Core 独立运行；现有 [Inventory 网店 API 契约](../acuven_inventory/docs/API_CONTRACT.md)仍是草案，正式接入前要确定两种适配器的共同语义与切换验收。
