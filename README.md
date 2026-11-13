# Acuven Stock Core

`acuven_stock_core` 是商品身份目录与基础库存账的**独立服务项目**。需求见 [REQUIREMENTS.md](REQUIREMENTS.md)，契约 [docs/CONTRACT_V1.md](docs/CONTRACT_V1.md) 已于 2026-11-10 批准。目前只有从项目模板起步的最小服务骨架（健康检查、CI、部署工作流、OpenClaw 契约），还没有业务功能。仓库已推到 GitHub（公开），Demo Core 实例已部署（无公网入口）；尚未登记 OpenClaw 控制面。

服务范围：统一 SKU、条码、基本商品名称与参考价；记录库存地点、预留、出入库与可追溯流水。`acuven_inventory` 另负责跨销售入口的库存分配、外部平台同步与对账。

项目名确定为 **Acuven Stock Core**，部署边界确定为**独立常驻服务及独立持久化库**。V1 先接公开网店 Demo（独立的 Demo Core 实例），单一库存池；真实网店何时接入待定，Inventory、ERP、POS 后续接入。恢复目标：RPO≤5 分钟、RTO≤4 小时（2026-11-10 由 RPO=0 放宽）。不同套餐的装配方式见 [部署包装讨论](../acuven_hub/STOCK_CORE_DEPLOYMENT_PACKAGING_2026-10-08.md)；原方案比较见 [商品与库存关系](../acuven_hub/REVISED_PRODUCT_STOCK_ARCHITECTURE_2026-10-07.md)及[开发优先顺序](../acuven_hub/DEVELOPMENT_PRIORITY_2026-10-08.md)。

## 阶段 1 设计文档（2026-11-10，契约已由 Kelvin 批准）

| 文档 | 内容 |
| --- | --- |
| [docs/SHOP_STOCK_INVENTORY.md](docs/SHOP_STOCK_INVENTORY.md) | 网店现有商品与库存代码盘点，及将来换成调用 Core 的每个调用点 |
| [docs/CONTRACT_V1.md](docs/CONTRACT_V1.md) | Core V1 契约（0.5，2026-11-12 批准）：商品身份、库存池、数量模型与流水、预留→确认→释放、幂等与审计、`/v1` API、`StockGateway`、迁移与回退样例、租户与鉴权、恢复目标（RPO≤5 分钟、RTO≤4h）的设计约束 |
| [docs/TEMPLATE_BOOTSTRAP_PLAN.md](docs/TEMPLATE_BOOTSTRAP_PLAN.md) | 从项目模板起步的方案（2026-11-10 已按它起步，2026-11-11 已推送并部署，登记待做） |
| [docs/ESTIMATE_BREAKDOWN.md](docs/ESTIMATE_BREAKDOWN.md) | 工期重估（I-04）用的工作拆分，不是排期 |
| [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md) | 设计问题与 Kelvin 的答复（Q-24 暂缓） |

开发任务清单与 OpenClaw 计划见 [docs/TODO.md](docs/TODO.md)；项目规则见 [CLAUDE.md](CLAUDE.md)（与 `AGENTS.md` 同一内容）。

## 运维命令行（开通租户、地点、库存池与销售入口；服务凭据与令牌）

Core 没有 HTTP 管理接口（契约第 10 节第 4 条），租户、地点、库存池与销售入口只能由运维在服务器上、在运行中的 Core 容器里用 `python -m app.cli` 建；服务凭据的签发、轮换与吊销也只走这里。
命令用容器里已有的 `CORE_ENV` 与 `CORE_DATABASE_URL`（与服务同一个数据库账号）。写法同 [deploy/healthcheck.sh](deploy/healthcheck.sh)：按 compose 标签找到容器，再 `docker exec`：

```bash
container="$(docker ps -q \
    --filter label=com.docker.compose.project=acuven_stock_core \
    --filter label=com.docker.compose.service=core_api)"
docker exec "$container" python -m app.cli --help
docker exec "$container" python -m app.cli tenant create --name "Demo Shop"
docker exec "$container" python -m app.cli location create --tenant ten_… --name "Warehouse"
docker exec "$container" python -m app.cli pool create --tenant ten_… --location loc_… --name "Main"
docker exec "$container" python -m app.cli channel create --tenant ten_… --pool pool_… --name "Web shop"
docker exec "$container" python -m app.cli show
docker exec "$container" python -m app.cli show --tenant ten_…
```

- 不用 `docker compose exec`：它要展开 `docker-compose.yml` 里只由部署脚本设置的镜像变量，没有它直接报错。
- 不用 `docker compose run`：新容器的输出经容器的日志驱动落盘，之后凭据签发打印的明文令牌会进日志。
- 成功时标准输出一行 JSON（新对象的 ID 与字段；`show` 列出全部租户，或一个租户的地点、库存池与销售入口），退出码 0；
  参数或校验错误（名称为空、超过 200 个字符或不是单行，租户不存在或已停用，地点或库存池不属于该租户，地点已停用，库存池已有销售入口等）标准错误一行说明、退出码 2；
  数据库未配置、连不上等其他失败标准错误一行说明、退出码 1。一个命令一个事务，失败时什么也不写。
- 新库存池的状态是 `initializing`（期初只在这个状态可用）；V1 每租户建一个地点、一个池、一个入口，命令行不另加数量限制，由运维照做。
  命令行只建不改：停用、改名、删除与库存池切 `live` 不在这里。

### 服务凭据与令牌

```bash
docker exec "$container" python -m app.cli credential issue --tenant ten_… --name "Demo shop" \
    --channel chan_… --scope catalog.read --scope stock.read --scope orders.write \
    --actor ops-7 --reason "Demo shop launch"
docker exec "$container" python -m app.cli credential rotate --credential cred_… --overlap-hours 24 \
    --actor ops-7 --reason "Scheduled rotation"
docker exec "$container" python -m app.cli token revoke --token ctk_… --actor ops-7 --reason "Token leaked"
docker exec "$container" python -m app.cli credential revoke --credential cred_… --actor ops-7 --reason "Shop retired"
docker exec "$container" python -m app.cli credential list --tenant ten_…
```

- 会改变权限的四个子命令（`credential issue`、`credential rotate`、`token revoke`、`credential revoke`）都必须带 `--actor` 与 `--reason`：
  `--actor` 是运维人员标识（1 到 128 个字母、数字或 `.` `_` `:` `-`，不写姓名与邮箱）；`--reason` 去首尾空格后 1 到 500 个字符、单行。
- **明文令牌只显示一次**：`credential issue` 与 `credential rotate` 成功时，标准输出的那行 JSON 里的 `token` 是唯一一次出现，
  库里只存摘要，安全事件、报错与日志里都没有。把它直接交给调用方的配置，不要另存文件；输出丢了就用 `token revoke` 吊销那个令牌（`ctk_` 开头的令牌 ID），再轮换一次。
- `credential issue`：租户与销售入口（可选）须为 active 且入口属于该租户；`--scope` 可重复，须是已知值且不重复，`ops.read` 只能单独出现且不带入口。
  `CORE_ENV=production` 时拒绝带 `demo.reset` 的凭据（轮换也拒绝）。签发后 scope 与入口不能改：要改就签发新凭据、吊销旧的。
- `credential rotate`：签发新令牌；`--overlap-hours` 是 0 到 168 的整数，该凭据其余未吊销的令牌截止时间改为「轮换时刻 + 重叠小时数」
  （已更早到期的不动）。0 表示旧令牌即刻失效。
- `token revoke` 与 `credential revoke` 立即生效、不可撤销；凭据吊销后它的所有令牌都失效。`--token` 收令牌 ID，误传 `csk_` 开头的明文令牌会被拒绝。
- `credential list` 只读：列出该租户的凭据与各自令牌的 ID、创建、截止与吊销时间，不输出摘要。
- 每次成功的签发、轮换与吊销在提交后记一条安全事件（操作者、原因、目标凭据与令牌 ID）。事件写入失败时操作照样生效、退出码 0，
  标准错误多一行 `warning: security event not written (...)`，带操作类型与目标 ID，请据此手工补记审计。

## 网店单卖与加购 Inventory 的建议连接方式

| 购买组合 | 网店的商品身份读取 | 网店的库存操作 | 数量权威 |
| --- | --- | --- | --- |
| 只有网店 | 使用随附的 Core 商品目录 | 网店后端通过稳定的库存接口直连 Core，查可售量、预留、确认销售及释放 | Core 的一份基础库存账 |
| 后加 Inventory 并与其他入口共卖同一批货 | 仍使用原 Core 商品目录；图片、文案与渠道售价留在网店 | 网店后端的库存适配器改接 Inventory；Inventory 处理入口映射、份额、同步与异常，再向同一 Core 提交唯一的库存命令 | **同一个** Core 库存账，不在 Inventory 另建余额账 |

Inventory 只在**库存命令路径**上处于网店与 Core 之间，不代理网店所有页面、订单或付款。Core 在单一数量交易内校验已生效的入口额度与预留，Inventory 不可先在自己数据库扣数、再要求 Core 扣第二次。Shop 与 Inventory 都不得同时对 Core 提交同一笔销售事实。

为了日后加购，网店业务代码从第一版真实经营起只依赖稳定的 `StockGateway` 契约；单卖时由 Core 适配器实现，加购时由 Inventory 适配器实现。切换前须核对 SKU／库存池映射、未结预留和期初数量，停止旧路径写入，再启用新路径并对账。即使 Core 已独立运行，加购 Inventory 仍需要适配器切换与业务验收，不能承诺零改造。

这是后续加购 Inventory 时的**架构建议**，不是已实现的接口。首版需求已确定 Core 独立运行；现有 [Inventory 网店 API 契约](../acuven_inventory/docs/API_CONTRACT.md)仍是草案，正式接入前要确定两种适配器的共同语义与切换验收。
