# 网店库存接口盘点（acuven-shop → Stock Core）

> 2026-11-10｜**只读盘点，没有改 `acuven-shop`。**
> 盘点时网店 HEAD `ddfae55`（2026-11-10，分支 `chore/register-shop-task-229-232`）；`main` 为 `69a802e`，两者在 `app/ alembic/ frontend/src tests` 下无差异。
> 行号随网店后续提交会漂移；改造时以函数名为准。
> 用途：列出网店里所有商品身份与库存的读写点，标出将来各自换成哪个 `StockGateway` / `CatalogGateway` 调用（契约第 9 节）。
> 2026-11-10 按 Kelvin 答复修订：**公开 Demo 是 Core V1 的第一个接入方**（独立 Demo Core 实例），下表“将来”一列按此写；真实网店何时接待定（Q-24）。

## 1. 结论先说

1. **网店的库存只有一列：`product_variants.available_stock`。**没有预留表、没有流水表、没有版本号。“预留”不是实体，是“状态为
   `awaiting_demo_payment` 的订单的订单行件数”推算出来的。
2. **数量语义和 Core 不同：**下单时直接从 `available_stock` 扣；付款成功**不动**库存（预留即被消耗）；取消/超时加回；**退款不回补**；
   发货不碰库存；每天按马来西亚时间重建为 `daily_initial_stock − 仍待支付的预留`。这是 Demo 规则（`docs/DESIGN.md` 第 67 行），
   Core 需求要求的“支付确认→待发货→按行发货→退款回库/退货验收”这几步**网店里都不存在**。
3. **ID 全是 int 自增**，规格有全站唯一的 `sku`（String 64），**没有条码列**；商品名是三语（en/zh/ms）。
4. 并发靠 MySQL 带条件的 UPDATE（`available_stock >= q`）和“按规格 ID 升序加锁”的全局约定；**没有 Redis 锁或库存缓存**。
   网店后端测试跑在 SQLite 上，**没有真实 MySQL 的超卖并发测试**。
5. 节日促销（SHOP-TASK-229～232）**还没写代码**，设计上明确“不做活动限量”，不会形成第二个库存计数器。

## 2. 数据结构

| 对象 | 位置 | 与 Core 有关的字段 | 将来 |
| --- | --- | --- | --- |
| `Product` | `app/models/catalog.py:57–89` | `id` int；`slug` 唯一；`name_en/zh/ms`；`is_active`；`max_per_order`（每单限购 1–1000，**不是库存**）；`version` | 加 `core_product_id`（可空、唯一）；三语名称留网店，Core 只存一个基本名称（Q-05） |
| `ProductVariant` | `app/models/catalog.py:189–213` | `id` int；`product_id`；`sku` String(64) **全站唯一**；`price_sen`（营业价，整数仙）；`daily_initial_stock`；**`available_stock`**；`is_active` | 加 `core_sku_id`（可空、唯一）；`sku` 作为导入 Core 的 `sku_code` 初值；`price_sen` 留网店（渠道价）；生产环境 `daily_initial_stock` / `available_stock` 停用（Demo 继续用） |
| 规格选项 | `ProductOption`、`ProductOptionValue`、`VariantOptionValue`（catalog.py:140–246） | 颜色/尺码等多语言标签 | 留网店（展示资料） |
| `StockReset` / `StockResetLine` | `app/models/stock_reset.py`（迁移 0013） | 每日重置记录 | Demo 接 Core 后改记“调 Core 重置命令”的结果；生产不启用 |
| `SiteSetting.daily_stock_reset_enabled` | `app/models/site.py:74` | 每日重置开关，默认开 | Demo 保留（开关仍在网店，控制是否调 Core 的 Demo 重置命令）；生产必须强制关闭（不只是开关） |
| `Order` | `app/models/order.py:100–189` | `id` int；`order_number`（16 位 Base32，唯一）；`status`；`idempotency_key` 唯一；`payment_expires_at`；`paid_at` | `order_ref` 建议用 `order_number`（不含个人资料）；`payment_expires_at` 改为取 Core 返回的 `expires_at` |
| `OrderItem` | `app/models/order.py:192–232` | `variant_id`（可空，SET NULL）；快照 `sku`、三语名称、规格标签、`unit_price_sen`、`quantity`；`(order_id, line_index)` 唯一 | `line_ref` 建议用 `line_index`；加 `core_sku_id` 快照列（下单时固定，契约 5.1）；**没有快照 `product_id`** |
| 订单状态 | `order.py:37–54`；迁移表 `app/services/order_rules.py:44–56` | `awaiting_demo_payment → demo_paid → demo_packed → demo_shipped → demo_completed`，另有 `demo_cancelled` | 生产需要“部分发货”状态与按行/件数的发货记录（现在没有发货表，只有整单状态） |
| `RefundRequest` / `RefundLine` / `RefundLineUnit` | `app/models/refund.py` | 按件数部分退款；`requested/approved/rejected` | `approved` 且未发货的件数 → `restock_refund`；需要新增“确认未交运”的步骤 |
| `PaymentAttempt`、`StripeCheckout`、`StripeRefund` | `order.py:310–338`；`app/models/stripe.py` | 支付记录（模拟/Stripe 测试） | `payment_ref` 用网店支付记录 ID，不用 Stripe 的 ID |
| 迁移 | `alembic/versions/20260929_0002_catalog.py:143–176`（建列）、`…0003_demo_catalog.py:693–709`（种子）、`…0013_stock_resets.py`、`…0024_admin_catalog_schema.py`（重置开关） | 之后没有迁移改过库存列 | |

种子数据（迁移 0003）：6 个分类、30 件商品、76 个 SKU，初始库存合计 1058。**这些是 Demo 数字，不进生产（STK-03）。**

## 3. 写库存的全部位置（替换清单）

| # | 位置（函数） | 触发 | 现在做什么 | 并发/幂等 | 将来换成 |
| --- | --- | --- | --- | --- | --- |
| W1a | `app/services/ordering.py:356–375` `_reserve_stock`，被 `place_guest_order`（161–277，第 201 行）调用 | `POST /api/orders/guest` | 按规格 ID 升序逐行 `UPDATE … SET available_stock = available_stock − q WHERE available_stock >= q`；任一行失败回滚整单 | 条件 UPDATE；`Idempotency-Key` 头＋`orders.idempotency_key` 唯一＋请求指纹 | `StockGateway.reserve_order(order_number, lines, key)`；**不能**再与订单写入同一个数据库事务（见第 5 节） |
| W1b | 同一 `_reserve_stock`，被 `member_ordering.py:308–458` `_create_order`（第 385 行）调用 | `POST /api/member/orders` | 同上；加锁顺序：会员 → 规格 → 券 → 积分 | 同上 | 同上 |
| W2 | `app/services/payment.py:1329–1349` `_release_stock`，唯一调用者 `_cancel`（1303–1326，第 1320 行） | 见下表 7 个入口 | 按订单行合并件数，加回 `available_stock`；无上限保护 | 防重复靠 `_update_awaiting`（1287–1300）的条件 UPDATE：只有 `awaiting_demo_payment` 才命中 | `StockGateway.release(reservation_id, reason, key)` |
| W3 | `app/services/stock_reset.py:81–106` `reset_daily_stock` → `_reset_variants`（160–204） | 常驻进程 `python -m app.jobs run`（`shop_jobs` 容器，每 60 秒一轮，`jobs.py:245–285`），马来西亚 00:00 后首轮；手动 `python -m app.jobs reset-stock` | 锁全部规格，重建 `available_stock = max(daily_initial_stock − 待付预留, 0)`，覆盖当日调整 | `business_date` 唯一；READ COMMITTED | Demo：改为调 Core 的 `POST /v1/demo/resets`（契约 §6.1，target＝`daily_initial_stock`）；生产不存在，生产配置下任务必须拒绝运行 |
| W4 | `app/services/admin_stock_adjust.py:61–135` `adjust_available_stock` | `POST /api/admin/variants/{id}/available-stock` | CAS：`available_stock == expected` 才改成新值（0–9999），写审计 | CAS | Core 没有“直接改数量”（STK-02）。Demo 的当日调整改为调 Core 的盘点更正；另需收货 / 盘点 / 不可售 / 更正等新页面 |
| W5 | `app/services/admin_products.py:887–900`（`create_product` 第 893–895 行） | `POST /api/admin/products` | 新 SKU 的 `available_stock = daily_initial_stock` | 唯一约束冲突抛 `ProductChanged` | `CatalogGateway.create_product/create_sku`；新 SKU 在 Core 的余额为 0，入库走收货命令 |
| W6 | `admin_products.py:1156–1169`（`_insert_additions`，由 `save_product` 调用） | `PUT /api/admin/products/{id}` | 同 W5 | 同上 | 同 W5 |
| W7 | `admin_products.py:1313–1321`（`save_product`，`VARIANT_FIELDS` 含 `daily_initial_stock`） | `PUT /api/admin/products/{id}` | 改每日初始库存（不改当日可用） | 锁商品与规格行；商品 `version` 乐观校验 | 生产无此字段；价格/启停留网店，SKU 编码改动走 `CatalogGateway.update_sku`（正文带 `expected_version`，契约 §2.6） |
| W8 | `alembic/versions/20260930_0003_demo_catalog.py:693–709` | 部署时 `alembic upgrade head` | 插入种子商品与库存 | — | Demo：种子商品经目录导入进 Demo Core（契约 §11.2）；将来的生产库不能带这批种子（生产部署要能跳过，或种子改为 Demo 专用命令） |
| W9 | `app/services/site_settings.py:226–227` | `PUT /api/admin/site-settings` | 改每日重置开关 | — | 生产隐藏/拒绝该开关 |

### 3.1 释放（W2）的 7 个入口

| 入口 | 位置 | 将来 |
| --- | --- | --- |
| 本人取消 | `payment.py:481–522` `cancel_order`；`POST /api/pay/cancel`、`POST /api/member/pay/cancel` | `release(reason=customer_cancelled)` |
| 超时取消（后台） | `payment.py:981–1029` `expire_overdue_orders`；`jobs.py:210–217` 每 60 秒、每轮最多 100 张；CLI `cancel-expired` | **到期由 Core 关闭**（契约 5.2-4）。网店改为：按 Core 的 `expires_at` 把订单标为取消，并调 `release`（对已 `expired` 的预留是安全无操作） |
| 超时取消（懒检查） | `submit_payment`（388）、`cancel_order`（511）、`GET /api/pay/orders`（pay.py:240）、`POST /api/member/pay/order`、`GET /api/orders/lookup`、`POST /api/member/orders/view`、`start_checkout`（stripe_checkout.py:311） | 同上；查看类接口不应再触发库存写入，只读 Core 的预留状态 |
| 每日重置前 | `stock_reset.py:93` | 生产不存在 |
| Stripe 会话结束 | `payment.py:668–738` `cancel_after_stripe_end`；`stripe_overdue.py`、webhook `stripe_webhook.py:236`、`void_alerted_checkout` | `release(reason=payment_failed)` |
| 管理员作废告警结账 | `payment.py:912–978` `_void`；`POST /api/admin/stripe-pending/checkouts/{id}/void` | `release(reason=admin_verified)` |

## 4. 不写库存、但接 Core 后**必须新增**调用的位置

| 流程 | 现在位置 | 现在对库存 | 接 Core 后 |
| --- | --- | --- | --- |
| 模拟支付成功 | `payment.py:343–478` `submit_payment` / `mark_paid` / `_finish_paid`；`POST /api/pay/attempts`、`POST /api/member/pay/attempts` | 不动 | `commit_sale(order_ref, reservation_id, payment_ref, paid_at, key)`；**只有 Core 返回已确认后才能把订单标为已付款且库存已成交**（ORD-02）；`Unknown` 时订单进“支付已收、库存待确认” |
| Stripe 支付确认 | `stripe_confirm.py:215` `confirm_checkout`；`POST /api/pay/stripe/confirm`、会员版、webhook | 不动 | 同上 |
| 迟到付款 | `stripe_confirm.py:522–578` `_settle_late` | **自动退款**，不重新预留 | 改成契约 5.2-6：先 `commit_sale`，Core 有货则补扣（`LATE_SALE_COMMIT`）照常履约；缺货才进入 `late_payment_review` 由管理员决定退款或等补货（Q-26）。**这是网店现行规则与 Core 需求 ORD-03 的直接差异** |
| 打包/发货 | `POST /api/admin/orders/{id}/status`（admin_orders.py:154），整单 `demo_packed → demo_shipped` | 不动 | 需要新增按行/件数的发货记录与 `shipment_ref`，调 `record_dispatch` |
| 自动完成 | `jobs.py:288–295` `auto_complete_job` | 不动 | 不调 Core（完成不是库存事实） |
| 退款审核 | `admin_refunds.py:228、253`（approve/reject） | **不回补**（DESIGN 第 67 行） | `approved` 后，对**确认仍在库未交运**的件数调 `restock_refund`；已发货件数走退货验收 `receive_return`（需新增退货收货与验收流程） |
| 取消付款前订单 | 见 3.1 | 加回 | `release` |

## 5. 读库存的位置

| 位置 | 用途 | 将来 |
| --- | --- | --- |
| `app/services/catalog.py:153–158`、343、359（`_max_stock`、`sold_out_today`）；`GET /api/catalog/products` | 列表“今日售罄” | `get_availability`（批量），可短时缓存仅用于展示 |
| `catalog.py:106`、400–410（`VariantDetail.available_stock`）；`GET /api/catalog/products/{slug}`；前端 `ProductDetailPage.tsx:299` | 详情页**公开精确库存数** | 取 Core 的 `available`；是否继续对顾客公开精确数是网店产品决定（建议只显示有货/少量/售罄） |
| `app/services/checkout.py:203–296` `quote_cart`（236–250）；`POST /api/checkout/quote`、会员版 | 购物车/结账计价，`insufficient_stock` | `get_availability`；无锁读，只作提示，最终以 `reserve_order` 结果为准（现在也是这样） |
| `ordering.py:187–200`、`member_ordering.py:371–386` | 下单前先计价再扣 | 同上 |
| `admin_products.py:405–421` `load_admin_product` | 后台显示初始/可用库存 | `StockAdminGateway.get_balances`（四个账面量） |
| `admin_stock_resets.py:87–126`；`GET /api/admin/stock-resets[/{id}]` | 重置记录页 | Demo 专用 |

Redis 只用于限流（`app/services/rate_limit.py`），不涉及库存。

## 6. 改造时的结构性问题（不是逐行替换能解决的）

1. **事务边界变了。**现在“扣库存＋建订单＋券＋积分”在一个 MySQL 事务里，失败一起回滚。接 Core 后库存在另一个服务：
   网店要先在本地建“待预留”订单（带确定性幂等键），再调 Core；Core 成功后再推进；网店本地事务失败但 Core 已预留时，
   靠“同 `order_ref` 查询/重放”或到期自动释放兜底。需要网店侧一份小设计（结账状态机）——**这是网店改造里最需要过设计闸门的一块**。
2. **全局加锁顺序**（会员 → 订单 → 规格 → 券 → 积分）里的“规格”一环移到 Core 之后，网店本地剩下会员/订单/券/积分的锁，
   Core 调用不能放在持有本地行锁的事务里（否则 Core 慢时会长时间锁住会员行）。
3. **“预留”在网店没有实体**：接 Core 后网店订单要存 `core_reservation_id` 与 Core 返回的 `expires_at`。
4. **到期权威转移**：现在网店三层并存（后台任务、懒检查、付款条件 UPDATE）判断 15 分钟到期；接 Core 后以 Core 的 `expires_at` 为准，
   网店的付款条件 UPDATE 改为比较 Core 返回的时间，避免两端各算一个（ORD-01）。Stripe 订单“按 Stripe 定时处理”的特例要一起重新设计。
5. **退款与库存分离**：现在退款完全不碰库存；生产要在退款批准后加一个“确认这些件仍在库、未交运”的人工/系统步骤，再回库（ORD-04）。
6. **发货粒度**：现在只有整单状态，没有发货记录表；生产需要按行/件数发货（ORD-07），这是网店新功能。
7. **后台新页面**：收货入库、盘点（开单/录入/确认）、不可售/报损、更正、SKU 合并向导、晚到支付待办、每日对账差异待办——网店现有后台都没有（REQUIREMENTS §1 要求运营在网店后台完成这些）。
8. **Demo 与生产共用代码**：Demo 接 Core 后，每日重置改调 Core 的 Demo 命令，当日调整改为盘点更正；将来生产配置下，每日重置、种子迁移、重置开关都要彻底失效（不是默认关闭，而是不可开启）。
9. **测试**：网店后端测试跑 SQLite，没有真实并发；接 Core 后网店侧并发风险主要在“本地订单 ↔ Core 预留”的一致性，需要在 CI 里用 Core 的测试替身（或真 Core 容器）做集成测试。
10. **Demo 现行规则要改**（网店 DESIGN.md 第 6、12 条与 Stripe 第 6 条）：退款要按 Core 规则回库（已批准且未发货）；迟到付款从“自动退款”改为“有货补扣、没货交管理员（退款或等补货）”；Core 不可用时按最后可售量封顶继续收款（契约 §5.5）。这些要过网店自己的设计闸门。

## 7. 网店侧将来的最小数据变更（建议，不在本轮执行）

- `products.core_product_id`、`product_variants.core_sku_id`：可空、唯一；回填后生产要求非空才能上架。
- `order_items.core_sku_id`：下单时快照，之后不随映射变化（SKU 合并时由 Core 负责把订单行改挂，网店快照保留原值用于追溯）。
- `orders.core_reservation_id`、`orders.stock_state`（`pending_reserve / reserved / committed / commit_unknown / late_review / released`）。
- 每个调 Core 的动作持久化其幂等键（订单、支付、发货、退款回库、退货各自一列或一张“库存命令发件箱”表）。
- 按行/件数的发货记录、退货收货记录（新表）。

## 8. 每日对账要比较的项（ORD-08，网店发起）

对每个在对账窗口内有变化的订单行：

| 网店侧 | Core 侧（`GET /v1/reconciliation/order-lines`） | 不一致的典型原因 |
| --- | --- | --- |
| 订单已付款 | `committed_qty = quantity` | 确认命令结果未知后未补交 |
| 订单已取消（付款前） | 预留 `released/expired`，`committed_qty = 0` | 释放未送达（会在 Core 到期后自愈，但仍记差异） |
| 已发货件数 | `dispatched_qty` | 发货命令漏发/重复 |
| 已批准且确认未交运的退款件数 | `refund_restocked_qty` | 退款回库漏发 |
| 已验收退货件数（可售/不可售） | `returned_sellable_qty` / `returned_unsellable_qty` | 验收回执漏发 |
| 晚到支付待办 | 预留 `late_payment_review` | 管理员未处理 |

外加 Core 自查：四个账面量 = 流水累计（`/v1/ops/invariants`）。

## 9. 现有测试里将来要改写或作废的

- 纯重构阶段改为针对 `LocalStockGateway`，Demo 切到 Core 后大部分改写为针对 `CoreStockGateway` 或作废：`tests/test_guest_order.py`（562、765、769、788、830、871、933）、
  `tests/test_member_ordering.py`（392、709、800）、`tests/test_demo_payment.py`（893、1057、1101–1215）、
  `tests/test_payment_coupons_points.py`（488–598）、`tests/test_jobs.py`（356、589–684）、`tests/test_stock_reset*.py`、
  `tests/test_admin_stock_adjust.py`、`tests/test_admin_product_save.py:398`、`tests/test_admin_product_additions.py:400`、
  Stripe 取消/作废/超时/webhook 释放库存的用例。
- 新增：针对 `CoreStockGateway` 的契约测试（用 Core 的 OpenAPI 生成的替身）、`Unknown` 结果的重试/查询、迟到付款新规则、退款回库、按行发货。
- 前端：`AdminStockAdjust`、`AdminStockResetsPage`、`AdminProductsPage`（初始库存字段）在生产配置下隐藏；新增库存管理页面的测试。
