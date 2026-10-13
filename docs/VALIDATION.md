# 验证结果与边界

本页按日期保留当时的实测记录。旧章节中的“当前”“最近 1440 个样本”等表述只描述对应日期的版本；新版本的行为和部署状态以本页最新章节及 [HANDOFF.md](../HANDOFF.md) 为准。

## 2026-10-13 全面优化验收（本地完成，待发布部署）

本地 `codex/full-optimization-20261013` 从 `3db8d5834524ddee0fa64b239cfc58c933f04319` 开始实现。新代码待发布至 main 与部署；下面记录本地最终结果及新增验收范围，固定提交、PR/CI 和服务器证据将在实际取得后补记。下方日期更早的服务器验收不能算作本轮结果。

本地最终完整回归：**Python 486 项通过（17.00 秒、两条既有第三方弃用警告），Node 页面 24/24 通过，JS 语法、Ruff check 与 format --check（83 个 Python 文件）通过。**独立复核已结束，发现的启动钱包核对并发竞态已修复；本轮结果尚未对应已发布 Git 提交、CI 或服务器部署。

已取得的局部验收：Chrome headless 使用受控 API，在 1280px 与 320px 下无页面错误或横向溢出；1,000 个持仓初始只展示 20 个、总数仍为 1,000，分类焦点在 500ms 自动刷新后保持，10,005 条交易记录可继续“更多”，未接钱包的 LIVE START 禁用。这是本地浏览器实测，不是生产页面或真实账户现场结果。

隔离投影对照（同一组合成 1,000 持仓，全仓资金精确一致）：完整持仓响应 864,048 字节，前 20 明细 15,968 字节，纯摘要 1,316 字节；对应中位时间 7.149ms、1.136ms、1.037ms。收益采样从完整明细计算的 5.721ms 降至纯汇总 0.494ms。这些数值只说明本地投影/页面负载变化，不等价于实盘下单延迟或滑点。

- **BUY 精度及预算：**`test_live_full_optimization.py`、`test_rules_full_optimization.py` 覆盖 0.1/0.01/0.001/0.0001 tick 的 canonical 向下量化、5.02U 及浮点临界值、最小量、官方余额/手续费保护、原始限价及拒绝下一合法价格档；受控 LIVE POST 与 TEST 相同输入对比。BUY 从旧粗取整改为 SDK 精度是正确性修复，单独解释预期数量变化，不将旧误差当成必须保持的结果。没有真实资金签名、提交或成交。
- **回报与原生日志：**写入失败、写成后原生分发失败、份额转换失败分别注入，验证同一回报可恢复且只计一次；晚到取消不能越过已持久化 Fill。错误 Condition/token/方向及非精确正份额不能作为终态证据；FAILED 单笔、累计确认加失败份额及 REST matched 必须不超过持久化原生上限，超限保留占用并暂停，CONFIRMED 不回退；重复确认跳过无变更写盘和 Fill 构造。
- **账户与元数据：**Condition 索引覆盖替换、删除、重启；12 个自有持仓的执行客户端完整启动由 13 次整钱包读取减为 2 次：报告批次 1 次，完成订单恢复后最终核对 1 次。`test_live_activation_final_custody.py` 两项回归复现 custody 先读、订单随后恢复另一 Fill 的竞态，并验证 ready 前新查最终份额不足会拒绝、正常路径仍新查；之后没有 await 再发布 ready。更优价格的 BUY 数量可超出签名最低接收量，仍受原生数量/含费预算约束。下一轮及实际新买仍获取新结果。手动资产、已结算权利不被冒充为本程序当前 CLOB 仓位。独立只读预检的并行查询固定同一区块，全部账户准入条件保留。
- **链上事务与取消：**`test_redemption_ownership.py` 覆盖未知原交易跨重启、授权与赎回 nonce 互斥、确认下限原子提交、写入失败不广播、反复取消后保持所有权及先保存再解锁。实际标准/Neg-Risk 合约结果、实际回款和确认深度检查保留；测试使用受控钱包/RPC，不是实盘验收。
- **备份与关闭：**`test_backup_lifecycle.py` 覆盖导出/授权线程重复取消、取消期间删除钱包被拒绝、快照连接在 GC 前关闭、专用临时目录安全清理、最终文件及旧式凭据父目录 fsync、应用关闭前等待工作线程。待确认授权不能被空白恢复覆盖。
- **行情与规则：**`test_market_full_optimization.py` 验证旧 socket 关闭等待期间已不可交易、TEST 缩范围/重置不影响仍被 LIVE 使用的订阅、公开结算不延迟定时控制且停止时被等待。`test_rules_full_optimization.py` 覆盖整数预算、首个 SELL 批次/共享深度/尾仓、Bid 哈希与止损版本、进度边界；`test_store_historical_tokens.py` 验证历史身份 SQL 查询保留。
- **START 与页面：**`test_strategy_full_optimization.py` 验证完整异步校验与返回后当前状态检查；`test_full_optimization_views.py` 和实际 `app.js` 页面回归覆盖全仓深度估值/未知状态、摘要与明细隔离、格式器/焦点、隐藏页停止轮询/返回更新，以及超过 10,000 条后的稳定分页、迟到成交、代次和模式隔离。收益采样不构造页面明细，但保留全部资金计算。

既有策略设置与发布安全条件保留。局部基准、离线模拟或服务器 TEST 结果均不能证明真实成交延迟和滑点改善；真实钱包 BUY/SELL、实际费用与份额、正额赎回及故障恢复仍待独立授权验收。最新状态见 [HANDOFF.md](../HANDOFF.md)。

## 2026-10-12 代码与服务器验收（历史）

代码 `dbf36d40ca1f24c1286e85efc1409b89f53e6bfb`，[PR #6](https://github.com/yvettemiranda/PM-NAUTILUS/pull/6) 已合并；[main CI](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/38261461594) 的 Ubuntu x86_64/ARM64 均通过测试及 Docker verify。现有配置、交易规则、依赖锁和部署安全设置未改变。

- **完整回归：**405 项 Python、17 项 JavaScript 页面测试；Ruff 检查/格式、JS 语法通过。页面测试执行实际 `app.js` 控制处理函数，覆盖延迟响应、模式切换和旧市场缓存。
- **TEST/LIVE 执行一致性：**锁定 SDK 的精确 SELL size 到受控 LIVE 提交及 CONFIRMED Fill；5.02 份在最小量 5.02 下完整卖出，5.029 份保留 .009 份尾仓，TEST 与 LIVE 份额及原生持仓一致。价格下限、最小量和实际拥有数量检查继续执行。结果未知的订单继续占用；缺字段、错误身份、负数或非有限成交量不能确认终态。
- **退出及重试：**未变盘口下的 BUY/TARGET/STOP 零成交终态，每次控制检查最多重新评估一次，不在同步处理循环中空转；在途订单继续保持 Event 锁。恢复行情后用户确认 START，后续扫描不重新锁存旧 fatal，新故障仍会触发安全暂停。
- **持久化与生命周期：**实际 SQLite `RAISE(ROLLBACK)`、延迟外键造成的失败 COMMIT 后，重试更新持仓并删除目标；重复取消 LIVE 启用后 native queues/Store 正常关闭；取消恢复期间钱包锁和上传文件保留至真实工作线程完成。
- **共享行情：**未知错误及 HTTP 401/403 只暂停受影响的订阅模式；另一模式保留健康盘口。10,000→50 token 后 50 条连接压缩为 1 条；旧连接已失去归属并停止，移动 token 的增量须等待新完整快照。
- **正常场景对照：**相对 `7307f33`，720 次行情输入、111 份状态、26 个订单的现金、占用、业务账本、原生事件、持仓、盘口消费、资格及参数一致。该对照验证既有正常规则保留；单独回归验证错误场景得到修正。
- **服务器：**完整 TEST validation `ok=true`、`errors=[]`，容器 healthy、零重启、后台错误为空；恢复 TEST/RUNNING，LIVE 关闭且钱包 LOCKED。更新前后两库业务和原生历史、参数、代次及钱包文件保留；页面脚本匹配已部署源码，HTTPS/认证核对通过。备份及版本记录见 [HANDOFF](../HANDOFF.md)。

以上为离线/受控执行与服务器 TEST 验收，未完成正式钱包买入、卖出、实际费用、正额赎回及实盘故障恢复。运行速度的优化没有经过真实交易所成交延迟或滑点测量；不将模拟一致性解释为成交价格和收益相同。旧服务器地区检查仍引用 2026-10-07 的历史结果，新服务器须从新出口重新检查。

## 2026-10-07 实盘准入修复与部署后验收

- 本地固定依赖执行 246 项 Python 测试、9 项前端测试、Ruff 检查及格式、JavaScript 语法，均通过；`pm-nautilus-final-export --help` 可运行。离线本机应用的健康、TEST/LIVE 面板、钱包状态、TEST 验证和锁定的 LIVE 记录接口均返回 200。测试没有使用真实钱包或提交真实交易。
- [PR #1](https://github.com/yvettemiranda/PM-NAUTILUS/pull/1) 的 Ubuntu x86_64/ARM64 四项 CI 检查均通过并合并；服务器运行同一代码提交 `273c80e7711221b7f532633710d6be381f1f4e42`。
- 停机加密备份后升级，服务器 TEST 完整 `/api/TEST/validation` 返回 `ok=true`、`errors=[]`，SQLite `quick_check=ok`。升级前后 83 个模拟持仓、现金 `117.346484U` 及 TEST/LIVE 各 15 项交易设置一致。升级后恢复原 TEST/RUNNING；LIVE 钱包文件已保存但锁定、未启用、无订单意图或原生事件。公网 HTTPS 浏览器读回 TEST 资金/持仓/记录/市场；LIVE 视图不显示 TEST 资金，锁定的记录不可读且 START 禁用。
- 同一服务器出口的 Polymarket 地区检查返回 `blocked=true`、`US/CA`。真实账户余额、赎回授权、CLOB 订单、链上份额、真实成交费用、卖出和赎回没有进行正式钱包验收；这些结果不能从离线测试或 TEST 账本推断。停机备份成功生成，但本轮没有独立设备上的解密恢复演练。

## 2026-10-07 全流程审计（只读、未验收真实交易）

完整结论与下一轮交接见 [END_TO_END_HANDOFF_2026-10-07.md](END_TO_END_HANDOFF_2026-10-07.md)。本轮本地锁定依赖下 207 项 Python 测试、Ruff 检查/格式、JS 语法、7 项前端测试通过；独立公开行情短测用临时开发规则取得 10 个模拟 Fill，并在同库重启后验证通过。服务器 10:54 CST 只读巡检为 TEST/RUNNING、LIVE 钱包未配置、容器 healthy、SQLite `quick_check=ok`；`/api/TEST/validation` 在本轮巡检客户端 20 秒内超时，因此**没有本轮完整 TEST validation 的成功结果**。没有真实签名、真实订单或链上操作。发现实盘买单含费预算、成交份额/费用入账、首次钱包赎回授权和运行中网页备份用于迁移等缺口；此前章节的模拟测试和网页就绪不能替代修复及真实实盘验收。

## 2026-10-06 LIVE 设置齿轮修复与地区检查

- LIVE 页面原先点击右上角齿轮会展开位于钱包区域下方的交易设置，但不会滚动到该区域。`7802c181a4966add09b8045cce686a5ccbfbae45` 增加展开后的滚动，并更换 `app.js` 资源版本；没有改动钱包、账本或策略逻辑。本地 JS 语法、7 项前端测试和 [GitHub Actions run 37467946285](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/37467946285) 通过。
- 腾讯云服务器已从该提交构建并启动新镜像；停机时生成加密备份，标识和大小见 [HANDOFF.md](../HANDOFF.md)。21:23 CST 本机健康接口确认 `status=ok`、`TEST/RUNNING`、`LIVE=false`、钱包未配置、版本 SHA 匹配、后台错误为空；容器 healthy、零重启。浏览器强制刷新服务器网页、切到 LIVE 视图再点击右上角齿轮后，页面实测自动滚动到交易设置且内容可见；后台仍是 TEST/RUNNING。以上不等于真实实盘验收。
- 同一服务器直接请求 Polymarket 官方地区接口，得到 `blocked=true`、`US/CA`。这验证了**当前出口**无法通过 LIVE 新开仓地区检查；未来迁移后的服务器需单独复测。没有使用真实钱包或执行真实订单。

## 2026-10-06 网页 LIVE 钱包与 H/D 曲线（已发布及部署）

- 新增仅在 LIVE 页出现的钱包流程：12 词助记词或私钥导入、公开资金地址核对、加密保存、网页解锁、就绪检查、明确启用；启用后先停在 PAUSED，另行点击 START 才开始新买。TEST 不需要钱包。应用重启后 LIVE 锁定，未核对的真实资金和仓位显示为未知，不能把未展示的持仓解释为零。
- 只读就绪检查覆盖账户认证、资金与授权、开放挂单、官方持仓接口的完整分页和服务器网络出口的地区结果。首次导入有原有持仓会阻止启用；恢复后按各 token 的实际份额对账。任何查询失败或数据不完整都不能放行。启用前不做真实下单、取消或链上写入。
- 网页 `.pmnb` 加密备份包含同一时点的加密钱包和 LIVE SQLite 账本；恢复仅接受空白 LIVE 账本。正式 TEST 数据库已约 714 MB，促使将首轮 128 MiB 加密包上限升级为分块压缩和流式上传，当前上限为 4 GiB 原始 LIVE 数据库、2 GiB 加密包。离线测试覆盖 100 MiB 原始数据库跨线程导出/恢复、旧版包恢复、错误密码、损坏文件、私有临时文件权限及下载后清理。该包不含 TEST 模拟历史，也不替代有在途订单时的实际账户核对；尚未使用真实 LIVE 账本做网页迁移演练。
- 收益曲线保留原始独立采样，展示可选 **H（每小时一点）**或 **D（按 UTC 日期每天一点）**；这两个按钮改变曲线的数据粒度，不是切换最近一小时或最近一天的时间窗口。未知估值、重启及采样断层仍使曲线断开。去掉重复的每分钟说明字样。本地页面预览已核对 TEST→LIVE 切换、未接钱包时 START 禁用、H/D 按钮与恢复入口；这些 UI 检查没有接入真实钱包。
- 本轮本地测试使用受控钱包、CLOB、RPC、地区与持仓响应；没有输入真实助记词或私钥，没有真实签名订单、资金转移或链上操作。首轮提交 `d4446f1a4818fa781cf701535c9c73aa18460f74` 与备份补强提交 `0844bc5da1153da729d9d369e046a8820ea9b8ae` 均已发布；后者本地 **207 项 Python 测试、7 项前端测试**及 Ruff/JS 语法检查通过，[Actions run 37463286536](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/37463286536) 的 Ubuntu x86_64/ARM64 与 Docker verify 均成功。服务器镜像也已更新到 `0844bc5`；停机加密备份、TEST 账本逐项指纹、应用验证、恢复 RUNNING、HTTPS 页面和钱包未连接时 START 禁用的现场结果见 [HANDOFF.md](../HANDOFF.md) 最新章节。真实 LIVE 验收仍未进行。

## 2026-10-05—06 四项维护修复与部署验证

- LIVE 发单失败区分确定未发出与请求结果不明：签名准备、身份生成和重试管理器获取失败会结束本地订单并释放资金/Event 占用；进入可能发出请求的边界后，网络异常或取消会保留占用、暂停新买并等待对账。回归包含真实异步工作线程取消窗口；没有使用真实钱包或交易所私有接口。
- 开放挂单核对在远端查询后读取最新持久化订单；首次未知时再做一次完整远端查询，排除查询期间已结束的旧响应，持续未知挂单仍阻止新买。
- `/api/health` 增加交易运行时故障、恢复故障、LIVE 账户错误和 LIVE 维护任务状态；正常人工 PAUSED 保持健康。健康响应使用稳定错误码，不输出私有错误详情。
- 全部活动订单查询增加 SQLite 部分索引，旧库启动时自动创建；保留按 Event 查询索引和全部历史数据。
- 本地完整 Python 测试 **151 项通过**；Ruff 检查与格式检查、前端 JS 语法检查及 **3 项前端测试**通过。代码提交 `552b02d8aaf2719cb5230403c01cba8d41dad38e` 已发布到 GitHub `main`；[Actions run 37335756478](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/37335756478) 的 Ubuntu x86_64/ARM64 作业及 Docker verify 均通过。
- 腾讯云服务器使用该代码构建并启动镜像 `sha256:6f3cce552bacba4b72ef7429ee783debb87bbca98fc9b3a27faa3a3fa20b1035`。停机前制作 age 加密备份，TEST 的 2,355 条原生事件内容哈希、486 条订单记录的数量、账本代次和规则设置哈希在升级前后保持一致；LIVE 仍为 0 条事件及订单。两个模式的 SQLite `quick_check` 均为 `ok`，新 `intents_open` 索引均存在；`/api/TEST/validation` 返回 `ok=true`、`errors=[]`。
- 2026-10-06 约 00:03 CST 恢复 TEST 后，`/api/health` 返回 HTTP 200、`status=ok`、TEST/RUNNING、LIVE=false、`backgroundErrors={}`；容器 healthy、零重启且未 OOM。6/6 行情组与 1,161/1,161 个盘口 READY，最近完成的扫描含 21,712 个 Event，当前扫描/行情/服务错误为空。现场采样不等于长期稳定性或真实实盘验收；未使用真实钱包签名、下单、批准、赎回或链上写入。备份标识和完整核对值见 [HANDOFF.md](../HANDOFF.md)。

## 2026-09-20 长期 TEST 稳定性修复

- 最终代码 `a4947c5c884cf48b2e500e9ddec73bc628a7d414`：本地89测试、Ruff/格式通过，前端未改且Node语法检查通过；GitHub Actions [35515717722](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/35515717722) 的x86_64/ARM64均成功。用户服务器真实Linux verify镜像89测试通过（9.55秒），以非root身份导入生产模块成功。
- 新增回归覆盖临时500/502/503/504重连与401/403安全暂停、650次订阅增加的有界分组及全覆盖、按需盘口/native缓存、关闭元数据释放及历史持仓重放；1000次无持仓与1000次有持仓但无业务变化的报价均不增加SQLite变更计数。模拟深度缩减持久化、下单前写失败不生成订单、Fill投影写失败安全暂停且只重放一次均通过。
- 再次运行独立公开行情短测：100个Event样本，10个选中Event，48个监控Token，10笔原生模拟Fill，10持仓；运行和同库重启核对均通过，现金112000微单位一致。开发样本最终PAUSED、私有连接0，不是服务器正式账本或实盘验收。
- 服务器保留848条原生历史事件（seq<=1759）的完整序号/ID/类型/payload哈希，升级前后相同；SQLite完整性、原生持仓/周期/目标/预算/现金校验通过，未reset或删表。原配置与账本代次保留。
- 22:16:53 CST在6组1017盘口全部READY、pending=0及账本验证通过后，通过认证HTTPS/CSRF/Origin恢复用户已授权的正式TEST。22:17复核容器healthy、零重启，TEST/RUNNING、LIVE=false，扫描/分类/流/服务当前错误均为空。历史故障时间保留，不伪装成从未发生。
- 同一2GB主机采样：故障时容器约1.364GiB、available127MiB、swap约1.3GiB、I/O wait46–52%；恢复后容器646.5MiB、available966MiB、swap163MiB，采样I/O wait为0%。这是现场前后采样，不是相同市场负载的严格基准，也不等于已经通过长期无人值守验收。持续运行区间和后续复核见HANDOFF.md。

## 范围与版本

2026-09-21 11:50 CST补充跨夜采样：同一a4947c5镜像healthy、零重启/OOM，TEST/RUNNING、LIVE=false；6组1011盘口全部READY、pending=0，32持仓31 READY/1 NO_BID，账本校验通过。内存642.8MiB、available约1GiB、swap183MiB、采样I/O wait=0%。最新完成扫描11:44:12并已进入下一轮，当前四类错误为空。距恢复约13.5小时，但不把未采样区间当连续全量监控或72小时验收；管理网络间歇连通问题见HANDOFF。

验证日期：2026-09-06。原 PM-SMALL `eb8c6d8a09f9b0427890b7a2d744fc3485d1d3dc`；NautilusTrader 1.231.0，源码 `27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`。所有账本为本次新建开发数据，不导入旧仓位。

此次没有真实钱包导入、真实订单签名、实盘单、approve/redeem 或其他链上写入。LIVE 测试明确使用可控 CLOB/账户/RPC 响应；不能将其表述为真实交易或链上验收。

## 已有真实结果

| 项目 | 实际结果与证据 |
|---|---|
| 原项目完整测试 | 当前 Node 24.20.0 环境：32个测试文件、324项通过；`.reference/original-tests.log`。此前权限失败是历史结果，本次重新实测 |
| 两处原缺陷复核 | 在原 `PaperMarketProcessor` + `PaperDatabase` 上另外执行2个缺陷复现用例，确认低Bid变化会重复卖掉未补充的高Bid深度，getOrderBook的Ask版本导致已消费Bid再被预演计入；`.reference/original-defects.log`。该2项是证实错误存在，不是声称旧实现正确 |
| Mac 新程序回归 | 68项通过，2项依赖弃用警告，1.38秒；Ruff check/format与Node语法检查通过。包括原生运行、LIVE回报、赎回、API和生命周期，不只测试纯规则 |
| 前端 | Node `--check src/pm_nautilus/web/app.js` 通过；无单独前端编译步骤，原生静态HTML/CSS/JS由FastAPI提供 |
| 公开行情到模拟成交 | 实际 Gamma/WS：采样100个公开Event，选10个完整Event、108个合格Token；10个READY，14笔原生模拟Fill，10个持仓，现金4.37U；同库重启现金4.37U，数量/预算/目标/现金核对通过，结束PAUSED。日志 `.reference/public-smoke.log`，重跑 `scripts/public_smoke.py` |
| Linux | Linux aarch64 / Debian bookworm / Python3.12.14：最终代码 e91563f5723609f126ed540d9bd3d3d36f795de6 镜像68项通过（2.84秒），生产base镜像启动成功；`.reference/linux-verified-68.log` |
| GitHub/自有服务器 | 公开仓库已创建并持续同步 main；双架构 CI 通过，详情见下节。服务器后续已完成独立 TEST 部署、HTTPS 入口、认证、备份、只读巡检与正式 TEST 启动；动态运行状态和最新 SHA 见 HANDOFF.md |

公开行情短测使用独立临时目录、100U初始资金，每轮10U、进度100%、最长365天、比例1%、全部市场类型的明确开发设置，便于在有限时间内产生可验证成交。这不是产品默认配置，也不是正式长期TEST；生产发现仍为全部分页，无10个Event/108Token上限。此前默认设置的短测只有真实盘口、没有成交，不计为成交端到端成功。

## 回归覆盖

- `test_rules.py` / `test_arbitration.py`：参数边界、费用净份额、tick及99¢上限、多档目标、共享深度、小目标聚合、逐价位消费与重连；六级仲裁及稳定ID逐层确定预期，输入顺序不影响Winner；生命周期ASC/DESC；30秒不同Bid版本、等值/空值/倒退/不可逆退出。
- `test_runtime.py`：真实Nautilus下单/成交/账户/仓位；部分卖出与重启；1U现金计价多档买入；投影写失败后暂停并只重放一次；Fill当时规则快照；当前READY先排序；旧周期预算/止损冻结、新Fill用新目标；多档FAK归组但目标仍逐Fill独立。
- `test_live.py` / `test_live_recovery.py`：真实Nautilus LIVE执行队列及所选官方REST/WS schema，MATCHED不记可支配成交、CONFIRMED去重、恢复签名订单归属与base quantity、未刷新现金不再融资、合法tick下取整且不提高预算。HTTP与签名结果为可控响应。
- `test_live_redemption.py` / `test_settlement.py`：0/0.5/1结果，等待、失败、错误回款、同一raw/hash重播、重启继续、实际余额刷新失败、32块/Transfer/烧毁权利核验、手动混合权利拒绝；到账但尚未本地结算的窗口不重复计总资金；本地结算意图中断后不重复链上赎回、不重复记账。
- `test_market.py`：完整原始N、正式结果与不明确等待、303个Event分页到末页、静态兄弟未知阻断/已收到空书不阻断，生产无隐藏总数截断。
- `test_app.py` / `test_live_health.py` / `test_lifecycle.py`：CSRF/Host/认证、TEST/LIVE配置与reset隔离；未知LIVE现金；活动意图SQL索引；瞬时核对失败恢复但不自动START；激活失败清理、关闭等回报与适配器任务后关SQLite；旧回调失效、后台异常暂停并健康503。

测试中注入的模拟响应和时钟只是证据生成手段，实际业务使用原生Nautilus对象及同一份规则。测试未删除旧断言或导入旧账本。

## UI 实际操作

通过本机浏览器操作真实API：

- 320px配置面板与含持仓页面均无横向溢出；桌面维持紧凑单列。
- 修改最低买价的未保存草稿跨刷新保留，保存后生效；START/PAUSE按钮正确转换。
- LIVE初始余额只读/未知显示“—”，未启用时START禁用；LIVE保存每轮2U后切回TEST仍1U。
- 使用独立21Event/21持仓模拟账本：默认20项，展开为21/21；扫描展开全部、ASC/DESC切换正确；记录默认折叠、展开读取21总条数，按原UI显示最近20条。
- 该离线样本重启后行情未知，估值显示未知，现金79U；没有假设旧快照仍可成交。浏览器控制台未发现error/warn。
- 临时视口覆盖已恢复。所有UI样本均为开发目录，不作为正式TEST账本交付。

## 剩余真实环境验收

实际钱包类型/签名及资金账户、CLOB权限/授权、POL余额、RPC、标准及neg-risk真实链上交易、真实服务器架构/HTTPS入口均未实际验收。现有代码仅支持EOA和单所有者Safe；其他钱包不能冒充已支持。缺这些条件不影响本地源码、部署配置和可控验证交付。

依赖目前产生2项弃用警告（Starlette/AnyIO别名、websockets legacy）；不影响本次通过结果，固定依赖升级时复核，不在迁移中随意换版本。

## Linux 生产镜像与同库重启实测

使用真实Docker、非root UID10001、只读rootfs、临时/tmp、全部capability删除、no-new-privileges及独立持久卷。启动检查版本SHA与代码一致、健康ok、TEST/PAUSED/LIVE=false；未认证dashboard返回401，认证后HTML/JS/API可读。首次保存每轮2U，再restart同一容器同一数据卷，仍读取2U、现金100U、相同generation、LIVE独立1U，最后恢复TEST默认1U，未START。证据 `.reference/container-first.json`、`.reference/container-restart.json`。

Compose在缺PM_GIT_REVISION时明确拒绝，补完整SHA后config --quiet通过。2026-09-07 又通过 GitHub Actions 实测 Linux x86_64 与 ARM64，见下节。该临时Linux环境不等于已部署用户自有服务器。


## 2026-09-07 公开发布与恢复验证

用户明确授权公开上传、服务器延后。仓库为 [yvettemiranda/PM-NAUTILUS](https://github.com/yvettemiranda/PM-NAUTILUS)，默认分支 main。首次发布提交 `f39c3de95de6a514c3e0555e1774ccd789cb0a73` 的 [GitHub Actions run 34071046061](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/34071046061) 全部通过：

- Ubuntu 24.04 x86_64：宿主68测试通过（7.22秒），Docker verify内68测试通过（6.20秒）。
- Ubuntu 24.04 ARM64：宿主68测试通过（6.87秒），Docker verify内68测试通过（5.49秒）。
- 两组均通过 Ruff、格式及 Node 语法检查；均有相同2项依赖弃用警告。

公开范围检查覆盖首次发布前 main 历史74个唯一blob：禁止路径和常见私钥/token模式扫描无告警。只推送 main，不上传 `.reference/`、runtime、实际环境文件、凭据或本机其他Git引用。该检查不声称能识别任意形式的秘密。

恢复演练发现 uv 0.8.22 的内置Python下载目录没有3.12.14；已在 REINSTALL.md 改为先用 uv 0.12.10 下载Python，再用锁定的 uv 0.8.22 安装项目依赖。新下载的 Python 3.12.14 已实测安装成功，不依赖旧 Codex 内置 Python 路径。

干净恢复验证：从实际 GitHub 公开 URL 克隆到新目录，使用独立下载的 Python3.12.14 和新 `.venv`，uv0.8.22按锁文件安装75个包；Ruff check/format通过，68测试通过（首次冷启动82.46秒，2警告），JS语法检查通过。未复制旧虚拟环境或参考源码。首次服务探测20秒超时，等待首次依赖加载后健康成功；未绕过系统安全检查。离线服务实测 `status=ok, mode=TEST, strategyStatus=PAUSED, liveExecutionEnabled=false`，首页及app.js均HTTP200，随后停止临时服务。此验证恢复的是开发环境，不是运行账本或真实钱包。
# 2026-09-13 心跳与订阅恢复回归

- 77项本地测试通过；包括繁忙WS仍定期PING、PONG超时、仅重订阅缺失完整盘口、新增Token保持旧连接，以及未变化元数据不重复入库。既有逐Fill目标、共享深度、持久化恢复回归仍通过。
- 官方依据：[市场WebSocket](https://docs.polymarket.com/api-reference/wss/market)明确要求每10秒文本PING。真实公开探针35秒收到4次PONG，取消再订阅同一公开Token后再次收到book。没有真实签名或资金动作。
- 独立公开行情模拟短测产生14笔Fill，10个native持仓，验证与同库重启均通过，结束TEST/PAUSED；不是正式服务器长期TEST验收。
- 服务器部署、原账本保留及新运行区间见HANDOFF.md。没有通过减弱持久化或引入未知盘口估值来隐藏故障。
- 补充跨连接完整快照时间基线回归：新快照允许重新建立基线但保留双侧消耗量，已就绪后继续拒绝旧时间戳，未就绪不接受增量。78项测试通过，GitHub Actions 34755364023 在 Linux x86_64/ARM64 均测试及Docker verify成功；最终代码提交bcec57d。
- 19:48只读采样：第一轮心跳修复版本已运行约5小时，5组905盘口均READY、待定事件0、当前扫描/分类/流/服务错误为空，22个持仓，账本和SQLite校验通过。历史最近流事件为17:29的tick变更触发受控重连，不是新的心跳超时。此为按需采样，不宣称全时段零中断或长期验收完成。
- 最终bcec57d服务器部署复验：新容器healthy，扫描21427事件后5组READY、缺失盘口事件0，22持仓20 READY/2 NO_BID；362条维护前原生事件逐条与备份一致、preferences一致。20:13:19 CST认证控制请求恢复TEST，LIVE仍false。部署过程中源码权限问题已修复，过程和剩余容量风险详见HANDOFF。

## 紧凑首页与真实收益采样（2026-09-22，部署前）
- 用户批准以赚亏与运行状态为主的紧凑 UI，并授权推送 GitHub 与服务器：TEST/LIVE + 状态点、小收益曲线、资金明细、持仓/记录/市场分页及可展开详情，适配手机。
- 累计净盈亏沿用已实现+未实现口径；每60秒独立采样到 equity_samples，按模式/generation隔离，最近1440个样本展示。所有旧交易事件保留；不反推历史行情，未知估值/重启/缺采样处断开曲线。采样故障单独展示，不改变策略状态。
- 保留500毫秒面板刷新，展开状态保持；页面连接失败不显示全绿，NO_BID不冒充断线；LIVE仍关闭，策略参数不变。
- 本地91项Python测试、3项JS状态/曲线测试、Ruff与JS语法通过；320/390px浏览器无横向溢出，持仓展开、记录、LIVE隔离与禁用已实测。服务器及GitHub完成情况将在部署后补记。

## UI正式部署验收（2026-09-22 15:10 CST）
- 应用最终提交及运行镜像：`27146fb1c2bca490a714faac89012932a564217a`。GitHub Actions `35697922799` 的 Linux x86_64/ARM64 全部通过（91项Python测试、3项JS测试、Ruff、语法、Docker verify）；服务器前一应用提交 `1cf3aae` 亦实际完成91项Linux测试，最终差异仅HTML资源版本和文档。
- 服务器先完成镜像构建，再暂停TEST并停机备份。保留两份0600完整备份：`backups/pre-ui-20260922.tgz`（55,367,071字节）和 `backups/pre-ui-cache-20260922.tgz`，含runtime/server、.env、compose.override.yaml。未覆盖旧备份，未reset。
- 首次版本写入命令因换行转义失败而安全停止，修正后启动；正式浏览器随后发现历史JS/CSS缓存混用，补加资源版本标识并于15:07:58启动最终镜像。此维护打断此前连续运行区间，不声明72小时稳定性验收通过。
- 停机备份与新实例逐条核对：955条原生事件的序号、ID、类型和原始payload全部一致；generation、preferences、initial_capital、business完全一致（含持仓周期、目标与盈亏）；.env除镜像SHA外不变，compose.override.yaml逐字节一致。SQLite quick_check和应用账本validation均通过；没有清理books、模拟消耗或历史。
- 确认新实例PAUSED、完整行情恢复、账本校验通过后，通过正常HTTPS认证与CSRF接口于15:10:06恢复此前已授权TEST。15:10:21只读复验：TEST/RUNNING、LIVE=false、healthy、restarts=0、OOM=false、backgroundErrors为空。
- 全量扫描15:09:09完成，8组1429盘口全部READY、pending=0，扫描/分类/行情/服务当前错误均为空。41持仓，现金91.567528U、总资产104.242650U、已实现23.099122U、未实现-18.856472U。以上为瞬时模拟值。
- 容器约489.5MiB；主机available990MiB、swap170MiB。Nginx配置检查通过，HTTPS原配置保留，.env与两份备份仍0600。
- 正式Chrome已验证新UI、真实收益采样、195条交易记录入口、持仓展开、运行绿点；无浏览器脚本错误。本地320/390px无横向溢出；模拟断网明确标记旧快照并禁用控制。开发服务已停止，开发数据与正式账本分离。
- 本段为交付文档补记；后续文档同步不重启运行镜像。曲线每分钟采样，只展示最近1440条，旧采样仍保留；09-22部署前的历史没有被伪造为曲线。最大回撤/分类表现等扩展报告未在本次实现。
