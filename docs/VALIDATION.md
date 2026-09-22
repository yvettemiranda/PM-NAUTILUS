# 验证结果与边界

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
