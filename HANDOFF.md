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
