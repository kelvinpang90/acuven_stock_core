# 运行告警运行手册（契约 §8.5，OPS-04）

服务器上的 cron 每 5 分钟跑一次 `deploy/monitor.sh`，把三个维度的结论推到心跳服务（Healthchecks），由它通知 Telegram：

| 维度 | 心跳服务里的检查 | 判据（详见契约 §8.5） | 级别 |
| --- | --- | --- | --- |
| `readyz` | `stock_core demo readyz` | 恰好一个运行中的 `core_api` 容器，`GET /readyz` 200 且 `status` 为 `ok` | P1 |
| `periodic` | `stock_core demo periodic` | `/readyz` 正文的 `periodic.status` 为 `ok` | P2 |
| `invariants` | `stock_core demo invariants` | `GET /v1/ops/invariants` 200 且 `ok` 为 `true`；查出违反 P1，自查没跑成 P2 | P1 / P2 |

脚本只读：不改库、不重启容器。有不合格的隔 45 秒整轮复核一次，复核仍不合格才推失败；每个维度每轮都推（合格推成功，不合格推失败并带正文）。
通知正文一行一个问题，以级别开头；不写令牌、心跳地址、`order_ref` 与响应原文。每个不合格也写一行系统日志，标签 `stock_core_monitor`：

```bash
journalctl -t stock_core_monitor --since "1 hour ago"
```

下面的步骤都由人做，部署不会自动做其中任何一步。文中 `<检出目录>` 指服务器上本仓库检出的根目录，`<运行账号>` 指运行巡检的账号
（要能读检出目录里的 `.env`、能用 `docker`）。找 Core 容器的命令（与脚本相同的标签过滤）：

```bash
container="$(docker ps -q --filter label=com.docker.compose.project=acuven_stock_core \
    --filter label=com.docker.compose.service=core_api --filter label=com.docker.compose.oneoff=False)"
```

## 1. 在心跳服务里建三个检查

1. 用现有的心跳服务账号，即计费平台（`ai_billing_hub`）在用的同一个账号（契约 0.18）。**不另开账号**：心跳服务把用多个账号绕过免费档检查数上限视为滥用。
2. 等计费平台合并告警维度、腾出位置之后再建（那边的改动在 `ai_billing_hub` 另做）。建之前先在账号里看一眼剩几个位置，确认够 3 个；不够就停下，不删计费平台的检查。
3. 建三个检查，名字带实例：`stock_core demo readyz`、`stock_core demo periodic`、`stock_core demo invariants`。
   每个检查：周期（Period）5 分钟，宽限（Grace）15 分钟（容得下一次复核加一次错过），通知接 Telegram，接收人与计费平台的相同。
4. 这三个检查只由 Core 的巡检推，不与计费平台共用。以后的生产实例与第二个租户的检查没有空位，到时另定（再合并或升级付费档），也不另开账号。
5. 记下三个检查各自的心跳地址，只写进下面第 3 步的服务器 `.env`，不写进仓库、文档、工单与对话。

## 2. 签发运维凭据

运维凭据只带 `ops.read`、不带销售入口，只查它所属的租户（V1 一个租户、一个令牌）。在服务器上：

```bash
docker exec "$container" python -m app.cli credential issue --tenant ten_… --name "Stock core monitor" \
    --scope ops.read --actor ops-7 --reason "Ops monitor"
```

- 不设截止时间（签发命令本来就不设）。
- 标准输出那行 JSON 里的 `token` 只显示这一次：直接填进下一步的 `.env`，不另存文件。输出丢了就用 `token revoke` 吊销那个令牌，再轮换一次。
- 轮换按契约 §10 第 4 条：`credential rotate --credential cred_… --overlap-hours …`，拿到新令牌后改 `.env` 里的 `CORE_MONITOR_TOKEN`，
  下一轮巡检起用新令牌；重叠期结束前确认 `invariants` 仍是绿的。

## 3. 写服务器 `.env`

在 `<检出目录>/.env` 里加四行（键名见 `.env.example`）：

```
CORE_MONITOR_TOKEN=上一步拿到的令牌
CORE_HEALTHCHECK_READYZ_URL=stock_core demo readyz 的心跳地址
CORE_HEALTHCHECK_PERIODIC_URL=stock_core demo periodic 的心跳地址
CORE_HEALTHCHECK_INVARIANTS_URL=stock_core demo invariants 的心跳地址
```

- 值不加引号，不写行尾注释（脚本不认行尾注释，`#` 之后的字会被当成值的一部分）。每个键只写一行（出现多次时脚本取最后一个）。
- 脚本按行只读这四个键，不 `source` 整个文件；应用容器不读它们。
- 编辑时不要把令牌或地址写在命令行上（会进 shell 历史），用编辑器改。
- 某个心跳地址没配时，该维度的结论不会通知任何人：脚本每轮记一行“`<键名> is not configured; … will not notify anyone`”并以非 0 退出。

## 4. 装 cron

`deploy/monitor.cron` 是模板，检出目录与运行账号是占位符。装到系统的 `/etc/cron.d`：

1. 文件名只用字母、数字、下划线、连字符（cron 会忽略带点的文件名，例如 `monitor.cron`），例如 `stock_core_monitor`；
2. 属 root、权限 0644（cron 不认组或其他人可写的文件）：

   ```bash
   sudo install -o root -g root -m 0644 deploy/monitor.cron /etc/cron.d/stock_core_monitor
   sudoedit /etc/cron.d/stock_core_monitor   # 把 <CHECKOUT_DIR> 与 <RUN_AS_USER> 换成实际值
   ```

3. **不用 `crontab <文件>`**：那会整个替换该账号的 crontab，把账号里别的任务冲掉。
4. cron 每分钟检查 `/etc/cron.d` 的变化，不用重启。文件最后一行要以换行结尾（模板已是）。
5. 那一行用 `bash` 显式调用脚本，不依赖文件的执行权限；不要给它加互斥锁（巡检只读，重叠无害，一次卡死却会把之后每一轮都挡在外面）。

部署（`deploy/deploy.sh`）不碰 cron；检出目录随部署更新时，cron 跑的就是最新的脚本。

## 5. 装好后确认与演练

### 5.1 确认 cron 在推

装好后先不要手工跑脚本，这样收到的心跳只可能来自 cron。5 分钟内，三个检查都应收到一次心跳、变成绿的（心跳服务的检查页会显示最近一次心跳的时间）。
同时在系统日志里确认是 cron 以运行账号起的：

```bash
journalctl -u cron --since "10 minutes ago" | grep monitor.sh
```

有检查没收到：看第 6.4 节。

### 5.2 错令牌演练

用一个错的令牌手工跑一轮，确认 `invariants` 会变红、Telegram 收得到。不改真实的 `.env`，不为演练停容器或改库。以 `<运行账号>` 在 `<检出目录>` 里：

```bash
umask 077
drill_env="$(mktemp)"    # 只有运行账号可读（0600）；里面有真实的心跳地址
grep -E '^CORE_HEALTHCHECK_(READYZ|PERIODIC|INVARIANTS)_URL=' .env >"$drill_env"
echo 'CORE_MONITOR_TOKEN=csk_drill_not_a_real_token' >>"$drill_env"
CORE_MONITOR_ENV_FILE="$drill_env" bash deploy/monitor.sh; echo "exit $?"
rm -f "$drill_env"
```

预期：

- 退出码 1；标准错误与系统日志里有 `P2 invariants: not checked: HTTP 401`；`readyz`、`periodic` 照常推成功；
- `stock_core demo invariants` 变红，Telegram 收到这条通知；
- 首轮加复核各请求一次自查，Core 会记 2 条 `invalid_credential` 安全事件（契约 §7.2），是预期的，不用处理；
- 演练完立刻删掉临时 `.env`（上面最后一行）；下一轮由 cron 用真实 `.env` 跑时 `invariants` 恢复，Telegram 收到恢复通知。没恢复就照第 6.3 节查。

## 6. 维度变红时怎么查

先看通知正文与系统日志（`journalctl -t stock_core_monitor`）里是哪一行。要再看一眼时可以手工跑一轮（`bash deploy/monitor.sh`，会照常推心跳），
结论打在标准错误上。正文里的原因只有固定的几种：容器数不对、超时、HTTP 状态码、正文读不出、没配令牌。

### 6.1 `readyz`（P1）

- `found 0 running core_api containers`：栈没起来。`docker compose ps` 看容器状态，`docker logs` 看最近的启动输出；
  部署正在换容器时首轮会失败、复核通常已恢复，复核后仍红说明部署失败或回滚没起来，看部署任务的输出。
- `found 2 …`：同一服务有两个运行中的容器（不含部署跑迁移的一次性容器），先查是谁起的，留下当前版本的那个。
- `timed out finding the core_api container`、`docker ps failed`：Docker 守护进程不响应，先看 `systemctl status docker`。
- `/readyz returned HTTP 503`：Core 连不上数据库；查共享数据库是否在跑、Core 的数据库账号与网络。
- `not checked: timed out`、`response unreadable`、`status is not ok`：容器在但应用不响应，`docker logs "$container"` 看进程日志。

### 6.2 `periodic`（P2）

正文 `stale tasks:` 后面是停转的任务名（判据见契约 §8.5：线程不在跑、最近一次运行抛了异常，或太久没正常结束）。
按任务名在进程日志里找原因：`docker logs --since 1h "$container" 2>&1 | grep 'periodic task'`。

- 只有 `reservation-expiry`：偶尔一轮失败会被复核滤掉；持续红时多半是数据库问题，按日志修好后下一轮正常结束就自己恢复。
- **只有 `security-event-purge`**：清理失败一次就一直 `stale` 到下次正常结束（最长约 24 小时），期间 `periodic` 一直红，
  **到期扫描再停转也不会另行通知**。所以要先按进程日志查清失败原因并处理，再用重启（`docker restart "$container"`）或下次部署清掉；
  清理在启动后 60 秒就跑一次，重启后约 1 分钟内若仍失败会再次报出。不查原因就重启，只是把问题藏一天。
- `/readyz body has no periodic`：跑的是 0.17 之前的镜像（例如回滚了），重新部署当前版本。
- `not checked: …`：同 6.1，`/readyz` 本身没取到。

### 6.3 `invariants`

- **P1 `violation_count=…`**：账面不一致。正文列出各 `check` 的项数与最多前 5 项的 ID（`violation_count` 超过 100 时注明只数了返回的前 100 项）。
  用运维凭据取完整结果，按 ID 定位（契约 §4.4“查出违反怎么办”）：`pool_id`＋`sku_id` 用 §6.8 的流水查询 `GET /v1/ledger`，
  `reservation_id` 用 `GET /v1/reservations/{id}`，再用 §5.8 的订单库存查询 `GET /v1/orders/{order_ref}/stock`。自查只读，不会修任何东西。
- **P2 `not checked: …`**（自查没跑成）：
  - `CORE_MONITOR_TOKEN is not configured`：`.env` 里没有这个键或值为空，或 `.env` 不存在、运行账号读不了；
  - `HTTP 401`：令牌错、已吊销或已过轮换重叠期，按第 2 节轮换并改 `.env`；`HTTP 403`：凭据不带 `ops.read`；
  - `HTTP 503`：自查时数据库出错，同 6.1 的 503；
  - `timed out`：自查超过 45 秒，进程日志里 `invariant check: … duration_ms=…` 有每次的用时；持续到 30 秒量级时按契约 §4.4 另行设计；
  - `response unreadable`：响应不是预期的形状，多半是版本不匹配，看 `docker logs`。

### 6.4 没收到心跳（检查因超时变红，正文为空）

脚本没跑或跑不完：cron 没装好（看 `journalctl -u cron`、`/etc/cron.d` 里文件名与权限）、机器挂了、脚本卡住。
系统日志里有 `heartbeat for … not delivered` 时是推心跳失败（网络或心跳服务的问题），脚本的结论不受影响，下一轮会再推。
有 `… is not configured; … will not notify anyone` 时按第 3 节补 `.env`。

## 不在 V1

命令失败率、积压、数据库连接池与备份状态的监控；第二个租户的自查（每个租户一个令牌、一个 `invariants` 检查，到时另行设计）。
