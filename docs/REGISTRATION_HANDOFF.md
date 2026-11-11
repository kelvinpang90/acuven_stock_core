# OpenClaw 登记交接材料（TEMPLATE-GUIDE 第 8 步）

> 2026-11-10。登记**不在本项目会话里做**：把本文交给控制面会话（在控制面目录另开会话），由它按 `docs/ONBOARD-PROJECT.md`
> 改登记表、渲染、自查、提 PR，经独立受限评审、Kelvin 合并。
> **现状（2026-11-11）**：已推到公开仓库 `kelvinpang90/acuven_stock_core`，main 受保护（必需检查 `unit`、`integration`）；
> Demo Core 实例已按提交 SHA 部署上线，健康检查报出所部署的提交。下面的 `contract_check` 是对 GitHub 上 main（`1e15a57`）的
> 干净克隆重跑的；设计预审是对 `01c3908` 跑的，此后契约、`tasks.yaml`、`docs/TODO.md` 与应用代码（`app/`、`alembic/`、`tests/`）
> 都没有改动；其间只改了 `deploy/healthcheck.sh`（部署健康检查）与 `.platform/project.yaml` 的 `roles`（审查方改为 `claude_code`），
> 都不涉及任务范围，预审结论仍有效。

## 登记表字段

| 字段 | 值 |
| --- | --- |
| `project_id` | `stock_core` |
| `display_name` | `Acuven Stock Core` |
| `task_id_pattern` | `^CORE-TASK-[0-9]{3}$` |
| `planning_source` | `{"path": "docs/TODO.md", "format": "planning-v1"}`，`proposals: false` |
| `worker` | `none`（新项目一律如此起步；改成 `windows-native` 是之后单独复审的任务） |
| `status` | `pilot` |
| `auto_closeout` | 可以随首次登记启用：`deploy.yml` 保留了骨架的 `paths-ignore`，`CLAUDE.md` / `AGENTS.md` 保留了自动收尾规则文本；另需 Worker 本机 `merge_enabled` 为真（Kelvin 在 Worker 主机确认） |

## 需要 Worker 主机登记 `check_win32k` 的命令

`check_win32k` 写在 Git 忽略的 Worker 本机配置里，项目分配为 `windows-native`（单独复审的任务）时随本机配置一起登记；
`worker: none` 阶段用不到。逐字与 `.platform/commands.yaml` 一致：

- `lint.check`：`python ["-I", "-m", "ruff", "check", "."]`
- `format.check`：`python ["-I", "-m", "ruff", "format", "--check", "."]`

`tests.unit`（`python ["-I", "-m", "unittest", "discover", "-s", "tests/unit", "-t", "."]`）只跑纯领域代码，不需要 Win32k；
`tests.integration` 只在 CI 跑，没有写进任何任务的 `allowed_commands`。

## `contract_check` 输出（对 GitHub 上 main `1e15a57` 的干净克隆）

```text
ok    project.yaml: identity and worker consent
ok    commands.yaml: 4 commands
WARN  executables: the Worker host must pin: python
WARN  python lint.check: ruff needs Win32k in the sandbox: the Worker host must register this exact command in check_win32k (ACVDEV-TASK-017)
WARN  python format.check: ruff needs Win32k in the sandbox: the Worker host must register this exact command in check_win32k (ACVDEV-TASK-017)
ok    task CORE-TASK-001: loads (ready)
ok    task CORE-TASK-002: loads (ready)
ok    planning: planning-v1 ok, next: CORE-TASK-001
ok    deploy: deploy.yml present; D1-D5 are reviewed by a person, not checked here
result: ready to register (host readiness is checked after registration)
```

## 设计预审（`worker.design_precheck`，设计文件 `docs/CONTRACT_V1.md`）

两个任务都碰状态机或数据库结构，登记为 `ready` 前都跑了预审。共七轮：前六轮的发现都已处理——会改业务语义的写进契约 0.3、0.4（第 14 节），
Kelvin 2026-11-10 批准；其余是列级细节、校验写严与一致性更正，写进任务说明与契约的编辑性澄清。最后一轮（对 `01c3908`）：

```text
===== CORE-TASK-001
(no findings)
session verdict: READY
PRECHECK_RESULT: READY

===== CORE-TASK-002
(no findings)
session verdict: READY
PRECHECK_RESULT: READY
```

## 部署观察契约 D1–D5（人工审阅要点）

- D1：`deploy.yml` 由 push 到 `main` 触发；`paths-ignore` 只含 `.platform/tasks.yaml` 与 `docs/TODO.md`。
- D2：镜像以 DEPLOY_SHA 为标签在 GitHub 构建；服务器 `git checkout --detach` 到它再跑它自己的 `deploy/deploy.sh`。
- D3：迁移失败、不健康、回滚都以非零退出；**Core 没有公网入口**，所以上线后健康检查不是从 GitHub 访问地址，而是第二个 SSH 步骤在服务器上进容器取 `/healthz`（`deploy/healthcheck.sh`），要求返回体带 DEPLOY_SHA。不用 `HEALTHCHECK_URL`。
- D4：`concurrency.cancel-in-progress: false`。
- D5：两个 job 的超时合计 22 分钟；没有需要人工批准的 environment。
