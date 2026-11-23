# 后台管理员账号、密码登录与会话（V1 设计补充）

> 版本：0.1 · 日期：2026-11-23 · 状态：**Kelvin 2026-11-23 批准全文**（含第 7 节的取舍与残余风险）。批准的是设计；INVENTORY-TASK-001～004 按登记的契约逐个开启（CLAUDE.md「碰个人数据的任务先出设计」）。
> 依据：[V1_MINIMAL_DESIGN](V1_MINIMAL_DESIGN.md) 第 2 节（登录限流用库表计数，不用 Redis）、第 10 节（V1 只有管理员一个角色；登录页：密码登录、限流、记录异常尝试）、第 12 节（独立账号、密码登录、带限流；变更记操作者）；
> [REQUIREMENTS](REQUIREMENTS.md) 第 5 节「安全」（独立账号、不与他人共用、登录限流、记录异常尝试、支持立即停用、首期不做双重验证）。
> 取值与做法尽量沿用 acuven-shop 已获批、已上线的管理员登录（同样是标准库 scrypt、按来源与用户名组合锁定、服务端会话），不同处与接受的残余风险在第 7 节列出。
> 本文是 V1_MINIMAL_DESIGN 的补充，只管后台登录；与它冲突时以 V1_MINIMAL_DESIGN 为准。设计有改动时，改动部分的批准作废、须重新批准。

分四个任务实现，每个只做一层：

| 任务 | 层 | 本文章节 |
| --- | --- | --- |
| INVENTORY-TASK-001 | 数据模型与第一条迁移 | 第 3 节 |
| INVENTORY-TASK-002 | 规则与服务（密码、锁定、登录、会话、账号变更） | 第 4.1–4.5 节 |
| INVENTORY-TASK-003 | 服务器上的账号命令 | 第 4.6 节 |
| INVENTORY-TASK-004 | 登录、会话、退出接口与后台共用的依赖 | 第 5 节 |

---

## 1. 范围

| 做 | 不做（本文） |
| --- | --- |
| 管理员账号（可以有多个，每人一个，同一个角色）、密码哈希、服务器上的建号与改密命令、停用与恢复 | 网页注册、网页改密或找回密码、邮件或短信、双重验证（REQUIREMENTS 第 5 节：列为后续增强） |
| 密码登录、服务端会话、退出、取当前会话 | 多角色与权限分级（V-06：只有管理员） |
| 按「来源 + 提交的用户名」与按来源的登录失败锁定，每次登录尝试记一条 | 业务变更的审计记录（数量、配额、映射、连接的操作者与原因由各业务任务写；它们从第 5.4 节的依赖拿到操作者） |
| 之后所有后台接口共用的「须为已登录管理员」依赖与 CSRF 校验 | 登录页前端（随「后台前端骨架」任务做）；登录尝试的查看与过期清理（已列为待登记，见第 6 节） |

## 2. 个人数据

- 账号只存用户名，不存姓名、电话、邮箱。用户名是登录标识，建议用不含真实姓名的短名。
- 登录尝试**不存来源地址原文、不存提交的用户名原文**，只存两个 SHA-256 摘要：`throttle_key`（来源与用户名，4.2）和 `source_key`（只有来源，4.2），用于计数。
  IPv4 地址空间小，这两个摘要是假名化不是匿名化：能读库的人可以穷举出来源地址。所以它们和账号表一样只在库里，不进日志、不进接口响应（为什么不加密钥，见第 7 节）。
- 密码只存 scrypt 哈希；会话令牌只存 SHA-256；CSRF 令牌不入库（4.4）。三者的原文都不进日志、异常消息、命令输出。

## 3. 数据模型（INVENTORY-TASK-001）

沿用 `app/db.py` 的 `Base` 与命名约定；指向账号的外键一律 `ON DELETE RESTRICT`。

**时间**：一律是不带时区的 UTC、**精确到整秒**（`DateTime`，即 MySQL 的 `DATETIME`，不带小数秒）。由此存进库的时间与算出来的时间完全相同，4.2 的窗口、4.4 的到期与 5.1 的 `expires_at` 不会差一秒。
取当前时间的函数只有一个：`utc_now()`，在 `app/services/admin_auth.py`（002 写），返回 `datetime.now(UTC).replace(tzinfo=None, microsecond=0)`（截断，不四舍五入）；命令（4.6）与接口（第 5 节）都调它，不另写。
规则与服务函数接受的 `now` 参数必须是不带时区（`tzinfo` 为 `None`）的整秒，带时区或带小数秒时报 `ValueError`。

**`admin_accounts`**

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | Integer | 主键，自增 |
| `username` | String(32) | 非空；唯一 `uq_admin_accounts_username`；检查 `ck_admin_accounts_username_length`：`LENGTH(username) >= 3 AND LENGTH(username) <= 32`。字符集规则在规则层（4.1） |
| `password_hash` | String(255) | 非空 |
| `created_at` | DateTime | 非空 |
| `password_updated_at` | DateTime | 非空 |
| `disabled_at` | DateTime | 可空；非空 = 已停用 |

**`admin_sessions`**

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | Integer | 主键，自增 |
| `admin_account_id` | Integer | 非空；外键 → `admin_accounts.id`（RESTRICT）；普通索引 |
| `token_hash` | String(64) | 非空；唯一 `uq_admin_sessions_token_hash`；检查 `ck_admin_sessions_token_hash_length`：`LENGTH(token_hash) = 64` |
| `created_at` | DateTime | 非空 |
| `expires_at` | DateTime | 非空；检查 `ck_admin_sessions_expires_after_created`：`expires_at > created_at` |
| `revoked_at` | DateTime | 可空；非空 = 已撤销 |

**`admin_login_attempts`**

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BigInteger | 主键，自增 |
| `occurred_at` | DateTime | 非空 |
| `throttle_key` | String(64) | 非空；检查 `ck_admin_login_attempts_throttle_key_length`：`LENGTH(throttle_key) = 64`；与 `occurred_at` 组成普通复合索引（列顺序 `throttle_key`、`occurred_at`） |
| `source_key` | String(64) | 非空；检查 `ck_admin_login_attempts_source_key_length`：`LENGTH(source_key) = 64`；与 `occurred_at` 组成普通复合索引（列顺序 `source_key`、`occurred_at`） |
| `admin_account_id` | Integer | 可空（提交的用户名没有对应账号时为空）；外键 → `admin_accounts.id`（RESTRICT） |
| `outcome` | String(20) | 非空；检查 `ck_admin_login_attempts_outcome_known`：`outcome IN ('success', 'bad_password', 'unknown_user', 'disabled', 'locked')` |

- 迁移是本仓库的第一条：按 `alembic.ini` 的 `file_template`，文件是 `alembic/versions/20261123_0001_admin_auth.py`，revision `"0001"`，`down_revision = None`；
  upgrade 建三张表、逐个写出上面的约束与索引名（与模型按命名约定生成的名字一致）；downgrade 按依赖反序删表。
- 模型放 `app/models/admin_auth.py`，`app/models/__init__.py` 导出 `AdminAccount`、`AdminSession`、`AdminLoginAttempt`；`alembic/env.py` 加一行 `import app.models`，让 `Base.metadata` 里有这三张表（env.py 顶部注释已要求这样做）。
- **检查约束要对迁移建出的表测**：CI 的 `alembic check` 比对列、索引、唯一约束与外键，**不比对 CHECK 约束**。所以 001 的集成测试不用 `create_all`，而是用 `alembic.command.upgrade(Config("alembic.ini"), "head")` 在 CI 的一次性库上建表
  （测试里临时把环境变量 `INVENTORY_DATABASE_URL` 设成 `INVENTORY_TEST_DATABASE_URL` 的值，结束时恢复），跑完下面的例子后 `downgrade(…, "base")`，不影响 CI 之后的迁移步骤。没有测试库时跳过。
- 002、004 的集成测试只测规则与接口，用 `Base.metadata.create_all` / `drop_all` 建删表即可，测试结束必须删表；没有测试库时跳过。
- `app/db.py` 的 `make_engine` 改为 `create_engine(database_url, pool_pre_ping=True, hide_parameters=True)`：SQLAlchemy 的异常消息默认带着 SQL 的绑定参数（密码哈希、`token_hash`、`throttle_key` 等），开了它，SQLAlchemy 拼进异常的绑定参数就不出现。数据库自己的错误文本仍可能含列值（例如重复键报错里的用户名），所以异常消息一律不输出、不写进响应：命令只输出异常类名（4.6），接口回答固定的 503（5.1）。
- **上线前提**：生产 MySQL 至少 8.0.16（更早的版本只解析、不执行 CHECK 约束）。CI 用的 `mysql:8.0` 镜像是最新的 8.0 小版本。

**约束的例子**（001 的集成测试对迁移建出的表逐条验证）：

| 编号 | 操作前 | 操作 | 结果 |
| --- | --- | --- | --- |
| D1 | 已有账号 `kelvin` | 再插入用户名 `kelvin` 的账号 | 被 `uq_admin_accounts_username` 拒绝 |
| D2 | 空表 | 插入用户名 `ab`（2 个字符）的账号 | 被 `ck_admin_accounts_username_length` 拒绝；`abc` 可以 |
| D3 | 账号 1 存在 | 插入 `token_hash` 为 63 个字符的会话 | 被 `ck_admin_sessions_token_hash_length` 拒绝 |
| D4 | 账号 1 存在 | 插入 `expires_at` 等于 `created_at` 的会话 | 被 `ck_admin_sessions_expires_after_created` 拒绝 |
| D5 | 空表 | 插入 `outcome` 为 `ok` 的登录尝试；插入 `source_key` 为 63 个字符的登录尝试 | 分别被 `ck_admin_login_attempts_outcome_known`、`ck_admin_login_attempts_source_key_length` 拒绝；五个合法 `outcome` 都可以 |
| D6 | 账号 1 有一条会话或一条登录尝试 | 删除账号 1 | 被外键拒绝（停用账号用 `disabled_at`，不删行） |
| D7 | 空表 | 插入 `admin_account_id` 为空的登录尝试 | 可以（用户名不存在的尝试） |

## 4. 规则与服务（INVENTORY-TASK-002，命令是 003）

### 4.1 用户名与密码

- **用户名**：合法用户名是**整串匹配**（`re.fullmatch`，不用 `re.match`，否则结尾的换行也能通过）`[a-z0-9][a-z0-9._-]{2,31}` 的串（3–32 个 ASCII 字符，小写字母、数字、`.`、`_`、`-`，首字符是字母或数字）。
  命令行输入与登录时提交的用户名都先用 Python 的 `str.lower()` 转小写，得到「规范用户名」（`normalize_username(text) -> str`；合法与否用 `is_valid_username(name) -> bool`，两者都在 `app/domain/pw_hash.py`）。建号时规范用户名不合法即拒绝。
  **登录时规范用户名不合法就不查库**，直接按「用户名不存在」处理（4.3 第 3 步）。这样查库用的永远是纯 ASCII 小写串，MySQL 默认排序规则的大小写与重音不敏感（例如 `kélvin` 会匹配到 `kelvin`）用不上；
  能匹配到同一账号的输入，规范用户名必然相同，`throttle_key`（4.2）也就相同，换写法拿不到新的尝试次数。
- **密码**：长度 12–128 个字符（Python `len`，按码点计），不做组成规则（不强制大小写、数字、符号）；转小写后不得等于规范用户名。不查常见弱口令表（见第 7 节）。
  送进 scrypt 的是密码的 UTF-8 字节，不做 Unicode 规范化；含无法按 UTF-8 编码的字符（孤立代理项）时，建号与改密拒绝，登录校验返回不通过。
- **哈希**：标准库 `hashlib.scrypt`，参数 N=2^14、r=8、p=5，16 字节随机盐，64 字节摘要，`maxmem = 128 × 16 × 2^16 + 2^20` 字节（显式给出，接受的参数上限也能算）；存成自描述字符串
  `scrypt$n=16384,r=8,p=5$<盐 Base64url>$<摘要 Base64url>`（不带填充，不超过 255 个字符）。校验按串里记下的参数重算、`hmac.compare_digest` 比较；
  串格式不合法、参数越界（N 不是大于 1 的 2 的幂或大于 2^16、r 或 p 大于 16）、盐不是 16 字节或摘要不是 64 字节、计算失败时返回不通过，不抛异常。
- **同时计算的上限不在这里**：限制未认证请求能占用的内存与 CPU（一次 scrypt 约 16 MiB），由接口层在进入 `authenticate` **之前**做（5.1 第 6 步）。放在这里会让排队的请求一边等、一边已经占着线程池线程、数据库连接与账号行锁。
- **假哈希**：固定的 `DUMMY_PASSWORD_HASH`（参数同上、盐与摘要全零字节），对任何密码都不通过。用户名不存在时照样对它校验一次，让两种失败的耗时相近。
- 以上在 `app/domain/pw_hash.py`（文件名不能含 password：OpenClaw 的允许路径不收凭据形状的文件名）。它要加载 `_hashlib`，Worker 沙箱关闭 Win32k 时加载不了，所以它的测试放 `tests/integration/test_pw_hash.py`（pytest，不需要数据库），`tests/unit` 仍然保持在沙箱里可跑。

| 编号 | 输入 | 结果 |
| --- | --- | --- |
| P1 | 用户名 `kelvin`，密码 11 个字符 | 拒绝（过短）；12 个字符可以；129 个字符拒绝 |
| P2 | 用户名 `warehouse.lead`（14 个字符），密码 `Warehouse.Lead` | 拒绝（转小写等于用户名）；密码 `warehouse.lead2` 可以 |
| P3 | 同一密码哈希两次 | 两个串不同（盐不同），各自都能校验通过 |
| P4 | 校验 `DUMMY_PASSWORD_HASH`、空串、`scrypt$n=3,r=8,p=5$…`（N 不是 2 的幂） | 都返回不通过，不抛异常 |

### 4.2 登录失败锁定

- **规范来源** `normalize_source(text)`：合法 IPv4 地址 → 规范文本；IPv4 映射的 IPv6 地址（`::ffff:a.b.c.d`）→ 其中的 IPv4；其他合法 IPv6 地址 → 所在 /64 网段的规范文本（例如 `2001:db8:1:2::/64`，同一个 /64 里换地址不算新来源）；其余（含 `unknown`）原样。来源原文怎么取见 5.3。
- `throttle_key = SHA-256 十六进制( UTF-8( 规范来源 + "\n" + 规范用户名 ) )`；`source_key = SHA-256 十六进制( UTF-8( 规范来源 ) )`。规范用户名见 4.1（合法与否都用它算）；
  提交的用户名含无法按 UTF-8 编码的字符时，按 `errors="replace"` 编码后再算（只用于计数，不查库）。
- **失败**指 `outcome` 为 `bad_password`、`unknown_user`、`disabled` 的尝试。`locked` 不算失败（被锁时继续尝试不会延长锁定），`success` 不清零。
- **窗口**：`现在 − 60 分钟 < occurred_at <= 现在`（下界严格大于，上界含等于；时间在「现在」之后的记录不算）。
- **两道上限**，一次登录开始时按顺序数：
  1. 同一 `source_key` 在窗口内的失败**满 30 条**（不论用户名）→ 锁定；
  2. 同一 `throttle_key` 在窗口内的失败**满 10 条** → 锁定。

  锁定时不查账号、不校验密码（不做 scrypt），写一条 `locked`，回答「已锁定」。
- 锁定随窗口内最早的失败滑出 60 分钟而自动解除，没有人工解锁命令；解除后照常校验密码。
- 来源与用户名的组合锁定不影响别处的来源：攻击者无法从**自己的**地址把管理员锁在外面。让管理员**自己的浏览器**替攻击者发登录请求（跨站表单）的路子，由 5.1 的 Content-Type 与 `Sec-Fetch-Site` 检查挡掉。
- 按来源的上限挡的是同一来源换着用户名试（每次都要做一次假哈希的 scrypt）。代价：与攻击者共用出口地址的管理员会被一起锁住 60 分钟；代理没有设置 `X-Real-IP` 时，所有人共用代理的地址，**任何人每小时 30 次失败就能把全站管理员一起锁住**，所以 5.3 的上线检查必须先过（第 7 节）。
- 计数与 `throttle_key`、`source_key`、`normalize_source` 的计算在 `app/domain/login_throttle.py`（纯函数，只用标准库的 `hashlib.sha256` 与 `ipaddress`，单元测试 `tests/unit/test_login_throttle.py`，unittest 写法）；查库数条数在服务里。
- 并发：计数在本函数里读，读完才校验密码，两个同时进行的登录可能都读到 9 条、都去校验，各多出一条失败。接口层同时最多让两个 `authenticate` 运行（5.1 第 6 步），所以超出上限的失败最多一两条；不为此加锁（单人后台，可接受）。

| 编号 | 操作前 | 操作 | 结果 |
| --- | --- | --- | --- |
| L1 | 来源 A、用户名 `kelvin` 在 10:00:00～10:09:00 每分钟失败一次，共 10 条 | 10:15 来源 A 用正确密码登录 | 拒绝，`locked`；不建会话；失败仍是 10 条 |
| L2 | 同 L1 | 11:00:00 来源 A 用正确密码登录 | 10:00:00 那条不再满足 `> 10:00:00`，失败只剩 9 条 → 校验密码，成功，建会话，写 `success` |
| L3 | 同 L1 | 10:15 来源 B 用正确密码登录 `kelvin` | 来源 B 没有失败 → 成功 |
| L4 | 来源 A 对不存在的用户名 `ghost` 失败 10 次 | 第 11 次来源 A 提交 `ghost` | `locked`；与 `kelvin` 已存在时的表现一致，不泄露用户名是否存在 |
| L5 | 来源 A、`kelvin` 有 9 条失败、1 条 `success` | 再失败一次 | 失败满 10 条（`success` 不清零），下一次即锁定 |
| L6 | 来源 A、`kelvin` 有 10 条失败 | 来源 A 提交 `KELVIN`、`kélvin` | `KELVIN` 的规范用户名是 `kelvin`，同一个 key → `locked`；`kélvin` 不合法、不查库，算作另一个 key 的 `unknown_user`，永远登录不进 `kelvin` |
| L7 | 来源 A 在窗口内对 30 个不同的用户名各失败 1 次 | 来源 A 用正确密码登录 `kelvin` | `locked`（按来源满 30 条）；来源 B 登录 `kelvin` 不受影响 |
| L8 | 来源 `2001:db8:1:2::5` 对 `kelvin` 失败 10 次 | 来源 `2001:db8:1:2::9` 登录 `kelvin` | 两者的规范来源都是 `2001:db8:1:2::/64`，同一个 key → `locked`；`2001:db8:1:3::5` 不受影响 |

### 4.3 一次登录（`authenticate`）

`authenticate(db, *, source, username, password, now) -> LoginResult`：`source` 是 5.3 取到的来源原文（本函数自己做 `normalize_source`）；`db` 是调用方给的 SQLAlchemy `Session`，本函数在里面完成并**提交**一个事务。按顺序：

1. 算规范来源、`throttle_key`、`source_key`。
2. 按 4.2 数两道上限，任一满了 → 写 `locked`（`admin_account_id` 为空），提交，返回「已锁定」。
3. 规范用户名不合法（4.1），或按它查不到账号 → 对 `DUMMY_PASSWORD_HASH` 校验一次，写 `unknown_user`（`admin_account_id` 为空），提交，返回「凭据无效」。
   查账号用 `SELECT … FOR UPDATE`，**锁住账号行到本事务提交**（见 4.4 的「并发」）。
4. 校验密码。不通过 → 写 `bad_password`（带账号 id），提交，返回「凭据无效」。
5. 账号已停用 → 写 `disabled`（带账号 id），提交，返回「凭据无效」（与密码错误不可区分；先校验密码再看停用，耗时一致）。
6. 建会话（4.4），写 `success`（带账号 id），提交，返回账号与会话令牌原文。

每次调用恰好写一条登录尝试。本函数是同步的（数据库与 scrypt 都会阻塞），接口层必须把它放到线程池里调用（5.1）。

`LoginResult` 是不可变的数据类：`outcome`（`"success"`、`"invalid_credentials"`、`"locked"` 之一）；成功时另有 `account_id`、`username`、`session_id`、`token`（会话令牌原文）、`expires_at`，失败时这几项为 `None`。
它的 `repr` 不含 `token`（字段声明 `repr=False`）。数据库出错（`SQLAlchemyError`）时回滚并原样抛出，由调用方处理。

### 4.4 会话与 CSRF

- **令牌**：`secrets.token_urlsafe(32)`；库里只存它的 SHA-256 十六进制（`token_hash`）；原文只在登录成功时返回一次。
- **有效期**：自登录起 **12 小时**绝对到期，使用中不延长。
- **解析**：`resolve_session(db, *, token, now) -> ActiveSession | None`。按 `token_hash` 找会话；`revoked_at` 为空、`expires_at > 现在`、账号 `disabled_at` 为空，三条都成立才返回
  `ActiveSession(session_id, account_id, username, expires_at)`（不可变数据类），否则返回 `None`。`token` 不是字符串、为空串或无法按 UTF-8 编码时直接返回 `None`，不抛异常。只读，不提交。
- **撤销**：`revoke_session(db, *, session_id, now) -> None`，把该会话的 `revoked_at` 设为现在并提交；已撤销的再撤销不改原值；没有这个 `session_id` 时什么都不做、不报错。重设密码、停用账号时撤销该账号**全部**未撤销的会话（与改账号在同一事务）。
- **并发**：登录（4.3）与重设密码、停用、启用（4.5）都先对账号行 `SELECT … FOR UPDATE`，在同一事务里改完才提交，所以对同一账号是串行的：
  停用先提交时，排在后面的登录看到 `disabled_at` 已设，写 `disabled`；登录先提交时，它建的会话已在库里，随后的停用把它一起撤销。重设密码同理（后到的登录用新哈希校验）。
  不会出现「撤销全部会话之后又冒出一条有效会话」。
- **CSRF 令牌**：`csrf_token_for(token) -> str`，`HMAC-SHA256(key=会话令牌原文的 UTF-8 字节, msg=b"inventory-admin-csrf")` 的十六进制，**不入库**，随时可由 cookie 里的会话令牌重算；
  `csrf_token_matches(token, presented) -> bool` 用 `hmac.compare_digest` 比较，`presented` 不是字符串、为空或含非 ASCII 字符时为 `False`（`compare_digest` 对含非 ASCII 的 `str` 会抛 `TypeError`，所以先判断，再比较两者的 ASCII 字节）。浏览器脚本读不到 HttpOnly 的会话 cookie，只能从接口响应拿到 CSRF 令牌。
- 以上函数、`authenticate`、`utc_now` 与 4.5 的函数都在 `app/services/admin_auth.py`；命令（4.6）与接口（第 5 节）只调用它们，不另写查询。

| 编号 | 操作前 | 操作 | 结果 |
| --- | --- | --- | --- |
| S1 | 08:00 登录成功 | 19:59 解析 | 有效；20:00 解析无效（`expires_at = 20:00`，要求严格大于） |
| S2 | 会话有效 | 退出后解析 | 无效；再退出一次，`revoked_at` 不变 |
| S3 | 账号有两条有效会话 | 重设密码（`reset_password`） | 两条的 `revoked_at` 都被设上；新密码能登录，旧密码 `bad_password` |
| S4 | 账号有一条有效会话 | 停用账号（`disable_account`） | 会话被撤销；之后正确密码登录写 `disabled`；恢复（`enable_account`）后可以重新登录，被撤销的旧会话不复活 |

### 4.5 账号变更的服务函数

在 `app/services/admin_auth.py`，各自完成并提交一个事务，时间由参数传入（S3、S4 直接调它们）：

- `create_account(db, *, username, password, now) -> int`（返回账号 id；`created_at` 与 `password_updated_at` 为 `now`）
- `reset_password(db, *, username, password, now) -> None`（更新哈希与 `password_updated_at`，撤销该账号全部会话）
- `disable_account(db, *, username, now) -> bool`（设 `disabled_at` 并撤销全部会话；`False` = 原本已停用，未改）
- `enable_account(db, *, username) -> bool`（清空 `disabled_at`，不恢复已撤销的会话；`False` = 原本未停用，未改）
- `account_exists(db, *, username) -> bool`（只读，供命令在提示输入密码之前检查）
- 五个函数都先按 4.1 把 `username` 转成规范用户名，不合法即抛 `invalid_username`、不查库；查库只用规范用户名。
- 检查顺序：用户名合法 → 账号存在与否（`already_exists` / `not_found`）→ 密码规则（`weak_password`，含无法按 UTF-8 编码的密码）。
- 业务错误抛 `AccountError`，带 `code`：`invalid_username`、`already_exists`、`not_found`、`weak_password`（消息不含密码）。`create_account` 撞上唯一约束的 `IntegrityError` 时回滚并转成 `already_exists`。

### 4.6 服务器上的账号命令（INVENTORY-TASK-003）

`python -m app.admin <子命令> <用户名>`，在服务器上的 API 容器里运行；数据库连接串取自 `INVENTORY_DATABASE_URL`（`app/config.py` 的 `Settings`）。
判定顺序：`argparse` 解析参数 → 没设连接串（退出码 2）→ 用户名合法（退出码 1）→ 查库与提示输入密码。输出的具体措辞由实现定，但必须含规范用户名与结果；测试断言退出码、输出去向与是否含规范用户名，不逐字比对措辞。

| 子命令 | 做什么 |
| --- | --- |
| `create-account` | 先校验用户名并用 `account_exists` 查库，已存在则报错——都在提示输入密码**之前**。密码用 `getpass` 不回显地输入两次，不一致则报错；再调 `create_account` |
| `reset-password` | 先查账号必须存在，再同样两次输入新密码，调 `reset_password` |
| `disable-account` | 调 `disable_account`；返回 `False` 时输出「已是停用状态」 |
| `enable-account` | 调 `enable_account`；返回 `False` 时输出「已是启用状态」 |

- 命令只负责解析参数、提示输入密码、用 `utc_now()` 取当前时间、调 4.5 的函数、把结果与错误转成输出和退出码，不另写规则或查询。
- 入口是 `main(argv: list[str] | None = None) -> int`，`if __name__ == "__main__": sys.exit(main())`；测试直接调 `main([...])`，替换 `getpass.getpass` 提供输入。
- 密码只经交互输入，不接受命令行参数或环境变量。
- **退出码**：0 成功（含两种「已是……状态」）；1 业务错误或取消——`AccountError` 的四种、两次输入不一致、输入密码时遇到 EOF 或 Ctrl+C（输出「已取消」，什么都不写）；
  2 运行环境错误——没设 `INVENTORY_DATABASE_URL`、连不上数据库、表不存在或其他 `SQLAlchemyError`（输出「数据库错误」加异常类名，不输出连接串）。参数不对由 `argparse` 处理（它自己的退出码 2）。
- 成功结果（含两种「已是……状态」）写到标准输出，错误与「已取消」写到标准错误；输出只有规范用户名与结果，不打印密码或哈希。规范用户名用 `normalize_username`（4.1，在 `app/domain/pw_hash.py`）算，命令不另写规则。

## 5. 接口（INVENTORY-TASK-004）

### 5.1 路径与响应

| 方法与路径 | 需要 | 成功 | 失败 |
| --- | --- | --- | --- |
| `POST /api/v1/admin/session` | `Content-Type: application/json`，请求体 `{"username": <1–64 个字符>, "password": <1–256 个字符>}`，不超过 4096 字节 | 200 `{"username", "expires_at", "csrf_token"}`，并设会话 cookie | 401 `{"detail": "invalid_credentials"}`（用户名不存在、密码错误、账号停用三种不可区分）；429 `{"detail": "login_locked"}`；403、415、422 见下 |
| `GET /api/v1/admin/session` | 有效会话 cookie | 200 `{"username", "expires_at", "csrf_token"}` | 401 `{"detail": "not_authenticated"}` |
| `DELETE /api/v1/admin/session` | 有效会话 cookie ＋ 请求头 `X-CSRF-Token` | 204，撤销会话并清除 cookie | 401 `not_authenticated`；403 `{"detail": "csrf_failed"}` |

**登录接口**是 `async def`，按顺序判定，前五步都不写登录尝试、不计入锁定：

1. 未配置数据库（`database_url` 为空）→ 503 `{"detail": "database_not_configured"}`。
2. 请求头 `Sec-Fetch-Site` 的值是 `cross-site` → 403 `{"detail": "cross_site_request"}`。没有这个头（非浏览器客户端）时放行。
3. `Content-Type` 的媒体类型（分号前部分，忽略大小写与首尾空白）不是 `application/json` → 415 `{"detail": "unsupported_media_type"}`。跨站的 HTML 表单只能发 `text/plain` 等三种类型，发 `application/json` 必须先过 CORS 预检，本服务不开 CORS，所以这一条让跨站表单发不出能被接受的登录请求。
4. 读请求体：`Content-Length` 大于 4096，或实际读到的字节超过 4096（逐块读，超过即停）→ 422 `{"detail": "invalid_request"}`。
5. `json.loads`；解析失败（`ValueError`、`RecursionError`）、不是 JSON 对象、缺 `username` 或 `password`、不是字符串、长度不在上表范围内 → 422 `{"detail": "invalid_request"}`。多出来的字段忽略。**不用框架默认的 422**：它会把提交的值（含密码）放进响应的 `input` 字段。
6. 进行中的登录（已过第 5 步、还没回答的）已有 10 个 → 503 `{"detail": "login_busy"}`，不写尝试。否则 `await anyio.to_thread.run_sync(authenticate, …, limiter=LOGIN_LIMITER)`，`LOGIN_LIMITER` 是模块级的 `anyio.CapacityLimiter(2)`：
   同时最多两个 `authenticate` 在线程里运行，其余在事件循环里等，**等待时不占线程、数据库连接或行锁**，`/healthz`、`/readyz` 照常应答。`authenticate` 是同步的，直接在事件循环里调会卡住整个进程。按结果回答 200、401 或 429。
   进行中的计数是模块级整数，在事件循环线程里加减（进入时加一，`finally` 里减一），不需要锁。
7. 成功、且请求带着会话 cookie 时：在 `authenticate` **之后**用 `resolve_session` 解析那个旧 cookie，仍有效且不是刚建的新会话就调 `revoke_session`（都在线程池里），再设新 cookie（旧令牌不会再有人持有，留着只会白白有效到到期）。
   这一步出错（`SQLAlchemyError`）不改变回答：照样 200 并设新 cookie，旧会话留到自然到期；不能因此回答 503，否则新会话已建、cookie 却没设。

- `GET`、`DELETE` 先做第 1 步，再解析 cookie，最后查库。
- 所有查库与 scrypt 都不在事件循环线程上跑：依赖与 `GET`、`DELETE` 的处理函数写成普通 `def`（FastAPI 自动放进线程池），登录接口按第 6、7 步显式放进线程池。
- `expires_at` 用 ISO 8601 UTC，形如 `2026-11-23T20:00:00Z`（库里本就是整秒，原样格式化，不再取整）。
- 本节列出的全部响应（200、204、401、403、415、422、429、503）都带 `Cache-Control: no-store`。
- 处理中数据库出错（`SQLAlchemyError`）→ 503 `{"detail": "database_unreachable"}`，与 `/readyz` 的原因一致，响应不含异常内容。
- 每个请求用一个 SQLAlchemy `Session`：`create_app` 用已有的 engine 建 `sessionmaker`，存到 `app.state.sessionmaker`（未配置数据库时为 `None`）；`app/api/admin_session.py` 里的依赖 `get_db(request)` 从 `request.app.state.sessionmaker` 取，为空时回答第 1 步的 503，否则建一个 `Session`、请求结束关闭。之后的后台接口也用这个依赖。
- `app/api/admin_session.py` 导出 `router`（`APIRouter`），`create_app` 用 `include_router` 挂上。
- 依赖用 `Annotated[..., Depends(...)]` 写法（`pyproject.toml` 的 ruff 选了 B 规则，参数默认值写 `Depends(...)` 会触发 B008，而本任务改不了 `pyproject.toml`）。

### 5.2 会话 cookie

名字 `__Host-inventory_admin_session`（浏览器要求 `__Host-` 前缀的 cookie 必须 `Secure`、`Path=/`、不带 `Domain`，防止被同站其他子域名设同名 cookie 覆盖）；
属性 `HttpOnly`、`Secure`、`SameSite=Strict`、`Path=/`、`Max-Age=43200`（12 小时，与 4.4 一致），不设 `Domain`。退出时用同名同属性、`Max-Age=0` 清除。
TLS 在边缘反向代理终止，浏览器看到的是 HTTPS，所以 `Secure` 不需要开关；测试客户端用 `https://` 的 base URL。

### 5.3 来源地址

沿用 acuven-shop 的取法：请求头 `X-Real-IP` **恰好一个**、且是合法的 IPv4 或 IPv6 地址时用它；缺失、出现多次、不合法时退回连接的对端地址；再没有时用 `unknown`。得到的原文交给 `authenticate`，规范化（含 IPv6 取 /64）在 4.2。
不用 `X-Forwarded-For`（访客能在前面追加伪造的地址）。

按来源锁定**依赖一个部署前提**：边缘反向代理给本站的 vhost 用 `proxy_set_header X-Real-IP $remote_addr` **覆盖**这个头（共享基建其他 vhost 已是这样）。三种情形：

| 代理的做法 | 结果 |
| --- | --- |
| 覆盖设置（要求的做法） | 来源是访客的真实地址，锁定按设计工作 |
| 不设置这个头 | 所有访客共用代理的地址：任何人每小时 30 次失败就能把**全站所有管理员**锁住 60 分钟（按来源的上限），没有人工解锁办法 |
| 把访客自己送来的头原样转进来 | **锁定可被绕过**：攻击者每次换一个头的值就是一个新的 key |

- 所以「本站 vhost 覆盖设置 `X-Real-IP`」是上线前提，由运营者在开通子域名时确认，并从外网用一个伪造的 `X-Real-IP` 头试登录失败几次、在库里看到 key 没有随头变化（之后的上线检查记录）。
- 本服务不发布端口，只挂在共享的代理网络上；同一网络里的其他容器能绕过代理直接带任意头访问。那些都是 Acuven 自己的服务，这条残余风险接受（要 Kelvin 确认，第 7 节）。
- 本仓库的代码无法判断代理有没有覆盖这个头，所以不做「只信任某个代理地址」的配置。

### 5.4 供之后后台接口用的依赖

- `require_admin`：解析会话 cookie（4.4），无效时 401 `not_authenticated`；有效时返回 `AdminPrincipal(account_id, username, session_id)`，业务任务用它记操作者。
- `require_admin_csrf`：先做 `require_admin`，再校验 `X-CSRF-Token`，不符时 403 `csrf_failed`。之后所有改数据的后台接口（POST、PUT、PATCH、DELETE）都用它。

| 编号 | 操作 | 结果 |
| --- | --- | --- |
| A1 | 正确用户名密码登录 | 200，响应有三个字段，`Set-Cookie` 带 5.2 的全部属性；库里多一条 `success` 与一条会话 |
| A2 | 密码错误、用户名不存在、账号停用各登录一次 | 三次都是 401 `invalid_credentials`，响应体相同 |
| A3 | `X-Real-IP: 203.0.113.7` 失败 10 次后第 11 次 | 429 `login_locked`；换 `X-Real-IP: 203.0.113.8` 用正确密码登录 200 |
| A4 | 登录后不带 `X-CSRF-Token` 调 `DELETE`；再带正确的调一次 | 第一次 403 `csrf_failed`、会话仍有效；第二次 204，之后 `GET` 为 401 |
| A5 | 以 `Content-Type: text/plain` 提交一个合法 JSON 的请求体；以 `Sec-Fetch-Site: cross-site` 提交合法请求 | 分别 415 `unsupported_media_type`、403 `cross_site_request`；库里都没有多出登录尝试 |
| A6 | 请求体 4097 字节；请求体是嵌套 2000 层的数组（`[` 与 `]` 各 2000 个，共 4000 字节，不超上限；`json.loads` 抛不抛 `RecursionError` 随 Python 版本而定，两种情况都必须回答 422）；请求体缺 `password` | 都是 422 `invalid_request`，响应体不含提交内容；库里没有多出登录尝试 |
| A7 | 带着有效会话 cookie 再登录一次 | 200；旧会话被撤销，新会话有效 |
| A8 | 进行中的登录已有 10 个时（测试里直接把模块级计数设为 10，并在 `finally` 里复原，不影响其他测试）再登录 | 503 `login_busy`；库里没有多出登录尝试；计数复原后照常登录 |

## 6. 留给之后任务的事

- 登录尝试的查看（后台显示最近的异常尝试）与过期清理（保留 90 天，随定时作业表一起做）：已在 `docs/TODO.md`「后续计划」列为待登记。清理上线之前，记录一直保留。
- 严重告警：本文不推送登录锁定（V1_MINIMAL_DESIGN 第 11 节的严重异常清单里没有它）。
- 登录页前端：随「后台前端骨架与 CI 前端检查」做，调用 5.1 的接口。

## 7. 与 acuven-shop 的不同、没有采用的加固与接受的残余风险（要 Kelvin 确认）

| 项 | acuven-shop | 本文 | 理由 |
| --- | --- | --- | --- |
| 账号数 | 至多一个（单例槽） | 可以多个，同一角色 | REQUIREMENTS 第 5 节要求独立账号、不与他人共用，审计记实际个人账号 |
| 会话有效期 | 30 天 | 12 小时，不续期 | 这个后台能改真实店铺的库存与配额 |
| 失败计数 | Redis 固定窗口，按来源与用户名 | 库表 `admin_login_attempts`，60 分钟滑动窗口；按来源与用户名（10 条）加按来源（30 条）两道；IPv6 按 /64 | V1_MINIMAL_DESIGN 第 2 节：不用 Redis，登录限流用库表计数；按来源一道挡换用户名的尝试 |
| 登录记录 | 审计表只记已有账号的尝试 | 每次尝试一条，用户名不存在的也记（不存用户名原文） | V1_MINIMAL_DESIGN 第 10 节「记录异常尝试」 |
| 停用 | 无 | `disable-account` / `enable-account` | REQUIREMENTS 第 5 节「支持立即停用账号」 |
| CSRF 令牌 | 会话响应里给 | 同样在响应里给，由会话令牌 HMAC 导出、不入库 | 少一列，页面刷新后 `GET` 能重新拿到 |
| 登录接口的跨站防护 | — | 只收 `application/json`，拒绝 `Sec-Fetch-Site: cross-site`，请求体 4096 字节上限 | 防止借管理员的浏览器把他自己锁在外面 |

没有采用的加固（独立评审提出，建议维持不做；Kelvin 不同意的话改设计、重新批准）：

| 加固 | 不做的理由 | 残余风险 |
| --- | --- | --- |
| 常见弱口令表 | 账号由运营者本人在服务器上建，人数个位；只要求 12 个字符以上 | 运营者自己选了 `password1234` 这类密码时，在线猜测比较快 |
| 按用户名的全局失败上限（不论来源） | 任何人都能借它把管理员锁在外面 | 分布在很多来源（僵尸网络）的在线猜测，只受每来源 10 次 / 30 次每小时与 scrypt 速度限制 |
| `throttle_key`、`source_key` 改用带服务器密钥的 HMAC | 要多一个密钥配置与轮换办法；摘要只在库里 | 能读库的人可以穷举出来源地址（与库里其他运营数据同等保护） |
| 登录时先不加锁校验密码、再加锁复核 | 多一套复核逻辑；同时最多两个 `authenticate`，排队的请求不持锁（5.1 第 6 步），持锁的只有正在算的那两个，每个约一次 scrypt 的时间 | 多来源同时攻击时，合法登录要和攻击请求一起排队或拿到 503 `login_busy`，重试即可 |
| 框架默认的 500 响应也加 `no-store` | 500 的响应体只有固定文字，不含会话或个人数据 | 无 |
| 人工解除锁定的命令 | 锁定 60 分钟自动解除；加命令要删或改登录尝试记录 | 被锁时只能等；代理没设置 `X-Real-IP` 时是全站级（见下） |

接受的部署残余风险：共享代理网络里的其他容器能绕过代理、直接带任意 `X-Real-IP` 访问（5.3）；与攻击者共用出口地址的管理员会被按来源的上限一起锁住 60 分钟（4.2）；
代理没设置 `X-Real-IP` 时任何人都能把全站管理员锁住 60 分钟——所以 5.3 的上线检查是硬前提，没过不开放登录。

## 8. 修订记录

- **0.1**（2026-11-23）：草案。同日按设计预审补齐：时间截断到整秒；登录时规范用户名不合法即不查库（避开 MySQL 排序规则的大小写与重音不敏感）；
  密码的编码与 scrypt 的 `maxmem`；账号命令的退出码与先校验后提示；服务函数的签名与返回值；登录与账号变更对账号行加锁；登录请求体不回显提交值的 422；数据库出错时的 503；来源头的部署前提与残余风险；
  `now` 不得带时区、窗口上界、用户名整串匹配、CSRF 与会话令牌的非 ASCII 输入、「已是……状态」写到标准输出、判定顺序。
  再按独立评审第 1 轮：账号命令拆成单独任务（003），接口改为 004；登录接口放进线程池、只收 `application/json`、拒绝跨站、限制请求体大小、`RecursionError` 回答 422、带旧会话再登录时撤销旧会话；
  新增按来源的失败上限（`source_key` 列）与 IPv6 按 /64 计；检查约束改为对迁移建出的表测试、上线前提 MySQL ≥ 8.0.16；迁移文件名按 `alembic.ini` 模板带日期；
  密码哈希的测试移到 `tests/integration`（`tests/unit` 保持沙箱可跑）；`utc_now()` 定在服务模块；cookie 改用 `__Host-` 前缀；依赖用 `Annotated` 写法；第 7 节列出没有采用的加固与残余风险。
  再按预审：A6 的嵌套例子改到 4096 字节以内；`sessionmaker` 存在 `app.state`、由 `get_db` 依赖取用；账号变更的检查顺序与无法编码的密码；命令输出规范用户名。再按最终一轮预审：engine 开 `hide_parameters`（001 改 `app/db.py`）；命令的判定顺序与输出措辞；带旧 cookie 再登录时撤销的时机与出错处理。
  再按独立评审第 2 轮：scrypt 的并发上限从 `pw_hash` 的信号量改为接口层的 `anyio.CapacityLimiter(2)` 加进行中 10 个的上限（等待不占线程、连接、行锁），新增 A8；写明代理不设置 `X-Real-IP` 时按来源上限会造成全站锁定；A6 不再断言一定抛 `RecursionError`；第 7 节补「人工解除锁定的命令」。按第 3 轮建议改措辞（不改规则）：第 7 节表格断行、`hide_parameters` 的保证范围、A8 测试复原计数。
- **0.1 批准**（2026-11-23）：Kelvin 批准全文，包括第 7 节的取舍、没有采用的加固与接受的残余风险。
