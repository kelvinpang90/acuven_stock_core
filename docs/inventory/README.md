# Inventory 模块的设计文档

> 2026-11-23 起草。Kelvin 2026-11-23 决定把 `acuven_inventory` 并入本仓库（`acuven_hub/DECISIONS.md` C-21）：同一仓库、同一服务、同一个 OpenClaw 项目 `stock_core`，Inventory 只在生产实例按开关打开，在进程内调用 Core。
> Core 一侧的条文在 [CONTRACT_V1.md](../CONTRACT_V1.md) 0.22（草案，待批准；§8.6 进程内入口），取舍见 [OPEN_QUESTIONS.md](../OPEN_QUESTIONS.md) 第 2.1 节的 Q-81～Q-91。

## 现行设计（在本目录）

| 文件 | 内容 | 状态 |
| --- | --- | --- |
| [V1_MINIMAL_DESIGN.md](V1_MINIMAL_DESIGN.md) | Inventory 最小 V1：只接自营 Shopee 与 TikTok Shop；配额、映射、事件映射、上线切换、人工模式、发布与读回、对账、告警、工作包 | 0.3 已批准（`acuven_inventory` 的 `d421aba`）；0.4 按合并改写，**草案，待 Kelvin 批准** |
| [ADMIN_AUTH_DESIGN.md](ADMIN_AUTH_DESIGN.md) | 后台管理员账号、密码登录与会话 | 0.1 已批准（同一提交）；0.2 改到本仓库的表、迁移、命令行与接线，**草案，待 Kelvin 批准** |

两份都是先原样搬入（一个提交），再在下一个提交里改写，便于只看改动。V1 范围内，它们与下面那些旧文档冲突的地方以它们为准。

## 留在 `acuven_inventory` 仓库的旧文档（不搬，供查阅）

`acuven_inventory` 仓库即将归档。下面这些是 2026-10-07 前后的草案，V1 设计引用了它们（例如“REQUIREMENTS 4.3”“STOCK_RULES §3”），但它们**不是现行设计**；V1 设计里引用它们的地方照旧指向那个仓库的 `docs/` 目录。
它们与现行设计的出入列在 V1 设计第 14 节。

| 文件 | 内容 |
| --- | --- |
| `REQUIREMENTS.md` | Inventory 需求文档 1.0 草案（多入口、平台、网店、权限、告警、Demo） |
| `STOCK_RULES.md` | 库存数量与状态规则草案（O/H/C/U、超卖缺口、份额与恢复条件）；Core 契约 §4.2 有与它的换算 |
| `DEVELOPMENT.md` | 开发与集成设计草案（技术栈、库存模型、平台适配器的七个动作、OAuth） |
| `API_CONTRACT.md` | 网店库存 API v1 草案（给将来网店经 Inventory 接入用；V1 不做，V-01） |
| `INTEGRATION_STANDARD.md` | 网店与平台接入规范草案（含 `manual_quota` 人工模式） |
| `INTEGRATION_GUIDE.md` | 自建网店接入指南草案 |
| `OPERATIONS.md` | 界面、Demo 重置与恢复操作草案 |
| `HUB_ARCHITECTURE_ALIGNMENT.md` | 与 Acuven Hub 系统群的架构对齐草案 |
| `SYSTEM_DECOUPLING.md` | 库存、AI 客服、CRM 与 ERP 的独立运行与集成边界草案 |
| `DELIVERY_PLAN.md` | 跨项目交付与 OpenClaw 任务计划草案 |
| `REVIEW_2026-10-07.md` | 2026-10-07 的需求与交付流程复核 |
| `TODO.md`、`.platform/tasks.yaml` | 原 INVENTORY-TASK-001～004（后台登录四层）；合并后不在那边执行，按 [ADMIN_AUTH_DESIGN.md](ADMIN_AUTH_DESIGN.md) 0.2 在本仓库改登记 |

另有一份没有合并的草案：`acuven_inventory` 已关闭的分支 `docs/w1-core-client-design` 里的 `docs/CORE_CLIENT_DESIGN.md`（Core HTTP 客户端、命令发件箱与作业表，0.1 草案，从未批准）。合并后它的大部分作废（没有 HTTP 客户端与发件箱）；关于幂等键、同一订单的先后、可能已发出的命令不能取消、时间取整秒的考虑，已体现在 V1 设计 0.4 第 3.5 节与 Core 契约 §8.6。

## 不在这里的

- Inventory 的私有台账（平台接入、支付商户、TikTok 拒审核对）含经营信息，在 `acuven_hub` 私有仓库的 `inventory/` 下，不进本仓库（本仓库公开）。
- 真实子域名、主机名与服务器路径只配在服务器与 GitHub 变量里，不写进本目录。
