# PM-SMALL → NautilusTrader 迁移设计与当前架构

## 2026-10-20 自动空间维护增量

Docker 日志 10 MiB×3、每 15 分钟受限主机清理、每 30 秒运行时磁盘/inode 检查；WARNING 时每个已运行模式每分钟最多检查 128 条且结束超过 90 天的无归属公共缓存。主机使用率达到 80% 或可用不超过 6 GiB 时按保留规则回收旧镜像；运行时严重不足、检测失败或采样过期会阻止 START/新增 BUY，并暂缓批量扫描落库。已有交易及卖出/赎回继续遵守原所有权和先落账后发送规则，未明提交不释放或重下。 新增 `intents_token` 索引，删除仅限有严格所有权保护的公共 Token 缓存；不清理财务数据或改变策略参数。正式部署及完整验证已通过，固定应用 `640d1b1dbc58f2e61ef9cf3e131cd37a1e163de9`，运行及容量边界见 [自动存储维护](STORAGE_MAINTENANCE.md) / [最新验证](VALIDATION.md)。

## 2026-10-19 存储与调度增量

保留 `tokens(token_id,payload)`、全部历史及策略规则，新增兼容旧平面 JSON 的 v1 压缩外壳、活跃部分索引与独立扫描任务。已有库转码仅通过显式离线工具执行，不在正常启动隐式重写。格式、冻结字典、旧包/旧镜像边界、双库备份/逻辑核对及合成基准见 [本轮记录](METADATA_OPTIMIZATION_2026-10-19.md)；正式升级、双库离线整理和原 TEST 状态恢复均已验收，应用 `6dac94bf6bc9cbc954a22e112a52dd49a798d2a6`；实际文件减少 31.95%，全部旧逻辑数据保留，详见 [最新验证](VALIDATION.md)。

## 文档范围（2026-10-14）

本页保留原迁移依据与规则对应表，并说明当前实现。版本、服务器采样、发布证据和待完成的实盘验收以 [HANDOFF.md](../HANDOFF.md) 和 [VALIDATION.md](VALIDATION.md) 最新章节为准；[原始迁移要求](../PM-SMALL_Nautilus_Codex_Instructions.md) 保留为历史需求来源。

**2026-10-14 本轮精确持仓核对：**[修复 PR](https://github.com/yvettemiranda/PM-NAUTILUS/pull/12)，固定应用代码 `3e765ccb067c8012af5f4ea4f23c3bda91d7372f`；回归：705 项 Python / 33 项页面回归、Ruff/JS 与 PR/main 双架构 CI 通过，明细见 [最新验证](VALIDATION.md)；服务器验收：2026-10-14 19:03 CST 通过，TEST/RUNNING、LIVE 未启用、钱包 LOCKED；实际持仓与授权只读核对通过，资金/POL 与地区仍未满足条件。Data API 全分页仅发现 token，精确自有份额来自本次固定区块的 CTF 原子余额；严格零收益外部残仓的例外不改写归属或策略/资金账本。已有 Safe 正确重连，升级前交易所及两个赎回适配器权限只读核对通过，资金/POL 和地区仍须满足条件。最新证据见交接及验证文档。既有策略参数、筛选、FAK、预算、目标和止损条件保留；代码和受控响应测试不能替代真实买卖及正额赎回验收。

2026-10-13 历史优化由 [PR #8](https://github.com/yvettemiranda/PM-NAUTILUS/pull/8) 发布部署，固定应用代码 `b805c4f11bffe09c0bc5dc610e106223a94025aa` 的双架构 CI 与服务器完整 TEST 验收通过；账本和规则保留。10-14 `512c8a2` 后续修复亦已部署验收。BUY 精度修复对齐锁定 SDK，纠正部分旧模拟数量；旧错误成交数不是必须保持的策略规则。

10-14 [PR #10](https://github.com/yvettemiranda/PM-NAUTILUS/pull/10) 已发布部署代码 `fefcdb3963f57ceaea8f0eba93686c6024dc1f0f`，591 项 Python/33 项页面与 Ruff（85 文件）、PR/main 双架构 CI/Docker verify 和服务器完整验收通过。13:36 CST 状态 TEST/RUNNING、LIVE 未启用、钱包 LOCKED，原规则/账本保留；旧 `512c8a2` 证据留在有日期历史。新代码资金地址留空只读发现已有 canonical 单签 Safe，失败拒绝，不猜测 EOA。没有交易或在途时，用户密码确认清除规范未签名准备及旧绑定后重导，nonce 下限保留，不自动改已有 vault。完整路径仍只支持 EOA(0)/单签 Safe(2)，EOA 允许交易名单资格未验证，Deposit Wallet(3) 不支持。

## 原迁移版本与依据（2026-09-06）

2026-09-06 核对原项目远端 HEAD/main：`eb8c6d8a09f9b0427890b7a2d744fc3485d1d3dc`，没有相对于指令的新增差异。只读副本 `.reference/PM-SMALL` 排除 Git。已完整阅读 HANDOFF、DECISIONS、README、PLAN、SERVER_DEPLOY、SECURITY，并核对领域规则、配置、处理器、数据库关键路径及 UI/API。旧阶段禁写 LIVE 的限制由本次指令替代；旧运行数据和历史发布授权不继承。

选定依赖 `nautilus_trader==1.231.0`，发布源码 commit `27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`。已比较安装的 `2.0.0rc4`（commit `a0400251110653b6d8ae6a9b5b89c4543fa85a2d`）：v2 提供 Rust 适配器及 fill void，但 Python 执行客户端扩展与缓存写入接口不同。v1 稳定发布包含 CLOB V2/pUSD Python 适配器，可在应用层作必要窄扩展；无需复制或修改框架核心。以下实际代码只按 v1 wheel 的接口编写，latest 文档仅用于发现与查证差异。

最初核对的 Mac 为 macOS 26.6.2 arm64、CPython 3.12.14；v1 wheel 要求 macOS 26+ arm64。当前 Docker 基础镜像固定为 `python:3.12.14-slim-bookworm`（glibc 2.36）；[HANDOFF 的 2026-09-07 部署记录](../HANDOFF.md) 核对过现有服务器 Ubuntu 24.04 amd64，2026-10-12 更新记录没有重新采集系统版本。Ubuntu x86_64/ARM64 CI 均执行锁定依赖测试与 Docker verify。官方 Linux wheel 要求 glibc >=2.35。依赖使用精确版本与 uv.lock，不在启动时追踪 latest；新服务器仍须核对其实际架构与兼容性。

公开 Gamma Event API 已实际返回带完整 markets、tags、feeSchedule 的 JSON。公开扫描不需要钱包。最终兼容性、回归及 Linux 执行证据在 VALIDATION.md；未执行项不能据此文档算通过。

## 应用结构与数据所有权

一个 Python 服务，FastAPI 托管复用的静态 UI 与业务 API；Nautilus Strategy、DataEngine、RiskEngine、ExecutionEngine、Cache、Portfolio 处理行情事件、订单状态、成交、账户和持仓。TEST/LIVE 的配置、执行上下文、盘口消费、订单、账本及资金独立，共用 rules/strategy 和公开市场发现/行情连接；共享公开输入不会合并两套交易状态。默认只创建 TEST 执行上下文，PAUSED；LIVE 未启用时不建立私有交易连接，加密钱包须通过独立导入/解锁流程操作。

- `config.py`：所有现有字段的唯一合法范围、固定默认值和 API 转换。
- `rules.py`：整数微单位、资格、FAK、费用、目标批次和仲裁纯函数。
- `books.py`：完整性、独立 Bid/Ask 版本及 TEST 按价位消费投影；行情只按 token→event 索引调度。
- `store.py`：一个模式一套 SQLite 文件。原生 Nautilus 序列化事件是唯一成交事实来源；v2 `business_parts` 按周期/目标/赎回记录分行保存业务投影，按 Event 增量更新，同一 PM-NAUTILUS 账本的旧存储格式可自动迁移。订单归属、提交意图、配置和重放游标仍由应用持有。事件持久化先于业务投影，事务失败或 SQLite 自动回滚均清除未提交的比较缓存；重放可恢复崩溃窗口，不另建独立 fills/positions 财务主账本。
- `strategy.py`：Nautilus Strategy 编排同一套规则；正式周期从实际非零 Fill 推进；异步买单提交先占用现金、暂锁 Event，确认终态或 Fill 才能释放/转换。未变 Event 不重复预演；已确认零成交终态会安排下一轮约 5 秒定时控制重新评估，覆盖 BUY、目标卖出和止损退出，未知订单仍保持锁。Condition 元数据索引随映射变更维护，历史执行身份用 SQL 读取；网页 START 异步完成完整 SQLite 校验，再在所属事件循环核对当前状态，不重复执行同步扫描。
- `order_terms.py`、`execution.py`：TEST/LIVE 共用整数出站条款。BUY 的现金金额保留 SDK 整分精度，份额按 tick 对应 amount 精度 canonical 向下量化；Decimal 修复浮点少取一分，TEST 同步去掉旧 0.01 份粗取整。SELL 按 SDK 可签名的 0.01 份向下量化、最低价按 tick 向上量化，并保留最小订单约束；尾仓继续作为自有权利跟踪。TEST 从逐价位可用深度生成原生执行事件，BUY/SELL 使用 CLOB V2 的 pUSD 现金手续费模型，实收份额按成交份额入账。默认 Sandbox 的手续费/深度刷新不能直接作为规则一致性证据。
- `live.py`：继承所选官方适配器；仅本程序订单；使用官方签名、账户、订单查询和框架核对。SELL 向锁定 SDK 传递精确 Decimal 份额，避免 5.02 被浮点截为 5.01，并核对签名份额及价格；BUY 只窄适配金额算术，官方签名、手续费及余额保护保留。只接受 canonical 精度下限导致的签名比值差异，下一合法档及真实 Fill 的原始限价继续检查。BUY 签名准备与只读钱包检查可并行，实际 POST 前仍核对全部安全条件，每笔新买使用新账户结果。启动原生报告按 token 汇总正数量未结算自有权利，用链上精确余额确认覆盖；等待后重新读取最新权利，新增未读取 token 或不足份额拒绝启动。每 token 只报告本程序总权利，不导入手动资产；订单恢复完成后在 ready 前最终新查份额。旧 10-13 的 12 仓整钱包 API 13→2 次是历史对照，当前使用有界链上余额批次。等待官方 CONFIRMED 回报再形成可支配 Fill，未知/未确认资金及互斥继续占用，不能把取消请求当结果。FAILED 等回报在修改预约前也检查 Condition/token/方向/精确正份额；单笔 FAILED、累计确认加失败数量及 REST matched 不得超出持久化原生上限，异常保留占用。更优价格 BUY 可超过签名最低接收量，该值不被误当原生上限；已提交确认不回退。写盘失败清临时去重，已写盘但未送达的原事件恢复分发，重复确认不重复写入。REST 释放占用前必须确认订单/Token 身份、完整非负成交数量和已最终处理的相关交易；包括市场结算取消在内的终态遵守同一条件。迟到确认通过原生事件去重接入。
- `live_readiness.py`、`position_custody.py`：就绪检查完整分页发现 token，固定本次新取 Polygon 区块，以 CTF 六位原子余额精确核对自有份额；保留已知零数量 token 的身份，正数量权利在 API 缺行时也查链上。仅未知外部、官方 pUSD/USDC.e、空父集合的标准二元 CTF token 可证明零收益例外；Condition/index/两个 token 的派生身份、已结算 payout 分子之和及所持结果严格零分子、另一结果零余额均须通过。API 数量/价格/价值/`redeemable` 不作证明；正收益、Neg-Risk、未结算、身份不明或 RPC 失败继续阻止或 UNKNOWN。不烧毁、赎回、接管或写入这些外部残仓。
- `market.py`：公开全分页 Event 发现/栏目同步、元数据标准化、正式 Condition 结果由 `PublicMarketHub` 共享，WS 按两种模式所需 token 的并集订阅，范围大幅缩小时合并稀疏组并释放多余连接。迁移 token 在等待旧 socket 关闭前失去 READY；TEST 缩范围或重置不移动仍由 LIVE 使用的订阅。各模式分别应用行情、消费深度和策略规则。原生 OrderBookDeltas 保留完整性检查并增量更新。故障按受影响订阅与模式隔离，恢复后用户 START 的确认不会被旧 Hub 错误覆盖；TEST 无私有连接。
- `redemption.py`：正式结算识别与实际回款分离；公开结果 GET 用有界并行任务，独立于控制检查，状态变更留在所属事件循环。钱包 token 余额每批固定本次新取区块、最多四个并行请求，校验规范 uint256 身份/响应，空集合不访问 RPC，不缓存跨笔结果。仅跟踪本程序已确认取得的权利。按所配置钱包/当前 collateral/standard 或 neg-risk 路径生成并提交官方合约交易；授权与赎回共享账户级待确认占用，提交前持久化原 raw/hash/nonce，未知结果跨重启只能恢复原交易。消费 nonce 下限与确认同事务保存，保存失败不广播；取消线程工作仍等待完成和持久化后释放所有权。收到成功收据并核对实际可用余额后才确认回款。TEST 使用同一状态机和模拟响应。
- `app.py`、`views.py`、`performance.py`：API、控制来源与身份校验、全仓摘要和有界历史/权益采样。所有摘要仍按全部仓位、完整 Bid 深度和全部权利计算，明细只按当前面板/数量构建；采样复用资金计算，不生成列表。记录以首次 Fill 序号分页，保留总数量及增量游标，迟到 Fill 更新原订单，不受一万条前缀上限阻断。用户导入的助记词/私钥由自有 HTTPS 网页传给后端加密保存，API 不把已保存的明文密钥或 API 凭据返回前端；解锁后签名密钥会留在服务器内存。部分 LIVE 启用也归应用生命周期管理，请求取消时完成资源关闭；备份/恢复/授权请求取消时保持钱包锁直到实际工作线程结束。应用关闭保留进程锁到清理结束，启动后安全清理专用遗留临时目录。前端按模式丢弃旧缓存及过期控制响应，缓存格式器和未变化分类节点，仅轮询可见浏览器页；隐藏页恢复后立即重查。

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
| 费用启用明确、rate 0–1/exponent整数0–10 | domain/event-filter.ts, trading-strategy.ts | rules + live/应用+框架 | 实收份额、pUSD现金费、含费预算、不双扣 |
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

已持久化的原生日志按前缀投影，后来的取消不能跨过已经保存但尚未完成原生分发的 Fill。内存去重标记不充当持久事实，写入失败必须允许同一回报重试。纯计算优化（整数预算/进度、首 SELL 批次、单次排序、Bid 哈希缓存）用边界和差异回归核对；BUY 精度纠错单独标明预期变化，不以旧误差当作策略规则。

正式结算先留权利及应收，再提交赎回；签名或提交结果不明时保留交易标识和保守状态，只查询原交易。外部全钱包余额是实际账户事实，程序只按自身成交/赎回改变自有周期；不把手动仓纳入目标或赎回调用。若所选官方合约的赎回会覆盖同Condition手动资产，须验证并阻止自动赎回混合权利，避免接管。

开发先接通全路径，再集中完整回归；针对性用例验证高风险规则。发布验收包括真实公开行情+无资金模拟、可控订单/账户/赎回响应、重复/迟到/重启、资金数量关系、UI交互、Linux依赖。真实钱包/链上写不运行，另行真实验收清楚标记。

本地合理分段提交并通过普通 Git 工作流同步现有公开仓库；服务器操作只依据 HANDOFF 中当前有效的地址、用户、目录和授权。应用首次启动或重启默认 TEST + PAUSED；维护升级可在完整核对后恢复此前已授权的 TEST RUNNING。LIVE 解锁、启用及 START 是独立控制动作，不由部署或切换页面触发。

## 主要官方依据

- [所选发布](https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.231.0) 与该 wheel 内 Python 源码。
- [当前适配器文档](https://nautilustrader.io/docs/latest/integrations/polymarket/)：只作为核查差异入口，不能混作 v1 API。
- [Polymarket 仓位与赎回](https://docs.polymarket.com/trading/positions/manage)：pUSD 与标准/负风险抵押适配器。
- [Polymarket 当前合约](https://docs.polymarket.com/resources/contracts)。

赎回现金与 CONFIRMED 权利的 cash_included 标记随实际余额快照原子持久化；到账后不再把该金额叠加为未收应收，但权利持续跟踪直到原生 SETTLEMENT Fill 归账。本地结算意图没有远端订单副作用，重启后清理未执行意图并继续本地结算；已持久化 Fill 幂等重放。
