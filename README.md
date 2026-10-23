# PM-NAUTILUS

## 界面整理与近一周曲线（2026-10-23，已发布部署验收）

主界面突出盈亏、总资产估算与现金，估值解释、策略和市场技术信息按需展开；记录页直接展示。收益曲线默认最近七天，可左右拖动或按周翻看旧记录，点击“回到最新”恢复跟随。完整历史、原估值口径、策略及交易控制保持。快捷键与验证范围见 [界面说明](docs/UI_REFRESH.md)。固定应用 `a45abef7093ea55d1af0244442f2f93a2c05285d` 已部署，服务器完整验收通过，TEST 已恢复运行，LIVE 关闭；详细证据见 [HANDOFF](HANDOFF.md)。后续源码／文档同步不重启应用。

## 2026-10-21 执行与查询恢复优化（已发布部署验收）

新增未提交旧价买单复核、逐笔订单核对、公开分页有限重试、赎回独立调度，以及扫描／结算查询持续异常提示。策略规则、参数、账本与未知仓位成本暂计保持，故障后自动恢复运行未加入。实现及验收边界见 [执行与恢复优化](docs/EXECUTION_RESILIENCE.md)；实际部署结果见 HANDOFF 最新记录。

固定应用 `257b1fbe205cea083d6a9a80ac739fcfea2c17b4` 已部署，2026-10-21 17:40:41 CST 最终验收 PASS；TEST/RUNNING、LIVE=false、钱包 LOCKED，原参数和两库账本保留。实际证据、备份及源码／应用版本区分见 [HANDOFF 最新记录](HANDOFF.md)；后续纯文档同步不重启应用。

## 2026-10-20 缺报价仓位按成本暂计（已发布部署验收）

本轮按用户确认口径调整展示：正常持仓继续显示报价、市值、剩余成本与浮盈亏；`UNAVAILABLE`／`NOT_READY` 仓位稳定排到列表底部，并保留数量、成本、均价、目标和原因。先对全部持仓排序，再截取前 20 笔或展开全部，纯摘要与各页的全仓汇总一致。

资产估算为账面现金（含已冻结现金）＋可估值持仓市值＋缺报价持仓剩余成本＋原有已证明待赎回应收；失败赎回权利仅在未计入现金时以原剩余成本暂计，诊断回款金额不作为现金或应收。盈亏估算保留原已实现盈亏、可估值持仓浮盈亏和原已知待赎回盈亏；缺报价持仓及失败赎回权利的当前浮盈亏暂按 0 计入，并注明数量、成本暂计金额及“浮盈亏暂记 0，恢复报价后重算”。成本暂计不代表可兑现市值，暂记 0 不是已确认零收益，也不抹掉原已实现盈亏。

这些值只用于只读展示，交易报价、原严格完整估值字段、账本、权益、参数和收益历史不改写。新的 `estimatedTotalFunds`／`estimatedPositionValue`、`knownUnrealizedPnl`／`knownTotalPnl` 和逐仓 `estimatedUnrealizedPnl` 明确区分展示估算与真实报价；锁定或未核对的 LIVE 账户继续隐藏账户资料。存储维护保留，LIVE 不启用。

本地最终 886 项 Python（25.30 秒）、41 项页面测试、Ruff 检查／格式（106 文件）通过；14 项部署门禁专项测试通过，代码已做独立复核。[PR #17](https://github.com/yvettemiranda/PM-NAUTILUS/pull/17) 已合并，固定应用为 `2095d1427b6aaf3eae886f1172b2b1c10c8981d0`；代码 push、PR 和 [main CI 39444708014](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/39444708014) 的 x86_64／ARM64 回归及 Docker verify 全部通过。2026-10-20 23:19:15 CST（15:19:15 UTC）服务器最终验收 PASS：容器 healthy、restarts=0、OOM=false，原 TEST/RUNNING 已恢复，LIVE=false、钱包 LOCKED，后台错误为空。完整在线 TEST validation PASS（62.018 秒）、真实 TEST 账本隔离副本 Runtime 验证及 TEST/LIVE 两库全部停机逻辑数据保留通过；原参数、代次、native／equity 历史、钱包、schema／codec v2、依赖／配置／安全设置与自动存储维护保留。 本次现场 94 笔持仓中 93 笔正常估值，1 笔归档持仓稳定排在第 94 笔；其他未就绪持仓为 0，剩余 702/702 盘口、4/4 连接组 READY。资产估算包含该笔精确剩余成本，行内浮盈亏显示 `0.00U（暂计）`，既有已实现盈亏保留；完整、摘要、前 20 笔的全仓聚合一致，正常仓排序保留。真实 Chrome 生产页已确认资产和盈亏均显示数字、94 笔可展开及异常项末位成本／零浮盈亏；行情变动会更新估算数字。本轮未启用 LIVE 或操作真实资金，短时验收不等于实盘闭环或长期稳定性证明。 源码／文档 SHA 以本次纯 Markdown 提交及私有 `docs-sync.json` 记录为准；镜像、env 和 runtime 保持固定应用代码，纯文档同步无需重启。下方保留前轮历史证据。

**2026-10-20 历史部署（停用识别）：**[PR #16](https://github.com/yvettemiranda/PM-NAUTILUS/pull/16) 已合并，固定应用代码 `ebbe888a56213f3c976fdb5291b045354cb821e9`；代码 push、PR 和 [main CI 39397396195](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/39397396195) 的 x86_64／ARM64 回归及 Docker verify 全部通过。本地 880 项程序、37 项页面、Ruff（105 文件）及 JS/shell 语法通过。17:14:13 CST 服务器验收 PASS，healthy／零重启／未 OOM，TEST/RUNNING 已恢复，LIVE 未启用、钱包 LOCKED，后台错误为空；原账本、参数、钱包及空间维护机制保留。源码／文档 SHA 以本次纯 Markdown 提交及私有 `docs-sync.json` 记录为准；纯文档同步仅推进源码，应用镜像、env、runtime 保持上述固定代码，无需重启。

本次现场 92 笔持仓中 1 笔已归档、报价不可用，其他未就绪持仓为 0；剩余 4/4 组、767/767 盘口完整。旧价已屏蔽，完整资产／盈亏未知，另列精确的可估值持仓小计；原持仓和权利保留，处置安排待核查，不保证恢复、退款或结算。在线完整 TEST validation（38.999 秒）、离线 Runtime、数据保留、认证 dashboard 语义及 API／JS 检查通过；本轮未做生产浏览器登录后的视觉验收。仅为短时采样，真实资金闭环仍未验证；详情见 [市场可用性](docs/MARKET_AVAILABILITY.md) 和 [HANDOFF](HANDOFF.md)。

**2026-10-20 历史部署（空间维护）：**自动空间维护与运行时保护已发布，应用 `640d1b1dbc58f2e61ef9cf3e131cd37a1e163de9`；847 项程序/34 项页面及双架构 CI、服务器完整验收通过。日志自动轮转、15 分钟主机清理和磁盘/inode 保护已实际安装；当时 TEST/RUNNING、LIVE 未启用、钱包 LOCKED，原参数/账本/钱包保留。清理范围、阈值及容量边界见 [自动存储维护](docs/STORAGE_MAINTENANCE.md)。新服务器须安装并验收定时任务；真实资金闭环仍未验证。

**2026-10-19 历史维护：**元数据压缩与扫描调度优化已发布，应用 `6dac94bf6bc9cbc954a22e112a52dd49a798d2a6`，752 项程序/33 项页面及双架构 CI、服务器完整验收通过；原 TEST/RUNNING 已恢复，LIVE 未启用、钱包 LOCKED，参数及历史保留。实际库文件减少 31.95%；新格式库不能直接配旧镜像回滚，见 [本轮格式、维护与验收](docs/METADATA_OPTIMIZATION_2026-10-19.md)。本次维护不代表真实资金闭环已验证。

PM-NAUTILUS 是基于 [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) 的 Polymarket 自动交易程序。项目将 PM-SMALL 已确定的扫描、筛选、Event 仲裁、FAK、周期预算、分批目标、主动止损和结算规则迁移到独立的 Python/NautilusTrader 应用，并保留手机与桌面共用的网页操作界面。

程序只管理自身产生的订单、仓位和赎回权利，不导入 PM-SMALL 的历史账本，也不接管手动仓位。TEST 与 LIVE 使用同一套策略算法，但各自保存参数，成交、手续费、资金和结算路径不同；TEST 的结果不能当作 LIVE 会同样成交或产生同样收益的证明。

**2026-10-14：精确持仓核对与零收益旧仓修复。**[本轮修复](https://github.com/yvettemiranda/PM-NAUTILUS/pull/12) 的应用代码为 `3e765ccb067c8012af5f4ea4f23c3bda91d7372f`；回归记录：705 项 Python / 33 项页面回归、Ruff/JS 与 PR/main 双架构 CI 通过，明细见 [最新验证](docs/VALIDATION.md)；服务器验收：2026-10-14 19:03 CST 通过，TEST/RUNNING、LIVE 未启用、钱包 LOCKED；实际持仓与授权只读核对通过，资金/POL 与地区仍未满足条件。已有 Safe 已正确重连，升级前只读核对确认账户身份、交易所及两个赎回适配器权限；资金、POL 和服务器地区仍未满足启用条件。完整证据及旧版本验收见 [HANDOFF.md](HANDOFF.md)。原策略参数保留，真实买卖、正额赎回及延迟/滑点仍未完成实盘验收。

## 换电脑、换服务器后从零开始

不保留旧 TEST 模拟记录、准备在新服务器首次使用本程序实盘时，先看 [从 GitHub 到首次实盘](docs/START_FRESH_LIVE.md)。初次服务器安装仍需配置 Docker、HTTPS 和网页登录；之后可在网页 LIVE 页导入并解锁钱包、查看只读检查、启用为 PAUSED，再由本人按 START。TEST 不需要钱包。

**先确认钱包类型和官方资格。**当前完整路径仅支持 EOA（签名类型 0）和单签 Safe（类型 2、唯一 owner、阈值 1）。[官方钱包说明](https://docs.polymarket.com/trading/wallets-auth#set-up-eoa-trading)要求 EOA 列入允许交易名单，本程序尚未验证此项；新建官方账号默认的 Deposit Wallet（类型 3）当前不支持。新钱包和新服务器不等于已就绪，确认兼容和准入后再准备资金。

已有平台账户的签名 EOA 可能与资金 Safe 不同。导入的资金地址可留空，只读发现并严格核对已有单签 Safe；查不到或核对失败拒绝，不猜测 EOA。接错的空白钱包须由用户输入密码并确认清除，再重新输入同一密钥，不自动改绑已保存钱包；有 LIVE 交易或未知提交时不能这样处理。明确填写资金地址仍保留原核对方式。

GitHub 保存程序、说明和你同意公开的 [策略设置文件](config/strategy-profile.json)；新服务器安装时须将这份文件中的规则应用到独立的 TEST、LIVE 账本并读回核对。该文件不会自动追踪旧服务器日后的改动，也不保存钱包秘密、密码或交易记录。只克隆仓库不会交易。若本程序后来已经产生真实订单或持仓，就不能再把旧 LIVE 记录当作可丢弃的模拟历史；应按 [部署与运维手册](docs/DEPLOY.md)迁移并对账。

## 主要功能

- 通过 Gamma keyset 全分页发现开放 Event，同步官方市场类别并持续订阅公开订单簿。
- 按市场类型、类别、价格、总时长、生命周期进度和 Bid/Ask 比例筛选候选市场。
- 在 Event 内进行确定性仲裁，使用共享盘口深度预演和执行 FAK 买卖。
- 冻结每轮预算，为每笔实际 Fill 建立独立目标价，并支持可持久化的主动止损流程。
- TEST 使用模拟资金和成交；LIVE 接入官方 CLOB V2 订单、账户核对、恢复及自动赎回流程。
- 使用原生交易事件日志和 SQLite 业务投影，支持同一账本重启恢复及一致性验证。
- 提供响应式网页 UI、START/PAUSE、配置、资金、持仓、交易记录和市场扫描视图；摘要按全部持仓计算，明细按当前页读取，交易记录支持超过一万条后继续分页。浏览器页面隐藏时停止自动轮询，返回页面立即重新读取；服务器策略不受影响。LIVE 页引导钱包导入、解锁与就绪核对，右上角齿轮会展开并定位到交易设置。
- LIVE 页可下载、恢复包含加密钱包和真实账本的 `.pmnb` 文件；尚无实盘交易时可在网页纠正接错的钱包。
- 收益曲线可按 H（每小时一点）或 D（按 UTC 日期每天一点）查看，原始采样仍用于连续性判断。
- 提供 Docker Compose、Nginx 示例、健康检查、GitHub Actions 双架构验证和只读 SSH 巡检脚本。

## 安全边界

每次进程启动都会进入 `TEST + PAUSED`；LIVE 钱包须在网页重新解锁、核对并启用，启用后仍是 `LIVE + PAUSED`。连接公开行情、切换 LIVE 页面或启用钱包都不会自动开始新买；只有显式 START 才允许新买。PAUSE 会停止新买并取消未完成买单，但已有仓位的目标卖出、止损和结算仍继续处理。LIVE 锁定期间已有真实仓位不会由程序维护，页面应明确显示锁定状态，不能将未展示的仓位视为零仓位。

LIVE 接入钱包后，资金显示取实际账户核对结果；TEST 的模拟本金（新账本默认 100U）不会复制到 LIVE，也不会自动作为实盘初始资金。实盘新买仍按 LIVE 页保存的每轮金额和其他规则执行。接钱包本身不会发单；启用和 START 前必须完成只读账户及服务器地区检查。

仓库默认 `PM_LIVE_ENABLED=false`，不包含钱包、API、RPC、服务器密码或真实账本。网页 LIVE 导入支持 12 词助记词或私钥，以及公开资金钱包地址；密钥只在 HTTPS 连接上传给本程序，不写入 GitHub。服务器在 `/data/wallet/live.vault` 存放 `0600` 加密文件，不保存助记词原文；解锁后进程内存仍持有签名密钥。实际钱包类型、余额、授权、原有订单与持仓和服务器地区须经只读核对；核对不通过不能启用。不要把自动化测试通过等同于真实资金验收。

LIVE 发单若确定尚未进入交易所请求，会结束本地订单并释放资金和 Event 占用；一旦请求可能已发出但结果不明，会暂停新买、保留占用并等待账户核对。已确认结束但零成交的买入、目标卖出及止损单会在下一次定时检查重新评估；结果未知的订单不会因此释放占用或重发。REST 查询须包含一致的订单身份、成交数量和交易列表才能确认终态。账户挂单核对使用查询完成后的最新本地订单记录；持续存在的程序外挂单仍阻止新买。健康接口也会报告交易运行时及 LIVE 维护任务故障。上述行为不改变策略金额和筛选规则。

BUY 使用精确十进制金额并按锁定 SDK 对应 tick 的份额精度向下量化，TEST 使用同一口径；SELL 仍按可签名的 0.01 份向下量化。SDK 的签名、含费余额保护及真实成交的原始限价检查保留。钱包就绪检查完整分页发现 token，再以本次固定区块的链上 CTF 原子份额精确核对账本；Data API 的显示数量不能替代六位份额。原生启动报告按 token 汇总本程序未结算权利，读取链上余额后再检查最新权利，并在恢复完成、ready 前保留最终新查。每批链上读取有界并行，不缓存跨轮结果；每笔新买仍重新查询账户、挂单与 Condition 份额。START 会先异步完成全部账本检查，再核对当前状态；公开结算查询独立于控制检查。

本程序外的旧仓通常阻止启用；仅经链上严格证明已结算、收益分子为零的标准二元输方残仓不再误拦截。该例外须核对受支持抵押资产、Condition/结果/token 派生身份及另一结果余额为零，不使用显示价格或舍入后的金额作证明。程序不烧毁、赎回、接管或入账这些外部残仓；本程序已知 token 即使账本数量为零，仍按自有身份核对。未结算、正收益、Neg-Risk、身份不明或查询失败不会因此放行。

当前服务器、正式 TEST 和最新验证状态会变化，以 [HANDOFF.md](HANDOFF.md) 为准；README 只描述长期有效的程序使用方式。

## 开发环境

项目固定使用：

- Python 3.12.14
- uv 0.8.22
- NautilusTrader 1.231.0
- `uv.lock` 中锁定的完整 Python 依赖

原生 Mac 开发环境要求 macOS 26+ arm64。不兼容的 Mac、Windows 或其他平台应使用 Linux/Docker 环境；生产镜像支持 Linux x86_64 和 ARM64。Node 用于前端语法检查和离线页面回归，应用启动不需要 Node 或前端构建步骤。

在全新电脑上开发，请先阅读 [新开发环境配置指南](docs/REINSTALL.md)。不要复制其他电脑的 `.venv`、`runtime` 或凭据文件。

## 快速开始

先按上述环境配置指南安装指定的 Python 与 uv，再克隆仓库并安装锁定依赖：

```sh
git clone https://github.com/yvettemiranda/PM-NAUTILUS.git
cd PM-NAUTILUS
uv sync --frozen --extra dev
```

启动连接真实公开行情的本地 TEST：

```sh
uv run --frozen pm-nautilus --data-dir runtime/local
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。新建本地账本的初始模拟资金为 100U、每个 Event 每轮默认 1U，状态为 PAUSED；检查配置后再决定是否点击 START。用 `Ctrl+C` 停止本地服务。

只查看页面、不连接公开行情：

```sh
uv run --frozen pm-nautilus --offline --data-dir runtime/ui-preview
```

离线模式不能提供实时盘口，也不能用于验证已有仓位退出。

## 验证

```sh
uv run --frozen ruff check src tests scripts deploy/live-secrets.py
uv run --frozen ruff format --check src tests scripts deploy/live-secrets.py
uv run --frozen pytest -q
node --check src/pm_nautilus/web/app.js
node --test tests/*.test.mjs
```

当前代码的回归数量及服务器验收记录见 [验证文档](docs/VALIDATION.md)；上述测试不会提交真实交易。

`scripts/public_smoke.py` 使用独立临时账本进行真实公开行情短测。它会使用明确的开发采样配置，不替代正式全分页扫描或长期 TEST，也不会建立私有交易连接、读取钱包或进行链上操作。

## 部署

生产部署使用 `Dockerfile` 和 `compose.yaml`，应用端口只绑定服务器回环地址，再由 Nginx 提供 HTTPS 与认证。部署、备份、升级、回滚、TEST 操作及网页 LIVE 使用见 [部署与运维手册](docs/DEPLOY.md)。

服务器必须固定运行已经通过验证的完整 Git SHA，并将 `.env`、`runtime/server`、本地 Compose 覆盖和备份留在服务器，不提交仓库。代码升级不会自动恢复 RUNNING；重启后的显式 START 属于独立控制动作。

以后迁移已有 LIVE 交易时，网页 `.pmnb` 只表示下载时的账本快照；PAUSE 后卖出和赎回仍可改变账本，不能把“先下载、后停机”当作最终迁移备份。当前版本应由管理员停止旧实例后制作最终一致性加密备份，再迁移并核对，确保新旧服务器不同时管理钱包。网页备份使用压缩和分块传输，当前上限为 4 GiB 原始 LIVE 数据库、2 GiB 加密文件；服务器还需有足够空间暂存快照。GitHub 克隆不能代替真实账本迁移。详见[部署手册](docs/DEPLOY.md)。

## 项目结构

```text
src/pm_nautilus/       应用、策略、行情、执行、持久化与网页 UI
tests/                 规则、恢复、生命周期、LIVE 与结算回归测试
scripts/               公开行情短测和源码清单工具
deploy/                Nginx、LIVE 覆盖示例与只读巡检脚本
docs/                  设计、验证、环境配置和运维文档
compose.yaml           默认 TEST、LIVE 禁用的生产 Compose
Dockerfile             Linux x86_64/ARM64 镜像定义
uv.lock                固定依赖锁文件
```

## 文档

- [新开发环境配置](docs/REINSTALL.md)
- [迁移设计与规则对应表](docs/MIGRATION.md)
- [验证结果与尚未验收范围](docs/VALIDATION.md)
- [部署、备份、升级与回滚](docs/DEPLOY.md)
- [当前运行状态与继续入口](HANDOFF.md)
- [逐环节审计与新对话交接](docs/END_TO_END_HANDOFF_2026-10-07.md)
- [依赖与许可证声明](NOTICE.md)

原始迁移要求保存在 `PM-SMALL_Nautilus_Codex_Instructions.md`，用于追溯产品规则，不应被当作每次开发都要重新执行的初始化任务。
