# 后台前端设计

> 版本：**0.1 · 日期：2026-11-24 · 状态：草案，待 Kelvin 批准**。批准之后才登记前端任务（工作包 P3i 起，[PLATFORM_CHANNELS.md](PLATFORM_CHANNELS.md) 第 14.2 节）；设计有改动时，改动部分的批准作废、须重新批准。
> 依据：[CONTRACT_V1.md](CONTRACT_V1.md) 0.23 的 §3.1、§3.2、§6.2、§6.8、§7.1（后台表单防重复）、§8.1、§8.3、§8.6 第 2 条（后台接口、检查顺序、两种响应形状、页面表）、§10 第 3 条（角色表）；[ADMIN_AUTH.md](ADMIN_AUTH.md) 0.3 第 5 节；[PLATFORM_CHANNELS.md](PLATFORM_CHANNELS.md) 0.2 第 4～8 节；[PRODUCTION_RUNBOOK.md](PRODUCTION_RUNBOOK.md) 第 1.5b、1.7 节；`.platform/tasks.yaml` 里 CORE-TASK-095～127 写定的后台接口。
> 本文只定前端怎么做、调哪些接口、后端还缺什么；**不改契约、不改已登记任务**。第 9 节列出的后端缺口要另行登记（要改契约的先改契约）。
> 2026-11-24 Kelvin 已定的四项取舍：不用 UI 组件库；前端任务一次全部登记；CI 新开 `frontend` job；V1 先不启用浏览器验收（第 10 节）。

## 0. 一页看懂

- 前端放在本仓库 `frontend/`，Vite ＋ React ＋ TypeScript，只依赖 `react`、`react-dom`（同 acuven-shop 的前端），不用组件库、不用路由库、不引外部 CDN。
- 构建产物在镜像构建时打进 Core 镜像，由 **Core 进程自己在 `/admin/` 下提供**（后台开关为真时才挂上），不另起前端容器：边缘反向代理只把 `/admin/` 与 `/api/` 转给生产 Core（契约 §10 第 5 条）。
- 会话 cookie 是 HttpOnly 的，页面拿不到；CSRF 令牌、用户名、表单内容只在页面内存里，**不写 localStorage、sessionStorage、IndexedDB 或 cookie**。
- 每个写表单打开时生成一个提交键（`adm:` 加 ULID）放进 `Idempotency-Key`；结果未知时用同一个键重试，成功或得到确定的拒绝后换新键（契约 §7.1）。
- 前端检查（lint、类型检查、vitest、构建）只在 CI 的 `frontend` job 跑，不写进任何任务的 `allowed_commands`。
- 页面按契约 §8.6 第 2 条的页面表做，共 11 组；拆成 21 个任务（1 个后端、20 个前端，第 11 节），另有 11 处后端缺口（第 9 节）。

## 1. 技术栈与构建

### 1.1 依赖

| 项 | 取法 | 理由 |
| --- | --- | --- |
| 框架 | React 19、TypeScript、Vite；版本与 acuven-shop 前端当时锁定的大版本一致 | 原 Inventory 设计与网店都是这套；同一个人维护两套前端 |
| 运行依赖 | 只有 `react`、`react-dom` | Kelvin 2026-11-24 定不用组件库（见下） |
| 开发依赖 | `typescript`、`vite`、`@vitejs/plugin-react`、`vitest`、`eslint`、`@eslint/js`、`typescript-eslint`、`eslint-plugin-react-hooks`、`globals`、`@types/react`、`@types/react-dom`、`@types/node`；与 acuven-shop 相同的一组 | 不多加 |
| 路由 | 自写：`history.pushState` 加一张路由表（路径模式 → 页面组件），约 60 行 | 页面十来个、都是平的两层路径，路由库省不了多少 |
| 表格、表单、对话框、分页、提示条 | 自写十来个小组件（第 1.3 节） | 同上 |
| 样式 | 一个全局 CSS 文件（`frontend/src/styles.css`），系统字体，不用 CSS-in-JS | 严格 CSP 下不能有运行时注入的 `<style>`（第 6 节） |
| Node | 24（与 acuven-shop 的 CI 与 Dockerfile 一致） | 两边漂移会出现“CI 绿而镜像构建失败” |

**不用组件库的取舍**（Kelvin 2026-11-24 定）：完整组件库（Ant Design、Mantine 等）多数在运行时注入 `<style>`，CSP 要放开 `style-src 'unsafe-inline'` 或另配 nonce，包也大、升级负担重；无样式组件库（Radix 一类）对话框、下拉的可访问性更好，但多一批要跟着升级的依赖。代价：表格、表单、对话框要自己写，页面朴素。
另一个好处：页面任务不改 `package.json` 与锁文件，Worker 不需要装依赖（第 10.3 节）。

### 1.2 目录

```text
frontend/
  package.json  package-lock.json  index.html  vite.config.ts  eslint.config.js
  tsconfig.json  tsconfig.app.json  tsconfig.node.json
  scripts/check-dist.mjs      构建产物检查（第 6.3 节）
  src/
    main.tsx  App.tsx  styles.css
    lib/       纯逻辑：请求、错误分类、提交键、ULID、分页、时间、CSV、引用生成（都有 vitest 测试）
    api/       每组接口一个模块：请求与响应的类型、调用函数（纯函数部分有测试）
    components/  通用组件
    pages/     每页一个目录
```

- **逻辑放纯函数**：凡是能不依赖 DOM 的（错误分类、提交键的换与不换、分页何时停止、时间换算、CSV 解析、配额页的 `F'` 预览、表单正文的拼法），都写成 `lib/` 或 `api/` 里的纯函数，用 vitest 测；组件只接线。同 Core 的“领域规则写成纯函数”，也因为 acuven-shop 的前端同样没有引入 jsdom 与测试库。
- Vite 的 `base` 为 `/admin/`，产物里资源的地址都是 `/admin/assets/<名字>.<hash>.<扩展名>`。

### 1.3 通用组件（骨架任务写好，页面任务只用）

按文件归并，免得一个任务的允许路径超过 12 个：
`components/Layout.tsx`（左侧导航、顶栏、横幅区）；`components/table.tsx`（`DataTable`：列定义、空状态、加载与错误三态；`Pager`：上一页、下一页）；`components/form.tsx`（`FormField`：标签、提示、字段错误；`SubmitButton`：提交中禁用、结果未知时变成“用同一提交重试”；`ConfirmDialog`：原生 `<dialog>`；`SkuPicker`：输入 SKU 编码，经 `GET /api/v1/admin/skus?sku_code=` 换成 `sku_id`，库存命令只收 `sku_id`，契约 §6.2 第 3 条）；`components/display.tsx`（`Notice`：成功、警告、错误三种提示条，错误带 `request_id`；`TimeText`：第 4.4 节；`Qty`：整数，负数红色）；`components/ReloginDialog.tsx`（第 3.3 节）。

### 1.4 怎么打包进镜像、由谁提供

- **Dockerfile 加一个构建阶段**（照 acuven-shop 前端 Dockerfile 的写法）：`FROM node:24-slim AS admin-frontend`，先只拷 `package.json` 与 `package-lock.json` 跑 `npm ci --no-audit --no-fund`，再拷源码跑 `npm run build`。
  运行阶段 `COPY --from=admin-frontend /src/dist ./admin_static`（落在现有 `WORKDIR` 下）。运行镜像里没有 node。两个实例用同一个镜像，Demo 上后台开关为假，不挂这些文件。
- **`.dockerignore`** 加 `frontend/node_modules` 与 `frontend/dist`（现有的 `*.md`、`docs`、`tests` 排除照旧）。
- **由 Core 提供**：新模块 `app/api/admin_static.py`，`create_app` 只在后台开关为真时挂上（同登录接口的挂法）：

  | 请求 | 回答 |
  | --- | --- |
  | `GET`/`HEAD /admin/assets/<文件名>` | 目录里有这个文件就回它，`Cache-Control: public, max-age=31536000, immutable`（文件名带内容 hash）；没有就回 Core 格式的 404。文件名只收 `[A-Za-z0-9._-]`，不跟随 `..` 与子目录 |
  | `GET`/`HEAD /admin/` 与 `/admin/<页面路径>`（最后一段不含 `.`） | 回 `index.html`，`Cache-Control: no-cache`（部署后下次打开就拿到新入口）。页面路由在浏览器里分，服务端不认页面名 |
  | 其他方法 | 405（Core 格式） |
  | `/admin`（没有结尾斜杠） | 反向代理只转发 `/admin/` 前缀，到不了 Core；不处理 |

  - 静态文件**不要求登录**：里面没有数据，数据都经 `/api/v1/admin/…` 取。所以平台授权回调 303 跳回 `/admin/connections?oauth=…` 时（CORE-TASK-126；这一跳来自平台，`SameSite=Strict` 的会话 cookie 不随它带来），页面照样能打开，之后页面自己发的同源请求会带上 cookie。
  - 目录位置：默认是进程工作目录下的 `admin_static/`（即镜像的 `WORKDIR`）；`create_app` 接受测试传入别的目录。**开关为真而目录里没有 `index.html`**（本地开发、集成测试没构建前端）：`/admin/` 下回 Core 格式的 404，启动时记一条警告，不拒绝启动。
  - 响应头见第 6.1 节。不新增配置项（不碰 `app/config.py`、`.env.example`）。
- **CI 核对镜像**：`integration` job 已有的 “Image builds” 一步之后，加一步在构建出的镜像里检查 `admin_static/index.html` 存在（`docker run --rm <镜像> test -f admin_static/index.html`）。

## 2. 页面清单

### 2.1 路由与导航

| 导航分组 | 路径（都在 `/admin/` 下） | 页面 |
| --- | --- | --- |
| — | `login` | 登录 |
| 总览 | `stock`（`/admin/` 跳到这里） | 库存总览 |
| | `stock/<sku_id>` | 一个 SKU：配额表单、配额变更记录、平台商品、流水入口 |
| 目录 | `catalog`、`catalog/products/<product_id>`、`catalog/skus/<sku_id>` | 商品与 SKU |
| | `catalog/imports/new`、`catalog/imports/<import_id>` | 目录导入 |
| | `listings` | 平台商品 |
| 库存 | `stock-imports/new`、`stock-imports/<import_id>`、`go-live` | 期初与上线切换 |
| | `receipts/new`、`adjustments/new`、`corrections/new?entry_id=` | 收货、不可售与报损、更正 |
| | `counts/new`、`counts/<count_id>` | 盘点 |
| 平台订单 | `platform-orders`、`platform-orders/<platform_order_row_id>` | 平台订单列表、待处理、详情 |
| | `platform-orders/register` | 人工登记 |
| | `manual-checks` | 人工核对 |
| 平台 | `connections` | 连接 |
| | `alerts` | 告警与“继续销售或手动停售” |
| | `reconciliations`、`invariants` | 对账、不变量自查 |
| 审计 | `audit/ledger`、`audit/catalog-changes`、`audit/allocation-changes` | 流水、目录变更、配额变更（平台订单与事件在平台订单详情里看） |

- 列表页的筛选条件放进查询串（刷新与分享链接还在）；**查询串只放 ID、枚举值与日期**，不放用户名、备注或任何自由文本。
- 顶栏：用户名、会话到期时间（第 4.4 节的格式）、退出；池选择（V1 每租户一个池，只有一个时不显示，取 `GET /api/v1/admin/pools` 的第一个）。
- 横幅区（每次切换页面时刷新，不轮询）：有未确认告警时“有未确认的告警”（取 `GET /api/v1/admin/alerts?acked=false&limit=1` 是否有项，不数条数）；人工模式的入口人工核对已逾期时“<平台> 人工核对已逾期”（从 `GET /api/v1/admin/connections` 取 `mode` 为 `manual` 的入口，各取一次 `GET /api/v1/admin/manual-checks?channel_id=&limit=1` 的 `overdue`；V1 两个入口，最多两次）；自动写平台暂停已设上时“自动写平台已暂停”（依赖第 9 节的缺口 G5，补上前不显示）。

### 2.2 每页调哪些接口、显示什么

“写”一列的按钮都按第 4.2 节处理提交键。“角色”是契约 §10 第 3 条的角色表里该命令接受的角色；V1 每个管理员都持有三个角色（契约 §8.6 第 2 条），所以**按钮一律显示**，不按角色隐藏（第 2.3 节）。

| 页面 | 读 | 写（角色） | 内容要点 |
| --- | --- | --- | --- |
| 登录 | `GET /api/v1/admin/session` | `POST`、`DELETE /api/v1/admin/session` | 第 3 节 |
| 库存总览 | `GET /api/v1/admin/pools`；`GET /api/v1/admin/pools/{pool_id}/stock-overview?after=&limit=` | — | 每个 SKU 一行：`sku_code`、SKU 状态、`stock_balance`、`reserved`、`unavailable`、`paid_pending_dispatch`、`available`、`S`、每个平台入口的 `Q` 与 `pending`（入口按 `channel_id` 升序成列，列头写平台）、`F`（为负标“超额”，红色）、异常标记（某入口 `review_hold` 为真标“待核对”；人工模式下 `manual_confirmed_quota` 与 `quota` 不等标“待生效”，[PLATFORM_CHANNELS.md](PLATFORM_CHANNELS.md) 第 6.2 节）。平台读数与最近发布结果、本次读取的数据库时间依赖缺口 G1、G2。页内按 `sku_code` 过滤只过滤已载入的这一页 |
| SKU 详情与配额 | 同上（取这个 SKU 那一行，见缺口 G3）；`GET /api/v1/admin/allocation-changes?pool_id=&sku_id=`；`GET /api/v1/admin/listings?sku_id=` | `POST /api/v1/admin/allocations`（`stock_admin`） | 表单：`S` 与每个平台入口的 `Q`，`reason` 必填；只提交改了的入口（`expected_quota` 取页面上读到的值），`expected_version` 取 `allocation_version`。表单旁实时显示按新值算的 `F'`（只是预览，判定以服务端锁内为准，契约 §3.1）。冲突见第 5.3 节 |
| 商品与 SKU | `GET /api/v1/admin/products/{product_id}`、`GET /api/v1/admin/skus/{sku_id}`、`GET /api/v1/admin/skus?sku_code=&barcode=&ids=`（CORE-TASK-101 沿用 `/v1` 的三个读）；没有分页列表，见缺口 G4 | 新建、修改（带 `expected_version`）、停用、重新启用商品与 SKU（`catalog_admin`） | 首页是按 `sku_code` 或条码查找；缺口 G4 补上之前，另从库存总览列出有余额行的 SKU。停用遇 409 `SKU_HAS_ACTIVE_LISTINGS` 时给出到平台商品页（按这个 SKU 过滤）的链接。不做 SKU 合并（契约 §8.6 第 2 条） |
| 目录导入 | `GET /api/v1/admin/catalog-imports/{import_id}` | 预览、确认（`catalog_admin`） | 选 CSV 文件，在浏览器里解析成 `{"rows": [...]}`（第 4.6 节），超过 100 行直接提示拆成几次导入，不发请求；预览结果逐行显示状态与原因、汇总；确认只执行合格的行，结果逐行列出。`import_id` 放进页面路径，刷新后能接着确认；过期 409 `IMPORT_EXPIRED` 时提示重新预览 |
| 平台商品 | `GET /api/v1/admin/listings?channel_id=&sku_id=&status=&after=&limit=`、`GET /api/v1/admin/listings/{listing_id}` | 挂上、改挂、解除（`catalog_admin`） | 挂上表单：入口、平台商品 ID、规格 ID（没有规格填 `-`）、SKU、原因；改挂与解除带 `expected_version`。错误码 `LISTING_ALREADY_LINKED`、`LISTING_SKU_TAKEN`、`LISTING_HAS_QUOTA` 各有一句说明与下一步（例如“先把这个入口的配额减到 0”链到 SKU 详情）。“从平台拉规格列表逐行确认”依赖缺口 G7；补上之前，人工模式下照卖家后台手填 |
| 期初与上线切换 | `GET /api/v1/admin/stock-imports/{import_id}`、`GET /api/v1/admin/pools/{pool_id}` | 期初预览、确认、池切 live（`stock_admin`） | 期初的 CSV 解析同目录导入；期初之前库存总览是空的，所以 CSV 的 SKU 列填 `sku_code`，页面按不重复的编码逐个调 `GET /api/v1/admin/skus?sku_code=` 换成 `sku_id`（V1 不到 100 个，顺序调用），找不到的编码在浏览器里报出、不发期初请求（缺口 G4 补上后改为一次读出）。池切 live 用确认对话框，写明“切换后不能回到初始化，期初一律被拒”；切换后提示下一步：已付款未发货的平台订单去人工登记补进 Core（[PLATFORM_CHANNELS.md](PLATFORM_CHANNELS.md) 第 6.1 节第 3 步） |
| 收货 | — | `POST /api/v1/admin/receipts`（`stock_operator`、`stock_admin`） | 行：SKU、件数；`receipt_ref` 与各行 `line_ref` 由页面生成（第 4.5 节）；`reason_code` 可选、`note` 可选、`occurred_at` 可选。成功后显示写后的四个量 |
| 不可售与报损 | — | `POST /api/v1/admin/unavailable-adjustments`（标不可售、恢复可售：`stock_operator`、`stock_admin`；报损：`stock_admin`） | 动作三选一，`reason_code` 必填；`QUANTITY_EXCEEDS_UNAVAILABLE`、`INSUFFICIENT_STOCK` 的 `details` 逐行显示 |
| 更正 | `GET /api/v1/admin/ledger`（从流水页的某一条进入） | `POST /api/v1/admin/corrections`（`stock_admin`） | 只从流水页“更正这条”进入，带着原流水；`ENTRY_NOT_CORRECTABLE` 的 `reason` 为 `already_corrected` 时提示“这条已被更正过”，链到这个 SKU 的流水（没有按单据读更正的接口） |
| 盘点 | `GET /api/v1/admin/counts/{count_id}`、`GET /api/v1/admin/counts?pool_id=&count_ref=` | 开单、提交实数、确认、取消（开单、提交、取消：`stock_operator`、`stock_admin`；确认：`stock_admin`） | 开单后 `count_id` 放进路径。提交实数前页面提醒“先停止在平台上交运这些 SKU；人工模式下先把交运登记完”（[PLATFORM_CHANNELS.md](PLATFORM_CHANNELS.md) 第 6.3 节）；409 `INBOX_NOT_DRAINED` 时列出 `details.sku_ids`，等收件箱处理完再用**新的提交**重交。分批提交实数是两次提交、两个键（契约 §7.1）。找回没完成的盘点单依赖缺口 G8 |
| 平台订单与待处理 | `GET /api/v1/admin/platform-orders?channel_id=&stage=&attention=&after=&limit=`、`GET /api/v1/admin/platform-orders/{platform_order_row_id}` | 待处理重试、映射补上后继续处理、关闭平台已取消的待处理单（`stock_operator`、`stock_admin`）；清除已确认单的待处理标记、记已知差异见缺口 G5 | 列表预设两个视图：“待处理”（`stage=held`）与“有标记”（`attention=any`）。详情：订单头、各行（接口不给已知差异的说明文字）、事件按时间列出（来源、动作、结果、结果码）。三个按钮在 `held` 时显示；能不能做由服务端判，被拒按第 5 节显示 |
| 人工登记 | `GET /api/v1/admin/pools`（取 `platform` 入口）；`GET /api/v1/admin/listings?channel_id=` | `POST /api/v1/admin/platform-orders/events`（`stock_operator`、`stock_admin`） | 第 2.4 节 |
| 人工核对 | `GET /api/v1/admin/manual-checks?channel_id=&after=&limit=`（带 `overdue`） | `POST /api/v1/admin/manual-checks`（`stock_operator`、`stock_admin`） | 每个人工模式的入口一栏：是否逾期（只用服务端给的 `overdue`，不在浏览器算）、最近的核对。表单：起止时间（起点默认上一次的 `checked_until`）、是否发现差异、说明。`checked_until` 晚于数据库当前时间会被 422：表单的“填到现在”按钮填浏览器时间减 1 分钟，被拒时提示“结束时间不能晚于服务器当前时间” |
| 连接 | `GET /api/v1/admin/connections` | 授权、断开、切换模式（`stock_admin`）；设上与解除自动写平台暂停见缺口 G5 | 每个入口：平台、店铺 ID、模式、状态、能力、令牌是否存在与到期时间、最近刷新与同步时间、收件箱积压与最早未处理事件的时间。“授权”：`POST …/authorize` 成功后立即用 `location.assign(redirect_path)` 整页跳到返回的路径（同源，再 303 到平台）。这个跳转接口出错时（404、409 `state_expired`/`state_used`、503）回的是 JSON，整页跳转会把它直接显示出来；授权 state 有 10 分钟有效期、立即跳转时很少碰到，补上缺口 G6 之前在“授权”按钮旁写明“看到错误页就返回连接页重新授权”。页面带 `?oauth=authorized|failed|expired` 打开时显示对应提示，然后用 `history.replaceState` 去掉这个参数。切到自动前列出运行手册第 1.7 节的前两步让管理员逐项勾选，再提交；`MANUAL_CHECK_OVERDUE` 与 `STATE_CONFLICT`（`manual_events_after_check`）各给一句下一步；成功后显示 `takeover_at`，并提示“马上手动发起一次对账”（链到对账页） |
| 告警 | `GET /api/v1/admin/alerts?acked=&after=&limit=` | 确认告警、记“继续销售或手动停售”（`stock_operator`、`stock_admin`） | 默认未确认；每条显示种类、对象（入口、SKU、平台订单可点进去）、首次发出、通知次数、是否已转超时待办。已确认的 409 `STATE_CONFLICT`（`already_acked`）当作成功刷新 |
| 对账 | `GET /api/v1/admin/reconciliations?after=&limit=` | 手动发起（`stock_admin`） | 列出每次对账的触发方式、发起人、起止时间、差异数；未结束的显示“进行中”。409 `reconcile_pending` 提示“已有一次手动对账在进行，15 分钟内不能重复发起” |
| 不变量自查 | `GET /api/v1/admin/ops/invariants` | — | 每一项的结果与违反的明细，原样表格化 |
| 审计 | `GET /api/v1/admin/ledger?pool_id=&sku_id=&from=&to=&after=&limit=`、`GET /api/v1/admin/catalog-changes?object_id=&after=&limit=`、`GET /api/v1/admin/allocation-changes?pool_id=&sku_id=&after=&limit=` | — | 三个标签页。流水要先选 SKU（接口要求）；每条可“更正这条”。显示 `actor_ref`、`actor_role`、`request_id`；`changes`、`deltas` 这类 JSON 按键值对展开显示。平台运营日志依赖缺口 G9（可选） |

**空状态**：每个列表都有一句说明下一步的空状态，例如库存总览“这个池还没有余额行：先做期初导入”，平台订单“待处理”视图“没有待处理的平台订单”，告警“没有未确认的告警”。
**错误状态**：列表或详情读失败时，表格区域换成错误提示条（第 5 节的文字＋`request_id`）与“重试”按钮，不显示旧数据冒充新数据。

### 2.3 角色与按钮

- 前端为每个写按钮登记它要的角色（`lib/roles.ts`，一张从命令到“接受的角色”的表，抄契约 §10 第 3 条），V1 不用它隐藏按钮：`GET /api/v1/admin/session` 不返回角色，而每个管理员都持有三个角色。
- 以后分操作员与管理员时（契约 §8.6 第 2 条“加一张账号的角色表”），会话响应要加角色（第 9 节 G10，那时再登），页面按这张表隐藏或禁用按钮。服务端的 403 `FORBIDDEN` 始终是最终把关。
- **按状态禁用**（与角色无关）：例如盘点单已确认时只读、自动写平台暂停已设上时“设上”不可用（[PLATFORM_CHANNELS.md](PLATFORM_CHANNELS.md) 第 5 节）、池已 live 时期初入口不显示。

### 2.4 人工登记

[PLATFORM_CHANNELS.md](PLATFORM_CHANNELS.md) 第 6.2 节第 2、4 条：登记后先复核再提交；提交之前可以改或作废。

- 一次登记一个事件：入口 → 平台订单号 → 动作（新订单、待发货、取消、交运、退货验收）→ 按动作显示的字段（行、平台商品与规格、件数、发生时间；交运、已确认后的取消、退货要填平台的包裹、退款、退货 ID，或勾选“平台没有给 ID”）。`payload` 按 CORE-TASK-106 引用的 `parse_payload` 规则在 `api/platformOrders.ts` 里拼，字段名与取值以实现任务时的代码为准。
- “复核”是只读的摘要页，显示将要提交的全部内容；“返回修改”回到表单，提交键不变（内容改了，键还没用过，不冲突）；“作废”清空草稿。草稿只在内存里，离开页面前有未提交的草稿时浏览器提示。
- 409 `POSSIBLE_DUPLICATE`：显示 `details` 里可能重复的单据，管理员核对后勾选“已核对，不是重复”再交（`confirm_possible_duplicate` 为真，这是新的一次提交、新键）。
- 平台订单号与各种 ID 只在请求体里，不进页面路径与查询串（登记成功后跳到详情页，路径里用的是 `platform_order_row_id`）。
- 表单上方常驻一句提示：只填平台给的编号与件数，不填顾客姓名、电话、地址（契约 §10 第 6 条）。

## 3. 登录与会话

### 3.1 登录

- 打开任何页面先 `GET /api/v1/admin/session`：200 把 `username`、`expires_at`、`csrf_token` 放进内存；401 跳登录页，带 `next=<当前路径>`。`next` 只接受以 `/admin/` 开头、不以 `//` 开头的路径，否则回 `/admin/stock`（防开放跳转）。
- 登录表单：用户名 `autocomplete="username"`、密码 `autocomplete="current-password"`；`POST /api/v1/admin/session`，`Content-Type: application/json`。提交后立即清空内存里的密码。

| 回答 | 显示 |
| --- | --- |
| 200 | 存会话三项，回 `next` |
| 401 `invalid_credentials` | “用户名或密码错误”（三种原因不区分，ADMIN_AUTH 第 5.1 节） |
| 429 `login_locked` | “登录尝试过多，已锁定；最早一次失败满 60 分钟后自动解除” |
| 503 `login_busy` | “登录的人太多，请稍后再试” |
| 503 `database_unreachable` | “服务暂时不可用” |
| 403 `cross_site_request`、415、422 | “请求被拒绝”（页面不会这样发，出现即缺陷）；`detail` 原样附在后面 |
| 网络错误 | “连不上服务器” |

### 3.2 每个请求

- `fetch` 用 `credentials: "same-origin"`、`cache: "no-store"`；写请求带 `Content-Type: application/json`、`X-CSRF-Token: <内存里的令牌>`、`Idempotency-Key`（第 4.2 节）。不带 `X-Request-ID`（服务端一律生成）。
- 不发跨域请求；所有路径都是 `/api/v1/…` 的相对路径。

### 3.3 401、403 与会话到期

- **401 `not_authenticated`**（会话到期、被撤销、账号停用）：不丢掉当前页面。弹出 `ReloginDialog`（同一个登录接口），登录成功后更新内存里的 CSRF 令牌，关掉对话框，**由管理员再点一次提交**（提交键不变；作用域含管理员的主体，同一个人重新登录主体不变，所以同键重放仍然有效）。换了别的账号登录时，页面重载到当前路径、丢弃表单。
- **403 `csrf_failed`**：重新 `GET /api/v1/admin/session` 取令牌。返回的 `username` 与内存里的相同，才用**同一个提交键**自动重发一次；用户名变了（别的标签页换了账号登录），不重发——否则会以另一个管理员的主体再执行一次——按“换了别的账号”处理（重载、丢弃表单）。重发后还是 403 就显示“会话校验失败，请刷新页面”。（同一会话的 CSRF 令牌由会话令牌导出、不会变，出现这个多半是会话换过。）
- **403 `FORBIDDEN`**（`/v1` 形状）：“没有这个操作的权限”。V1 不会发生。
- 会话 12 小时绝对到期、不续期（ADMIN_AUTH 第 4.4 节）：顶栏显示到期时间；离到期 10 分钟起顶栏变成警告色“会话将于 <时间> 到期，请先保存手头的操作”。到期判断只用于提示，以服务端的 401 为准。
- **退出**：`DELETE /api/v1/admin/session`（带 CSRF 头）；204 或 401 都清空内存里的会话与页面数据，回登录页。
- 多个标签页各自取会话；同一会话的 CSRF 令牌相同。一个标签页退出后，别的标签页在下一次请求时得到 401。

## 4. 通用约定

### 4.1 两种响应形状

页面统一经 `lib/request.ts` 发请求，它把回答归成下面几类（纯函数 `classify(status, body, headers)`，有测试）：

| 类 | 怎么认 | 页面怎么做 |
| --- | --- | --- |
| 成功 | 2xx；正文 `{"data", "request_id"}`（登录三件是平的 JSON，204 无正文） | 用 `data`；响应头 `Idempotency-Replayed: true` 时提示“这次提交之前已成功，显示的是当时的结果” |
| 会话类拒绝 | 正文 `{"detail": …}`（登录三件，及会话依赖给出的 401、403） | 第 3 节 |
| 确定的业务结果 | `/v1` 错误形状 `{"error": {"code", "message", "retryable", "details"}, "request_id"}`，状态为 400、403、404、405、409（除 `REQUEST_IN_PROGRESS`）、413、422 | 第 5 节；**换新提交键** |
| 进行中 | 409 `REQUEST_IN_PROGRESS` | 按 `Retry-After`（秒）等待后用同键重发，最多 5 次，之后显示“仍在处理，请稍后用同一提交重试” |
| 确定未生效 | 503 `UNAVAILABLE`、429 `RATE_LIMITED`；会话依赖给出的 503 `{"detail": "database_unreachable"}`（ADMIN_AUTH 第 5.1 节，在进入接口之前） | 显示原因，按钮仍是同一提交（可同键重试） |
| 结果未知 | 500、其他 5xx、超时（30 秒，`AbortController`）、网络中断、正文不是上面任一形状 | 第 4.3 节 |

错误提示都带 `request_id`（有的话），方便在 Core 的日志里查。`message` 是给开发看的英文，页面显示按 `code` 映射的中文（`lib/errorText.ts`，没映射的显示 `code` 原文）。

### 4.2 提交键（`Idempotency-Key`）

照契约 §7.1“后台表单防重复”：

- **打开表单时**生成：`adm:` 加一个 ULID（26 个 Crockford Base32 大写字符：48 位毫秒时间戳＋80 位 `crypto.getRandomValues` 随机数），共 30 个字符，在 §7.1 的格式之内。不用 `crypto.randomUUID`，因为契约写的是 ULID。
- 同一次提交的重复点击、网络重试、`REQUEST_IN_PROGRESS` 后的重发、401 重新登录后的再点、403 `csrf_failed` 后的自动重发：**都用这个键**。提交中按钮禁用。
- 得到成功或确定的业务结果（第 4.1 节）之后**换新键**；要再做一次同样的操作就是新的一次提交。
- 409 `IDEMPOTENCY_CONFLICT` 是页面的缺陷（同键换了内容）：显示“提交键冲突，已换新键，请核对后再提交”，换新键、不自动重发。
- 平台订单的人工操作（人工登记、重试、继续处理、关闭）不写幂等记录、用的是收件箱的提交键（契约 §8.6 第 2 条、§5.10），页面的做法完全相同。
- 这些规则写成一个纯函数的状态机（`lib/submission.ts`：表单打开、内容改动、得到某类回答 → 用哪个键、表单是否锁定），每种转移各有一条测试。

### 4.3 结果未知

- 表单**锁定**（字段只读），按钮变成“用同一提交重试”（同键同内容，安全：已生效就原样重放，未生效就执行）。
- 另给一个“放弃这次提交”：先提示去对应的列表或流水核对结果，再解锁、换新提交键，但**保留表单里页面生成的业务引用**（第 4.5 节的 `receipt_ref`、`adjustment_ref`、`count_ref`）。这样上次若其实已生效，换键重交同一内容得 200 与原结果，改了内容得 409 `BUSINESS_REF_CONFLICT`，都不会重复入账（契约 §6.2 第 7 条）。不允许改了内容再用旧键提交。
- **盘点的提交实数与确认、期初确认、目录导入确认**结果未知时**只允许同键重试**，没有“放弃”（提交实数换键会重新取快照）；可以先读那个对象看状态再决定。
- 平台订单的人工操作结果未知时同样只允许同键重试，或去平台订单详情看事件是否已写入（人工登记的去重靠提交键，契约 §5.10）。

### 4.4 时间与时区

- 服务端的时间都是 UTC 的 RFC 3339（微秒、`Z`）。页面**一律按马来西亚时间（UTC+08:00，`Asia/Kuala_Lumpur`，没有夏令时）显示**，格式 `2026-11-24 20:05:31`，列头或字段名后写“（UTC+8）”；悬停显示服务端原文。不跟随浏览器所在的时区，免得两个人看到不同的数。
- 输入（人工核对的起止、流水的筛选区间、人工登记的发生时间、库存命令的可选 `occurred_at`）用 `datetime-local` 输入框，按 UTC+8 理解，提交成 `YYYY-MM-DDTHH:mm:ss+08:00`（契约接受带偏移的 RFC 3339，服务端换成 UTC）。
- 逾期、过期、“多久以前”这类判断**只用服务端给的结果**（`overdue`、错误码），不用浏览器时钟算；浏览器时钟只用于会话到期前的提示与“填到现在”的默认值。

### 4.5 页面生成的业务引用

契约 §1（0.22）：后台的单据引用由后台生成。页面在打开表单时与提交键一起生成；成功或得到确定的业务结果后与提交键一起换新，结果未知后“放弃”时只换键、**不换引用**（第 4.3 节）：`receipt_ref` 为 `adm-rcp-<ULID>`，`adjustment_ref` 为 `adm-adj-<ULID>`，`count_ref` 为 `adm-cnt-<ULID>`（都在 `[A-Za-z0-9._:-]`、128 个字符以内）；`line_ref` 为行号 `1`、`2`……（同一请求内唯一即可）。
管理员要记供应商单号之类的，写在 `note` 里（提示不写个人资料）。

### 4.6 分页、CSV 与数字

- **分页**：后台列表（CORE-TASK-100 的规则）与 `/v1` 沿用的列表（流水）都只当 `next_cursor` 是不透明文本，原样作下一页的 `after`。**停止条件**写成一个纯函数：`next_cursor` 为 `null`，或等于这次发出的 `after`（流水的规则：没有新记录时原样返回），或这一页没有项。每页 `limit` 取 50。上一页靠内存里的游标栈；游标不进查询串。
- **CSV**：自写 RFC 4180 的解析（逗号分隔、双引号转义、`\r\n` 与 `\n`），第一行是列名，列名与正文字段名相同（期初例外：SKU 列填 `sku_code`，页面换成 `sku_id`，第 2.2 节）；只读 UTF-8（去掉 BOM）；空行跳过；超过 100 行不发请求。数量列转成整数，转不了就在浏览器里报这一行，不发请求。
- **数字**：件数都是整数；`F` 为负标红并写“超额”。

## 5. 错误怎么显示

### 5.1 原则

- 错误放在发生的位置：字段错误在字段下，整单错误在表单顶部，列表读失败在表格区域。
- 每个错误都写“发生了什么”与“下一步做什么”，不只显示错误码。
- `details` 里有行号、SKU、入口的，逐条列出，SKU 显示为编码并链到 SKU 详情。

### 5.2 422 与 413

- 422 `INVALID_REQUEST`：`details` 里能对上字段的标到字段上，其余显示 `message` 与 `details` 原文。页面先在浏览器里做一遍同样的格式检查（必填、长度、整数范围、引用字符集），正常使用时不该看到 422；看到即视为前后端规则不一致，提示里带 `request_id`。
- 413：批量超过 100 行或请求体超过 128 KiB，提示拆成几次。

### 5.3 409

| 码 | 显示与下一步 |
| --- | --- |
| `VERSION_CONFLICT`、`QUOTA_CHANGED` | “别人或平台订单刚改过这些数”：自动重读，把页面上的旧值与新值并排标出，管理员确认后用**新键**再交（契约 §3.1“两种冲突后台页面都自动重读、让管理员确认后再提交”） |
| `ALLOCATION_EXCEEDS_UNALLOCATED` | 显示 `details` 的 `available`、`safety_stock`、`quota_total`、`pending_total`、`unallocated` |
| `NO_ACTIVE_LISTING`、`MANUAL_CHECK_OVERDUE`、`SKU_HAS_ACTIVE_LISTINGS`、`LISTING_HAS_QUOTA`、`INBOX_NOT_DRAINED` | 说明原因，链到要先去处理的页面（平台商品、人工核对、配额、平台订单） |
| `POSSIBLE_DUPLICATE` | 第 2.4 节 |
| `INSUFFICIENT_STOCK`、`QUANTITY_EXCEEDS_UNAVAILABLE`、`QUANTITY_EXCEEDS_OUTSTANDING` | 逐行列出 `details.lines` |
| `STATE_CONFLICT`、`POOL_NOT_LIVE`、`POOL_ALREADY_LIVE`、`IMPORT_EXPIRED`、`COUNT_STALE`、`COUNT_BELOW_COMMITMENTS`、`ENTRY_NOT_CORRECTABLE` 等 | 契约 §8.3 那一行的“调用方动作”改写成一句中文；有 `details.reason` 的按它细分 |
| `BUSINESS_REF_CONFLICT` | 只会出现在结果未知后“放弃”、改了内容再交时（第 4.3 节）：提示“上一次提交其实已经生效，内容与这次不同”，链到这个 SKU 的流水核对，然后换新引用与新键 |
| `ORDER_REFERENCE_CONFLICT` | 页面缺陷：显示并带 `request_id` |
| `IDEMPOTENCY_CONFLICT`、`REQUEST_IN_PROGRESS` | 第 4.1、4.2 节 |

### 5.4 404

“这个<对象>不存在”（跨租户与不存在的回答相同，页面不区分），给回列表的链接。

## 6. 安全

### 6.1 响应头（由第 1.4 节的静态模块与 CORE-TASK-098 的中间件给）

`/admin/` 下的响应：

```text
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: same-origin
```

- 契约 §8.6 第 2 条只要求 `frame-ancestors 'none'` 与 `X-Frame-Options: DENY`；上面的完整策略包含这两项，更严。CORE-TASK-098 的中间件给 `/admin/` 与 `/api/` 的响应加的是只有 `frame-ancestors 'none'` 的 CSP；骨架的后端任务把 `/admin/` 前缀的那一份换成上面的完整策略（`/api/` 的不变）。这是第 9 节的 G11，是对已登记任务 098 行为的补充，不改契约。
- 没有 `'unsafe-inline'`、`'unsafe-eval'`：Vite 生产构建只产出外部的模块脚本与样式文件；React 的 `style` 属性经 DOM 设置，不受 `style-src` 限制。第 6.3 节的检查保证产物里没有内联脚本与样式。
- `/admin/assets/` 的长缓存见第 1.4 节；`index.html` 是 `no-cache`；`/api/v1/admin/` 的响应已由 098 加 `Cache-Control: no-store`，浏览器的前进后退缓存不会留下数据页面。

### 6.2 页面的约束（ESLint 规则强制）

- 不用 `localStorage`、`sessionStorage`、`indexedDB`、`document.cookie`（`no-restricted-globals` 与 `no-restricted-properties`）。会话、CSRF 令牌、用户名、草稿只在内存里；刷新页面靠 `GET` 会话重新取。
- 不用 `dangerouslySetInnerHTML`、`eval`、`new Function`（`no-restricted-syntax`、`no-eval`、`no-new-func`）；所有服务端文本经 React 转义显示。
- 不嵌框架、不开新窗口；外链只有一处：授权时整页跳转（同源路径，再由服务端 303 到平台）。
- 不引外部 CDN、字体、统计或错误上报：产物只引用 `/admin/` 下自己的文件（第 6.3 节检查）。
- 页面不记录任何东西到控制台以外的地方；控制台也不打印令牌、会话响应与请求体。

### 6.3 构建产物检查

`frontend/scripts/check-dist.mjs`（`npm run check:dist`，在 CI 的 `frontend` job 构建之后跑）：`dist/index.html` 里没有内联 `<script>` 正文与 `<style>`、没有 `on*=` 属性；`dist/` 下所有 HTML、JS、CSS 里不出现 `http://`、`https://` 开头的地址（React 与 Vite 产物里的少数规范链接与注释，按实际产物列一个精确的允许清单，写在脚本里）。

### 6.4 个人数据

Core 不存顾客资料（契约 §10 第 6 条），后台页面也就没有个人数据可显示；管理员账号只有用户名。要防的是管理员往自由文本里写：`note`、`reason`、说明类字段旁一律提示“不写顾客姓名、电话、地址”。本设计不收集、不存储、不展示、不导出个人数据，所以不在“碰个人数据”的设计闸门内（这份设计仍按要求请 Kelvin 批准）。

## 7. 本地开发

- `npm run dev`：Vite 开发服务器把 `/api/` 代理到本机起的 Core（后台开关为真、本地库）。会话 cookie 带 `__Host-` 前缀、要求 `Secure`，所以开发服务器用 Vite 自带的 HTTPS 选项（自签证书），或在说明里写用 `localhost`（浏览器把它当安全上下文）；实现骨架时实测后写进 `frontend/README`。不改 Core 的 cookie 属性。
- 开发用的地址、端口只写在 `vite.config.ts` 的开发配置里，用 `localhost`。

## 8. 测试

| 层 | 测什么 | 在哪跑 |
| --- | --- | --- |
| 前端纯函数（vitest） | 第 4.1 节的分类、第 4.2 节的提交键状态机、ULID 格式与单调性、分页停止条件（三种）、时间换算（UTC ↔ UTC+8，含跨日）、CSV 解析（引号、换行、BOM、空行、超过 100 行）、配额页的 `F'` 预览（用契约 §3.1 例子一的第 6、6′ 步）、各表单正文的拼法、`next` 参数的过滤、错误码到中文的映射覆盖第 5.3 节列出的码 | CI `frontend` job |
| 构建与产物 | `tsc --build`、`vite build`、第 6.3 节的检查 | CI `frontend` job |
| Core 静态托管（pytest） | 开关为真：`/admin/`、`/admin/stock/sku_x` 回 `index.html` 与 `no-cache`；资源文件回长缓存；`..`、不存在的资源 404；`POST` 405；`/admin/` 下响应带第 6.1 节的四个头；开关为假：`/admin/` 404；目录里没有 `index.html` 时 404 且能启动 | CI `integration` job（用临时目录放假的产物） |
| 镜像 | 镜像里有 `admin_static/index.html` | CI `integration` job |
| 接口 | 已由 CORE-TASK-098～125 各自的集成测试覆盖；前端任务不重复 | — |

不做组件渲染测试与端到端测试（没有 jsdom、测试库与浏览器；V1 不启用浏览器验收，第 10 节）。页面上线后由 Kelvin 按第 11 节每个任务的“人工点检”一条在生产后台过一遍。

## 9. 后端缺的接口或字段（只列出，不改契约、不改已登记任务）

| 编号 | 缺什么 | 哪页要 | 建议 | 要不要先改契约 |
| --- | --- | --- | --- | --- |
| G1 | 库存总览每个“SKU × 平台入口”的平台读数（最近一次读回的值与时间）与最近一次发布的结果（目标值、平台确认的值、结果、时间） | 库存总览 | 在 CORE-TASK-100 的 `channels` 每项加 `last_read`、`last_publish`（取 `platform_publishes` 与读回记录的最新一行）；即 TODO 里已有的“待登记：库存总览加平台读数与最近发布结果”。依赖 CORE-TASK-121、122 | 否（契约 §8.6 第 2 条的页面表已列这两项） |
| G2 | 库存总览的读取时间（数据库时间） | 配额 | 响应加 `as_of`（读快照时的数据库时间）；契约 §3.1 要求“配额页面标出这些数的读取时间（数据库时间）”，CORE-TASK-100 没给 | 否 |
| G3 | 只读一个或几个 SKU 的总览行 | SKU 详情与配额（冲突后重读） | `stock-overview` 加可选的 `sku_ids`（逗号分隔，最多 100 个，同 §6.8 余额）；补上之前页面重读第一页并按 `sku_id` 取（V1 的 SKU 不到 100 个） | 否 |
| G4 | 商品与 SKU 的分页列表（含还没有余额行的） | 目录 | `GET /api/v1/admin/skus?after=&limit=&status=` 按 `sku_id` 升序，照后台列表规则；CORE-TASK-101 写了“不另做分页列表” | 否 |
| G5 | 其余运营操作：设上与解除自动写平台暂停（及读当前状态）、解除读回待核对、确认人工配额已生效、清除已确认单的待处理标记、记已知差异 | 连接、总览、平台订单详情 | 即 TODO 里已有的“待登记：第 3 组 其余运营操作”；连同一个读写入暂停状态的接口一起登记 | 否（契约 §3.2、§10 第 3 条已有） |
| G6 | 授权跳转接口出错时回到页面 | 连接 | `GET /api/v1/admin/connections/authorize-redirect` 的 404、409、503 改为 303 到 `/admin/connections?oauth=<结果>`（同回调的写法），而不是回 JSON；页面已能显示 `oauth` 参数。可选，不做时页面按第 2.2 节提示 | 否（CORE-TASK-116 的行为补充） |
| G7 | 从平台拉规格列表 | 平台商品 | 经适配器的 `list_models` 读出某入口的规格列表，供逐行确认挂上（契约 §8.6 第 2 条页面表）；只能在授权之后用，CORE-TASK-102 写明留给第 4 组、第 4 组没有登记它 | 否 |
| G8 | 按池列出未完成的盘点单 | 盘点 | `GET /api/v1/admin/counts?pool_id=&status=`；现在只能按 `count_ref` 查，页面丢了 `count_id` 就找不回 | 否 |
| G9 | 平台运营日志（`platform_ops_log`）的列表 | 审计（可选） | 只读、按主键倒序、照后台列表规则；契约 §8.6 第 2 条的审计页没列它，要不要做请 Kelvin 定 | 否 |
| G10 | 会话响应里的角色 | 以后按角色隐藏按钮 | V1 不需要；分操作员时随账号角色表一起加 | 到时再改 |
| G11 | `/admin/` 的完整 CSP、`nosniff`、`Referrer-Policy` 与静态托管 | 全部 | 骨架的后端任务（第 11 节 F01）做：改 `app/api/requests.py`，`/admin/` 前缀的 CSP **替换**成第 6.1 节的完整策略（不是再加一条），`/api/` 的照 098 不变 | 否 |

另外两条：CORE-TASK-101 的读接口沿用 `/v1` 的 `GET skus` 查找参数，若实现时没有保留 `ids` 以外的组合，目录页的查找按实际接口改；登录尝试的查看（ADMIN_AUTH 第 6 节）本设计不做页面。

## 10. CI、Worker 与浏览器验收

### 10.1 CI 的 `frontend` job（Kelvin 2026-11-24 定新开一个 job）

- 照 acuven-shop：`actions/setup-node@v4`（node 24，npm 缓存以 `frontend/package-lock.json` 为键），`working-directory: frontend` 里依次 `npm ci --no-audit --no-fund`、`npm run lint`、`npm run typecheck`、`npm test`、`npm run build`、`npm run check:dist`。
- **成为必需检查的顺序**：F03 把 job 加进 `ci.yml` 并合并之后，① Kelvin 在 GitHub 分支保护里把 `frontend` 加为必需检查；② 运营者 PR 把 `frontend` 加进 `.platform/project.yaml` 的 `required_status_checks`。先改 `project.yaml` 而 job 还不存在，Worker 会一直等一个不会出现的检查。页面任务都排在这两步之后。
- 改动只涉及 `frontend/` 的 PR，`unit` 与 `integration` 照跑（必需检查不能被路径过滤跳过）。

### 10.2 只在 CI 跑

前端检查不写进任何任务的 `allowed_commands`（本仓库 CLAUDE.md）；前端任务的验收标准写“由必需 CI 检查 `frontend` 执行并通过”。前端任务的 `allowed_commands` 照其他任务列 `lint.check`、`format.check`、`tests.unit`（它们不看 `frontend/`，照常零退出）。

### 10.3 Worker 写前端的限制

- Worker 的沙箱里不跑 node：实现会话写完页面后，类型与测试错误要到 PR 的 CI 才看得到。所以页面任务要小（第 11 节每个 300–800 行），逻辑尽量进有测试的纯函数。
- **两个骨架任务由 Claude Code 会话实现**（照登记的任务，照常走分支、Draft PR、独立审查与 CI），再由 OpenClaw 接后面的任务：F02 要联网跑 `npm install` 生成 `package-lock.json`，Worker 沙箱多半做不到；F03 改 `.github/workflows/ci.yml` 与 `Dockerfile`，推送工作流文件要 `workflow` 权限，而已登记任务的验收标准一律写“不改 CI、部署配置”。之后的任务不加依赖、不改锁文件、不改 CI 与 Dockerfile（验收标准写明）。
- `allowed_change_paths` 要逐个精确文件、不超过 12 个（本仓库 CLAUDE.md 的拆分规则）；`package-lock.json`、`.dockerignore`、`ci.yml` 等文件名能不能被控制面接受，登记时用 `contract_check` 核对。

### 10.4 浏览器验收（Kelvin 2026-11-24 定 V1 先不启用）

本仓库还没启用浏览器验收（`.platform/project.yaml` 没有 `acceptance_hosts`，`commands.yaml` 没有验收命令）。只有生产实例开着后台，Demo 上后台是关的，没有合适的验收环境。所以前端任务**不带 `acceptance` 块**；每个任务的验收标准另写一条“合并部署后的人工点检”（Kelvin 在生产后台照列出的两三步看一遍），不进 CI、不挡合并。以后有了可验收的环境再按 CLAUDE.md 的浏览器验收约定启用（另开运营者 PR）。

## 11. 建议的任务拆分（只建议，不登记）

Kelvin 2026-11-24 定**一次全部登记**。下表按依赖排好；“依赖”一列的后端任务必须先合并。F07、F20、F21 依赖第 9 节还没登记的后端缺口，要等那些后端任务登记之后才能一起登（登记时按缺口的任务号填 `depends_on`；G2 只是给总览响应加一个字段，建议在前端登记之前先登）。
每个任务的允许路径不超过 12 个（括号里是数目）。新页面要在 `src/App.tsx` 的路由表与 `src/components/Layout.tsx` 的导航里各加一行，这两个文件是页面任务的共享文件（同 Core 的 `app/main.py`），登记时逐个列进 `allowed_change_paths`。

| 编号 | 任务 | 层 | 依赖 | 粗估 |
| --- | --- | --- | --- | --- |
| F01 | Core 托管后台静态文件：`app/api/admin_static.py`、`app/main.py` 挂载、`app/api/requests.py` 把 `/admin/` 的 CSP 换成完整策略并加两个头（G11）、`tests/integration/test_admin_static.py`（临时目录放假产物）（4）。不碰 Dockerfile 与前端目录，合并后单独通过 CI | 后端 | CORE-TASK-098 | 300 |
| F02 | 前端工程骨架：`package.json`、`package-lock.json`、三个 `tsconfig`、`vite.config.ts`、`eslint.config.js`（含第 6.2 节的规则）、`index.html`、`src/main.tsx`、最小的 `src/App.tsx`、`src/styles.css`、`scripts/check-dist.mjs`（12）。**会话实现**（第 10.3 节），PR 正文贴本机跑 lint、类型检查、测试、构建与产物检查的结果 | 前端 | F01 | 400 |
| F03 | CI 与镜像：`ci.yml` 加 `frontend` job，`integration` job 的镜像构建之后查 `admin_static/index.html`；`Dockerfile` 加前端构建阶段；`.dockerignore`（3）。**会话实现** | CI 与部署 | F02 | 100 |
| — | （人）Kelvin 把 `frontend` 设为必需检查；运营者 PR 改 `project.yaml`（第 10.1 节） | 运营 | F03 | — |
| F04 | 公共库一：`lib/request.ts`（第 4.1 节的分类）、`lib/errorText.ts`、`lib/submission.ts`（提交键状态机）、`lib/ulid.ts`、`lib/refs.ts`，各带测试（10） | 前端 | F03 | 600 |
| F05 | 公共库二：`lib/paging.ts`、`lib/time.ts`、`lib/csv.ts`、`lib/roles.ts`、`lib/router.ts`，各带测试（10） | 前端 | F03 | 500 |
| F06 | 外壳与登录：`App.tsx` 路由表、`components/` 的五个文件（第 1.3 节）、登录页、`api/session.ts` 及测试、`styles.css`（10） | 前端 | F04、F05 | 700 |
| F07 | 库存总览与 SKU 详情、改配额、配额变更记录（显示读取时间） | 前端 | F06、CORE-TASK-100、G2 | 700 |
| F08 | 商品与 SKU 的查找、新建、修改、停用、重新启用 | 前端 | F06、CORE-TASK-101 | 700 |
| F09 | 目录导入的 CSV 预览与确认 | 前端 | F08 | 400 |
| F10 | 平台商品列表、挂上、改挂、解除 | 前端 | F06、CORE-TASK-102 | 500 |
| F11 | 期初导入（按编码换 `sku_id`）与池切上线 | 前端 | F06、CORE-TASK-103 | 500 |
| F12 | 收货、不可售与报损、流水页（审计的流水标签）与从流水发起更正 | 前端 | F06、CORE-TASK-103、CORE-TASK-104 | 700 |
| F13 | 盘点：开单、提交实数（含收件箱未清空的提示）、确认、取消 | 前端 | F12 | 600 |
| F14 | 平台订单列表、详情与待处理的三个操作 | 前端 | F06、CORE-TASK-106、CORE-TASK-108 | 600 |
| F15 | 人工登记（含复核与可能重复） | 前端 | F14 | 600 |
| F16 | 人工核对 | 前端 | F06、CORE-TASK-109 | 400 |
| F17 | 连接：列表、授权、断开、切换模式 | 前端 | F06、CORE-TASK-116、CORE-TASK-120 | 600 |
| F18 | 告警、继续销售或停售的决定、对账、不变量自查，顶栏的告警与逾期横幅 | 前端 | F06、CORE-TASK-104、CORE-TASK-109、CORE-TASK-116、CORE-TASK-125 | 600 |
| F19 | 审计的目录变更与配额变更两个标签页 | 前端 | F12、CORE-TASK-100、CORE-TASK-101 | 300 |
| F20 | 其余运营操作的按钮与“自动写平台已暂停”横幅（G5） | 前端 | F14、F17、G5 的后端任务 | 500 |
| F21 | 库存总览加平台读数与最近发布结果，SKU 详情改用按 SKU 读（G1、G3） | 前端 | F07、G1 与 G3 的后端任务 | 300 |

- **一次登记、分两步进当前计划**：21 个任务在同一个登记 PR 里登为 `ready`，但只把 F01 排进 planning-v1 的「当前计划」，其余放「后续计划」。F02、F03 由会话实现、不交给 Worker。F04 起的任务等运营者 PR 把 `frontend` 加进 `project.yaml` 的必需检查（Kelvin 同时在分支保护里设好）时，由那个 PR 一并移进「当前计划」：人工步骤写不进 `depends_on`，先放进去的话 Worker 不会等 `frontend` 检查。
- 每个任务只做一层（F01 是后端，F03 是 CI 与部署，其余前端）。纯前端任务不碰钱与个人数据的规则，不在设计闸门内；它们依据的就是本设计，本设计批准即可登记。设计预审按 CLAUDE.md 的条件：前端任务没有闸门、不改库，`contract_check` 给出拆分 WARN 或预计接近 1500 行时才跑。
- 第 9 节的 G1～G9 由后端会话另行登记（G1、G5 已在 TODO 的待登记里），F07、F20、F21 跟在它们后面。

## 12. 需要 Kelvin 定的

已定（2026-11-24）：不用组件库；一次全部登记；新开 `frontend` job；V1 先不启用浏览器验收。

随本设计一起请 Kelvin 确认（都按推荐写进了正文，不同意的改设计）：

1. **由 Core 进程在 `/admin/` 下提供前端文件**（第 1.4 节），不另起前端容器。理由：反向代理只把 `/admin/` 与 `/api/` 转给生产 Core，再起一个容器要改 vhost 与部署；代价是 Core 镜像多一个 node 构建阶段（构建时间多一两分钟），运行镜像不含 node。
2. **页面一律按马来西亚时间（UTC+8）显示与输入**（第 4.4 节），不跟随浏览器时区。
3. **界面只做简体中文**，错误码原文附在中文后面。
4. **两个骨架任务（F02、F03）由会话实现**（第 10.3 节：Worker 沙箱多半装不了 npm 依赖，推送工作流文件要额外权限）。
5. **缺口 G9（平台运营日志列表）做不做**：契约的审计页没有它，推荐做（低频操作在一处可查），放在 G5 那一批。
6. **上线后的人工点检**代替浏览器验收（第 10.4 节），由 Kelvin 在生产后台过一遍。

## 13. 修订记录

- **0.1**（2026-11-24）：草案。Kelvin 同日先定了组件库、登记范围、CI job 与浏览器验收四项。第 1 轮独立评审后改：结果未知后“放弃”保留业务引用、盘点等确认类只许同键重试；Dockerfile 与 CI 改动从 F01 移到 F03，骨架拆成每个不超过 12 个允许路径；期初按编码换 `sku_id`；CSRF 重发前核对用户名；会话依赖的 503 归为确定未生效；G6 改为授权跳转出错回页面（逾期横幅用已有的 `overdue`）；F07 依赖 G2；更正已被更正时链到流水。第 2 轮后改：期初 CSV 的列名例外写进第 4.6 节；F04 起的任务登记后先放「后续计划」，等 `frontend` 成为必需检查再移进「当前计划」；F03 的层改为 CI 与部署。
