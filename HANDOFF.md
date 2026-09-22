# PM-NAUTILUS 接手记录

## 当前有效范围与状态
完整执行根目录 PM-SMALL_Nautilus_Codex_Instructions.md。独立新应用、新账本，只管理本程序交易；保留原UI及交易规则；实现TEST、LIVE接入、自动赎回。安全默认仍为TEST + PAUSED，LIVE未启用。未导入旧账本或钱包，未做真实签名/下单/approve/redeem/链上写入。正式长期TEST已于2026-09-08启动；其连续区间与中断须以下文实测记录为准。

## 已完成
- 原项目HEAD/main核对为 eb8c6d8a09f9b0427890b7a2d744fc3485d1d3dc。只读参考 .reference/PM-SMALL 排除Git；有效文档及关键代码已完整核对，原324测试在当前环境全部通过。
- 选定NautilusTrader 1.231.0（源码27a8e54e7ac3c57d6cbf8891f0283dfbaee97317），对比过2.0.0rc4；Python3.12.14、uv0.8.22、精确依赖与uv.lock已固定。
- docs/MIGRATION.md提供整体设计及逐条旧来源/新模块/职责/验证表。框架承担Strategy、DataEngine、RiskEngine、ExecutionEngine、Cache、Portfolio；应用补充PM的Event仲裁、周期、目标、止损、费用与赎回权利。
- 全分页Gamma、公开WS、静态资格、FAK、六级仲裁、共享深度、首Fill预算/止损冻结、逐Fill目标、30秒止损、暂停/重置/模式隔离均已接通原UI/API。
- 原生事件唯一成交事实源；先持久化事件再投影，带Fill当时规则快照。崩溃重放恢复预算/目标/消费；LIVE临时额度和Event互斥持续到真实确认终态。
- LIVE使用所选官方CLOB V2客户端及LiveExecutionEngine扩展；现金FAK与合法tick、不提高预算、提交前确定性身份、CONFIRMED-only真实回报、自有归属与重启核对、实际余额接通。
- 自动赎回覆盖标准/neg-risk pUSD、EOA/单所有者Safe、实际权利核对、提交前raw/hash/nonce持久化、同身份重查/重播、32块及Transfer/烧毁核对。到账标记和实际现金原子提交，未本地归账时不重复加总资金；本地结算意图重启可继续。
- 两处旧深度缺陷已在原处理器+数据库实际复现；新逐价位消费修复且回归验证。多档FAK记录按订单归组，各笔实际Fill与目标仍独立。
- 真实公开行情短测产生14笔模拟Fill、10持仓，现金4.37U，同库重启核对通过，PAUSED。独立临时目录和明确开发设置，不是产品默认或正式活动。
- Mac完整68测试通过；Linux aarch64/bookworm生产依赖实际构建，68测试通过。代码验证提交 e91563f5723609f126ed540d9bd3d3d36f795de6；之后仅文档交付提交。
- Linux生产base镜像通过非root/只读rootfs、认证/API/HTML/JS、健康和同库重启：2U设置保留、100U现金不变、同generation、LIVE独立1U；最后恢复TEST每轮1U，始终PAUSED。
- 浏览器实测320px配置/持仓页无横向溢出、桌面布局、草稿保存、START/PAUSE、LIVE余额只读与模式配置隔离、21项展开和排序、记录读取。临时视口已恢复。
- Dockerfile、Compose、CI、环境样例、Nginx样例、未来LIVE覆盖样例、许可证、Mac/上传/部署/备份/更新/回滚手册齐备。

- 2026-09-07 从实际公开仓库克隆至新目录，独立下载Python3.12.14并重建虚拟环境；68测试、Ruff和JS语法检查通过。离线服务健康为TEST/PAUSED/LIVE=false，HTML/JS可读，验证后停止。详见docs/VALIDATION.md与REINSTALL.md。

## 本次恢复核验（2026-09-07）
- 在 `/Users/d4clt/Documents/Codex/2026-09-07/zhe/PM-NAUTILUS` 完成真实 Git 克隆；`origin` 为 `https://github.com/yvettemiranda/PM-NAUTILUS.git`，本地 `main`、`HEAD` 与远端 `main` 均为 `cf910d25aedb380ab88e45a104ed057a525d715d`。克隆后工作树干净，`git fsck --no-dangling` 通过；随后仅维护本文件，未提交或推送。
- 本机为 macOS 26.6.2 arm64，已安装 Apple Command Line Tools 26.6（Git 2.50.1）；按 REINSTALL 使用 Python 3.12.14、uv 0.8.22 和 `uv sync --frozen --extra dev` 重建 `.venv`（75 个锁定包）。Ruff check/format 通过；当前真实工作树 `pytest -q` 为 68 项通过、2 项既有依赖弃用警告（1.77 秒）。已使用校验过的 Node 24.20.0 执行 `node --check src/pm_nautilus/web/app.js`，通过。
- 显式 `PM_LIVE_ENABLED=false` 的离线服务验收返回 `mode=TEST`、`strategyStatus=PAUSED`、`liveExecutionEnabled=false`、100U/每轮1U，首页和 API 可读后已 SIGINT 停止。本机没有 `.env`、真实凭据或 LIVE 启用；未进行真实签名/下单/approve/redeem/链上写入。
- 随后在用户确认的独立 Ubuntu 24.04 amd64 服务器上，以固定 SHA `cf910d25aedb380ab88e45a104ed057a525d715d` 部署主 Compose。服务器 `.env` 为本机生成、`0600` 保护且不记录密码；容器只监听 `127.0.0.1:8765`，Nginx 仅通过 HTTPS 反代，HTTP 不承载应用而是跳转 HTTPS（ACME 验证路径除外）。保留服务器本地 `compose.override.yaml`：它仅信任回环和 Docker 网桥的转发来源，使 HTTPS 控制请求保留正确协议；已用不启用 LIVE 的 POST 验收 CSRF/Origin 路径返回预期 400。无邮箱签发的短期 IP 证书及 Certbot 定时续期/成功后重载 Nginx 均已配置并演练通过。三地独立公网 HTTPS 探测均为 200；服务器健康检查、Basic 认证页面和容器重启后均确认 `TEST`、`PAUSED`、`liveExecutionEnabled=false`。未启用 LIVE，未接触钱包或链上写入。地址、密码、证书序列号等敏感运行细节不写入交接文件。

## 扫描与分类线上修复（2026-09-07）
- 用户页面的“扫描失败”不是网络或筛选规则问题：旧 Gamma `GET /events` 的 offset 分页在 `offset=2100` 必定返回 HTTP 422。扫描循环因此在第 22 页中断，保留旧结果；首页分类也仍依赖主站的 filtered tags 路径。
- 本地提交 `ce96a05bc6ddbaabd6f902f232bcc118bf8326fe` 将正式扫描改为官方 Gamma `/events/keyset` 的不透明 cursor 分页，校验 wrapper、事件前进和 cursor 前进；分类改为 Gamma related-tags 接口（`status=active`、`omit_empty=true`），并在成功同步后清除过期 `categoryError`。公开行情短测也同步迁移至 keyset。
- 回归结果：Ruff check/format、前端 JS 语法检查均通过，`pytest -q` 为 69 通过（2 个既有第三方弃用警告）。真实公开行情短测抽取 100 个事件、选中 10 个、监控 20 个 Token，模拟成交及重启核对均通过，结束后仍为 TEST + PAUSED，私有连接数为 0。
- 2026-09-08 已通过 GitHub 官方设备授权恢复 `yvettemiranda` 的 HTTPS 推送能力；推送前重新 fetch 并确认 `origin/main` 没有新提交，本地修复及交接记录随后通过普通快进同步到公开 `origin/main`，未使用强推。服务器仍固定运行已验证的代码提交 `ce96a05bc6ddbaabd6f902f232bcc118bf8326fe`；其后的提交仅维护交接记录，不需要重新构建或部署，服务器本地 `compose.override.yaml` 保持未改。
- 同日整体复核发现 GitHub Actions 对旧版官方动作发出 Node 20 弃用警告；已按官方当日稳定主版本将 `actions/checkout`、`actions/setup-python` 和 `actions/setup-node` 统一升级到 v7，保留原 Python、uv、Node、双架构测试和 Docker verify 流程。
- 部署后容器 healthy；实际首次扫描完成且 `lastError=null`、`categoryError=null`、14 个分类、10,922 个监控 Token、46 个可交易事件；随后一轮定时全量扫描同样完成且所有扫描/服务/行情流错误字段均为空（公开市场数据会动态变化）。Chrome 页面已实测显示“扫描完成”、候选市场列表、14 个市场类别及其下方筛选项。带有效认证、CSRF 和同源头的 `POST /api/live/start` 明确返回 400 “LIVE尚未在服务器配置并明确启用”，因此没有启动 TEST 或 LIVE，亦未触碰钱包、签名、下单、approve、redeem 或链上写入。

## 正式 TEST 启动（2026-09-08）
- 用户在页面完成配置并于 2026-09-08 08:55:25 CST（2026-09-08T00:55:25Z）明确点击 `START`。启动前页面为100U、0持仓，扫描已完成并显示监控742个Token、3个可交易Event；公开市场数据会继续动态变化，这些数量只作为启动基线。
- 点击后独立健康检查确认 `status=ok`、`strategyStatus=RUNNING`、`liveExecutionEnabled=false`、`revision=ce96a05bc6ddbaabd6f902f232bcc118bf8326fe` 且 `backgroundErrors={}`。这是正式长期 TEST 的开始时间，不代表72小时已经验收，也不构成任何 LIVE 授权。
- 用户不要求定时提醒，将按需回来询问进度；后续每次检查应实时核对健康、扫描错误、资金、持仓、交易记录和账本验证，区分实际采样区间与无人检查区间，不把缺少证据的时间自动记为已验收。保持 LIVE 禁用，不接触钱包或链上写入。
- 同日为按需复核建立专用 ED25519 SSH 入口：Mac 别名为 `pm-nautilus-monitor`，服务器公钥使用 `restrict` 与强制命令 `/usr/local/sbin/pm-nautilus-monitor`，不能取得 PTY、转发端口或执行调用方传入的任意命令。脚本只输出资源、Docker/Nginx/证书续期、固定源码、健康、脱敏面板、账本验证、SQLite quick-check 和近期错误；版本化副本为 `deploy/pm-nautilus-monitor`。任意命令阻断、应用 healthy/零重启、账本验证及 quick-check 均已实测；首次资源快照显示内存余量较小且已使用 swap，尚无运行错误，后续复核需持续观察。写操作、升级和故障修复仍须用户另行授权并使用腾讯云控制台，不能借监控密钥执行。

## TEST 自动暂停修复（2026-09-08）
- 09:20 CST 按需巡检首次发现正式 TEST 已为 `PAUSED`；Nginx 控制审计只有 08:54:59 的 `POST /api/test/start`，没有后续 pause 请求。应用、容器和 Nginx 仍健康、容器零重启，`/api/TEST/validation` 与 SQLite `quick_check` 均为 `ok`，4 个 TEST 仓位及账本保留；该次正式 TEST 的连续运行区间因此在自动暂停时中断，准确暂停时刻因旧代码未留时间戳而未知。
- 增强后的只读巡检从内存面板与 SQLite `scan` 元数据同时确认 `streamError=ConnectionClosedError`。根因是公共行情 WebSocket 的常规断线异常不属于原可重试异常元组，落入未知异常分支后执行安全暂停；后台下一轮又会清除 `serviceError`，使健康接口看似正常且丢失原因。
- 修复将 `websockets.exceptions.ConnectionClosed` 明确归为可重试断线：先使相应盘口失效，再按既有上限30秒的退避重连，不暂停策略。真正未知的行情或后台异常仍保持 fail-closed：暂停、取消未完成买单，并将类型、截断详情和 UTC 时间持久化；正常后台轮次不再自动抹除致命错误，只有用户显式 START 才确认并清除当前错误，历史详情继续保留。只读巡检升级为 version 2，同时显示内存/面板和持久层的扫描与错误字段。
- 本地 Ruff、70项完整测试、前端 JS 语法和巡检 shell 语法通过；新增回归直接构造 `ConnectionClosedError` 并断言不会调用 pause。容器构建由双架构 GitHub Actions 与服务器部署继续验收。修复部署、重新显式 START 及其新连续区间应在完成后追加，不把本段诊断期间计入连续 TEST。
- 修复提交 `41d148a0d9faf693eeeaf81af32a2860d5928ec6` 已普通快进推送 GitHub；Actions run `34177018593` 在 Ubuntu x86_64 与 ARM64 均通过70项测试、Ruff、前端语法及实际 Docker verify 构建。部署前确认 TEST 已为 PAUSED，停止容器后将完整 `runtime/server` 与 `.env` 创建为服务器本地 `0600` 一致性备份；随后固定 checkout 该 SHA、更新镜像 revision、保留本地 `compose.override.yaml` 并重建。部署后源码、镜像和健康 revision 一致，容器 healthy/零重启，扫描与分类成功、4个原 TEST 仓位和资金连续保留、账本 validation 与 SQLite quick-check 均为 `ok`，LIVE 仍为 false。
- 用户已授权本次恢复操作；因 macOS 未授予鼠标辅助访问，2026-09-08 09:42:09 CST 改用应用自身 Basic 认证与 CSRF 保护的 `POST /api/TEST/start` 恢复正式 TEST，没有绕过控制边界。API、独立健康巡检和刷新后的 Chrome 页面均确认 `RUNNING`，页面按钮为 PAUSE；新连续 TEST 区间从该时刻重新计算。首次启动后行情诊断记录了一次预期的 tick 变化并重新取得完整盘口；09:45 复查全量扫描已完成、状态仍为 RUNNING、`backgroundErrors={}`、未出现 `serviceError`，账本两项校验继续通过。

## 开发入口文档常态化（2026-09-08）
- 电脑重装后的开发恢复已完成，根 README 不再以单次“重装接手任务”为入口，现改为面向任意电脑和长期维护的常规程序说明：功能、安全边界、固定依赖、快速启动、验证、部署、目录与文档导航。
- `docs/REINSTALL.md` 保留原文件路径以免旧链接失效，但内容改为长期适用的“新开发环境配置”；删除“服务器尚未部署”等已失效阶段描述。变化中的服务器、正式 TEST 与版本事实继续只维护在本文件，避免 README 随运行状态频繁过时。

## 按需巡检与实盘准备核对（2026-09-08 14:06–14:07 CST）
- 两次专用只读 SSH 快照均为 TEST/RUNNING、LIVE=false、容器 healthy/零重启，服务器仍运行 `41d148a0d9faf693eeeaf81af32a2860d5928ec6`。应用账本 validation 与 SQLite quick_check 均通过；本次未重启、部署或修改服务器设置。距离 09:42:09 启动约 4 小时 25 分钟，不能宣称 72 小时连续运行已验收，也不能从两次快照证明中间每一刻的行情可用性。
- 5 个模拟持仓，可用现金 95.819480U，持仓估值 2.001661U，总资金 97.821141U，未实现盈亏 -2.178859U，已实现 0，待赎回 0。快照不含完整逐笔成交，因此不能据此完成订单级审计或宣称卖出/回款闭环通过。
- 扫描最后完成于 14:06:10 CST，扫描与分类错误为空、14 个分类。当前可交易 Event 为 0、待定 Event 为 7；配置仍为用户的 1–3¢、总时长 30–365 天、进度≤20%、止损关闭。仅凭摘要不能确定每个待定 Event 的具体盘口原因。
- 发现诊断缺陷：`streamError=ConnectionError` 保留 09:42:05 的 tick 变化记录。代码 `remember_error` 写入当前及历史字段，但 socket 恢复后没有清除当前字段；该字段不能证明当前仍断线，也不能据此断言全部盘口已恢复。最近4小时日志无匹配错误。后续修复需按 socket 分组区分当前恢复与历史错误，避免一个分组恢复掩盖另一个分组故障；本次仅诊断，尚未修复或部署。
- 内存 available 约616MiB，swap 约189–190MiB，容器约871MiB；磁盘使用19%。Docker/Nginx/续期 timer active，证书到期为 2026-09-14 05:37:20 CST，下次续期计划 09-09 06:10 CST。当前无资源耗尽证据，仍需继续观察。
- 本地70测试、Ruff检查及格式检查通过。官方中英文合约地址本次均与源码一致，公共 Polygon RPC 对六个关键合约返回非空代码；撤回聊天中需要更换 adapter 地址的未证实推断。详见 `docs/LIVE_READINESS.md`。
- 重要纠正：PAUSED 仍允许已有仓位卖出/赎回；`auto_approve_redemption=false` 仅禁止自动授权，没有独立关闭自动赎回开关。因此不能把 LIVE+PAUSED 当成只读预检。真正只读工具应独立于应用 LIVE runtime，当前尚未实现；不得为预检启用实盘服务。

## 行情诊断修复与独立预检实现（2026-09-08）
- 用户明确要求处理后，在本地实现每组 socket 的 CONNECTING/AWAITING_BOOKS/READY/RECONNECTING/STOPPED 状态。仅在该组完整盘口全部到齐后清除该组当前错误，其他组错误继续呈现；历史 lastStreamError/At 保留。移除的订阅不再贡献当前错误。行情、交易和暂停规则未变。
- dashboard 诊断增加每组任务是否存活、最近接收时间、完整盘口数量与缺失 Token；每个 INCOMPLETE Event 明确列出缺失盘口或等待评估原因。此信息只扩展诊断/API，不重新设计 UI。现有只读巡检已输出 diagnostics，部署新版后即可看到这些字段。
- 新增 `python -m pm_nautilus.preflight`，只接收公开地址，固定区块查询 Polygon 钱包归属、合约代码、余额、交易及赎回授权；不读取 LIVE 凭据、不构造 signer、不启动 runtime。RPC 方法白名单拒绝发送交易。详见 `docs/LIVE_READINESS.md`，成功报告不是实盘验收。
- 73项测试通过；新增回归验证完整盘口恢复、不同分组错误互不遮盖、历史错误保留、待定 Event 缺失 Token、取消后盘口失效、只读 RPC 白名单、固定区块及错误链/不支持钱包类型拒绝。Ruff通过；实际公开 RPC 预检完成，未进行任何签名或链上写入。
- 本次修复先交付本地/GitHub；服务器仍固定旧代码 `41d148a0`，正式 TEST 不因诊断改动重启。待约定 TEST 检查节点再备份部署；如果实际出现行情故障或影响交易则提前处理。新增诊断尚未在正式服务器生效，不能声称已据此排查完那7个待定 Event。
- 代码提交 `d78eea20eeef29a729f0c1c21348e4c47841f759` 已普通快进推送并核对远端。GitHub Actions run `34194224894` 的 Linux x86_64/ARM64 均通过测试、Ruff、JS语法及 Docker verify 构建。14:20:54 CST 再次只读复查服务器仍 RUNNING/healthy/零重启，扫描推进到14:19:06、持仓估值变为2.039816U，账本及数据库校验通过；这证明存在更新，但不证明所有待定市场盘口完整。完整受控源码输出为本地 `artifacts/FULL_SOURCE.md`。

## 晚间按需巡检（2026-09-08 22:04–22:07 CST）
- 专用只读 SSH 四次采样均为 TEST/RUNNING、LIVE=false、healthy、零重启；服务器版本仍为 `41d148a0`。距09:42:09启动约12小时25分，仅代表经过时间，不能据采样推断所有时刻的盘口可用性。未部署、重启、改配置或操作真实钱包。
- 模拟持仓由下午5个变为6个；22:07:03快照现金94.819580U、持仓估值2.938374U、总资金97.757954U、未实现盈亏-2.242046U、已实现0、待赎回0。账本validation与SQLite quick_check均通过；监控未提供完整成交明细，尚未完成逐笔审计。
- 现场观察分页扫描数量600→6800→20700→22327，最终22:06:52完成，scanning=false且扫描/分类错误为空。14个分类、当前READY Event为0、INCOMPLETE为12；后者不是12个报错，其具体缺失盘口仍须新版诊断部署后确认。
- 21:56:58记录过 `ConnectionClosedOK`/1001正常关闭，服务器版代码会使对应盘口失效并自动重连，但恢复后不清当前错误字段。采样期间没有新的lastStreamError、serviceError或后台错误；估值有更新，不能因此推断全部连接恢复。最新内存available606MiB、swap191MiB、容器约904MiB、磁盘19%，Nginx/Docker/续期timer均active，暂未见需紧急重启的证据。

## 五日运行巡检（2026-09-13 14:33–14:36 CST）
- 用户要求检查近期运行；通过专用SSH及用户已打开的腾讯云终端进行只读查询。TEST仍RUNNING，LIVE=false，版本41d148a0，容器Up5days且restarts=0。距09-08正式启动已经超过72小时，但缺少期间完整采样且当前有行情故障，不能宣称长期TEST验收通过。本次没有部署、重启、暂停或改变配置。
- 21个模拟持仓，现金88.099421U，已实现盈亏5.054063U；两次快照持仓价值/总资金/未实现盈亏均为null。只读SQLite统计为68笔BUY Fill和19笔SELL方向Fill（包括结算，不是订单数），有1项CREDITED结算且amount=0；此项不证明正额回款闭环通过，也不能用已实现盈利代替总盈亏。
- 终端读取内存dashboard确认18个持仓READY、2个NO_BID、1个NOT_READY。未就绪的是USD/Iranian rials December事件中4.0M方向；持久化盘口最后时间为当天13:17:42 CST，ready=True仅为旧数据库快照，当前内存已失效。该Token公开CLOB /book返回6档bid/19档ask，说明不能简单归因于市场完全没有订单簿；本次未向运行引擎注入HTTP盘口。
- 扫描/分类无报错，最后完成时间从14:29:05推进至14:35:56。行情却出现实际新异常：13:44:28的1008/no ping received，14:36:16的1011/keepalive ping timeout，14:36:31的opening handshake TimeoutError。待定Event由14升至119，不能再仅用旧版错误标记不清除解释。先前GitHub诊断修复尚未部署，根因仍需区分心跳发送、事件循环阻塞、订阅恢复及网络情况。
- 内存available205–219MiB，swap905–993MiB，容器1.175–1.278GiB，比09-08压力明显增加；该相关性不足以确认内存就是断线根因。磁盘19%，SQLite约211MB，账本validation和quick_check仍通过。主机运行6天，Docker/Nginx/续期timer正常；证书已自动更新为09-11 05:17:01至09-17 21:17:00 CST有效。
- 下一步应先备份原账本，排查并修复真实行情恢复/心跳问题，结合内存与阻塞检查验证，再部署已验证代码；保留原交易历史，准确记录新运行区间。仅部署诊断字段不能宣称修复了本次心跳故障。当前仍禁用LIVE。

## 心跳与订阅恢复修复（2026-09-13）
- 用户明确授权修复、推送GitHub及调整服务器。14:39:06 CST 已停止故障TEST并完成服务器本地0600完整备份 `backups/pre-feed-fix-20260913.tgz`（约30MB，包含runtime/server、.env及compose.override.yaml）。不重置资金或交易历史；旧连续TEST区间在本次维护处结束，恢复时间另行记录。
- 根因之一已由官方协议和代码共同确认：市场channel要求每10秒文本PING，旧代码仅在15秒无消息时发送，繁忙流不会触发。改为按单调时钟固定期限发送文本PING，跟踪PONG；连续30秒无PONG视为连接不可用并走既有重连流程。每个快照处理后让出事件循环，避免大批消息饿死其他连接。
- 对收到连接但长期缺少完整盘口的Token，每30秒仅对缺失项进行unsubscribe/subscribe；不因已完整的安静盘口没有价格变化而判错。保留现有订阅组，新增Token不再使全部分组重排重连。真实公共WS探针35秒收到4次PONG，unsubscribe/subscribe后再次收到完整book（共2次），验证恢复协议。
- 扫描入库将市场元数据写入合并为一笔事务，跳过未变化Token的SQLite写入和Nautilus instrument重注册，减少FULL同步磁盘与重复对象处理。未修改资金/成交/盘口消费规则，没有降低SQLite持久化等级。
- 本地77测试通过（2个既有依赖警告）、Ruff通过。真实公开行情独立短测抽样100事件、10个选中事件、108监控Token，产生14笔模拟Fill，账本及同库重启核对通过，结束PAUSED，私有连接0。此开发样本与正式服务器账本分离。

- 第一轮修复 bc02add 已通过 GitHub Actions 34743475867（x86_64/ARM64）并部署。14:48:04 CST 全量扫描20894事件完成，5条连接持续收到PONG，21持仓恢复为19 READY/2 NO_BID，旧缺失持仓已恢复；现金88.099421、已实现5.054063与维护前一致，账本/SQLite校验通过。HTTPS认证、CSRF/Origin通过，LIVE启动返回400，14:48:43恢复TEST。
- 部署复验进一步定位2个非持仓事件的8个缺失Token：公共HTTP均有盘口，直接WS也返回完整book。其中Token238381…336067的新WS快照时间1789281399681ms，数据库旧增量时间1789281399682ms；跨连接比较时间戳导致永久拒收。补充修复仅在not-ready时允许完整快照建立新连接基线；同连接内继续拒绝旧快照/增量，断线期间拒绝增量，并保留买卖两侧已消耗模拟深度。不能通过清空books/consumed绕过此问题。
- 证书到期时间以最新openssl输出为准：Sep17 13:17:00 UTC，即09-17 21:17 CST（上述五日巡检的UTC换算记录有误）；续期timer仍active。
- 用户19:47继续后，78测试、Ruff及GitHub Actions 34755364023双架构测试/构建通过。19:48第一轮版本bc02add仍healthy、RUNNING、LIVE=false，5条连接905盘口均READY、pending=0、账本通过；现金87.545221、22持仓，已实现5.054063，新增模拟成交产生于14:48恢复后的运行，不重置。
- 第二次完整备份为服务器本地0600 `backups/pre-snapshot-fix-20260913-1949.tgz`，19:49:27最终代码bcec57db85e726e2029f1209b08ceeb043a1c711构建并启动，保留HTTPS override与原.env配置，初始PAUSED。该备份包含最新5小时交易，不以14:39旧备份覆盖新账本。

- 第二轮部署初次启动发现books.py因备份umask影响检出而为0600，非root容器无法导入。只修正该公开源码为0644后重建，19:57:33启动成功，19:58健康且新容器零重启；未放宽.env/备份权限。docs/DEPLOY.md补充备份umask隔离说明，防止复发。
- 服务器本地临时副本与19:49备份逐条SQL对照：362条原生事件的序号/事件ID/类型/原始payload全部一致，preferences相同；临时审计副本已自动清理，正式账本及两份完整备份保留。
- 最终20:13:19 CST通过HTTPS认证、Origin/CSRF及TEST validation后恢复TEST/RUNNING，LIVE=false。恢复前扫描21427事件完成，5组全部READY、pendingEventCount=0，22持仓20 READY/2 NO_BID；现金87.545221、持仓价值5.985959、总资金93.53118、已实现5.054063、未实现-11.522883（时点估值，不是收益承诺）。最新代码bcec57d；后续纯文档提交不需要再重启。
- 服务器仍为约2GiB内存、磁盘19%；19:58容器约1.239GiB、available246MiB、swap169MiB。修复未证明长期内存问题完全消失，不自动付费扩容或停止无关服务。下一次巡检重点看内存/swap趋势、扫描耗时、心跳PONG、缺失盘口及账本；在进入LIVE前另行做资源容量及长期运行验收。保留定时证书续期及只读SSH。

## 按需巡检（2026-09-17 18:11–18:12 CST）
- 仅只读检查，无重启、暂停、改配置或部署。直接SSH在密钥交换前被关闭，HTTPS直连握手也失败；经用户已打开的腾讯云控制台成功读取服务器。访问链路原因未定，不能据此认定服务停机。
- 运行代码bcec57d，TEST/RUNNING、LIVE=false，容器healthy且restarts=0；距09-13 20:13恢复约94小时，但不据采样声称全时段无故障。扫描最后完成18:09:07，20841事件，分类/扫描/服务/当前流错误均空。18:08:46历史记录有文本PONG超时；采样时26组884盘口全部READY，PONG更新，缺失盘口0，说明已恢复。
- 27持仓（26 READY、1 NO_BID），现金88.418209U、持仓价值7.644994U、总资金96.063203U、已实现10.163853U、未实现-14.10065U。初始100U，当前总值仍低于初始，不把已实现盈利当总盈利。2项CREDITED结算均amount=0，尚不证明正额赎回闭环。账本validation及SQLite quick_check通过，数据库294506496字节。
- 内存available169MiB、swap992MiB、容器1.255GiB，磁盘20%；Docker累计block读407GB/写930GB（不是磁盘占用），有持续资源压力。26组较部署时5组增多，符合稳定分组累积现象，后续性能检查应关注连接碎片、行情写盘及内存；本次没有擅自优化或付费扩容。
- Nginx/Docker/证书续期timer均active；证书已续期，当前有效至09-21 04:57:57 CST，下一定时执行09-18 06:10 CST。巡检记录仅更新本地HANDOFF，未为状态检查额外推送GitHub。

## 按需巡检（2026-09-18 09:27–09:28 CST）
- 只读SSH仍在连接初期被关闭，经用户Chrome腾讯云控制台读取固定巡检脚本。未改配置、重启、暂停或部署；代码仍bcec57d。TEST/RUNNING、LIVE=false、容器healthy、Up4days、restarts=0；距09-13 20:13恢复约109小时，不据此声称全时段零中断。
- 扫描最后完成09:24:09，20929事件；分类/扫描/服务/当前流错误为空，30组873盘口READY、缺失0，PONG更新至09:27:36。09:24:34曾记录文本PONG超时，采样已恢复，偶发心跳中断仍存在。
- 35持仓（昨日27），现金82.397228U、持仓价值10.112799U、总资金92.510027U、已实现10.981061U、未实现-18.471034U；相对初始100U合计-7.489973U，相对昨日巡检总值降低3.553176U。2项CREDITED结算仍均为0，不能视为正额回款验收。账本validation及SQLite quick_check通过，数据库304508928字节。
- 内存available180MiB、swap996MiB、容器1.255GiB，与昨日大致相当但余量仍低；磁盘20%，Docker累计block读623GB/写1.19TB，不是磁盘占用。订阅组26→30，后续需关注碎片化与资源趋势。
- Docker/Nginx/续期timer均active，证书再次自动续期至09-24 21:14:07 CST，下一timer为当天13:50。记录仅更新本地HANDOFF，未推送GitHub。

## 异常巡检（2026-09-20 16:41–16:43 CST）
- 用户仅要求查看进度，本次未重启/暂停/恢复/部署/改配置。SSH仍连接初期关闭，改用用户Chrome腾讯云控制台。固定巡检阻塞在应用HTTP阶段，停止的只是本次巡检命令，未停止应用。
- 容器Up6days但unhealthy，restarts=0、OOMKilled=false；健康探针最近多次读响应TimeoutError，FailingStreak=6075，不从该计数反推精确故障起点。当前资金/持仓/完整账本校验未取得，不能沿用09-18结果冒充最新。
- 只读SQLite meta确认status=PAUSED、serviceError=InvalidStatus，lastServiceError为WebSocket握手HTTP503，时间09-19 01:36:36 UTC（09:36:36 CST）；该错误会触发自动安全暂停。最新持久化扫描仍推进到09-20 16:35:07 CST、18640事件，当前持久化streamError为空，历史最新文本PONG超时16:28:51。PAUSED并非进程完全停止，仍允许既有仓位退出/结算；最新原生事件seq1759 OrderFilled时间09-20 10:58:04 CST。
- available127MiB、swap约1.3GiB、容器1.364GiB、磁盘20%。vmstat短采样si约24–30MB/s、I/O等待46–52%，明确有资源压力，但尚未证明这是健康超时唯一根因。累计block写2.3TB不是磁盘占用。证书仍有效至09-24 21:14 CST，Docker/Nginx/续期timer均active。
- 下一步需用户授权修复后先保留/备份现场账本，处理HTTP503恢复分类和资源/响应超时，再校验账本与盘口，不能直接START或以重启代替根因处理。巡检记录只更新本地，未推送GitHub。

## 长期运行故障修复（2026-09-20）
- 用户明确要求修复并同步GitHub和服务器。16:47:05 CST服务器完成停机与本地0600备份 `backups/pre-recovery-20260920.tgz`（48MB，含账本/.env/HTTPS override），另存容器故障状态与日志；不得外传备份或重置账本。停止后available从约127MiB恢复到1.5GiB，swap从约1.3GiB降至160MiB。
- 修复WS握手503错误分类：沿用固定websockets客户端对500/502/503/504和网络异常的重试判定，断线盘口立即失效、指数退避上限30秒；401/403及未知错误继续锁存安全暂停。不自动清除历史fatal或绕过账本检查。
- 订阅分组保留稳定完整组，仅在碎片超过 `ceil(监控数/200)+1` 时压紧部分组。完整市场发现和监控范围不截断；650次逐个新增及删除回归验证全覆盖、无重复、有界连接数。
- 行情原始快照改为按需加载，仅为实际行情/历史执行创建Nautilus instruments；移除已取消订阅且无执行历史的缓存。模拟消耗深度仍持久化：下单前保存完整盘口、Fill投影原子保存消耗、公共深度缩减影响已消耗量时立即保存。保留SQLite WAL/FULL和原生事件先提交后投影规则，写盘失败fail-closed。普通报价和未变业务状态不再重复写盘，零消费价位不再占用shadow字典。
- Docker构建显式保证公开源码可读，并以实际非root用户执行模块导入测试，防止备份umask再次影响运行权限。未放宽.env/备份权限。
- 本地88测试通过（2项既有依赖弃用警告），Ruff/格式/JS检查通过。新增覆盖503与认证失败区别、碎片有界、按需缓存、下单前写失败及旧Fill投影失败重放；1000次无持仓报价不产生数据库变更。真实公开行情独立短测：100事件样本、10个选中Event、64个监控Token、12笔原生模拟Fill、10持仓，同库重启现金128000微单位一致，双重账本校验通过；开发临时账本最终PAUSED、私有连接0。
- 服务器部署和正式账本复验尚未完成，不能将本段开发测试当作线上恢复成功。LIVE始终禁用。

- 第一轮代码 `f5635a1161661b37467e756d0c4c09464752fa09` 已推送，GitHub Actions 35500937623 双架构通过；服务器Linux verify镜像88测试通过，21:46:30 CST已启动同SHA生产base镜像。21:48全量扫描18755事件完成，行情错误为空；旧fatal仍等待显式START确认，不因部署自动抹除。原848条事件（seq<=1759）序号/ID/类型/payload整体SHA256与维护前一致，完整性及原生账本验证通过；现金97.085789、30持仓、已实现19.777069。面板本地响应0.168秒，内存893.8MiB，采样I/O wait为0%，较故障现场明显改善；仍PAUSED。
- 继续复验发现数据库约30.7万Token中23.6万已关闭且无须常驻。补充优化仅将无历史执行的关闭市场从运行时内存移除，数据库不删除；启动加载全部开放市场及所有历史执行身份，已关闭持仓/历史仍可重放。完整发现和筛选不截断，重新开放市场可由下次全量扫描重新加载。新增关闭市场持仓重启回归，本地89测试通过；此补充版本待部署复验。
- 补充版本 `a4947c5c884cf48b2e500e9ddec73bc628a7d414` 已推送，GitHub Actions 35515717722双架构通过；服务器第二次备份 `backups/pre-metadata-20260920.tgz` 保留第一轮恢复后的最新账本。Linux verify镜像89测试通过，22:14:29 CST同SHA生产base启动，默认PAUSED、LIVE=false。原848条事件完整哈希再次一致，SQLite及原生账本通过。
- 22:16:01全量扫描完成，随后6组1017盘口全部READY、pending=0，30个原有持仓恢复；扫描/分类/流当前错误为空，旧503 fatal时间仍为09-19未变。22:16:53 CST通过认证HTTPS、CSRF/Origin和启动前账本校验恢复用户已授权的正式TEST，健康200、RUNNING、LIVE=false、backgroundErrors={}；开始新的有效观察区间，不延续故障及停机时间。
- 22:17:43只读SSH实测重新可用，无权限扩大；Docker healthy、零重启，Nginx/续期timer active。内存646.5MiB、available966MiB、swap163MiB，较原故障改善。6组1017盘口仍全部READY，账本/quick_check通过；31持仓、现金96.086029、总资金104.7952、已实现19.777069、未实现-14.981869。新增买入为恢复后的模拟成交，不重置资金。当前GitHub与镜像代码均为a4947c5，后续仅文档交付提交不要求重启镜像。

## 修复后跨夜复核（2026-09-21）
- 11:49–11:50 CST通过现有腾讯云终端实测，运行镜像仍为 `a4947c5c884cf48b2e500e9ddec73bc628a7d414`，容器healthy、零重启、OOM=false；TEST/RUNNING、LIVE=false、backgroundErrors={}。距09-20 22:16:53恢复约13.5小时，此为跨夜采样，不声称期间每秒均被监控或已经完成72小时验收。
- 全量扫描已持续推进至11:44:12，采样时新一轮正在扫描；扫描/分类/流/服务当前错误均为空。6组1011盘口全部READY、pending=0；32持仓为31 READY/1 NO_BID，NO_BID是已收到盘口但没有买盘，不是断线。最近流事件为10:18:55的tick变化触发按设计重连，已恢复。
- 原生账本校验通过。现金95.086129、总资金104.955219、持仓估值9.86909、已实现19.777069、未实现-14.82185；为模拟资金，不构成收益保证。
- 容器642.8MiB、主机available约1.0GiB、swap183MiB；累计容器I/O为读911MB/写278MB，采样I/O wait=0%，无故障前持续swap抖动。CPU瞬时约一个核心，不能据此声称零负载。未扩容、未改变策略配置、未删除历史数据。
- 本机直连SSH本次在握手前被关闭，公网curl也出现TLS连接中断；云终端仍可用且本机上次SSH已成功，因此直连管理网络的间歇问题仍需区分，不能声称永久修复SSH链路。没有扩大密钥权限或关闭安全保护。后续按需检查优先尝试只读SSH，失败时使用已授权云终端。
- 11:50:53 CST服务器自身通过实际HTTPS入口请求健康、首页、app.js和认证面板均HTTP200；新一轮完整扫描11:50:28完成，四类当前错误仍为空，14个分类可读。Nginx与证书续期timer active，证书有效至09-24 21:14:07 CST。无需为了纯文档同步再次中断TEST；运行镜像保留已验证a4947c5，服务器源码可快进到仅文档交付版本，应用代码/依赖须与a4947c5完全相同。

## 接手只读复核（2026-09-21 14:08–14:14 CST，线上待验证）
- 完整恢复本文件、REINSTALL、根执行指令、VALIDATION、DEPLOY上下文；本地及父级未发现适用AGENTS.md。起始工作树干净。本地HEAD与实时git ls-remote返回的GitHub main均为 `15d0cdcd64aed340433e0454859662455ceb8966`；与 `a4947c5c884cf48b2e500e9ddec73bc628a7d414` 的差异仅为HANDOFF与VALIDATION两份文档。
- 本次重新运行本地pytest：89通过、2项既有依赖弃用警告。GitHub API确认最新文档提交Actions run 35558957637的Ubuntu x86_64和ARM64作业均success。
- 强制只读SSH在握手阶段被服务器关闭，HTTPS直连出现TLS连接中断。现有Chrome OrcaTerm会话已断开，点击普通“重新连接”后要求用户微信扫码身份验证；尚未执行任何服务器命令。终端背后的旧输出不能作为当前状态证据。当前健康、扫描、盘口、账本、资金、日志、资源和服务器版本仍待实时复核，不将11:50历史结果冒充本次实测，也不将管理链路失败归因为应用故障。
- 未重启、START、PAUSE、reset、部署、改参数或扩大权限；未触碰LIVE或钱包。仅本地追加本交接记录，未提交或推送。
- 代码检查：已有分组状态/最近收包/PING/PONG/缺失盘口、快照重订阅次数、当前及最近流/服务错误，已有资金占用字段与原生成交记录。尚无完整暂停原因时间线、累计重连统计或连续资产估值历史。拟小范围复用现有诊断展示，补充低频状态/资产采样和只读TEST报告；分类重叠需明确统计口径，历史缺失估值不能补造最大回撤。仅方案，实施范围待用户确认。

## 交付与继续入口
- README.md：启动入口；docs/DEPLOY.md：完整操作步骤；docs/VALIDATION.md：结果、覆盖与未验收部分。
- artifacts/FULL_SOURCE.md：所有受控文本文件完整内容（含锁文件，无省略）；SOURCE_MANIFEST.json逐文件SHA256及最终提交。
- artifacts/PM-NAUTILUS-source.zip为完整源码；PM-NAUTILUS.bundle保留分段Git历史。以git log及manifest为最终交付版本，不把代码验证SHA误认为后续纯文档提交SHA。
- .reference/下保存原项目、公开短测、缺陷复现及Linux证据，不入Git。runtime/ui-preview、runtime/ui-acceptance为开发样本，不作为正式账本。
- 临时UI服务已在确认PAUSED后SIGTERM停止；Linux验收容器已healthy验证后停止，临时Lima pm-verify已确认停止，保留镜像/卷供复验，不作为生产服务。

## 发布状态与后续外部事项
1. 2026-09-07 用户明确授权新建公开仓库 yvettemiranda/PM-NAUTILUS 并上传，覆盖原指令的拟定私有安排。公开仓库已创建并推送完整 main 历史：https://github.com/yvettemiranda/PM-NAUTILUS 。GitHub Actions run 34071046061 在 Linux x86_64 与 ARM64 均通过68测试、前端语法检查及Docker verify构建。恢复指南见 docs/REINSTALL.md，不再询问首次上传授权。
2. 2026-09-07 用户提供并确认独立服务器后，已完成固定 SHA 的 TEST 部署；公网入口为 HTTPS IP 反代，应用内部端口未公开，HTTP 不承载 Basic 认证。服务器本地 `compose.override.yaml` 是 HTTPS POST 的必要信任边界，更新时保留并用禁用 LIVE 的 POST 复验 CSRF/Origin。IP 证书为约六天的短期证书，Certbot 定时续期和 Nginx 重载钩子已通过 dry-run 演练。继续维护时先核对 `certbot renew` 定时服务、Nginx 和 Compose 健康；不得改为明文 HTTP 或启用 LIVE。未停止/更改任何旧项目或旧服务。
3. 实际LIVE钱包类型、服务器本地凭据、RPC、授权及资金未提供；当前代码支持EOA和单所有者Safe，其他类型须补对应路径。真实钱包/链上验收另行明确授权，不把可控测试当真实成功。
4. ~/.codex/templates/agent_memory/仍不存在。按用户AGENTS要求不能伪造模板结构，因此只建立agent_memory/archive/，暂由本文件承载当前上下文/进度/风险。模板提供后原样建立三个.md再填当前事实。

## 复验命令
```sh
cd /Users/d4clt/PM-NAUTILUS
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/pytest -q
.reference/node-v24.20.0-darwin-arm64/bin/node --check src/pm_nautilus/web/app.js
```

Linux临时环境使用 `.reference/lima/bin/limactl`，LIMA_HOME=$PWD/.reference/lima-home，实例pm-verify；Compose生产部署流程见DEPLOY。所有重启默认PAUSED。中断恢复先读本文件、MIGRATION/VALIDATION/DEPLOY、git status/log；不要重新设计已确定规则。外部条件补齐后按根指令流程推进，保持实盘未启用。

## 紧凑首页与真实收益采样（2026-09-22，部署前）
- 用户批准以赚亏与运行状态为主的紧凑 UI，并授权推送 GitHub 与服务器：TEST/LIVE + 状态点、小收益曲线、资金明细、持仓/记录/市场分页及可展开详情，适配手机。
- 累计净盈亏沿用已实现+未实现口径；每60秒独立采样到 equity_samples，按模式/generation隔离，最近1440个样本展示。所有旧交易事件保留；不反推历史行情，未知估值/重启/缺采样处断开曲线。采样故障单独展示，不改变策略状态。
- 保留500毫秒面板刷新，展开状态保持；页面连接失败不显示全绿，NO_BID不冒充断线；LIVE仍关闭，策略参数不变。
- 本地91项Python测试、3项JS状态/曲线测试、Ruff与JS语法通过；320/390px浏览器无横向溢出，持仓展开、记录、LIVE隔离与禁用已实测。服务器及GitHub完成情况将在部署后补记。

- 首次正式浏览器复验发现历史静态缓存仍可能被复用，补充HTML资源版本标识以保证旧用户加载新JS/CSS；部署完成状态待最终核对。

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
