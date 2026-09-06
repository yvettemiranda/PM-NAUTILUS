# PM-SMALL → NautilusTrader 整体迁移设计

## 版本、依据及状态

2026-09-06 核对原项目远端 HEAD/main：`eb8c6d8a09f9b0427890b7a2d744fc3485d1d3dc`，没有相对于指令的新增差异。只读副本 `.reference/PM-SMALL` 排除 Git。已完整阅读 HANDOFF、DECISIONS、README、PLAN、SERVER_DEPLOY、SECURITY，并核对领域规则、配置、处理器、数据库关键路径及 UI/API。旧阶段禁写 LIVE 的限制由本次指令替代；旧运行数据和历史发布授权不继承。

选定依赖 `nautilus_trader==1.231.0`，发布源码 commit `27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`。已比较安装的 `2.0.0rc4`（commit `a0400251110653b6d8ae6a9b5b89c4543fa85a2d`）：v2 提供 Rust 适配器及 fill void，但 Python 执行客户端扩展与缓存写入接口不同。v1 稳定发布包含 CLOB V2/pUSD Python 适配器，可在应用层作必要窄扩展；无需复制或修改框架核心。以下实际代码只按 v1 wheel 的接口编写，latest 文档仅用于发现与查证差异。

Mac：macOS 26.6.2 arm64，CPython 3.12.14；v1 wheel 要求 macOS 26+ arm64。Linux：CPython 3.12，官方 wheel 提供 aarch64 与 x86_64，要求 glibc >=2.35，采用 Debian bookworm 容器（glibc 2.36），目标服务器架构待实际部署只读核对。依赖使用精确版本与 uv.lock，不在启动时追踪 latest。

公开 Gamma Event API 已实际返回带完整 markets、tags、feeSchedule 的 JSON。公开扫描不需要钱包。最终兼容性、回归及 Linux 执行证据在 VALIDATION.md；未执行项不能据此文档算通过。

## 应用结构与数据所有权

一个 Python 服务，FastAPI 托管复用的静态 UI 与业务 API；Nautilus Strategy、DataEngine、RiskEngine、ExecutionEngine、Cache、Portfolio 处理行情事件、订单状态、成交、账户和持仓。TEST/LIVE 运行上下文完全隔离，共用 rules/strategy。默认只创建 TEST 执行上下文，PAUSED；LIVE 配置门关闭时不得读取钱包或建立私有连接。

- `config.py`：所有现有字段的唯一合法范围、固定默认值和 API 转换。
- `rules.py`：整数微单位、资格、FAK、费用、目标批次和仲裁纯函数。
- `books.py`：完整性、独立 Bid/Ask 版本及 TEST 按价位消费投影；行情只按 token→event 索引调度。
- `store.py`：一个模式一套 SQLite 文件。原生 Nautilus 序列化事件是唯一成交事实来源；业务表只保存订单归属/提交意图、周期预算、目标、止损、配置、结算应收、赎回状态和重放游标。事件持久化先于业务投影；重放可恢复崩溃窗口，不另建独立 fills/positions 财务主账本。
- `strategy.py`：Nautilus Strategy 编排同一套规则；正式周期从真实非零 Fill 推进；异步买单提交先占用现金、暂锁 Event，终态或 Fill 才能释放/转换。
- `execution.py`：Nautilus 原生执行事件路径与 TEST 窄模拟扩展；保留完整 FAK、净份额费用和按价位消费。默认 Sandbox 的普通报价币手续费/深度刷新不能直接作为规则一致性证据。
- `live.py`：继承所选官方适配器；仅本程序订单；使用官方签名、账户、订单查询和框架核对。v1 对 FAILED 只日志、MATCHED 提前入账，故本应用必须等待官方 CONFIRMED 回报再形成可支配 Fill，未知/未确认资金及互斥继续占用，不能把取消请求当结果；迟到确认通过原生事件去重接入。该差异需要专门测试。
- `market.py`：公开全分页 Event 发现/栏目同步、元数据标准化、正式 Condition 结果；公开 WS 传输将完整快照/增量归一后送入 Nautilus DataEngine 的原生 OrderBookDeltas；避开官方数据工厂中不必要的私有环境凭据依赖，TEST 无私有连接。
- `redemption.py`：正式结算识别与实际回款分离；仅跟踪本程序已确认取得的权利。按所配置钱包/当前 collateral/standard 或 neg-risk 路径生成并提交官方合约交易；提交前持久化可恢复身份，重启查询原事务，收到成功收据并核对实际可用余额后才确认回款。TEST 使用同一状态机和模拟响应。
- `app.py`：API、控制来源与身份校验、500ms 轻量快照、模式隔离；前端不接触私钥。

## 规则对应表

来源均相对于采用的 PM-SMALL SHA。表中“框架+应用”代表交易事实由框架持有，业务约束由应用补充。验证须在 VALIDATION.md 给出实际执行结果。

| 规则 | 旧来源 | 新模块/责任 | 验证 |
|---|---|---|---|
| 开放 Event 全部分页、无日期截断及隐藏数量上限 | infrastructure/polymarket/market-data.ts streamOpenEventPages | market/应用 | 多页、末页、非重复游标、全遍历 |
| Event/Market 明确 active/closed/archived、acceptingOrders、enableOrderBook、Condition | domain/event-filter.ts | market + rules/应用 | 缺失布尔拒绝，身份不匹配拒绝 |
| 单 Market 二元、标准 neg-risk 原始完整 N、排除 augmented | domain/event-filter.ts, market-type.ts | market/应用 | 已关闭子市场不缩 N，2/3/4+ |
| 二元/三元默认开、多元默认关 | services/paper-trading-preferences-service.ts | config/应用 | 默认、保存、重置 |
| 官方首页 Tag 全选、动态同步与已退栏目交集 | infrastructure/polymarket/market-data.ts | market + config/应用 | 官方数据、同步失败缓存、不造标签 |
| 具体 Market 总时长，缺失回退 Event；1–365 整天 | domain/event-filter.ts, market-eligibility.ts | market + rules/应用 | 1/30/365 边界和剩余时间反例 |
| 已开始未结束、进度 1–100% 默认20%、体育开赛不新买 | domain/market-eligibility.ts | rules + strategy/应用 | 时间、开赛、旧仓仍退出 |
| 静态合格全部监控，价格高/空书不剔除 | domain/market-scanner.ts, services/candidate-service.ts | market + strategy/框架+应用 | 高价后进区、空书后恢复 |
| token→event 增量重算、READY 优先和 ASC/DESC | services/candidate-service.ts, paper-automation-service.ts | strategy/应用 | 单事件调度与排序 |
| 0.1¢–99¢ 闭区间/0.1¢步进、Bid/Ask 1–100% | domain/market-eligibility.ts, price.ts | config + rules/应用 | 边界、配置实际生效 |
| tick、最小量、完整盘口与动态费用必需 | domain/event-filter.ts, trading-strategy.ts | market + rules/框架+应用 | 无元数据拒绝、非法 tick |
| 全部静态兄弟完整；空书仅淘汰自身，未知阻断 Event | services/event-opportunity-service.ts | strategy + books/应用 | 未收到/重连/已空/active退出 |
| Preview 无副作用、共享 Bid 深度、当前资金预算 | domain/trading-strategy.ts, database.ts planTestFakBuy | rules + books/应用 | 预演前后快照一致、共享深度 |
| Terminal Target 最高、六级确定顺序和稳定身份 | domain/event-arbitration.ts | rules/应用 | 各比较层、并列、NO_FILL |
| 发单前重读配置/书/资金/锁 | database.ts, paper-automation-service.ts | strategy/应用 | 过时 winner 丢弃重算 |
| Ask FAK 多档、允许部分、剩余取消 | domain/trading-strategy.ts, database.ts | execution/框架+应用 | 多档、无成交、部分、上限 |
| 首 Fill 冻结1U周期、默认100U、新轮不自动放大 | database.ts executeTestFakBuy | strategy + store/应用 | 配置增大不扩旧轮、利润循环 |
| 提交临时占用与正式 Event 周期分离 | 旧同步提交由新要求扩充 | strategy + live/框架+应用 | 迟到/拒单/取消交错/未知 |
| 首 Fill 确认唯一 active、首次卖出后 Event 禁买 | database.ts Event locks/first_sell_at | strategy/应用 | 兄弟互斥、零成交不建周期 |
| 每实际 Fill 目标 min(99¢,tick↑max(price+A,price×B)) | domain/price.ts | rules + strategy/应用 | 多价 Fill、参数更新不追溯 |
| 低目标优先、同价时间/ID、小量聚合、最高目标限价 | domain/trading-strategy.ts planFakSellTargets | rules + strategy/应用 | 聚合、部分分配、共用深度 |
| 买入后立即复查卖出、不等下一行情 | services/paper-market-processor.ts | strategy/框架+应用 | 单次盘口即可退出 |
| 清仓无活动卖单释放，合格兄弟可重新仲裁 | database.ts | strategy/应用 | 全清下一轮换 winner |
| 止损开关/M首 Fill 冻结、毛份额加权均价 | domain/stop-loss.ts, database.ts | strategy + rules/应用 | 加仓重算、冻结不改 |
| Bid 严格低于，两个不同版本间隔>=30秒 | database.ts executeTestStopLoss | strategy + store/应用 | 29/30秒、重复、倒退、等于、空书 |
| ARMED 禁买，EXITING 不可逆、取消目标和买单 | database.ts | strategy/框架+应用 | 部分退出、价格恢复、部分目标剩余保护 |
| 触发后 Event 跨暂停/重启禁入，TEST reset 不清 LIVE | database.ts | store + strategy/应用 | 两模式独立与恢复 |
| 费用启用明确、rate 0–1/exponent整数0–10 | domain/event-filter.ts, trading-strategy.ts | rules + live/应用+框架 | 毛/净份额、净现金、实盘不双扣 |
| 估值为 Best Bid 扣预计费，空 Bid=0、未知=未知 | database.ts | app/框架+应用 | 五项摘要、未知和零区分 |
| 赎回应收不作可用现金，不双计资产 | 新要求扩充 | redemption + app/应用+框架 | 结算/提交/到账各阶段资金守恒 |
| START 续账，PAUSE 撤未成交买、卖/止损/结算/赎回继续 | services/paper-automation-service.ts | strategy + app/框架+应用 | 暂停持仓全流程 |
| 初始资金仅暂停无历史可改；TEST reset 双精确确认 | app.ts, preferences-service.ts | config + app/应用 | 合法/非法请求、固定默认 |
| reset代次隔离在途行情/结算/任务；不迁旧库 | services/paper-settlement-service.ts | runtime + store/应用 | 延迟回调和重启 |
| Condition正式closed+resolved/settled；1/0和0.5/0.5 | domain/paper-settlement.ts | market + redemption/应用 | 不明确等待、兄弟条件隔离 |
| 自动赎回 standard/neg-risk + 钱包/抵押资产核对 | 新要求及官方当前合约 | redemption/应用 | 可控RPC/余额/收据、幂等与恢复 |
| LIVE已知订单持仓核对，不接管手动 | 新要求、框架执行核对 | live + native cache/框架+归属过滤 | 外部订单、迟到Fill、重启差额 |
| 原生事件持久化→业务投影恢复 | 新要求替代旧SQLite跨远端事务假设 | store + strategy/框架+应用 | 崩溃窗口注入与幂等重放 |
| 原布局、320px、前20展开、Event聚合、分批记录 | src/web/*, app.ts | web + app/应用 | 手机/桌面/草稿/按钮/真实API |
| LIVE实际余额只读、服务器凭据、切视图不启动 | 新要求 | web + app + live/应用 | 模式切换、无泄密、TEST无私有连接 |
| Linux持久目录、健康、同库重启、Git版本、回滚 | docs/SERVER_DEPLOY.md + 新要求 | Docker/Compose/DEPLOY/应用 | 容器、来源校验、身份、重启 |

## 两处已确认的旧缺陷及修正定义

1. processor 的 `bidExternalVersion` 对完整 Bid 侧求哈希，数据库消费按 token+side+该哈希+price。10份仓、3.5¢只有5份，先卖5份，再只改变1¢档会更换整侧哈希，旧3.5¢消费查不到，可再卖5份。新规则按 `(mode,token,side,price)` 保存外部数量与已消费数量；未变化档保持消费，数量减少保守扣可用量，明确数量增加仅释放增加量，删除后重新出现视为该档新增。等价快照/重连/重启不重置；从未获知的重连间流动性不猜测补充。
2. `getOrderBook()` 使用 askExternalVersion，`planTestFakBuy()` 用同一 bookVersion 同时查 ASK 和 BID，但实际 SELL 用 bidExternalVersion。新 Preview 和 TEST 执行从同一逐价位可用盘口读取，独立 Bid/Ask 版本只用于状态完整性、仲裁新鲜度及止损确认，不混用消费键。

以上是纠正已证实错误，回归不追求重现旧错误成交数。

## 一致性、恢复与发布关口

配置/控制请求在本模式事件循环中串行执行；网络等待不得持有跨远端 SQLite 事务。保存提交意图及归属→占用→提交 Nautilus 订单→原生回报持久化→幂等业务投影→释放已确认终态剩余占用。未知请求不重发新订单。买入回报按实际价格生成目标；新 Fill 用接收时的当前 A/B；该目标价及首 Fill 冻结参数同时保存于原生成交事件，崩溃重放不读取后来配置重新计算。周期预算和止损设置以第一笔 Fill 当时规则冻结。

正式结算先留权利及应收，再提交赎回；签名或提交结果不明时保留交易标识和保守状态，只查询原交易。外部全钱包余额是实际账户事实，程序只按自身成交/赎回改变自有周期；不把手动仓纳入目标或赎回调用。若所选官方合约的赎回会覆盖同Condition手动资产，须验证并阻止自动赎回混合权利，避免接管。

开发先接通全路径，再集中完整回归；针对性用例验证高风险规则。发布验收包括真实公开行情+无资金模拟、可控订单/账户/赎回响应、重复/迟到/重启、资金数量关系、UI交互、Linux依赖。真实钱包/链上写不运行，另行真实验收清楚标记。

本地合理分段提交；首次上传目标与账户按指令集中确认；服务器只依据用户提供地址/用户/目录。交付 TEST + PAUSED，正式长期 TEST 和 LIVE 不由部署自动触发。

## 主要官方依据

- [所选发布](https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.231.0) 与该 wheel 内 Python 源码。
- [当前适配器文档](https://nautilustrader.io/docs/latest/integrations/polymarket/)：只作为核查差异入口，不能混作 v1 API。
- [Polymarket 仓位与赎回](https://docs.polymarket.com/trading/positions/manage)：pUSD 与标准/负风险抵押适配器。
- [Polymarket 当前合约](https://docs.polymarket.com/resources/contracts)。

赎回现金与 CONFIRMED 权利的 cash_included 标记随实际余额快照原子持久化；到账后不再把该金额叠加为未收应收，但权利持续跟踪直到原生 SETTLEMENT Fill 归账。本地结算意图没有远端订单副作用，重启后清理未执行意图并继续本地结算；已持久化 Fill 幂等重放。
