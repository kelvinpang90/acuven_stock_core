# 从模板起步的方案（只写方案，未执行）

> 2026-11-10｜依据 `acuven-project-template/TEMPLATE-GUIDE.md`（以控制面 `docs/ONBOARD-PROJECT.md` 为准）。
> 本文**不执行**任何一步：没有建 GitHub 仓库、没有复制模板文件、没有写控制面登记表。标「停」的地方等 Kelvin 回复。
> 本目录已 `git init`（只含需求与本轮设计文档，未推送、无远端）——这是任务授权范围内的唯一 git 动作。

## 0. 身份（已定）

**Kelvin 2026-11-10 定（Q-22）：`project_id` = `stock_core`，`task_id_pattern` = `^CORE-TASK-[0-9]{3}$`，`display_name` = `Acuven Stock Core`。**下表保留当时的比较。

| 项 | 建议 | 备选 | 理由 |
| --- | --- | --- | --- |
| `project_id` | **`stock_core`** | `acuven_stock_core` | 规则 `^[a-z][a-z0-9_]{1,30}$`、全表唯一（现有：`ai_billing_hub`、`acuven_shop`、`crm_os`、`acuven_ai_api`、`acuven_aichat`…）。运营者要在 Telegram 手打，短的好打；但 `acuven_shop` 先例用了全名，若要与目录名一致就选备选。 |
| `task_id_pattern` | **`^CORE-TASK-[0-9]{3}$`** | `^STOCK-TASK-[0-9]{3}$` | 只描述本项目编号。避免用 `STK-`：需求文档里 `STK-01` 等已是**需求编号**，任务编号再用 STK 容易混。 |
| `display_name` | `Acuven Stock Core` | — | |

控制面登记表此时**不写**。

## 1. 需求分析与设计闸门

- 需求说明已有：根目录 `REQUIREMENTS.md`（2026-10-08）。模板要求放在 `docs/` 下——建议复制模板后**移动**到 `docs/REQUIREMENTS.md`，根目录 README 改链接（或保留原位、在 `CLAUDE.md` 写明位置；二选一，影响很小）。
- **几乎全部属于设计闸门范围**：Core 的核心就是账本、幂等、状态机和数据库结构，按 TEMPLATE-GUIDE 第 1 步“碰钱”同类处理（库存账虽不是钱，但同样是不可逆的账本与状态机）。本轮的 [CONTRACT_V1_DRAFT.md](CONTRACT_V1_DRAFT.md) 就是交给 Kelvin 的设计；**批准前不写这些部分的代码**，设计一变，批准作废。
- 碰个人数据：Core 设计上不收个人资料（契约第 10 节第 6 条）；这条本身要在设计批准时一并确认。
- **停**：设计交 Kelvin 批准（本轮交付）。

## 2. 技术栈（已定：与网店、计费一致，Q-23）

建议沿用 `acuven-shop` / `ai_billing_hub` 的组合，理由是同一人维护、Worker 主机已装好依赖、CI 写法可复用：

| 层 | 建议 | 说明 |
| --- | --- | --- |
| 语言/框架 | Python 3.12 ＋ FastAPI ＋ Pydantic | 与网店、计费一致 |
| 数据访问 | SQLAlchemy 2 ＋ Alembic | 迁移规则见契约 12.1 第 8 条 |
| 数据库 | **MySQL 8，共享 `infra_mysql` 里的独立库**（Q-17） | 独立账号、最小权限；binlog 异地传输与备份见契约 §12 |
| 缓存 | 不用 Redis 存任何权威数据；V1 可完全不用 Redis | 契约 12.1 第 2 条 |
| 后台任务 | 进程内定时任务（预留到期扫描、outbox），状态全在数据库 | 无需 Celery |
| 前端 | **无**（Core 没有客户界面；运营界面在网店后台） | CI 不需要 node |
| 部署 | Docker 镜像 ＋ 按提交 SHA 部署的 `deploy.yml`（D1–D5） | 数据库拓扑另行实施，不在 deploy.yml 里自动做 |

沙箱可跑的检查（Worker MXC：无网络、无数据库、Win32k 默认关、回环禁用）：

| 检查 | 能否进 Worker 沙箱 | 说明 |
| --- | --- | --- |
| 纯领域逻辑单元测试（数量不变量、状态机、幂等摘要计算，不碰数据库、不起 asyncio） | ✅ `python -I -m unittest …` 或 pytest（不引 TestClient） | **建议有意把领域层写成纯函数**，让大部分规则能在沙箱里测 |
| `ruff check` / `ruff format --check` | ⚠️ 需 Worker 主机登记 `check_win32k` | args 逐字照模板 |
| API 测试（TestClient） | ❌ 只放 CI | asyncio 回环被禁 |
| MySQL 并发/锁/事务测试 | ❌ 只放 CI（CI 起真实 MySQL 服务容器） | Inventory STOCK_RULES 也要求“不以 SQLite 锁行为证明生产并发安全” |
| OpenAPI 与响应一致性检查 | 视实现：纯 schema 校验可进沙箱 | |

- 技术栈已由 Kelvin 确认（Q-23）。

## 3. 生成骨架（批准后才做）

建议的目录（最小可运行，不写投机性代码）：

```
app/
  main.py            # FastAPI 入口、/healthz（带 commit SHA）、/readyz
  config.py
  domain/            # 纯领域：数量不变量、状态机、幂等摘要——无 I/O，沙箱可测
  db/                # SQLAlchemy 模型、会话
  api/v1/            # 路由（按任务逐个加）
alembic/
tests/
  unit/              # 沙箱可跑
  integration/       # 需要 MySQL / TestClient，只在 CI
docs/
  REQUIREMENTS.md  CONTRACT_V1_DRAFT.md（批准后改名 CONTRACT_V1.md）  TODO.md …
.platform/ project.yaml commands.yaml tasks.yaml
.github/workflows/ ci.yml deploy.yml
CLAUDE.md  AGENTS.md（两份一致）  .gitignore  .env.example
```

骨架阶段只放：入口＋健康检查＋一个真实的单元测试。数据模型、路由留给各自任务。

## 4. CI 与部署工作流

- `ci.yml`：建议两个 job——`unit`（ruff ＋ 纯单元测试）与 `integration`（起 MySQL 8 服务容器，跑迁移、API 测试与并发测试）。
  `required_status_checks` 写这两个 job 名。
- `deploy.yml`：按 D1–D5 清单逐项落实；删掉模板里 `is_template` 那一行；保留 `paths-ignore` 与形状约束注释。
- **与恢复目标的关系**：`deploy.yml` 只部署**应用**；binlog 异地传输、全量备份、恢复演练是独立的运维工作（ESTIMATE_BREAKDOWN 的 W7a，涉及 `vps_infra`），
  其配置脚本放 `ops/`，由人执行，不进 Worker allowlist（模板 `never_low_risk` 已排除 `database_migration`、`deployment`）。
- **生产迁移的特殊点**：网店、计费都是“合并即部署”。Core 也可以这样，但数据库迁移须满足契约 12.1 第 8 条（只做向前兼容的扩展、
  迁移前自动备份）。建议在 `deploy.yml` 里迁移前做一次备份并在失败时以失败结束（D3）。
- 需要 Kelvin 在 GitHub 上配的 secrets / variables（会话不接触凭据）：部署主机连接信息（名称照网店 `deploy.yml` 现有命名）、
  `HEALTHCHECK_URL`（**注意：Core 没有公网入口**，健康检查在部署主机本机经内部容器网络执行（Q-12），D3 的实现方式和网店不同）。
- 目前要部署的只有 **Demo Core 实例**（`CORE_ENV=demo`，V1 首个接入方是公开 Demo）；生产实例等真实网店定了再加（Q-24）。

## 5. `.platform/` 与 planning-v1（批准后才写）

- `project.yaml`：`project_id` 填第 0 步结果；`execution` 两行保持模板值；`deploy_workflow: deploy.yml`；
  `repository.slug` 待第 6 步；`required_status_checks: [unit, integration]`。
- `commands.yaml`（只登记 CI 也在跑的）：
  - `tests.unit`：`python ["-I", "-m", "unittest", "discover", "-s", "tests/unit"]`——沙箱可跑，任务可引用；
  - `lint.check`、`format.check`：照模板逐字写，需 Worker 主机 `check_win32k`；
  - `tests.integration`：登记给 CI 与人用，**不进任何任务的 `allowed_commands`**。
- `tasks.yaml` 第一批建议（**全部 `ready` 前都要跑 `design_precheck`**，因为都碰状态机/数据库结构）：

| 任务 | 一句话标题（草拟） | 依赖 | 说明 |
| --- | --- | --- | --- |
| CORE-TASK-001 | 生成服务骨架与健康检查并接通 CI | — | 骨架＋/healthz（含 SHA）＋一个单元测试；若骨架在第 3 步已人工做完，此任务改为下一项 |
| CORE-TASK-002 | 实现数量不变量与流水类型的纯领域层 | 001 | 契约 4.1、4.3；纯函数＋沙箱单测；不碰数据库 |
| CORE-TASK-003 | 建立租户池余额流水幂等的数据库结构与迁移 | 002 | 契约 4、7；首个 Alembic 迁移；MySQL 集成测试在 CI |

  之后按 [ESTIMATE_BREAKDOWN.md](ESTIMATE_BREAKDOWN.md) 的工作包逐个登记，每个任务一层、≤12 个路径、≤8 条验收、≤约 1500 行。
- `docs/TODO.md` planning-v1：当前计划放 001；其余写成 `- 待登记：…`。
- 搜一遍 `<`，确认无占位符。

## 6. 仓库与远端（可见性与起步方式已定；建远端仍等 Kelvin）

需要 Kelvin 回答：

1. **公开仓库**（Q-21a）。仓库名建议与目录同名 `acuven_stock_core`；建在哪个账号、什么时候建，仍等 Kelvin 指示，会话不自行建远端。
   公开意味着运行手册和工作流里不能写主机名、IP、存储桶名，这些一律放 secrets 或本机配置。
2. 起步方式（Q-21b 已选方式 1）：
   - **方式 1（已选）**：把模板文件复制进本目录（不带模板的 `.git`），在现有本地仓库上提交，然后推到新建的**空**远端。
     好处：本轮文档的提交历史保留，不需要合并两段历史。
   - 方式 2：GitHub 上用模板「Use this template」建仓库再克隆，然后把本目录的文档复制进去提交；本地这个 `git init` 的历史丢弃。
3. 推送后由 Kelvin 配分支保护（禁直推、禁 force push、必需检查 = `unit`、`integration`）和 secrets / variables。

## 7. 交接前自检

在控制面检出里对本项目**干净的默认分支检出**跑 `worker.contract_check`（命令见 TEMPLATE-GUIDE 第 7 步），结论须为
`result: ready to register`；需要预审的任务逐个跑 `worker.design_precheck`，设计文件指向批准后的契约文档。

## 8. 回控制面会话登记（不在本项目会话做）

交给控制面会话（在控制面目录另开会话）的清单：`project_id`、`display_name`、`task_id_pattern`、
`planning_source: {"path": "docs/TODO.md", "format": "planning-v1"}`、`proposals: false`、**`worker: none`**、`status: pilot`、
是否 `auto_closeout`、各任务的 `design_precheck` 输出、`contract_check` 输出、需要 `check_win32k` 的 ruff 命令
（`lint.check`、`format.check` 的逐字 args）。

最后在 `E:\projects\CLAUDE.md` / `AGENTS.md` 的项目一览里更新本项目一行（两份同步）。按工作区规则，从别的项目会话给本目录开会话后要
`detach_session`——那是发起方会话的事，本目录内的会话不涉及。

## 风险与提醒

- Worker 能跑的检查很少（无数据库、无 TestClient），而 Core 的正确性大部分要靠 MySQL 并发测试——**这些只能在 CI 里证明**。
  因此 Core 的 Worker 任务会比网店更依赖 CI 结果和人工评审；领域层写成纯函数可以把一部分验证拉回沙箱。
- 第一批任务全部要过设计预审，起步比网店慢，这是预期内的。
