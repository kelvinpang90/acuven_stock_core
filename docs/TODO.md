# TODO — 开发任务清单

> 最后更新：2026-11-10

---

## 本文件的约定

1. 一个 session 做一个编号任务：读本文件 → 做完 → 验收 → commit，中途不需要逐步审批。
2. **对计划本身有异议时才停下来讨论**，不要在每个任务上重新走一遍「先计划、等确认」。
3. 做完把 `[ ]` 改成 `[x]`，并在该任务下补一行记录：做了什么、偏离了什么、验证到什么程度。复选框只允许 `[ ]` / `[x]`。
4. 本文件里**没有**的工作（临时需求、探索型任务）才需要先写计划、等确认。

---

## 规划（OpenClaw planning-v1）

> 下面两个标记之间的块由 OpenClaw 控制面按固定格式解析，决定下一项任务；只管「做什么、先做什么」，执行权限仍以 `.platform/tasks.yaml` 为准。格式不对整块作废，项目 fail closed。

<!-- 块内每一行只能是下面三种格式之一（空行可以有，别的内容一律不行，包括注释）：
     当前计划：`1. `<任务 id>` <标题>`，从 1 连续编号，最多 20 项；每一项都必须在 tasks.yaml 登记为 ready，第一项就是下一次「开启」的任务。
     已阻塞：  `- `<任务 id>` <标题>｜阻塞：<原因>` 或 `- 待登记：<标题>｜阻塞：<原因>`（分隔符是全角竖线）。
     后续计划：`- `<任务 id>` <标题>` 或 `- 待登记：<标题>`。
     任务 id 必须符合控制面登记的 task_id_pattern，全块不重复；tasks.yaml 里每个 ready 任务都必须出现在块里。
     「待登记」行不带 id，只给人看，不会变成可执行的东西。三个标题与两个标记逐字不改。 -->

<!-- openclaw:planning-v1:begin -->
### 当前计划

### 已阻塞

### 后续计划
<!-- openclaw:planning-v1:end -->

---

## Inventory 深度融合进 Core（2026-11-23，Kelvin 同日批准）

Kelvin 2026-11-23 决定把 `acuven_inventory` 并入本仓库（`acuven_hub/DECISIONS.md` C-21），并否决了“Inventory 作为进程内模块”的第一稿，要求深度融合。改写后的设计 **Kelvin 2026-11-23 批准**：

- 契约 [CONTRACT_V1.md](CONTRACT_V1.md) 0.22：平台入口、配额进 Core（§3、§3.1）、平台商品（§2.9）、平台订单事件（§5.10）、操作主体（§10 第 4 条）、后台与生产实例的接口（§8.6）；取舍 Q-81～Q-102 见 [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) 第 2.1 节；
- [PLATFORM_CHANNELS.md](PLATFORM_CHANNELS.md) 0.1（授权、收事件、发布与读回、上线切换与人工模式、对账、告警；第 14 节是工作包与改造规模）；
- [ADMIN_AUTH.md](ADMIN_AUTH.md) 0.3（后台登录四层）；
- [PRODUCTION_AND_RECOVERY.md](PRODUCTION_AND_RECOVERY.md) 0.7与 [PRODUCTION_RUNBOOK.md](PRODUCTION_RUNBOOK.md) 的相应说明。

实现任务还没有登记（上面的 planning-v1 块与 `.platform/` 由登记 PR 改）：按 PLATFORM_CHANNELS 第 14.2 节的工作包登记 `CORE-TASK-NNN`（先第 0、1 组：运维前置、执行器与操作主体；再第 2 组：配额与平台事件命令；再第 3、4 组：后台与平台对接），照 CLAUDE.md 过设计预审。
