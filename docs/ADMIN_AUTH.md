# 后台管理员账号、密码登录与会话

> 版本：**0.3 · 日期：2026-11-23 · 状态：草案，待 Kelvin 批准**（随契约 0.22 一起）。
> 出处：原 Inventory 的后台登录设计（`acuven_inventory` 仓库 `docs/ADMIN_AUTH_DESIGN.md` 0.1，Kelvin 2026-11-23 批准全文，含第 7 节的取舍与残余风险）。
> 2026-11-23 Kelvin 定把 Inventory 并入 Core、而且要深度融合（`acuven_hub/DECISIONS.md` C-21，契约 0.22）：这个后台是 **Core 的后台**，管目录、库存与平台渠道，这套登录是 Core 的管理员登录。
> 0.3 把 0.1 改到 Core 的约定上；**密码、锁定、会话、CSRF 的规则与例子（P1–P4、L1–L8、S1–S4、A1–A8、D1–D7）没有改**。改动逐项列在第 0 节，取舍登记为 [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) 的 Q-86、Q-87、Q-91、Q-92、Q-93。
> 被否决的 0.2 草案（“Inventory 模块的后台登录”，放在 `docs/inventory/`）整体作废。
> 依据：[CONTRACT_V1.md](CONTRACT_V1.md) 0.22 草案的 §1、§2.2、§6.1、§7.1、§7.2、§8.1、§8.6、§10 第 1、3、4、5 条；原 Inventory 需求（`acuven_inventory` 仓库 `docs/REQUIREMENTS.md` 第 5 节「安全」：独立账号、不与他人共用、登录限流、记录异常尝试、支持立即停用、首期不做双重验证）；本仓库 `CLAUDE.md` 的代码约定。
> 取值与做法尽量沿用 acuven-shop 已获批、已上线的管理员登录（同样是标准库 scrypt、按来源与用户名组合锁定、服务端会话），不同处与接受的残余风险在第 7 节列出。设计有改动时，改动部分的批准作废、须重新批准。

分四个任务实现，每个只做一层（原 INVENTORY-TASK-001～004 不在 `acuven_inventory` 执行，批准后在本仓库登记为 `CORE-TASK-NNN`）：

| 任务 | 层 | 本文章节 |
| --- | --- | --- |
| 登录一：数据模型与迁移 | 数据库 | 第 3 节 |
| 登录二：规则与服务（密码、锁定、登录、会话、账号变更） | 规则 | 第 4.1–4.5 节 |
| 登录三：服务器上的账号命令 | 命令行 | 第 4.6 节 |
| 登录四：登录、会话、退出接口与后台共用的依赖 | 接口 | 第 5 节 |

前置：契约 0.22 的后台开关（`CORE_ADMIN_ENABLED`，§6.1）与操作主体的 `kind` 列（§10 第 4 条）已实现；没实现之前，第 3 节以后的任务不能登记。

## 0. 相对 0.1 的改动（0.3，待 Kelvin 批准）

| 项 | 0.1（`acuven_inventory`） | 0.3（Core） | 依据 |
| --- | --- | --- | --- |
| 这是谁的后台 | Inventory 的后台 | Core 的后台：目录、库存与平台渠道都在这里管（契约 §8.6 第 2 条） | C-21；Q-86 |
| 账号表的主键 | 自增整数 `id` | `admin_account_id`，`adm_` 加 ULID，`VARCHAR(32)`；它就是写进 Core 流水的 `actor_ref` | 契约 §1；Q-91 |
| 操作主体 | 没有 | 每个账号在 `credentials` 表里有一个 `kind` 为 `admin` 的主体，建号时同一事务建；账号表以 `credential_id`（唯一）指向它；流水、单据、幂等记录、安全事件上的 `credential_id` 填它 | 契约 §10 第 4 条；Q-85 |
| 角色 | 只有管理员一个角色 | 仍只有管理员一种账号；每个账号持有 Core 的 `catalog_admin`、`stock_admin`、`stock_operator` 三个角色，后台按契约的角色表断言 | 契约 §10 第 3 条、§8.6 第 2 条；Q-86 |
| 会话、登录尝试的主键 | 自增整数 | 不变：自增整数（只在后台登录内部用，同 Core 的 `sku_code_history`） | Q-91 |
| 租户 | 没有 | 账号、会话带 `tenant_id`（非空），登录尝试的 `tenant_id` 可空；表间用复合外键。用户名**全库唯一**（登录时还不知道租户，同令牌摘要的例外）；登录尝试认不出账号时 `tenant_id` 为空（同安全事件的例外） | 契约 §10 第 1 条；Q-91 |
| 时间列 | `DATETIME`（不带小数秒） | `DATETIME(6)`，UTC（Core 的列约定）；`utc_now()` 仍截到整秒，规则与例子不变 | 契约 §2.2；Q-91 |
| 服务函数收什么 | SQLAlchemy `Session` | `Connection`（Core 的服务层不用 ORM 会话）；“提交”指在这个连接上提交事务 | 本仓库代码约定 |
| 迁移 | 第一条，`0001` | 接在**登记时**本仓库的最后一条之后（写这一版时最后一条是 `0011`，`20261119_0011_sku_merges.py`），revision 与文件名按 `alembic.ini` 的模板与登记那天的日期取 | 本仓库 `alembic/versions/` |
| 配置 | `INVENTORY_DATABASE_URL` | Core 的 `CORE_DATABASE_URL`；测试库照本仓库 `tests/integration/conftest.py` 的 `CORE_TEST_DATABASE_URL` | 本仓库 `app/config.py` |
| 账号命令 | `python -m app.admin`，业务错误退出 1、环境错误退出 2，不记审计 | 并进 Core 的运维命令行 `python -m app.cli admin …`；退出码照它（参数与校验错误 2、其他失败 1）；改账号的子命令要 `--actor`、`--reason`，成功后记安全事件 | 契约 §7.2；Q-92 |
| 接口的数据库连接 | `app.state.sessionmaker` 与 `get_db` | 用 Core 已有的 `app.state.engine` 取连接，不另建 `sessionmaker`；“未配置数据库”的 503 去掉（Core 没有连接串就拒绝启动） | 本仓库 `app/main.py` |
| 路由 | `/api/v1/admin/session` | 不变；与 Core 的 `/v1` 不冲突；只在 `CORE_ADMIN_ENABLED=true` 时挂上；Core 的请求中间件对 `/api/` **一律不用调用方给的 `X-Request-ID`、总是生成**，也不读鉴权与操作者头 | 契约 §6.1、§8.1 的 0.22 一条；Q-87、Q-93 |
| 引擎 | `make_engine` 加 `hide_parameters=True` | 同样要加，改的是 Core 的 `app/db.py`，对 Core 全部查询生效；保留 Core 已有的 UTC 会话时区设置 | 本仓库 `app/db.py` |
| cookie 名与 CSRF 的 HMAC 常量 | `__Host-inventory_admin_session`、`b"inventory-admin-csrf"` | `__Host-core_admin_session`、`b"core-admin-csrf"`（都还没实现，改名没有迁移成本） | 一套后台，不带 Inventory 字样 |

---

## 1. 范围

| 做 | 不做（本文） |
| --- | --- |
| 管理员账号（可以有多个，每人一个，同一种账号）、密码哈希、服务器上的建号与改密命令、停用与恢复 | 网页注册、网页改密或找回密码、邮件或短信、双重验证（原需求第 5 节：列为后续增强） |
| 密码登录、服务端会话、退出、取当前会话 | 多种账号与权限分级（V1 只有管理员；以后分操作员时加账号的角色表，契约 §8.6 第 2 条） |
| 按「来源 + 提交的用户名」与按来源的登录失败锁定，每次登录尝试记一条 | 业务变更的审计（数量、配额、映射、连接的操作者与原因由契约规定的各表与流水记；它们从第 5.4 节的依赖拿到操作者） |
| 之后所有后台接口共用的「须为已登录管理员」依赖与 CSRF 校验 | 登录页前端（随「后台前端骨架」任务做）；登录尝试的查看与过期清理（第 6 节） |

## 2. 个人数据

- 账号只存用户名，不存姓名、电话、邮箱。用户名是登录标识，建议用不含真实姓名的短名。
- 登录尝试**不存来源地址原文、不存提交的用户名原文**，只存两个 SHA-256 摘要：`throttle_key`（来源与用户名，4.2）和 `source_key`（只有来源，4.2），用于计数。
  IPv4 地址空间小，这两个摘要是假名化不是匿名化：能读库的人可以穷举出来源地址。所以它们和账号表一样只在库里，不进日志、不进接口响应（为什么不加密钥，见第 7 节）。
- 密码只存 scrypt 哈希；会话令牌只存 SHA-256；CSRF 令牌不入库（4.4）。三者的原文都不进日志、异常消息、命令输出。

## 3. 数据模型（登录一）

沿用 `app/db.py` 的 `Base` 与命名约定；指向账号的外键一律 `ON DELETE RESTRICT`。列类型照 `app/models/catalog.py` 的写法：ID 列 `ID`（`VARCHAR(32)`，区分大小写的排序规则）、时间列 `TIMESTAMP`（`DATETIME(6)`）。

**时间**：一律是不带时区的 UTC。列用 `DATETIME(6)`（契约 §2.2 的列约定），存进去的值与 Python 的值完全相同；取当前时间**截到整秒**，所以 4.2 的窗口、4.4 的到期与 5.1 的 `expires_at` 不会差一秒。
时间都由应用写，不用数据库的默认值（与契约 §2.2“`created_at` 默认 `CURRENT_TIMESTAMP(6)`”有出入：数据库默认值不截整秒，与 `utc_now()` 的值对不上；这处出入登记在 Q-91，批准即接受）。登录、会话这类判定用应用时钟（`utc_now()`）；契约里“过期判断以数据库时间为准”管的是预留等库存命令，不管后台登录。
取当前时间的函数只有一个：`utc_now()`，在 `app/services/admin_auth.py`（登录二写），返回 `datetime.now(UTC).replace(tzinfo=None, microsecond=0)`（截断，不四舍五入）；命令（4.6）与接口（第 5 节）都调它，不另写。
规则与服务函数接受的 `now` 参数必须是不带时区（`tzinfo` 为 `None`）的整秒，带时区或带小数秒时报 `ValueError`。

**`admin_accounts`**

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `admin_account_id` | `ID` | 主键；`adm_` 加 ULID（`app/domain/ids.py` 的 `new_id`），永不复用；写进 Core 流水与审计的 `actor_ref` 就是它 |
| `tenant_id` | `ID` | 非空；外键 → `tenants.tenant_id`；`(tenant_id, admin_account_id)` 唯一（复合外键的引用目标） |
| `credential_id` | `ID` | 非空；**唯一**；复合外键 `(tenant_id, credential_id)` → `credentials`。指向这个账号的操作主体（`kind` 为 `admin`，契约 §10 第 4 条）；主体的 `kind` 由建号的服务保证 |
| `username` | String(32) | 非空；**全库**唯一 `uq_admin_accounts_username`（登录时还不知道租户，同 Core 令牌摘要的例外，契约 §10 第 1 条）；检查 `ck_admin_accounts_username_length`：`LENGTH(username) >= 3 AND LENGTH(username) <= 32`。字符集规则在规则层（4.1） |
| `password_hash` | String(255) | 非空 |
| `created_at` | `TIMESTAMP` | 非空 |
| `password_updated_at` | `TIMESTAMP` | 非空 |
| `disabled_at` | `TIMESTAMP` | 可空；非空 = 已停用 |

**`admin_sessions`**

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BigInteger | 主键，自增（只在后台登录内部用，不对外） |
| `tenant_id` | `ID` | 非空；外键 → `tenants.tenant_id` |
| `admin_account_id` | `ID` | 非空；复合外键 `(tenant_id, admin_account_id)` → `admin_accounts`（RESTRICT）；普通索引 |
| `token_hash` | `CHAR(64)`，排序规则 `utf8mb4_bin`（照 Core 令牌摘要 `credential_tokens.token_digest` 的写法，`app/models/service_auth.py`） | 非空；唯一 `uq_admin_sessions_token_hash`（全库唯一，契约 §10 第 1 条的例外）；检查 `ck_admin_sessions_token_hash`：`REGEXP_LIKE(token_hash, '^[0-9a-f]{64}$', 'c')` |
| `created_at` | `TIMESTAMP` | 非空 |
| `expires_at` | `TIMESTAMP` | 非空；检查 `ck_admin_sessions_expires_after_created`：`expires_at > created_at` |
| `revoked_at` | `TIMESTAMP` | 可空；非空 = 已撤销 |

**`admin_login_attempts`**

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BigInteger | 主键，自增 |
| `occurred_at` | `TIMESTAMP` | 非空 |
| `throttle_key` | String(64) | 非空；检查 `ck_admin_login_attempts_throttle_key_length`：`LENGTH(throttle_key) = 64`；与 `occurred_at` 组成普通复合索引（列顺序 `throttle_key`、`occurred_at`） |
| `source_key` | String(64) | 非空；检查 `ck_admin_login_attempts_source_key_length`：`LENGTH(source_key) = 64`；与 `occurred_at` 组成普通复合索引（列顺序 `source_key`、`occurred_at`） |
| `tenant_id` | `ID` | 可空（认不出账号时为空，同 Core 安全事件的例外）；外键 → `tenants.tenant_id` |
| `admin_account_id` | `ID` | 可空（提交的用户名没有对应账号时为空）；复合外键 `(tenant_id, admin_account_id)` → `admin_accounts`（RESTRICT）；检查 `ck_admin_login_attempts_account_tenant`：`admin_account_id IS NULL OR tenant_id IS NOT NULL`（没有这条，复合外键在 `tenant_id` 为空时不检查；同 Core 安全事件表的 `credential_tenant`） |
| `outcome` | String(20) | 非空；检查 `ck_admin_login_attempts_outcome_known`：`outcome IN ('success', 'bad_password', 'unknown_user', 'disabled', 'locked')` |

- **租户**：V1 生产实例只有一个租户。建号命令必须给 `--tenant`（4.6），租户须存在且 `active`。会话的 `tenant_id` 取自账号；第 5.4 节的依赖把它放进 `AdminPrincipal`，后台接口只操作这个租户的池与对象（契约 §8.6 第 2 条第 ⑧ 步）。
- **迁移**：接在**登记时**本仓库的最后一条迁移之后（不写死编号）；revision 照本仓库的四位顺序号，文件名按 `alembic.ini` 的 `file_template`（`<登记那天的日期>_<编号>_admin_auth.py`）。
  upgrade 建三张表、逐个写出上面的约束与索引名（与模型按命名约定生成的名字一致）；downgrade 按依赖反序删表。两种实例的库都建这三张表，Demo 库里始终为空（契约 §6.1 的后台开关一条）。
  第 4.6 节的安全事件要给 `security_events.event_type` 的检查约束加四个值，也在这一条迁移里重建那条约束（Q-92）。`credentials.kind` 列由操作主体的任务先加（前置）。
- 模型放 `app/models/admin_auth.py`，`app/models/__init__.py` 导出 `AdminAccount`、`AdminSession`、`AdminLoginAttempt`；按本仓库 CLAUDE.md 的共享文件清单，`alembic/env.py` 里 import 新模型模块（登记到 `Base.metadata`），`app/models/__init__.py` 与新迁移文件一起列。
- **检查约束要对迁移建出的表测**：CI 的 `alembic check` 比对列、索引、唯一约束与外键，**不比对 CHECK 约束**。所以登录一的集成测试不用 `create_all`，而是照 `tests/integration/conftest.py` 已有的迁移测试做法（例如 `test_demo_reset_schema.py`）对迁移建出的表测，跑完下面的例子后复原，不影响 CI 之后的步骤。没有测试库时跳过。
- 登录二、登录四的集成测试只测规则与接口，建表删表照本仓库集成测试的现有做法；测试结束必须复原；没有测试库时跳过。
- `app/db.py` 的 `make_engine` 加上 `hide_parameters=True`（已有的 `pool_pre_ping` 与把会话时区固定为 UTC 的 `connect_args` 保留）：SQLAlchemy 的异常消息默认带着 SQL 的绑定参数（密码哈希、`token_hash`、`throttle_key` 等），开了它，SQLAlchemy 拼进异常的绑定参数就不出现。数据库自己的错误文本仍可能含列值（例如重复键报错里的用户名），所以异常消息一律不输出、不写进响应：命令只输出异常类名（4.6），接口回答固定的 503（5.1）。
- **上线前提**：生产 MySQL 至少 8.0.16（更早的版本只解析、不执行 CHECK 约束）。CI 用的 `mysql:8.0` 镜像是最新的 8.0 小版本。

**约束的例子**（登录一的集成测试对迁移建出的表逐条验证；“账号 1”指某个 `adm_…` 账号）

| 编号 | 操作前 | 操作 | 结果 |
| --- | --- | --- | --- |
| D1 | 已有账号 `kelvin` | 再插入用户名 `kelvin` 的账号 | 被 `uq_admin_accounts_username` 拒绝 |
| D2 | 空表 | 插入用户名 `ab`（2 个字符）的账号 | 被 `ck_admin_accounts_username_length` 拒绝；`abc` 可以 |
| D3 | 账号 1 存在 | 插入 `token_hash` 为 63 个字符的会话；插入 64 个字符但含大写字母或非十六进制字符的会话 | 都被 `ck_admin_sessions_token_hash` 拒绝 |
| D4 | 账号 1 存在 | 插入 `expires_at` 等于 `created_at` 的会话 | 被 `ck_admin_sessions_expires_after_created` 拒绝 |
| D5 | 空表 | 插入 `outcome` 为 `ok` 的登录尝试；插入 `source_key` 为 63 个字符的登录尝试 | 分别被 `ck_admin_login_attempts_outcome_known`、`ck_admin_login_attempts_source_key_length` 拒绝；五个合法 `outcome` 都可以 |
| D6 | 账号 1 有一条会话或一条登录尝试 | 删除账号 1 | 被外键拒绝（停用账号用 `disabled_at`，不删行） |
| D7 | 空表 | 插入 `admin_account_id` 为空的登录尝试 | 可以（用户名不存在的尝试） |
| D8 | 租户 A、B 各有一个账号 | 插入一条会话：`tenant_id` 为 B、`admin_account_id` 为 A 的账号；插入一条登录尝试：`admin_account_id` 有值、`tenant_id` 为空；在租户 B 下建与 A 的账号同名的账号 | 前两条分别被复合外键与 `ck_admin_login_attempts_account_tenant` 拒绝；第三条被 `uq_admin_accounts_username` 拒绝（用户名全库唯一） |
| D9 | 账号 1 已指向主体 `cred_X` | 再插入一个指向 `cred_X` 的账号；插入一个指向租户 B 的主体、`tenant_id` 为 A 的账号 | 分别被 `credential_id` 的唯一约束与复合外键拒绝 |

## 4. 规则与服务（登录二，命令是登录三）

本节所有服务函数收 `connection`（SQLAlchemy `Connection`）；“提交”“回滚”指在这个连接上提交、回滚事务。账号 ID 是 `adm_…` 字符串。

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

`authenticate(connection, *, source, username, password, now) -> LoginResult`：`source` 是 5.3 取到的来源原文（本函数自己做 `normalize_source`）；本函数在给的连接上完成并**提交**一个事务。按顺序：

1. 算规范来源、`throttle_key`、`source_key`。
2. 按 4.2 数两道上限，任一满了 → 写 `locked`（`admin_account_id` 为空），提交，返回「已锁定」。
3. 规范用户名不合法（4.1），或按它查不到账号 → 对 `DUMMY_PASSWORD_HASH` 校验一次，写 `unknown_user`（`admin_account_id` 为空），提交，返回「凭据无效」。
   查账号用 `SELECT … FOR UPDATE`，**锁住账号行到本事务提交**（见 4.4 的「并发」）。
4. 校验密码。不通过 → 写 `bad_password`（带账号 ID），提交，返回「凭据无效」。
5. 账号已停用 → 写 `disabled`（带账号 ID），提交，返回「凭据无效」（与密码错误不可区分；先校验密码再看停用，耗时一致）。
6. 建会话（4.4），写 `success`（带账号 ID），提交，返回账号与会话令牌原文。

每次调用恰好写一条登录尝试。本函数是同步的（数据库与 scrypt 都会阻塞），接口层必须把它放到线程池里调用（5.1）。

`LoginResult` 是不可变的数据类：`outcome`（`"success"`、`"invalid_credentials"`、`"locked"` 之一）；成功时另有 `account_id`、`username`、`session_id`、`token`（会话令牌原文）、`expires_at`，失败时这几项为 `None`。
它的 `repr` 不含 `token`（字段声明 `repr=False`）。数据库出错（`SQLAlchemyError`）时回滚并原样抛出，由调用方处理。

### 4.4 会话与 CSRF

- **令牌**：`secrets.token_urlsafe(32)`；库里只存它的 SHA-256 十六进制（`token_hash`）；原文只在登录成功时返回一次。
- **有效期**：自登录起 **12 小时**绝对到期，使用中不延长。
- **解析**：`resolve_session(connection, *, token, now) -> ActiveSession | None`。按 `token_hash` 找会话；`revoked_at` 为空、`expires_at > 现在`、账号 `disabled_at` 为空，三条都成立才返回
  `ActiveSession(session_id, account_id, username, tenant_id, credential_id, expires_at)`（不可变数据类；`credential_id` 是账号的操作主体），否则返回 `None`。`token` 不是字符串、为空串或无法按 UTF-8 编码时直接返回 `None`，不抛异常。只读，不提交。
- **撤销**：`revoke_session(connection, *, session_id, now) -> None`，把该会话的 `revoked_at` 设为现在并提交；已撤销的再撤销不改原值；没有这个 `session_id` 时什么都不做、不报错。重设密码、停用账号时撤销该账号**全部**未撤销的会话（与改账号在同一事务）。
- **并发**：登录（4.3）与重设密码、停用、启用（4.5）都先对账号行 `SELECT … FOR UPDATE`，在同一事务里改完才提交，所以对同一账号是串行的：
  停用先提交时，排在后面的登录看到 `disabled_at` 已设，写 `disabled`；登录先提交时，它建的会话已在库里，随后的停用把它一起撤销。重设密码同理（后到的登录用新哈希校验）。
  不会出现「撤销全部会话之后又冒出一条有效会话」。
- **CSRF 令牌**：`csrf_token_for(token) -> str`，`HMAC-SHA256(key=会话令牌原文的 UTF-8 字节, msg=b"core-admin-csrf")` 的十六进制，**不入库**，随时可由 cookie 里的会话令牌重算；
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

- `create_account(connection, *, tenant_id, username, password, now) -> str`：返回 `adm_…`；同一事务里先在 `credentials` 建一个 `kind` 为 `admin` 的主体（名为规范用户名、没有令牌、scope 与入口都为空，契约 §10 第 4 条），再建账号行指向它；`created_at` 与 `password_updated_at` 为 `now`。
  租户不存在或已停用抛 `AccountError`，`code` 为 `invalid_tenant`（检查放在用户名合法之后、账号存在与否之前）。
- `reset_password(connection, *, username, password, now) -> None`（更新哈希与 `password_updated_at`，撤销该账号全部会话）
- `disable_account(connection, *, username, now) -> bool`（设 `disabled_at` 并撤销全部会话；`False` = 原本已停用，未改）。不吊销账号的主体：流水里的历史要指得到人，而停用的账号登录不进来，主体也就没法被使用。
- `enable_account(connection, *, username) -> bool`（清空 `disabled_at`，不恢复已撤销的会话；`False` = 原本未停用，未改）
- `account_exists(connection, *, username) -> bool`（只读，供命令在提示输入密码之前检查）
- 五个函数都先按 4.1 把 `username` 转成规范用户名，不合法即抛 `invalid_username`、不查库；查库只用规范用户名。
- 检查顺序：用户名合法 → （`create_account`）租户 → 账号存在与否（`already_exists` / `not_found`）→ 密码规则（`weak_password`，含无法按 UTF-8 编码的密码）。
- 业务错误抛 `AccountError`，带 `code`：`invalid_username`、`invalid_tenant`、`already_exists`、`not_found`、`weak_password`（消息不含密码）。`create_account` 撞上唯一约束的 `IntegrityError` 时回滚并转成 `already_exists`。

### 4.6 服务器上的账号命令（登录三）

并进 Core 的运维命令行：`python -m app.cli admin <子命令> …`（`app/cli/` 下新加一个模块，照 `app/cli/service_auth.py` 的写法接进 `app/cli/__main__.py`）；连接串取 `CORE_DATABASE_URL`。
在服务器上照 Core README 的做法经 `deploy/instance.sh` 找到生产容器；要交互输入密码，所以用 `docker exec -it`。

- 只在 `CORE_ENV=production` 且 `CORE_ADMIN_ENABLED=true` 时可用，否则报错退出（不碰库）。
- `create-account` 带必填的 `--tenant ten_…`；四个子命令都要 `--actor`（运维人员标识）与 `--reason`，格式同 Core 命令行的 `credential issue`。
- 判定顺序：`argparse` 解析参数 → 实例与开关 → 用户名合法 → 查库与提示输入密码。输出的具体措辞由实现定，但必须含规范用户名与结果；测试断言退出码、输出去向与是否含规范用户名，不逐字比对措辞。

| 子命令 | 做什么 |
| --- | --- |
| `create-account` | 先校验用户名并用 `account_exists` 查库，已存在则报错——都在提示输入密码**之前**。密码用 `getpass` 不回显地输入两次，不一致则报错；再调 `create_account` |
| `reset-password` | 先查账号必须存在，再同样两次输入新密码，调 `reset_password` |
| `disable-account` | 调 `disable_account`；返回 `False` 时输出「已是停用状态」 |
| `enable-account` | 调 `enable_account`；返回 `False` 时输出「已是启用状态」 |

- 命令只负责解析参数、提示输入密码、用 `utc_now()` 取当前时间、调 4.5 的函数、把结果与错误转成输出和退出码，不另写规则或查询。
- 测试照 Core 命令行的写法：直接调 `main`，传参数列表、`Settings` 与两个输出流，替换 `getpass.getpass` 提供输入。
- 密码只经交互输入，不接受命令行参数或环境变量。
- **退出码照 Core 命令行**（`app/cli/__init__.py`）：0 成功（含两种「已是……状态」）；2 参数或校验错误——参数不对、`AccountError` 的各种、两次输入不一致、实例或开关不对；1 其他失败——数据库连不上、表不存在或其他 `SQLAlchemyError`（输出异常类名，不输出连接串）。
  输入密码时遇到 EOF 或 Ctrl+C：输出「已取消」、什么都不写，退出码 2。
- 成功结果（含两种「已是……状态」）写到标准输出，错误与「已取消」写到标准错误；输出只有规范用户名、账号 ID 与结果，不打印密码或哈希。规范用户名用 `normalize_username`（4.1）算，命令不另写规则。
- **审计**：成功后照 Core 命令行的做法，在提交之后记一条安全事件（契约 §7.2），类型 `admin_account_created`、`admin_password_reset`、`admin_account_disabled`、`admin_account_enabled`（「已是……状态」不记），补充说明写 `--reason`、目标账号 ID、它的主体 `credential_id` 与规范用户名，不写密码与哈希；事件写入失败时操作照样生效，标准错误多一行警告（同 Core 命令行）。

## 5. 接口（登录四）

### 5.1 路径与响应

路径都在 `/api/v1/…` 下，与 Core 的 `/v1` 不冲突；路由只在 `CORE_ADMIN_ENABLED=true` 时挂上（契约 §6.1）；反向代理转发 `/api/`（契约 §10 第 5 条）。
Core 的请求中间件对 `/api/` 下的请求一律不用调用方给的 `X-Request-ID`、总是生成，不因它不合格回 400（契约 §8.1 的 0.22 一条）；128 KiB 的请求体上限先于本节的 4096 字节检查（超过 128 KiB 时回 Core 格式的 413，本节的例子都在 4096 字节附近，不受影响）；不存在的路径回 Core 格式的 404。

| 方法与路径 | 需要 | 成功 | 失败 |
| --- | --- | --- | --- |
| `POST /api/v1/admin/session` | `Content-Type: application/json`，请求体 `{"username": <1–64 个字符>, "password": <1–256 个字符>}`，不超过 4096 字节 | 200 `{"username", "expires_at", "csrf_token"}`，并设会话 cookie | 401 `{"detail": "invalid_credentials"}`（用户名不存在、密码错误、账号停用三种不可区分）；429 `{"detail": "login_locked"}`；403、415、422 见下 |
| `GET /api/v1/admin/session` | 有效会话 cookie | 200 `{"username", "expires_at", "csrf_token"}` | 401 `{"detail": "not_authenticated"}` |
| `DELETE /api/v1/admin/session` | 有效会话 cookie ＋ 请求头 `X-CSRF-Token` | 204，撤销会话并清除 cookie | 401 `not_authenticated`；403 `{"detail": "csrf_failed"}` |

**登录接口**是 `async def`，按顺序判定，前四步都不写登录尝试、不计入锁定（0.1 的第 1 步“未配置数据库回 503”去掉了：Core 没有连接串就拒绝启动；后面的步号不变）：

2. 请求头 `Sec-Fetch-Site` 的值是 `cross-site` → 403 `{"detail": "cross_site_request"}`。没有这个头（非浏览器客户端）时放行。
3. `Content-Type` 的媒体类型（分号前部分，忽略大小写与首尾空白）不是 `application/json` → 415 `{"detail": "unsupported_media_type"}`。跨站的 HTML 表单只能发 `text/plain` 等三种类型，发 `application/json` 必须先过 CORS 预检，Core 不开 CORS，所以这一条让跨站表单发不出能被接受的登录请求。
4. 读请求体：`Content-Length` 大于 4096，或实际读到的字节超过 4096（逐块读，超过即停）→ 422 `{"detail": "invalid_request"}`。
5. `json.loads`；解析失败（`ValueError`、`RecursionError`）、不是 JSON 对象、缺 `username` 或 `password`、不是字符串、长度不在上表范围内 → 422 `{"detail": "invalid_request"}`。多出来的字段忽略。**不用框架默认的 422**：它会把提交的值（含密码）放进响应的 `input` 字段。
6. 进行中的登录（已过第 5 步、还没回答的）已有 10 个 → 503 `{"detail": "login_busy"}`，不写尝试。否则 `await anyio.to_thread.run_sync(authenticate, …, limiter=LOGIN_LIMITER)`，`LOGIN_LIMITER` 是模块级的 `anyio.CapacityLimiter(2)`：
   同时最多两个 `authenticate` 在线程里运行，其余在事件循环里等，**等待时不占线程、数据库连接或行锁**，`/healthz`、`/readyz` 照常应答。`authenticate` 是同步的，直接在事件循环里调会卡住整个进程。按结果回答 200、401 或 429。
   进行中的计数是模块级整数，在事件循环线程里加减（进入时加一，`finally` 里减一），不需要锁。
7. 成功、且请求带着会话 cookie 时：在 `authenticate` **之后**用 `resolve_session` 解析那个旧 cookie，仍有效且不是刚建的新会话就调 `revoke_session`（都在线程池里），再设新 cookie（旧令牌不会再有人持有，留着只会白白有效到到期）。
   这一步出错（`SQLAlchemyError`）不改变回答：照样 200 并设新 cookie，旧会话留到自然到期；不能因此回答 503，否则新会话已建、cookie 却没设。

- `GET`、`DELETE` 先解析 cookie，再查库。
- 所有查库与 scrypt 都不在事件循环线程上跑：依赖与 `GET`、`DELETE` 的处理函数写成普通 `def`（FastAPI 自动放进线程池），登录接口按第 6、7 步显式放进线程池。
- `expires_at` 用 ISO 8601 UTC，形如 `2026-11-23T20:00:00Z`（库里本就是整秒，原样格式化，不再取整）。
- 本节列出的全部响应（200、204、401、403、415、422、429、503）都带 `Cache-Control: no-store`。
- 处理中数据库出错（`SQLAlchemyError`）→ 503 `{"detail": "database_unreachable"}`，与 `/readyz` 的原因一致，响应不含异常内容。
- 数据库连接：依赖 `get_connection(request)` 从 Core 已有的 `request.app.state.engine` 取一个连接、请求结束归还，不另建 `sessionmaker`；登录接口在线程池里自己从 engine 取连接（`authenticate` 收 `Connection`）。之后的后台接口也用这个依赖（契约 §8.6 第 2 条的后台执行器在它上面取锁、开事务）。
- `app/api/admin_session.py` 导出 `router`（`APIRouter`），`create_app` 只在开关为真时用 `include_router` 挂上（同 Core 的 Demo 重置按实例挂的写法）；按本仓库 CLAUDE.md 的共享文件清单，`app/main.py` 要列进 `allowed_change_paths`。
- 生产实例的内存上限现在是 256 MiB（`docker-compose.yml`），同时两个 scrypt 约 32 MiB；后台上线前按实测重估（[PRODUCTION_AND_RECOVERY.md](PRODUCTION_AND_RECOVERY.md) 0.7）。
- 依赖用 `Annotated[..., Depends(...)]` 写法（`pyproject.toml` 的 ruff 选了 B 规则，参数默认值写 `Depends(...)` 会触发 B008）。

### 5.2 会话 cookie

名字 `__Host-core_admin_session`（浏览器要求 `__Host-` 前缀的 cookie 必须 `Secure`、`Path=/`、不带 `Domain`，防止被同站其他子域名设同名 cookie 覆盖）；
属性 `HttpOnly`、`Secure`、`SameSite=Strict`、`Path=/`、`Max-Age=43200`（12 小时，与 4.4 一致），不设 `Domain`。退出时用同名同属性、`Max-Age=0` 清除。
TLS 在边缘反向代理终止，浏览器看到的是 HTTPS，所以 `Secure` 不需要开关；测试客户端用 `https://` 的 base URL。

### 5.3 来源地址

沿用 acuven-shop 的取法：请求头 `X-Real-IP` **恰好一个**、且是合法的 IPv4 或 IPv6 地址时用它；缺失、出现多次、不合法时退回连接的对端地址；再没有时用 `unknown`。得到的原文交给 `authenticate`，规范化（含 IPv6 取 /64）在 4.2。
不用 `X-Forwarded-For`（访客能在前面追加伪造的地址）。

按来源锁定**依赖一个部署前提**：边缘反向代理给生产实例的 vhost 用 `proxy_set_header X-Real-IP $remote_addr` **覆盖**这个头（共享基建其他 vhost 已是这样）。三种情形：

| 代理的做法 | 结果 |
| --- | --- |
| 覆盖设置（要求的做法） | 来源是访客的真实地址，锁定按设计工作 |
| 不设置这个头 | 所有访客共用代理的地址：任何人每小时 30 次失败就能把**全站所有管理员**锁住 60 分钟（按来源的上限），没有人工解锁办法 |
| 把访客自己送来的头原样转进来 | **锁定可被绕过**：攻击者每次换一个头的值就是一个新的 key |

- 所以「生产实例的 vhost 覆盖设置 `X-Real-IP`」是上线前提，由运营者在开通子域名时确认，并从外网用一个伪造的 `X-Real-IP` 头试登录失败几次、在库里看到 key 没有随头变化（[PRODUCTION_RUNBOOK.md](PRODUCTION_RUNBOOK.md) 的上线检查）。这项检查没过之前不把后台开关设为真（契约 §6.1）。
- 生产 Core 不发布端口，只挂在共享的代理网络上；同一网络里的其他容器能绕过代理直接带任意头访问。那些都是 Acuven 自己的服务，这条残余风险接受（要 Kelvin 确认，第 7 节）。
- 本仓库的代码无法判断代理有没有覆盖这个头，所以不做「只信任某个代理地址」的配置。

### 5.4 供之后后台接口用的依赖

- `require_admin`：解析会话 cookie（4.4），无效时 401 `not_authenticated`；有效时返回 `AdminPrincipal(account_id, username, session_id, tenant_id, credential_id)`。后台执行器用它建命令上下文：操作主体是 `credential_id`，`actor_ref` 是 `account_id`，角色按契约的角色表断言（契约 §8.6 第 2 条）。
- `require_admin_csrf`：先做 `require_admin`，再校验 `X-CSRF-Token`，不符时 403 `csrf_failed`。之后所有改数据的后台接口（POST、PUT、PATCH、DELETE）都用它；它们还要带页面生成的 `Idempotency-Key`（契约 §7.1 的 0.22 说明）。

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

- 登录尝试的查看（后台显示最近的异常尝试）与过期清理（保留 90 天）：清理是一个进程内定时作业（契约 §8.6 第 4 条，名字以 `admin-` 开头），另登记。清理上线之前，记录一直保留。
- 严重告警：本文不推送登录锁定（[PLATFORM_CHANNELS.md](PLATFORM_CHANNELS.md) 第 8 节的严重异常清单里没有它）。
- 登录页前端：随「后台前端骨架与 CI 前端检查」做，调用 5.1 的接口。

## 7. 与 acuven-shop 的不同、没有采用的加固与接受的残余风险（要 Kelvin 确认）

| 项 | acuven-shop | 本文 | 理由 |
| --- | --- | --- | --- |
| 账号数 | 至多一个（单例槽） | 可以多个，同一种账号 | 原需求第 5 节要求独立账号、不与他人共用，审计记实际个人账号 |
| 会话有效期 | 30 天 | 12 小时，不续期 | 这个后台能改真实店铺的库存与配额 |
| 失败计数 | Redis 固定窗口，按来源与用户名 | 库表 `admin_login_attempts`，60 分钟滑动窗口；按来源与用户名（10 条）加按来源（30 条）两道；IPv6 按 /64 | Core 不用 Redis 存权威数据；按来源一道挡换用户名的尝试 |
| 登录记录 | 审计表只记已有账号的尝试 | 每次尝试一条，用户名不存在的也记（不存用户名原文） | 原 Inventory 设计第 10 节「记录异常尝试」 |
| 停用 | 无 | `disable-account` / `enable-account` | 原需求第 5 节「支持立即停用账号」 |
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
并入 Core 之后多一条（契约 §8.6 第 6 条）：后台在 Core 的进程里，进程被攻破时拿到的是 Core 生产库的账号，兜底是备份、时间点恢复与每 5 分钟的不变量自查。

## 8. 修订记录

- **0.1**（2026-11-23，在 `acuven_inventory` 仓库）：草案；同日按设计预审与独立评审三轮修改，Kelvin 批准全文，包括第 7 节的取舍、没有采用的加固与接受的残余风险。修订细节见那边的修订记录。
- **0.2**（2026-11-23，PR #189 的 `docs/inventory/ADMIN_AUTH_DESIGN.md`）：按“Inventory 作为 Core 进程内模块”改写；随那一稿一起被 Kelvin 否决，作废。
- **0.3**（2026-11-23，**草案，待 Kelvin 批准**）：按深度融合改写，放在 `docs/ADMIN_AUTH.md`，是 Core 的后台登录。相对 0.1 的改动见第 0 节，要点：
  1. 后台是 Core 的后台；开关是 `CORE_ADMIN_ENABLED`（0.2 是 `CORE_INVENTORY_ENABLED`）；
  2. 每个账号有一个 `admin` 种类的操作主体，建号时同一事务建，账号表加 `credential_id`（唯一）；新增 D9；`ActiveSession` 与 `AdminPrincipal` 带上它；
  3. 每个账号持有 Core 的三个角色，后台按角色表断言（契约 §8.6 第 2 条）；
  4. 表、类型、迁移、配置、连接、命令行、退出码、安全事件按 Core 的约定（0.2 已做的改动保留）；
  5. cookie 名与 CSRF 的 HMAC 常量去掉 Inventory 字样（还没实现，没有迁移成本）。
