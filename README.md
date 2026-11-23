# Acuven Stock Core

`acuven_stock_core` 是商品身份目录与基础库存账的**独立服务项目**。需求见 [REQUIREMENTS.md](REQUIREMENTS.md)，契约 [docs/CONTRACT_V1.md](docs/CONTRACT_V1.md) 已于 2026-11-10 批准。V1 契约的业务功能已按 `.platform/tasks.yaml` 的 CORE-TASK-001 到 068 实现：商品目录与批量导入、库存池与期初、收货／调整／更正／盘点、预留→确认销售→发货与退货、订单对账与不变量自查、SKU 合并、运维命令行与巡检告警。仓库已推到 GitHub（公开）并登记 OpenClaw 控制面，Demo Core 实例已部署（无公网入口）；生产实例的部署脚本与工作流已就绪，首次上线的运行手册在做（CORE-TASK-069）。

服务范围：统一 SKU、条码、基本商品名称与参考价；记录库存地点、预留、出入库与可追溯流水。`acuven_inventory` 另负责跨销售入口的库存分配、外部平台同步与对账。

**2026-11-23：Inventory 深度融合进 Core**（`acuven_hub/DECISIONS.md` C-21；条文是契约 0.22与 [docs/PLATFORM_CHANNELS.md](docs/PLATFORM_CHANNELS.md)、[docs/ADMIN_AUTH.md](docs/ADMIN_AUTH.md)，**Kelvin 2026-11-23 批准**）。没有独立的 Inventory，也没有“Inventory 模块”：上一段说的库存分配、平台同步与对账成了 Core 自己的能力。生产实例上，自营 Shopee 店与 TikTok Shop 店各是 Core 的一个平台入口，配额、安全库存与平台已售未扣是 Core 的状态，平台订单事件在 Core 的一笔事务里处理；Core 有一个后台（目录、库存、平台渠道）与一套管理员登录；生产实例对公网只开放后台与平台回调（契约 §3、§3.1、§5.10、§8.6）。**Demo 实例不变**：只有 `/v1`、不挂公网、后台开关为假。同一个 OpenClaw 项目 `stock_core`，任务编号仍是 `CORE-TASK-NNN`。`acuven_inventory` 仓库即将归档，那边的文档只作历史。

项目名确定为 **Acuven Stock Core**，部署边界确定为**独立常驻服务及独立持久化库**。V1 先接公开网店 Demo（独立的 Demo Core 实例），单一库存池；生产实例先给 Inventory 用，由它代表 Acuven 自营的 Shopee 与 TikTok Shop 两个店（契约 0.19，Kelvin 2026-11-21 批准）；真实网店何时接入待定，ERP、POS 以及网店与 Inventory 共卖同一池后续接入。恢复目标：RPO≤5 分钟、RTO≤4 小时（2026-11-10 由 RPO=0 放宽）。不同套餐的装配方式见 [部署包装讨论](../acuven_hub/STOCK_CORE_DEPLOYMENT_PACKAGING_2026-10-08.md)；原方案比较见 [商品与库存关系](../acuven_hub/REVISED_PRODUCT_STOCK_ARCHITECTURE_2026-10-07.md)及[开发优先顺序](../acuven_hub/DEVELOPMENT_PRIORITY_2026-10-08.md)。

## 阶段 1 设计文档（2026-11-10，契约已由 Kelvin 批准）

| 文档 | 内容 |
| --- | --- |
| [docs/SHOP_STOCK_INVENTORY.md](docs/SHOP_STOCK_INVENTORY.md) | 网店现有商品与库存代码盘点，及将来换成调用 Core 的每个调用点 |
| [docs/CONTRACT_V1.md](docs/CONTRACT_V1.md) | Core V1 契约（0.5，2026-11-12 批准）：商品身份、库存池、数量模型与流水、预留→确认→释放、幂等与审计、`/v1` API、`StockGateway`、迁移与回退样例、租户与鉴权、恢复目标（RPO≤5 分钟、RTO≤4h）的设计约束 |
| [docs/TEMPLATE_BOOTSTRAP_PLAN.md](docs/TEMPLATE_BOOTSTRAP_PLAN.md) | 从项目模板起步的方案（2026-11-10 已按它起步，2026-11-11 已推送并部署，之后已登记控制面） |
| [docs/ESTIMATE_BREAKDOWN.md](docs/ESTIMATE_BREAKDOWN.md) | 工期重估（I-04）用的工作拆分，不是排期 |
| [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md) | 设计问题与 Kelvin 的答复（Q-24 暂缓；深度融合的取舍 Q-81～Q-102 待确认） |
| [docs/PLATFORM_CHANNELS.md](docs/PLATFORM_CHANNELS.md) | 平台渠道（0.1，Kelvin 2026-11-23 批准）：自营 Shopee 与 TikTok Shop 的授权与令牌、收事件、库存发布与读回、两种暂停、上线切换与人工模式、对账、告警、适配器，以及深度融合的工作包与改造规模 |
| [docs/ADMIN_AUTH.md](docs/ADMIN_AUTH.md) | Core 后台的管理员账号、密码登录与会话（0.3，Kelvin 2026-11-23 批准；规则沿用原 Inventory 后台登录 0.1） |
| [docs/PRODUCTION_AND_RECOVERY.md](docs/PRODUCTION_AND_RECOVERY.md) | 生产实例部署与 W7a 备份恢复的设计（0.5，Kelvin 2026-11-21 批准）：同机第二实例、网络隔离、手工升级、binlog 每 4 分钟传 R2、时间点恢复、整机恢复与演练 |

开发任务清单与 OpenClaw 计划见 [docs/TODO.md](docs/TODO.md)；项目规则见 [CLAUDE.md](CLAUDE.md)（与 `AGENTS.md` 同一内容）。

## 运维命令行（开通租户、地点、库存池与销售入口；服务凭据与令牌）

Core 没有 HTTP 管理接口（契约第 10 节第 4 条），租户、地点、库存池与销售入口只能由运维在服务器上、在运行中的 Core 容器里用 `python -m app.cli` 建；服务凭据的签发、轮换与吊销也只走这里。
命令用容器里已有的 `CORE_ENV` 与 `CORE_DATABASE_URL`（与服务同一个数据库账号）。写法同 [deploy/healthcheck.sh](deploy/healthcheck.sh)：在要操作的那个实例的检出根目录里 `source` [deploy/instance.sh](deploy/instance.sh)，
它按本检出 `.env` 的 `CORE_ENV` 认出实例（Demo 或生产），`core_find_api_containers` 只找本实例、本检出的 `core_api` 容器（排除一次性容器），再 `docker exec`。
第一行打出的不是预期的实例、或第二行打出的不是恰好一个容器 ID 时停下，不要往下跑（`CORE_ENV` 不对、栈没起来，或有不止一个容器）：

```bash
. deploy/instance.sh; echo "${CORE_INSTANCE:-$CORE_INSTANCE_ERROR}"
container="$(core_find_api_containers)"; echo "$container"
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

（2026-11-23，契约 0.22）本节说的是网店将来与别的入口共卖同一批货时的连接方式。深度融合之后没有单独的 Inventory 系统：平台入口的配额已经在 Core 的同一事务里改（契约 §3.1），网店若要与两个平台共卖同一个池，是在 Core 里给网店的 `api` 入口也加配额，另行设计（契约 §3 的组合规则）。下面两段是 2026-11 之前的建议，留作来历。

Inventory 只在**库存命令路径**上处于网店与 Core 之间，不代理网店所有页面、订单或付款。Core 在单一数量交易内校验已生效的入口额度与预留（这是网店与 Inventory 共卖同一池时的做法；生产实例上 Inventory 是唯一入口、配额不在 Core，见契约 §3 的 0.19 条），Inventory 不可先在自己数据库扣数、再要求 Core 扣第二次。Shop 与 Inventory 都不得同时对 Core 提交同一笔销售事实。

为了日后加购，网店业务代码从第一版真实经营起只依赖稳定的 `StockGateway` 契约；单卖时由 Core 适配器实现，加购时由 Inventory 适配器实现。切换前须核对 SKU／库存池映射、未结预留和期初数量，停止旧路径写入，再启用新路径并对账。即使 Core 已独立运行，加购 Inventory 仍需要适配器切换与业务验收，不能承诺零改造。

这是后续加购 Inventory 时的**架构建议**，不是已实现的接口。首版需求已确定 Core 独立运行；现有 [Inventory 网店 API 契约](../acuven_inventory/docs/API_CONTRACT.md)仍是草案，正式接入前要确定两种适配器的共同语义与切换验收。
