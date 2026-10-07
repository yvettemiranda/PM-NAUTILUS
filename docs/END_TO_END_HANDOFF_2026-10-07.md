# PM-NAUTILUS 全流程审计与新对话交接（2026-10-07）

> **先看结论：目前的 TEST 是接真实公开行情的模拟交易，不能代表真实订单一定会以相同价格、数量和费用成交。LIVE 的网页接钱包、核对、发单、退出和赎回程序框架已存在，但本轮查出几个会影响实盘资金和结算的缺口。当前服务器的 Polymarket 地区检查也明确阻止新开仓。现在不能把“网页检查通过”理解为“已完成真实资金全流程验收”，不建议直接点击 LIVE START。**

本页面向下一段对话及不熟悉编程的使用者。它是 **2026-10-07 的只读审计快照**，不是未来永远有效的运行状态。先修复下列优先项并重新验证，再讨论首次小额实盘。用户现有策略金额和筛选规则应保留；本审计没有提出新的总投入、单笔金额或交易笔数限制。

## 1. 本轮核对到了哪里

| 对象 | 2026-10-07 的证据与边界 |
|---|---|
| [公开 GitHub 仓库](https://github.com/yvettemiranda/PM-NAUTILUS) | 审计起点 `main` 为 `0637d3e8e31da61e3420e8a886d14157e327a513`；[Actions 37470838966](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/37470838966) 成功。该提交相对运行代码仅改说明文档。本交接文件发布后，文档提交的 SHA 会更新，不能用旧 SHA 判断最新文档。 |
| 服务器 | 2026-10-07 10:54 CST 的只读巡检：服务器源码 `0637d3e…`，运行镜像代码 `7802c181a4966add09b8045cce686a5ccbfbae45`，容器 healthy、零重启；`/api/health` 返回 `status=ok`、`mode=TEST`、`strategyStatus=RUNNING`、`liveExecutionEnabled=false`、`liveWalletStatus=UNCONFIGURED`、`backgroundErrors={}`。源码与镜像 SHA 不同仅因上述文档提交。SQLite `quick_check=ok`。这是一个时间点的采样。 |
| TEST 完整账本核对 | 此次只读巡检调用 `/api/TEST/validation` 超过客户端 20 秒超时，**没有取得本轮完整验证结果**；超时并不等于验证失败。随后公网 `/api/health` 在约 0.7 秒返回正常。勿把健康接口代替完整账本验证。 |
| 服务器地区 | 2026-10-06 从该服务器直接请求官方 geoblock，返回 `blocked=true`、`US/CA`。当前出口不能用于 API 新开仓，换服务器后须从**新服务器**重测。[官方地区规则](https://docs.polymarket.com/api-reference/geoblock)。 |
| 钱包与真实交易 | 服务器未导入或启用真实钱包；没有真实订单签名、提交、成交、卖出、授权、赎回或链上写入。本轮也没有执行这些动作。真实钱包类型、资金、额度和赎回授权仍未核实。 |
| 本地回归 | 锁定依赖环境下 `pytest -q` **207 项通过**；Ruff 检查与格式、JS 语法、7 项前端测试通过。独立公开行情短测取 100 个 Event 样本、10 个选中 Event、60 个监控 token，10 个盘口 READY、10 个模拟成交/持仓，账本与重启验证通过，最终 PAUSED、私有连接 0。短测采用临时开发设置，不是服务器当前规则，也不是全面扫描或 LIVE 验收。 |
| 网页实测 | 2026-10-06 的旧记录已验证 LIVE 页面和齿轮滚动；**本轮未重新完成 Chrome 浏览器交互**，所以以下 UI 新发现主要来自源码和离线测试。不能把前次网页观察说成本轮实测。 |

当前对外网页入口是 [https://43.159.133.129/](https://43.159.133.129/)；迁移服务器后地址、证书和地区都要重新核对。它是服务器上的程序页面，不是钱包或交易所网页。

## 2. 从网页点击到资金回来的真实路径

| 环节 | TEST 实际做的事 | LIVE 代码预期做的事 | 本轮结论 |
|---|---|---|---|
| 服务器启动与登录 | 启动 TEST，默认 PAUSED；网页经 HTTPS/Nginx 认证访问。 | 进程重启仍先回 TEST/PAUSED；LIVE 钱包要重新解锁、检查、启用，之后仍是 PAUSED。 | 启动逻辑和离线回归已查；本轮未重启服务器。见 [app.py](../src/pm_nautilus/app.py)、[部署手册](DEPLOY.md)。 |
| TEST/LIVE 切换 | 点击顶部 TEST/LIVE 切换的是当前**页面视图**。 | 切换视图不会替另一模式按 PAUSE；已经 RUNNING 的模式仍可继续。 | [app.js](../src/pm_nautilus/web/app.js) 与 [app.py](../src/pm_nautilus/app.py) 确认。尤其离开 LIVE 页面不能当成暂停实盘。 |
| 发现市场与行情 | Gamma 全分页读取开放 Event，订阅公开订单簿；用真实报价做筛选。 | 同一套发现、规则和盘口输入服务于 LIVE；两种模式设置分开保存。 | 真实公开行情短测和源码确认；短测不是长期稳定性或全部 Event 的现场证明。见 [market.py](../src/pm_nautilus/market.py)、[strategy.py](../src/pm_nautilus/strategy.py)。 |
| 资格、Event 仲裁与预算 | 使用公开参数和盘口，预演买入；每个 Event 的轮次预算、目标和止损状态写入 TEST 账本。 | 复用相同筛选与仲裁规则，再以真实账户、订单和持仓核对决定能否提交。 | 共用“选什么”规则不等于共用“怎么成交”；两套设置与账本独立。见 [rules.py](../src/pm_nautilus/rules.py)、[strategy.py](../src/pm_nautilus/strategy.py)。 |
| 买入 | [execution.py](../src/pm_nautilus/execution.py) 按当时公开 Ask 深度立即生成模拟 Fill。 | [live.py](../src/pm_nautilus/live.py) 先签名、核对账户、发送 FAK；只把交易所最终 `CONFIRMED` 回报入账。FAK 可能部分成交或不成交。 | **不等价。** 盘口时延、签名数量、费用和成交结果不能由 TEST 证明。官方说明见[下单](https://docs.polymarket.com/trading/place-orders)。 |
| 失败与未知结果 | 模拟执行同步结束。 | 代码已区分“确定未发出”和“可能已发出但结果未知”：前者终结本地意图，后者暂停新买并保留占用等待对账；不能盲目重试。 | 离线回归覆盖，未经过真实私有接口故障演练。见 [live.py 第 322 行附近](../src/pm_nautilus/live.py)。 |
| 账户核对 | 模拟现金和持仓按 TEST 账本更新。 | 查询 CLOB/pUSD 余额、开放挂单、Data API 持仓及同 Condition 链上份额；程序外挂单或不符份额阻止继续新买。 | 只读检查有代码和测试，尚无该钱包的现场结果；“检查通过”不等于真实下一笔可成交。见 [private_preflight.py](../src/pm_nautilus/private_preflight.py)、[live_readiness.py](../src/pm_nautilus/live_readiness.py)。 |
| 目标卖出与主动止损 | 依据公开 Bid 深度模拟卖出。当前公开规则 `stopLossEnabled=false`。 | 自有持仓按目标或止损状态尝试真实卖出，实际 Fill 和账户变化决定结果。止损若将来启用，需满足盘口版本/时间条件；卖出也可能不成交。 | 代码和离线测试通过，不代表 LIVE 能以页面所示估值立即卖出。见 [strategy.py](../src/pm_nautilus/strategy.py)、[rules.py](../src/pm_nautilus/rules.py)。 |
| 市场结算与回款 | TEST 在模拟账本中关闭/结算。 | LIVE 要等链上结果、检查份额与授权、签 Polygon 交易、等待足够确认、读回实际余额，之后才关本程序权利。 | 当前网页导入路径有赎回授权缺口，真实全链路未验收。见 [redemption.py](../src/pm_nautilus/redemption.py)。 |
| 资金、收益和记录 | 基于模拟余额、持仓标价、应收权利展示。 | 现金来自真实账户查询，收益仍依赖本地持仓/费用/赎回模型；曲线 H/D 对采样聚合。 | 页面数字不是银行流水，也不是按完整盘口马上平仓后的金额；下文列出已发现的具体误导点。见 [views.py](../src/pm_nautilus/views.py)。 |
| PAUSE、重启与恢复 | PAUSE 停新买，旧 TEST 账本恢复。 | PAUSE 停新买并处理买单；已有真实仓位的退出/结算维护仍应继续。重启后 LIVE 锁定，解锁核对前不能假定没有持仓。 | 逻辑和离线测试存在；生产重启后真实账户与账本的完整闭环未验收。 |
| 备份与换服 | 旧模拟记录可以按用户选择丢弃。 | 一旦有真实订单/持仓/待回款，必须连同钱包加密文件迁移**最终静止状态**的完整 LIVE 账本，并在新服核对。 | 当前网页 `.pmnb` 是某一时刻的一致快照；运行中下载后若继续交易，快照会落后。见第 4 节。 |

当前公开 [规则文件](../config/strategy-profile.json) 是：每个 Event 每轮 `orderAmount=1U`、买价 1–3¢、目标价至少 +1¢ 或 1.5 倍、主动止损关闭，另有类别、期限、进度和 Bid/Ask 条件。它是**当前公开预置**，不代表服务器后续页面更改会自动写回 GitHub；TEST 与 LIVE 各有自己的设置，换服首次启动前须分别导入并核对。

## 3. 发现的问题与修复优先级

### P0：完成这些后才讨论首次 LIVE START

1. **每轮金额可能没有包括实际买单手续费。** 当前 LIVE 给 SDK 的买入 `amount` 是本地预算 `i["cash"]`（[live.py 第 432–450 行](../src/pm_nautilus/live.py)）；[官方下单说明](https://docs.polymarket.com/trading/place-orders#cap-market-buy-spending)明确 MARKET BUY 的 `amount` 是手续费前金额，若要把总支出限制在该金额内，应使用 `maxSpend`。锁定 SDK 的余额保护只在账户余额不足时才缩小订单；钱包有足够余额时，1U 策略单可能实际多扣手续费。当前本地成本及轮次预算只计 `cost(price, gross)`，未证明与真实扣款相等（[live.py 第 279–313 行](../src/pm_nautilus/live.py)、[strategy.py](../src/pm_nautilus/strategy.py)）。离线 SDK 签单对照已证实签名金额路径差异；**实际钱包扣款数仍须在安全修复后用真实回报核对**。修复目标是维持用户原来的“每轮 1U”规则作为真实含费总支出，而不是另加交易限制。
2. **TEST 与 LIVE 的份额/手续费算法可能不符。** [rules.py 第 45–52 行](../src/pm_nautilus/rules.py)按 6 位微单位向上取整；[官方费用说明](https://docs.polymarket.com/trading/fees)规定 5 位小数精度。LIVE 又用这笔估算费用推算净份额（[live.py 第 288–291 行](../src/pm_nautilus/live.py)），后续 [链上份额核对](../src/pm_nautilus/live_account_guard.py)、[买前核对](../src/pm_nautilus/live.py)和[赎回](../src/pm_nautilus/redemption.py)要求与本地精确相等。离线例子：1U、2.1¢、4% 费率，本地值 0.039153U，按官方 5 位精度为 0.03915U。需要以锁定 SDK、官方当前规则及真实回报统一**实扣资金、实际到手份额、佣金和账本**；不能靠放宽核对来掩盖差额。另外 1U、2¢ Ask、3¢ 限价的离线例子中，TEST 预演 50 份，而 MARKET BUY 签名用限价计算约 33.33333 份，成交量不能直接类推。
3. **新网页钱包可能买得进，却不能自动赎回。** 网页导入默认 `auto_approve_redemption=False`（[live_web_setup.py 第 106–112 行](../src/pm_nautilus/live_web_setup.py)）；网页没有授予 CTF 赎回适配器授权的操作。若现有钱包未预先授权，[redemption.py 第 117–125 行](../src/pm_nautilus/redemption.py)会拒绝继续；目前只读就绪检查主要看 pUSD/交易所的正额度，未以 CTF 适配器授权作为完整退出的准入条件（[private_preflight.py 第 94–115 行](../src/pm_nautilus/private_preflight.py)）。需要补一个清楚、可确认且能核对链上结果的授权流程，或明确在未授权时阻止首次真实买入。不要把“READY”宣称为已验证能赎回。
4. **换服务器时运行中的网页备份可能漏掉之后的真实交易。** [live_backup.py 第 80–98、160–173 行](../src/pm_nautilus/live_backup.py)用 SQLite 制作一致快照，但没有冻结后续下单/卖出/赎回。若先下载 `.pmnb`、再让旧服务器继续交易、最后停机，恢复的历史可能落后。需要改操作说明和流程：先确认订单和链上处理状态，停止旧实例产生新事件，取得**最后一份静止账本与加密钱包文件**，在新服恢复后核对账户、订单、链上权利再允许 START。若希望全程网页迁移，必须另行实现可靠的停写/最终导出，不能把当前下载按钮称为无须进服务器的完整迁移。

### P1：修正可能误导操作或造成交易中断的地方

5. **切换 TEST/LIVE 页面时可能短暂显示上一模式参数。** [app.js 第 1382–1405 行](../src/pm_nautilus/web/app.js)清除 dashboard，却保留 `ui.preferences` 和表单旧值；[钱包摘要与资金标签](../src/pm_nautilus/web/app.js)会先按新模式重绘。若 LIVE 数据慢或失败，可能把 TEST 的 100U 模拟初始资金放在“实际账户余额（只读）”标签下，也可能出现旧设置草稿。成功加载后会纠正，但用户应在加载完成前看见“未核对/加载中”，相关保存与启用按钮也应等对应模式数据到齐。
6. **LIVE RUNNING 缺少跨视图持续可见的提醒。** TEST/LIVE 按钮切的是视图，不是运行开关；[app.js](../src/pm_nautilus/web/app.js)、[app.py](../src/pm_nautilus/app.py)的两个状态可同时存在。界面应一直说明真实交易是否 RUNNING，避免用户回到 TEST 后误以为 LIVE 已暂停；真正暂停请按 LIVE 的 PAUSE，并核对返回状态。
7. **资金与收益图有估值口径问题。** [views.py 第 53–57 行](../src/pm_nautilus/views.py)以最优 Bid 给整个持仓标价，没有按各档买盘深度计算可卖现金；只有 1 份在 5¢ Bid 时，100 份可能被标成 5U，实际这一档只可卖出 0.05U。[views.py 第 82–96 行](../src/pm_nautilus/views.py)把 `FAILED` 赎回的预计金额仍算入待回款、总资金和未实现盈亏。应明确区分标价、可执行卖出估计、已到账余额和失败/待核对权利；收益曲线沿用同一估值口径，不能称为已实现收益。
8. **真实市场类型和最小价格档位要显式兼容或排除。** [market.py 第 42–119 行](../src/pm_nautilus/market.py)按 `clobTokenIds` 归一化，但没有按市场 `version` 明确区分 CTF 与 Protocol V2。当前官方[下单说明](https://docs.polymarket.com/trading/place-orders)区分 CTF 的 `tokenId` 与 V2 的 `positionId`，而现有赎回写死 CTF 路径。[官方下单说明](https://docs.polymarket.com/trading/place-orders)还列出 0.005、0.0025 等 tick；锁定 SDK 的本地 builder 仅列出 0.1/0.01/0.001/0.0001，若接到不支持的 tick，可能在签单时失败并暂停。2026-10-07 抽样最新 100 个 Event 的 205 个市场均为 v1、tick 0.01，**未见到问题不等于所有市场都兼容**。先明确拒绝不支持版本/tick，或完成对应适配与回归。
9. **LIVE 锁定时历史可能显示为空。** [app.py 第 792–821 行](../src/pm_nautilus/app.py)在 LIVE 未构建运行时的记录、曲线接口返回空数组，即使冷账本有历史。余额/仓位已显示未核对，但记录区仍可能显示“暂无交易记录/收益数据”。应显示“已锁定，历史暂不可读”或只读读取本地历史，不要暗示从未交易。

### P2：运维与易用性

10. `/api/health` 已覆盖运行时故障、恢复错误、LIVE 账户错误和维护任务停止（[app.py 第 411–445 行](../src/pm_nautilus/app.py)），但健康成功只说明这些检查当时无故障。现有只读巡检重点是 TEST；增加 LIVE 专项的账户、订单、赎回、账本与状态巡检，且区分“网页检查失败”和“运行时必须暂停”。本轮 `/api/TEST/validation` 20 秒超时要另行查明原因与可接受执行时间，不可直接忽略或重复判定失败。
11. 钱包网页只接受 EOA 或单所有者 Safe（[live_web_setup.py](../src/pm_nautilus/live_web_setup.py)、[preflight.py](../src/pm_nautilus/preflight.py)）；[官方钱包认证说明](https://docs.polymarket.com/trading/wallets-auth)还描述 Deposit Wallet 等钱包模型，不能凭公开地址推断用户钱包一定受支持。就绪只判断存在正余额、正额度和 POL，并不验证额度足够下一笔或当前 CTF 授权。导入时固定保存 Polygon RPC，未来 RPC 失效或遗失钱包解锁密码，目前可能需要懂服务器的人协助恢复。用户体验应把“不满足哪一步、怎么解决”写成实际可操作提示。

这些优先级是本轮审计建议。前四项直接关系到真实资金扣款、持仓核对、退出回款与迁移完整性；P1/P2 也需进入修复计划。它们**没有**推翻原有筛选、仲裁、止盈或用户的 1U 每轮设置。

## 4. 网站、接口和链接分别做什么

| 地址或文件 | 用途 | 本轮核对 |
|---|---|---|
| [GitHub 仓库](https://github.com/yvettemiranda/PM-NAUTILUS) | 程序、公开规则、说明和版本历史；没有钱包秘密或真实账本。 | 起点 SHA 与 CI 已核对。换电脑/服务器时从此克隆。 |
| [当前服务器网页](https://43.159.133.129/) | 用户操作 TEST/LIVE 的 HTTPS 页面，经服务器认证；不是交易所页面。 | 健康接口本轮可达；本轮浏览器交互未完成。迁移后旧 IP 不再适用。 |
| [Gamma API](https://docs.polymarket.com/api-reference/events/list-events) `gamma-api.polymarket.com` | 查 Event、市场、类别及结算元数据。 | [market.py](../src/pm_nautilus/market.py)和公开短测；不提供真实账户资金。 |
| [公开市场 WebSocket](https://docs.polymarket.com/api-reference/wss/market) `ws-subscriptions-clob.polymarket.com/ws/market` | 实时公开盘口。 | 短测接到真实公开盘口；这不是用户私有成交回报。 |
| [CLOB 官方 API](https://docs.polymarket.com/trading/place-orders) `clob.polymarket.com` | LIVE API 凭据、资金/订单核对、签名订单提交及回报。 | 源码与离线 SDK 路径已审，**无真实钱包实测**。 |
| [私有用户 WebSocket](https://docs.polymarket.com/trading/realtime-order-updates) `ws-subscriptions-clob.polymarket.com/ws/user` | 登录账户后的订单和成交状态推送；与公开盘口流分开。 | 锁定 Nautilus 适配器组装该地址，[live.py](../src/pm_nautilus/live.py)只把归属本程序的确认成交入账；本轮没有真实私有订阅。 |
| [Data API 持仓](https://docs.polymarket.com/api-reference/wallet/list-positions-for-a-user-or-market) `data-api.polymarket.com/v2/positions` | 钱包公开仓位全分页只读核对。 | 路由与过滤参数已查；官方返回范围不等于链上所有历史状态，关键 Condition 仍需 RPC 核对。 |
| [官方 geoblock](https://docs.polymarket.com/api-reference/geoblock) `polymarket.com/api/geoblock` | 按**服务器出口**判断是否能 API 新开仓。 | 旧服务器实测 blocked，换服必须重测。 |
| [Polygon RPC](https://docs.polymarket.com/resources/contracts) `polygon-bor-rpc.publicnode.com` | 查询链上 pUSD、POL、CTF 份额与授权；LIVE 赎回时广播交易。 | 代码路径已查；没有真实钱包链上写入。RPC 可在部署配置中调整，但网页导入后替换路径不完整。 |
| `https://polymarket.com/event/{slug}` | 持仓或候选市场跳到交易所公开市场页。 | [views.py](../src/pm_nautilus/views.py)拼接 slug；它是查看市场的外链，不表示本程序订单已成交。 |

上表说明程序需要连接的主要外部服务，不代表所有服务的私有交易闭环已通过。不同链接的“余额”和“价格”含义不同：真实可用现金来自 CLOB/pUSD 查询；页面持仓价值是模型标价；旧 TEST 100U 是模拟起点。

## 5. 新电脑、新服务器与旧账本：给下一段对话的操作边界

**A. 此前只有 TEST，准备在新服务器首次 LIVE。** 从 [GitHub](https://github.com/yvettemiranda/PM-NAUTILUS)取得经过修复和 CI 验证的固定版本，按 [首次安装指南](START_FRESH_LIVE.md)、[部署手册](DEPLOY.md)完成 HTTPS、登录、Docker 和持久化目录；把 [公开策略文件](../config/strategy-profile.json)应用到全新的 TEST、LIVE 两个账本，并在网页分别读回确认。这些安装工作目前仍需要懂部署的人或新的 Codex 对话协助。旧 TEST 模拟成交可不迁移；GitHub 本身不能恢复钱包、密码或交易记录。完成 P0 修复、服务器地区变成允许、钱包类型及资金授权全部核对后，才在真实 HTTPS 页面导入助记词/私钥、确认签名者和资金地址、解锁、检查、启用为 PAUSED，再经用户明确决定是否 START。**不要把助记词、私钥或密码发到聊天/GitHub。**

网页导入时，助记词或私钥会通过 HTTPS 传给**自己的服务器**完成钱包建立；这不是“密钥只留在浏览器”。服务器把凭据写成加密钱包文件，解锁后签名密钥仍会在程序内存中供 LIVE 使用（[钱包导入与加密逻辑](../src/pm_nautilus/app.py)、[钱包配置](../src/pm_nautilus/live_web_setup.py)）。因此首次安装仍要保证登录、HTTPS 和服务器访问权限正确；浏览器本身无法代替服务器安装、地区核对或账户授权。

**B. 已经有 LIVE 历史，后来换电脑或换服务器。** 真实订单、持仓、未完成卖出、待赎回权利及钱包加密文件都必须跟着走。网页 `.pmnb` 下载只是下载时点的快照；在旧服务继续交易后它就不是最终迁移备份。新对话先检查旧服状态和未完成事项，在停止旧实例产生新事件后取得最终备份，再在新服空白账本恢复，确认同一钱包身份、数据库完整性、开放挂单、真实余额、两个 outcome 的链上份额、结算状态与本地记录一致。恢复后仍锁定/PAUSED；先解决不一致，最后才考虑 START。若旧服不能可靠停写或备份，**不能只从 GitHub 重新下载后直接继续实盘**。旧账本和密文备份不上传公开仓库。

**C. 当前服务器继续 TEST。** 2026-10-07 巡检显示 TEST/RUNNING、LIVE 未配置。原有 TEST 可继续作为模拟观察，但本轮完整 validation 因超时未取得结果，应补查。若只想看行情或网页，切换到 LIVE 视图不会让真实交易开始；将来若 LIVE 已 RUNNING，切回 TEST 也不会让真实交易停止。

## 6. 下一段对话建议的修复与验收顺序

1. **先冻结事实。** 读本文件及 [HANDOFF](../HANDOFF.md)，核对 GitHub `main`、本地提交、服务器源码和运行镜像的实际 SHA、工作树、服务器健康、TEST 完整 validation、地区与钱包状态。保留现有数据和策略参数，不重置 TEST/LIVE。
2. **先改真实扣款与实际份额入账。** 对照当前官方下单/费用说明和锁定 SDK，修正 BUY 含费总预算、实际订单数量、真实回报的现金/份额/佣金模型；加入能够区分成交价格、手续费精度、部分成交和重启的离线测试。修正后核对 TEST 模拟口径及网页金额名称。
3. **补赎回准入与授权路径。** 对 EOA/单所有者 Safe 分别校验授权对象、资金账户和 gas；未授权时给网页清楚的指引或受控授权流程。完善真实链上回款、失败和重启核对的接受条件。不要把“准备状态 READY”当成真实链上验收。
4. **修迁移与 UI 误导。** 最终停写备份/恢复流程，跨模式数据加载门控，LIVE RUNNING 常驻提示，历史锁定显示，估值与失败应收的分离；支持或明确排除不兼容的市场版本/tick。更新 README、部署/首次使用及验证文档，避免新对话继续读到旧结论。
5. **验证顺序。** 本地全套 Python、Ruff、前端、Docker/CI；公开行情及两模式账本恢复；新服务器 HTTPS/认证/地区只读核对；真实钱包类型/资金/授权/开放挂单/链上份额只读核对；得到用户对首次真实签名/交易的明确决定后，再做受控的小额真实买入、实际扣款/份额、卖出、结算/赎回、故障/重启验收。前面任一结果不明，就先保持 PAUSED 并查清。用户不要求新增投入或笔数上限，验收仍按现有策略规则。
6. **发布后复核。** 代码与文档同批推到 GitHub，核对 Actions 与服务器固定 SHA；如果只是文档提交，可保留相同代码镜像但要说明差异。任何真正的应用升级都先做可恢复备份，再按部署手册核对账本、配置和健康，不自动 START LIVE。

## 7. 复制给新对话的提示

> 请接手公开仓库 `https://github.com/yvettemiranda/PM-NAUTILUS`，先阅读 `docs/END_TO_END_HANDOFF_2026-10-07.md`、`HANDOFF.md`、`README.md`、`docs/DEPLOY.md`、`docs/START_FRESH_LIVE.md`。先用实时只读证据核对 GitHub、服务器源码 SHA、运行镜像 SHA、TEST/LIVE 状态、地区和账本，不要把 2026-10-07 的快照当成现在。我的现有策略参数不要擅自改变，也不要新增首轮投入、单笔金额或交易笔数限制。先按审计中的 P0 顺序修正 LIVE 的含费预算/订单数量/实际份额核算、自动赎回授权、最终迁移备份，再处理 UI 与市场兼容性。完成代码、测试、文档和部署后，分别说明 TEST 已验证什么、LIVE 仍缺什么。没有我的明确决定，不要导入或索取真实助记词/私钥，不要签真实订单、批准或发链上交易。未来我需要能在网页操作，但请如实说明新服务器首次安装及有 LIVE 历史的换服还需要哪些运维步骤。

**审计性质：** 本页汇总源码核对、锁定 SDK 读取、公开接口短测、离线复现和只读服务器巡检。它不能保证“没有任何问题”，也不能代替真实钱包及交易所的完整实盘验收。对未实测环节已逐一标明；未来若官方规则、市场版本、SDK 或服务器改变，须重新核对。
