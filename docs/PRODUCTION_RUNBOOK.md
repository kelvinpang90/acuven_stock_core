# 生产实例运行手册：首次上线、升级与回滚、整机恢复与演练

> 依据：[PRODUCTION_AND_RECOVERY.md](PRODUCTION_AND_RECOVERY.md) 0.6（下称“设计”）第 1.3、1.4、2.4、2.5 节，契约 [CONTRACT_V1.md](CONTRACT_V1.md) §2.5、§10、§12.1。
> 本手册只把设计写成照着做的步骤，不另立规则；与设计有出入的地方以设计为准。
> 巡检（心跳检查、运维令牌、cron）的装法在 [OPS_MONITORING.md](OPS_MONITORING.md)，这里只链过去。
> `vps_infra` 那边的步骤（建库与账号、专用数据网络、每晚备份、binlog 上传、还原）以 `vps_infra` 仓库的脚本与还原说明为准，这里只写要做什么、做完核对什么。

下面的步骤都由人做。

> **（2026-11-23，草案，待 Kelvin 批准）** Inventory 并入本仓库（C-21）：按契约 0.22 与设计 0.7，生产实例不再签发 Inventory 的业务凭据与对账凭据、改签一个没有令牌的内部凭据，生产 Core 改挂代理网络并开 vhost，恢复时 Inventory 随生产 Core 起来。受影响的步骤下面都加了“（0.22，待批准）”的说明；批准、相应任务实现之前，照原步骤做（Inventory 的两个凭据先不签）。

## 0. 约定

### 0.1 占位符

命令里的服务器目录、账号、主机名、库名、地址与 ID 一律用占位符，执行时换成实际值。实际值不写进本仓库、工单与对话。

| 占位符 | 指什么 |
| --- | --- |
| `<生产检出目录>`、`<Demo 检出目录>` | 服务器上本仓库的两个检出目录（GitHub secret `VPS_APP_DIR_PRODUCTION`、`VPS_APP_DIR` 的值） |
| `<生产库名>`、`<Demo 库名>` | infra_mysql 里两个实例的库 |
| `<生产库连接串>` | `provision-project.sh` 打印的连接串 |
| `<仓库克隆地址>` | 本仓库在 GitHub 上的克隆地址 |
| `<所有者>/<仓库名>` | 本仓库在 GitHub 上的所有者与仓库名，用于 GHCR 时全小写 |
| `<GitHub 账号>` | 登录 GHCR 用的 GitHub 账号 |
| `<R2 远端>`、`<桶>` | 本机 rclone 配置里 R2 的远端名与备份桶 |
| `<恢复工作目录>` | 恢复时在新机器上放下载文件的目录（检出目录之外，只有本账号可读） |
| `<演练标记库名>` | 演练量丢失窗口用的库（第 4.2 节） |
| `<临时实例容器>` | 误操作恢复用的临时 MySQL 实例的容器名（第 3.10 节） |
| `<运行账号>` | 服务器上跑 cron 的账号 |
| `<代理网络>` | （0.22，待批准）`infra_nginx` 所在的代理网络的名字（以 `vps_infra` 为准） |
| `<操作人引用>` | 命令行 `--actor` 用的引用（契约第 1 节的引用格式，不含姓名与邮箱），例如 `ops-7` |
| `<租户 ID>`、`<地点 ID>`、`<池 ID>`、`<入口 ID>`、`<凭据 ID>` | 命令行输出里的 `ten_…`、`loc_…`、`pool_…`、`chan_…`、`cred_…` |
| `<SHA>` | 完整的 40 位小写 commit SHA |

**密钥**一律从密码管理器取回（设计 P-05 的那条安全笔记），不写进命令行（会进 shell 历史）、不另存文件；本手册不写任何密钥值。
唯一的例外是整机恢复时拉镜像用的只读包令牌：它按设计第 2.4 节第 7 步临时新建、用完删掉，不进密码管理器（第 3.7 节）。

### 0.2 两段常用的 shell

**找本实例的容器**：开通、签发、轮换与恢复时起服务都只经 `deploy/instance.sh` 找容器，不用 `docker compose exec` 或 `docker compose run`。
在对应的检出目录里：

```bash
. deploy/instance.sh; echo "${CORE_INSTANCE:-$CORE_INSTANCE_ERROR}"   # 生产检出应打出 production，Demo 检出应打出 demo
container="$(core_find_api_containers)"; echo "$container"           # 应恰好一行容器 ID
[ "$(printf '%s\n' "$container" | grep -c .)" -eq 1 ] || echo "STOP: not exactly one core_api container"
core() { docker exec "$container" python -m app.cli "$@"; }
```

第一行不对、或容器不是恰好一个，就停下，按 [OPS_MONITORING.md](OPS_MONITORING.md) 第 6.1 节查。之后的 `core …` 都在同一个 shell 里跑。

**在 infra_mysql 里以 root 跑 SQL**：root 密码只在容器里取自它自己的环境变量（与 `deploy/deploy.sh` 的迁移前备份同一做法），不出现在主机的命令行与历史里：

```bash
mysql_root() { docker exec -i infra_mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --user=root "$@"' sh "$@"; }
```

---

## 1. 首次上线（设计第 1.3 节）

前提：CORE-TASK-067、068 已合并（实例片段与可选生产的部署工作流）。

### 1.1 建生产库与账号

照 `vps_infra/scripts/provision-project.sh` 在 infra_mysql 里建生产库与账号（用法见 `vps_infra`）。

- 它授本库全部权限，**密码只在终端打印一次**：直接填进第 1.3 步的 `.env`，不另存。
- 它打印的连接串主机名，就是专用数据网络上 infra_mysql 的别名（设计第 1.1 节），原样用。
- 它还会分 Redis 段，Core 用不上，不用管。

### 1.2 建专用数据网络

由 `vps_infra` 建生产 Core 的专用数据网络 `infra_mysql_core_production_net`（`--internal`），并把 infra_mysql 用 `docker network connect --alias mysql` 接上（设计第 1.1、4 节、P-07）。
做法与对 compose 文件的同步以 `vps_infra` 的 PR 与说明为准。

核对（这条网络不存在时，生产部署会以失败结束，不会自己建）：

```bash
docker network inspect infra_mysql_core_production_net --format '{{.Internal}} {{range .Containers}}{{.Name}} {{end}}'
```

应打出 `true`，成员里有 `infra_mysql`（生产 Core 部署之后还会有它自己）。

### 1.3 生产检出与 `.env`

在服务器上，以部署账号：

```bash
git clone <仓库克隆地址> <生产检出目录>
cd <生产检出目录>
```

用编辑器（不要在命令行上 `echo` 密钥）照 `.env.example` 写 `.env`，其中：

```
CORE_ENV=production
CORE_DATABASE_URL=<生产库连接串>
```

- `CORE_ENV` 那一行恰好是 `CORE_ENV=production`：不加引号、空格与行尾注释，只写一行（`deploy/instance.sh` 严格匹配）。
- 运维令牌与心跳地址（`CORE_MONITOR_TOKEN`、`CORE_HEALTHCHECK_*_URL`）之后按 [OPS_MONITORING.md](OPS_MONITORING.md) 第 1–3 节加（令牌在第 1.5 步签发）。
- 写好后按设计 P-05，把**去掉令牌那几行**的 `.env` 存进密码管理器的那条安全笔记。

在 GitHub 仓库设置里加 secret `VPS_APP_DIR_PRODUCTION`，值为 `<生产检出目录>`。

### 1.4 手工触发部署，选生产

1. 取当前 main 的 SHA：`git fetch origin && git rev-parse origin/main`。
2. GitHub → Actions → **Deploy** → Run workflow：`instance` 选 `production`，`ref` 填上面的 40 位 SHA。
3. 等运行以 success 结束。失败时看部署 job 的输出，不要在服务器上手工跑 compose。
4. 在服务器上核对两个项目都在：

   ```bash
   docker compose ls
   ```

   应同时列出 `acuven_stock_core`（Demo）与 `acuven_stock_core_production`（生产）。

### 1.5 开通与签发

在 `<生产检出目录>` 里，先按第 0.2 节找到生产的容器并定义 `core`，然后依次开通（每条命令输出一行 JSON，记下其中的 ID 给下一条用）：

```bash
core tenant create --name "<租户名>"
core location create --tenant <租户 ID> --name "<地点名>"
core pool create --tenant <租户 ID> --location <地点 ID> --name "<池名>"
core channel create --tenant <租户 ID> --pool <池 ID> --name "Inventory"
core show --tenant <租户 ID>
```

签发三个凭据，**每个各用一次 `credential issue`**。每次得到一个新凭据和它的第一个令牌，输出里的 `token` 只出现这一次：

```bash
# Inventory 的业务凭据：绑 Inventory 入口，--scope 按 Inventory 需要的 scope 逐个写（不能带 demo.reset）
core credential issue --tenant <租户 ID> --channel <入口 ID> --name "Inventory" \
    --scope <scope> --scope <scope> --actor <操作人引用> --reason "Inventory business credential"

# 两个运维凭据：只带 ops.read，不带 --channel，名字分开
core credential issue --tenant <租户 ID> --name "Stock core monitor" \
    --scope ops.read --actor <操作人引用> --reason "Ops monitor"
core credential issue --tenant <租户 ID> --name "Inventory reconciliation" \
    --scope ops.read --actor <操作人引用> --reason "Inventory daily reconciliation"
```

- 业务令牌与对账令牌直接写进 Inventory 服务器的密钥文件；巡检令牌写进 `<生产检出目录>/.env` 的 `CORE_MONITOR_TOKEN`（[OPS_MONITORING.md](OPS_MONITORING.md) 第 2、3 节）。都用编辑器写，不另存文件。
- 记下两个运维凭据的 `credential_id` 各归谁（巡检、对账），写进密码管理器那条安全笔记（只记 ID，不记令牌）。
- **不用 `credential rotate` 给已有凭据添第二个令牌**：`rotate` 会给同一凭据下其余的令牌设上截止时间（设计第 1.1 节）。每个凭据平时只有一个令牌。
- 某次输出丢了：先吊销那个令牌，再在**同一个凭据下**轮换一次（[OPS_MONITORING.md](OPS_MONITORING.md) 第 2 节）；先吊销，所以这个凭据下仍只有一个有效令牌：

  ```bash
  core token revoke --token <令牌 ID> --actor <操作人引用> --reason "Token output lost at issue"
  core credential rotate --credential <凭据 ID> --overlap-hours 0 --actor <操作人引用> --reason "Token output lost at issue"
  ```

核对：

```bash
core credential list --tenant <租户 ID>
```

应有三个 `active` 的凭据，每个下面恰好一个令牌，`expires_at` 与 `revoked_at` 都为空。例如两个运维凭据 `cred_M`（巡检，令牌 `ctk_M1`）与 `cred_R`（对账，令牌 `ctk_R1`）互不影响。

**（0.22，待批准）Inventory 并入之后的签发**：上面的三个凭据里只签巡检那一个（`Stock core monitor`）；**不签** `Inventory` 业务凭据与 `Inventory reconciliation` 对账凭据。另用命令行的内部签发方式签一个 Inventory 的**内部凭据**：绑 `<入口 ID>`，scope 为 `catalog.read`、`catalog.write`、`stock.read`、`stock.write`、`orders.read`、`orders.write`，`--actor`、`--reason` 照常；它没有令牌，输出里不会有 `token`（具体写法以实现它的任务为准）。
核对：`core credential list --tenant <租户 ID>` 应有两个 `active` 的凭据——巡检凭据下恰好一个令牌；内部凭据标为内部、下面没有令牌。
已经按上面签过 Inventory 业务凭据或对账凭据的，签好内部凭据后用 `core credential revoke --credential <凭据 ID> …` 吊销它们。

### 1.5b 开放 Inventory 的公网入口（0.22，待批准）

前提：契约 0.22 批准、生产实例改挂代理网络的部署任务已合并并部署（设计第 1.2 节第 1 条的 0.7 一项）。真实域名、vhost 文件名与路径都不写进本仓库、工单与对话。

1. 由 `vps_infra` 给生产实例开 vhost（做法以 `vps_infra` 的说明为准），要求：
   - 只转发 `/admin/` 与 `/api/` 两个前缀到生产 Core 在代理网络上的别名 `stock_core_production` 的 8000 端口，其余路径（含 `/v1`、`/healthz`、`/readyz`）一律 404；
   - `proxy_set_header X-Real-IP $remote_addr`（覆盖，不是追加）；
   - TLS 用现有的通配符证书。
2. 在 Cloudflare 配子域名。
3. 生产检出的 `.env` 加一行 `CORE_INVENTORY_ENABLED=true`（只写这一行，不加引号、空格与注释），按第 1.4 步手工触发一次生产部署。
4. 从外网核对：
   - `/api/` 下的一个回调路由能访问到（未授权时回答它自己的错误，不是代理的 404）；
   - `/v1/ops/invariants`、`/healthz`、`/readyz` 都是 404；
   - 带一个伪造的 `X-Real-IP` 头登录失败几次，在库里看 `admin_login_attempts` 的 `source_key` 没有随头变化（后台登录设计第 5.3 节）。
5. 在服务器上核对：`docker network inspect <代理网络> --format '{{range .Containers}}{{.Name}} {{end}}'` 里有生产的 `core_api` 容器；`data_net` 里没有。

之后按 [OPS_MONITORING.md](OPS_MONITORING.md) 装生产的巡检（心跳检查、`.env`、cron 与演练）。

### 1.6 真实期初之前的核对

四项都过了才做真实期初：

1. 第 4 节的演练通过（契约 §12.1 第 7 条），记录写进第 5 节。
2. 设计第 2.6 节的心跳检查已建好并演练过：生产的两个检查按 [OPS_MONITORING.md](OPS_MONITORING.md) 第 5 节；`infra_mysql backup` 按 `vps_infra` 的说明。
3. infra_mysql 的 binlog 设置：

   ```bash
   mysql_root -e "SHOW VARIABLES WHERE Variable_name IN ('binlog_format', 'binlog_row_image', 'gtid_mode')"
   ```

   应为 `binlog_format=ROW`、`binlog_row_image=FULL`、`gtid_mode=OFF`。不是就停下，先由 `vps_infra` 处理。
4. 内存与磁盘余量（P-02）：

   ```bash
   free -h
   df -h
   ```

   记下结果；余量不够就停下，先定扩容。

---

## 2. 升级与回滚的界线（设计第 1.4 节）

生产只由人手工触发 Deploy 工作流升级：`instance` 选 `production`，`ref` 填目标 SHA。合并到 main 只自动部署 Demo。

### 2.1 升级前：列出迁移，按“先加后删”核对

1. 取生产当前的 SHA（镜像标签就是部署的 SHA）。在 `<生产检出目录>` 里按第 0.2 节找到容器后：

   ```bash
   docker inspect "$container" --format '{{.Config.Image}}'   # 冒号后面的 40 位就是 <生产当前 SHA>
   ```

2. 列出两个 SHA 之间的迁移：

   ```bash
   git fetch origin
   git log --reverse --format='%H %s' <生产当前 SHA>..<目标 SHA> -- alembic/versions/
   git diff --name-status <生产当前 SHA> <目标 SHA> -- alembic/versions/
   ```

3. 逐个看迁移的内容，按契约 §12.1 第 5 条“先加后删”核对：**任何“删”的那一段，都要它对应的“加”那一段已经在生产上跑过一次**（即“加”那一段在 `<生产当前 SHA>` 里已有）。
   不满足时，先把生产升到“加”已进库、“删”还没进库的那个中间提交，确认上线成功后，再升到目标 SHA。

### 2.2 触发时机

**没有 Demo 部署在跑时才触发生产**：先在 GitHub → Actions → Deploy 里看没有进行中或排队中的运行。
两个实例的并发组是分开的，只靠服务器上的主机部署锁串行；生产部署（含迁移前备份）持锁较久，同时到来的 Demo 推送部署可能等不到锁而失败，OpenClaw 会把那次合并判为部署失败。

触发后等运行以 success 结束（工作流的上线后健康检查会核对生产报出的就是这个 SHA）。

### 2.3 回滚

- **只能回到上一个好的 SHA**：即上一次成功的生产手工部署运行里部署的 SHA。用同样的手工触发，`instance` 选 `production`，`ref` 填它。
- **不早于实例片段进库的那个提交**：即 `.github/workflows/deploy.yml` 里 `PRODUCTION_BASE_SHA` 那个提交（CORE-TASK-067 的合并提交）。早于它的 SHA 工作流会拒绝、检出不动。可以先在本地核对：

  ```bash
  git merge-base --is-ancestor <PRODUCTION_BASE_SHA 的值> <要回滚到的 SHA> && echo ok
  ```

- **已经跑过的迁移不倒退**：回滚只换代码，不降库结构。所以只能回到与当前表结构兼容的那个上一个好的 SHA（“先加后删”保证上一个版本能对着新表结构工作）。

---

## 3. 整机恢复（设计第 2.4 节）

**计时**：从“新开一台机器”开始，到“调用方补交、对账完成”为止，目标 4 小时内（契约 §12.1 第 3 条）。每一步开始与结束都记下 UTC 时间，最后写进第 9 步的记录。

下面 SQL 与 `mysqlbinlog` 里的时间一律按 **UTC**。

### 3.1 开机器、取密钥

1. 开新机器，装 Docker。
2. 从密码管理器取回重建要用的密钥（设计 P-05 的清单）：R2 凭据、`vps_infra` 的 `MYSQL_ROOT_PASSWORD`、源站证书、两个 Core 检出与 Demo 网店、Inventory 的 `.env`、心跳地址。（0.22，待批准：“Inventory 的 `.env`”改为生产 Core 检出里 Inventory 模块的密钥——平台应用凭据与令牌加密主密钥。）
   这些 `.env` 里没有令牌那几行（存的时候去掉了），令牌在第 3.7 步重新轮换出来。

### 3.2 起基础设施

从 `vps_infra` 起 infra_mysql、nginx、redis，建网络（含 `data_net` 与第 1.2 节的 `infra_mysql_core_production_net`），做法以 `vps_infra` 的说明为准。

- infra_mysql 第一次启动时**不挂 `mysql/init/`**，免得建出占位密码的账号（设计第 2.3 节）。

### 3.3 导入全量

1. 从 R2 取**最近一份时间点恢复用的全量导出**的 tar（路径 `infra_mysql/YYYY/MM/…`）到 `<恢复工作目录>`，解开后有三样：全量导出、记 `server_uuid` 与批次的小文件、账号文件。
2. 记下小文件里的 `<server_uuid>` 与 `<批次>`；从全量导出文件开头的注释里记下 binlog 位置：

   ```bash
   head -n 60 <全量导出文件> | grep -E 'CHANGE (MASTER|REPLICATION SOURCE) TO'
   ```

   注释里的文件名是 `<起始文件>`（例如 `binlog.000123`），位置是 `<位置>`。
3. **先导账号**（每个账号先删后建，再按导出的密码哈希授权），**再导全量**。命令照 `vps_infra` 的还原说明。

### 3.4 重放 binlog

1. 从 R2 取**该 `server_uuid`、该批次下**的全部 binlog：

   ```bash
   mkdir -p <恢复工作目录>/binlog
   rclone copy <R2 远端>:<桶>/infra_mysql_binlog/<server_uuid>/<批次>/ <恢复工作目录>/binlog/
   ```

2. 挑出起始文件及其后的全部文件，按序号排好，核对**第一个是 `<起始文件>`、序号连续没有缺号**：

   ```bash
   cd <恢复工作目录>/binlog
   ls binlog.[0-9]* | LC_ALL=C sort | awk -v s=<起始文件> '$0 >= s' > ../replay_files.txt
   cat ../replay_files.txt
   ```

   有缺号就停下：缺号之后的文件不能接着重放。

3. 用**一次** `mysqlbinlog` 按序号顺序重放，从 `<位置>` 起，**不加库过滤**（不加 `--database`），按位置恢复（`gtid_mode` 为 OFF），`TZ=UTC`：

   ```bash
   docker exec infra_mysql mkdir -p /tmp/replay
   while read -r f; do docker cp "$f" infra_mysql:/tmp/replay/; done < ../replay_files.txt
   docker exec -e TZ=UTC infra_mysql sh -c \
       'cd /tmp/replay && mysqlbinlog --start-position="$1" $(ls | LC_ALL=C sort) | MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --user=root' \
       sh <位置>
   ```

   以非 0 退出就停下查明原因，不跳过出错的事件。重放完删掉容器里的 `/tmp/replay`。

### 3.5 Core 专门步骤：调 `catalog_changes` 的自增起点

**两个实例的库**（`<生产库名>` 与 `<Demo 库名>`）各做一次，在起 Core 之前（契约 §2.5、§12.1 第 3 条）。新起点 = 恢复后的最大 `id` + 1,000,000，表为空时最大 `id` 按 0 算。

```bash
for db in <生产库名> <Demo 库名>; do
    start="$(mysql_root --batch --skip-column-names -e "SELECT COALESCE(MAX(id), 0) + 1000000 FROM \`$db\`.catalog_changes")"
    echo "$db new AUTO_INCREMENT: $start"
    mysql_root -e "ALTER TABLE \`$db\`.catalog_changes AUTO_INCREMENT = $start"
    mysql_root -e "SHOW CREATE TABLE \`$db\`.catalog_changes\G" | grep -o 'AUTO_INCREMENT=[0-9]*'
done
```

**核对**：每个库 `SHOW CREATE TABLE` 打出的 `AUTO_INCREMENT=` 等于上面算出的新起点。不等就停下，不起 Core。

> **调完之后不能再重放 binlog**：后来又找到更多 binlog 也不能补。那段写入按第 3.8 步由调用方补交、补录。

### 3.6 先把新机器保护起来

**在开放任何写入之前**（起 Core 与调用方之前）：

1. 做一次时间点恢复用的全量导出（设计第 2.3 节），用 `vps_infra` 每晚备份里的那段，手工跑一次，传到 R2。
2. 新机器的 `server_uuid` 变了：

   ```bash
   mysql_root --batch --skip-column-names -e "SELECT @@server_uuid"
   ```

   在上传配置里记下新的 `server_uuid` 与新批次（启用时刻的 UTC，例如 `20261125T0300Z`），再启用 binlog 上传 cron 与每晚备份（做法见 `vps_infra`）。

之后的补交、补录可能要几个小时，这段时间的写入要有异地备份。

### 3.7 起 Core 与调用方

**定 SHA**（不用 main）：

- 生产：最后一次**成功的生产手工部署**运行里的 SHA（生产落后 main）。
- Demo：最后一次**成功的 Demo 部署**运行里的 SHA（`paths-ignore` 跳过的提交没有部署运行，GHCR 里也就没有它们的镜像）。

在 GitHub → Actions → Deploy 里找：打开成功运行的 deploy job，Deploy 步骤的输出里 `holding the host deploy lock (instance production)`（或 `demo`）说明是哪个实例，`deployed <SHA>` 就是部署的 SHA。
手工触发的运行，列表里显示的提交是触发时 main 的提交，不一定是部署的 SHA，以日志里的为准。

**登录 GHCR**（镜像在 GHCR，平时由工作流用它自己的临时令牌登录）：由人在 GitHub 新建一个只带 `read:packages`、7 天内到期的令牌。它不进密码管理器、`.env` 与 P-05 的清单。在新机器上从标准输入登录：

```bash
read -rs GHCR_READ_TOKEN    # 粘贴刚建的令牌，不回显、不进历史
printf '%s' "$GHCR_READ_TOKEN" | docker login ghcr.io -u <GitHub 账号> --password-stdin
unset GHCR_READ_TOKEN
```

**部署两个实例**（工作流的 secrets 还指着旧机器，所以直接在机器上跑 `deploy/deploy.sh`）。每个实例：

```bash
git clone <仓库克隆地址> <生产检出目录>
cd <生产检出目录>
# 用编辑器按 P-05 写回 .env（从密码管理器取回的那份；令牌那几行这时还没有）
git checkout --detach <生产 SHA>
CORE_IMAGE_REPO=ghcr.io/<所有者>/<仓库名> bash deploy/deploy.sh <生产 SHA>
```

Demo 同样做，目录换 `<Demo 检出目录>`，SHA 换 `<Demo SHA>`。两个实例部署完：

```bash
docker logout ghcr.io
```

并在 GitHub 删掉这个令牌。

- 拉不到镜像就停下查明原因：**不换 SHA**（库里的迁移停在这个 SHA），也**不在新机器上自己构建**。这两个 SHA 都来自成功的部署运行，那次运行构建过镜像并推到了 GHCR，本仓库不清理 GHCR 的镜像，所以应该还在。
- `docker compose ls` 应同时列出两个项目。

**轮换令牌**：库里的旧令牌还有效，但没人持有明文。每个凭据**各在自己的凭据下**轮换一次，重叠 0 小时，旧令牌即刻失效。**不换新凭据**：换凭据会改变幂等作用域（契约 §10 第 4 条），调用方就没法按原键补交“结果未知”的命令。

要轮换的凭据：

| 实例 | 凭据 | 新令牌写回 |
| --- | --- | --- |
| Demo | Demo 网店的业务凭据 | Demo 网店的密钥位置 |
| 生产 | Inventory 的业务凭据 | Inventory 服务器的密钥文件 |
| 生产 | 巡检的运维凭据 | `<生产检出目录>/.env` 的 `CORE_MONITOR_TOKEN` |
| 生产 | Inventory 对账的运维凭据 | Inventory 服务器的密钥文件 |

（0.22，待批准）Inventory 并入之后，上表生产的两行 Inventory 凭据不存在；Inventory 的内部凭据没有令牌，不用轮换。生产上只轮换巡检的运维凭据。

在对应的检出目录里，按第 0.2 节找到**本实例**的容器并定义 `core`，然后：

```bash
core credential list --tenant <租户 ID>          # 按名字与第 1.5 步记下的 credential_id 认出每个凭据
core credential rotate --credential <凭据 ID> --overlap-hours 0 \
    --actor <操作人引用> --reason "Whole-machine recovery: token plaintext lost"
```

- 每个凭据跑一次 `rotate`，每次输出里的 `token` 只出现这一次，直接用编辑器写回上表的位置。
- 输出的 `expired_tokens` 应只有这个凭据自己的旧令牌，`expires_at` 为轮换时刻。
- 每个凭据平时只有一个令牌，轮换一个凭据不影响别的凭据。例子：之前 `cred_M` 下 `ctk_M1`、`cred_R` 下 `ctk_R1` 都有效、明文都已丢；轮换 `cred_M` 得 `ctk_M2`（写进巡检 `.env`），`ctk_M1` 即刻失效；再轮换 `cred_R` 得 `ctk_R2`（写进 Inventory 密钥文件），`ctk_R1` 即刻失效；第二次轮换不动 `ctk_M2`。
- 全部轮换完再 `core credential list` 核对：每个凭据下只有新令牌的 `expires_at` 为空。

生产的巡检按 [OPS_MONITORING.md](OPS_MONITORING.md) 第 3、4 节确认 `.env` 并装 cron。

**再起调用方**：网店、Inventory 等，按各自的部署说明。（0.22，待批准：Inventory 在生产 Core 的进程里，随上面的生产部署一起起来，不另起；`.env` 里的 `CORE_INVENTORY_ENABLED=true` 随 P-05 的那份写回。）

### 3.8 补交与补录（契约 §12.1 第 4 条）

1. 调用方补交“结果未知”的命令，补录丢失窗口：网店按支付记录；Inventory 按两个平台的订单补拉，人工模式下由管理员重新核对卖家后台、在 Inventory 重新登记。
   （0.22，待批准）Inventory 改为：先让它对“已记下、还没落定”的调 Core 步骤同键重放（它的定时作业会做，在后台确认没有积下的步骤），再从丢失窗口的起点补拉平台订单。
2. 网店与 Inventory 都用 `GET /v1/skus?ids=…` 重读映射，Core 查不到的 SKU 标为待处理。（0.22，待批准：Inventory 用 Core 的读函数重读，不经 `/v1`。）
3. 然后对账，跑一次不变量自查。生产上在 `<生产检出目录>` 里手工跑一轮巡检：

   ```bash
   bash deploy/monitor.sh; echo "exit $?"
   ```

   标准错误里不应有以 `P1 invariants` 或 `P2 invariants` 开头的行（判读见 [OPS_MONITORING.md](OPS_MONITORING.md) 第 6.3 节）。

到这里停表，记下第 3.1–3.8 步的总用时。

### 3.9 收尾

1. DNS 与 Cloudflare 指到新机器。
2. 改 GitHub secrets：`VPS_HOST`、`VPS_FINGERPRINT` 等指到新机器（检出目录变了的话也改 `VPS_APP_DIR`、`VPS_APP_DIR_PRODUCTION`）。
3. 记下每一步的用时（开始、结束、UTC），连同丢失窗口与发现的问题，写进事故记录。

### 3.10 误操作恢复（契约 §12.1 第 6 条）

误操作（误删、误改）**不在线上恢复**：不加过滤的整段重放会把所有库都退回误操作之前。做法：

1. **起一个临时实例**：另起一个与 infra_mysql 同版本的临时 MySQL 实例（另一台机器，或不接任何业务网络的临时容器），做法照 `vps_infra` 的还原说明。
2. 在临时实例上按第 3.3 步导入误操作之前最近的那份全量导出。
3. 按第 3.4 步第 1–2 步取 binlog、核对序号，并把文件拷进临时实例容器的 `/tmp/replay`。在 binlog 里找到误操作那条事件（`TZ=UTC`，时间按 UTC 读）：

   ```bash
   docker exec -e TZ=UTC <临时实例容器> sh -c \
       'cd /tmp/replay && mysqlbinlog --base64-output=DECODE-ROWS --verbose --start-position="$1" $(ls | LC_ALL=C sort)' \
       sh <位置> | less
   ```

   记下误操作那条事件开始处的 `# at` 位置与它所在的文件，即 `<停止位置>`；或记下它之前的 UTC 时间 `<停止时间>`（格式 `YYYY-MM-DD HH:MM:SS`）。

4. 按第 3.4 步第 3 步重放，但停在误操作之前，二选一：

   ```bash
   # 按停止位置（先在 binlog 里找到那条事件）：--stop-position 作用于最后一个文件，所以只给到误操作所在的那个文件为止
   docker exec -e TZ=UTC <临时实例容器> sh -c \
       'cd /tmp/replay && mysqlbinlog --start-position="$1" --stop-position="$2" $(ls | LC_ALL=C sort | awk -v e="$3" "\$0 <= e") | MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --user=root' \
       sh <位置> <停止位置> <误操作所在的文件>
   # 按停止时间（UTC）
   docker exec -e TZ=UTC <临时实例容器> sh -c \
       'cd /tmp/replay && mysqlbinlog --start-position="$1" --stop-datetime="$2" $(ls | LC_ALL=C sort) | MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --user=root' \
       sh <位置> '<停止时间>'
   ```

   运行 `mysqlbinlog` 的进程一律设 `TZ=UTC`（上面的 `docker exec -e TZ=UTC`）：`--stop-datetime` 按进程所在的时区解释，infra_mysql 容器是马来西亚时间，而库里的时间是 UTC（`--default-time-zone=+00:00`）。

5. 在临时实例上**只把受影响的库**导出（`mysqldump --single-transaction --databases <受影响的库>`），搬回线上。
6. 搬回的是 Core 的库时，第 3.5 步**只对搬回的那个 Core 库做**，并在起它的 Core 之前做：按第 0.2 节找到该实例的容器，`docker stop "$container"`，导入，调自增起点并用 `SHOW CREATE TABLE` 核对，再 `docker start "$container"`。调完之后同样不能再往这个库重放 binlog。
7. 用完删掉临时实例。

---

## 4. 演练（设计第 2.5 节）

上线前一次，之后每季度一次；备份方式或数据库拓扑变化后加做一次（设计第 6 节）。
**在另一台机器上做**：临时开一台云主机（与现在的 VPS 同一家、同规格，P-06），按第 3.1–3.8 步从零恢复，演练完当天删掉。

### 4.1 演练时不动线上

- 演练机**不改 DNS**、**不装任何 cron**，`.env` 里**不填心跳地址**（演练机推成功会把线上真实的故障遮住）。第 3.9 步不做，GitHub secrets 不改。
- 只起 Core 和第 3.8 步要用的调用方：Demo 网店与一套演练用的 Inventory 配置，不连线上的平台。它们的外发凭据（平台、支付、短信、邮件、WhatsApp、Telegram）一律换成假值。
  （0.22，待批准）“演练用的 Inventory 配置”即演练机上的生产 Core 打开 `CORE_INVENTORY_ENABLED`，Inventory 模块的平台凭据与 Telegram 换成假值；演练机不开 vhost。
- 读 R2 用一个**只读令牌**（由人新建，设计第 4 节），演练机的 rclone 配置只放它，不放可写的 R2 凭据。
- 第 3.6 步：只做那次全量导出并计时，导出留在演练机上、随机器一起删；**不传 R2**、不启用 binlog 上传 cron 与每晚备份。
- 第 3.7 步：轮换只发生在演练机恢复出的库里，不影响线上的凭据与令牌。拉镜像照第 3.7 步临时建只读包令牌登录，演练完在 GitHub 删掉。

### 4.2 量丢失窗口：标记表

这台机器业务写入很少，只看最后一条业务事件量不出上传节奏，所以用标记表。

1. **演练前**，在**线上的** infra_mysql 里建一个只为演练用的库与标记表：

   ```bash
   mysql_root -e "CREATE DATABASE \`<演练标记库名>\`"
   mysql_root -e "CREATE TABLE \`<演练标记库名>\`.drill_marks (id BIGINT AUTO_INCREMENT PRIMARY KEY, marked_at DATETIME(6) NOT NULL)"
   ```

   这个库要么在恢复用的那份全量导出里，要么在那份导出之后才建（建库语句在 binlog 里，重放时会一并建出）；哪些库进全量导出以 `vps_infra` 的说明为准。

2. 在**线上服务器**（不是演练机）上装一条每分钟写一行当时 UTC 时间的 cron，例如 `/etc/cron.d/stock_core_drill_marks`（属 root、权限 0644，文件以换行结尾）：

   ```
   * * * * * <运行账号> docker exec infra_mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --user=root -e "INSERT INTO <演练标记库名>.drill_marks (marked_at) VALUES (UTC_TIMESTAMP(6))"'
   ```

3. 选一个时刻 **T（UTC）**当作“机器损坏”，T 要在恢复用的那份全量导出之后。到 T 时立即用只读令牌（例如在演练机上）记下 R2 上的对象清单，**只用这些对象恢复**：

   ```bash
   rclone lsf -R <R2 远端>:<桶>/infra_mysql_binlog/<server_uuid>/<批次>/ > objects-at-T.txt
   rclone lsf -R <R2 远端>:<桶>/infra_mysql/ > full-at-T.txt
   ```

   第 3.3、3.4 步取文件时用 `rclone copy --files-from objects-at-T.txt …`，不取 T 之后才上传的对象。

4. 同时在线上对 `<生产库名>` 逐表数行，留作第 4.3 节对照：

   ```bash
   for t in $(mysql_root --batch --skip-column-names -e "SELECT table_name FROM information_schema.tables WHERE table_schema = '<生产库名>' AND table_type = 'BASE TABLE' ORDER BY table_name"); do
       printf '%s ' "$t"; mysql_root --batch --skip-column-names -e "SELECT COUNT(*) FROM \`<生产库名>\`.\`$t\`"
   done > rows-at-T.txt
   ```

5. 恢复完（第 3.4 步之后），在演练机上取最后一行标记：

   ```bash
   mysql_root --batch --skip-column-names -e "SELECT MAX(marked_at) FROM \`<演练标记库名>\`.drill_marks"
   ```

   丢失窗口 = T − 这个时间。

演练结束后撤掉线上的这条 cron（`sudo rm /etc/cron.d/stock_core_drill_marks`）。

### 4.3 核对项

全部满足才算演练通过：

| 核对项 | 怎么看 |
| --- | --- |
| 丢失窗口 ≤ 5 分钟 | 第 4.2 节第 5 步 |
| 第 3.1–3.8 步总用时 ≤ 4 小时 | 各步记下的 UTC 时间 |
| Core 生产库的行数对得上 | 演练机上同第 4.2 节第 4 步逐表数行，与 `rows-at-T.txt` 对照；有差异要能用 T 前最后一次上传之后的写入说明，记进“发现的问题” |
| 不变量自查 `ok` | 演练机的生产检出里 `bash deploy/monitor.sh`：没配心跳地址，会以非 0 退出并打出 `… is not configured …`；标准错误里不应有以 `P1 invariants` 或 `P2 invariants` 开头的行 |
| `catalog_changes` 自增起点核对通过 | 第 3.5 步两个库的 `SHOW CREATE TABLE` |

演练完：删掉演练机（连同上面的全量导出），在 GitHub 删掉拉镜像用的令牌，撤掉线上的标记 cron；在第 5 节写记录。

---

## 5. 演练记录

每次演练追加一条：日期、执行人、各步用时、丢失窗口、发现的问题。时间一律 UTC。

模板：

```
### YYYY-MM-DD

- 执行人：
- 演练机规格：
- T（UTC）：
- 恢复用的全量导出（R2 路径中的日期部分）与它的大小：
- 各步用时：
  | 步骤 | 开始 | 结束 | 用时 |
  | --- | --- | --- | --- |
  | 3.1 开机器、取密钥 | | | |
  | 3.2 起基础设施 | | | |
  | 3.3 导入全量 | | | |
  | 3.4 重放 binlog | | | |
  | 3.5 调自增起点 | | | |
  | 3.6 全量导出（只导出、不上传） | | | |
  | 3.7 起 Core 与调用方 | | | |
  | 3.8 补交与补录、对账 | | | |
  | 合计（目标 ≤ 4 小时） | | | |
- 丢失窗口（T − 最后一行标记，目标 ≤ 5 分钟）：
- 核对项（第 4.3 节）：丢失窗口 / 总用时 / 行数 / 不变量自查 / 自增起点
- 发现的问题：
```

（尚无记录。）
