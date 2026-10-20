# 新开发环境配置

## 2026-10-20 缺报价仓位按成本暂计（待发布验收）

本轮按用户确认口径调整展示：正常持仓继续显示报价、市值、剩余成本与浮盈亏；`UNAVAILABLE`／`NOT_READY` 仓位稳定排到列表底部，并保留数量、成本、均价、目标和原因。先对全部持仓排序，再截取前 20 笔或展开全部，纯摘要与各页的全仓汇总一致。

资产估算为账面现金（含已冻结现金）＋可估值持仓市值＋缺报价持仓剩余成本＋原有已证明待赎回应收；失败赎回权利仅在未计入现金时以原剩余成本暂计，诊断回款金额不作为现金或应收。盈亏估算保留原已实现盈亏、可估值持仓浮盈亏和原已知待赎回盈亏；缺报价持仓及失败赎回权利的当前浮盈亏暂按 0 计入，并注明数量、成本暂计金额及“浮盈亏暂记 0，恢复报价后重算”。成本暂计不代表可兑现市值，暂记 0 不是已确认零收益，也不抹掉原已实现盈亏。

这些值只用于只读展示，交易报价、原严格完整估值字段、账本、权益、参数和收益历史不改写。新的 `estimatedTotalFunds`／`estimatedPositionValue`、`knownUnrealizedPnl`／`knownTotalPnl` 和逐仓 `estimatedUnrealizedPnl` 明确区分展示估算与真实报价；锁定或未核对的 LIVE 账户继续隐藏账户资料。存储维护保留，LIVE 不启用。

本地最终 886 项 Python（25.30 秒）、41 项页面测试、Ruff 检查／格式（106 文件）通过；14 项部署门禁专项测试通过，代码已做独立复核。本轮应用 SHA、CI、备份和服务器／页面验收结果待发布后补录；目前线上仍为应用 `ebbe888`、源码／文档 `5640b0a`。下方保留前轮历史证据。

**2026-10-20 历史部署（停用识别）：**[PR #16](https://github.com/yvettemiranda/PM-NAUTILUS/pull/16) 已合并，固定应用代码 `ebbe888a56213f3c976fdb5291b045354cb821e9`；代码 push、PR 和 [main CI 39397396195](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/39397396195) 的 x86_64／ARM64 回归及 Docker verify 全部通过。本地 880 项程序、37 项页面、Ruff（105 文件）及 JS/shell 语法通过。17:14:13 CST 服务器验收 PASS，healthy／零重启／未 OOM，TEST/RUNNING 已恢复，LIVE 未启用、钱包 LOCKED，后台错误为空；原账本、参数、钱包及空间维护机制保留。源码／文档 SHA 以本次纯 Markdown 提交及私有 `docs-sync.json` 记录为准；纯文档同步仅推进源码，应用镜像、env、runtime 保持上述固定代码，无需重启。

本次现场 92 笔持仓中 1 笔已归档、报价不可用，其他未就绪持仓为 0；剩余 4/4 组、767/767 盘口完整。旧价已屏蔽，完整资产／盈亏未知，另列精确的可估值持仓小计；原持仓和权利保留，处置安排待核查，不保证恢复、退款或结算。在线完整 TEST validation（38.999 秒）、离线 Runtime、数据保留、认证 dashboard 语义及 API／JS 检查通过；本轮未做生产浏览器登录后的视觉验收。仅为短时采样，真实资金闭环仍未验证；详情见 [市场可用性](MARKET_AVAILABILITY.md) 和 [HANDOFF](../HANDOFF.md)。

**2026-10-20 历史部署（空间维护）：**自动空间维护与运行时保护已发布，应用 `640d1b1dbc58f2e61ef9cf3e131cd37a1e163de9`；847 项程序/34 项页面及双架构 CI、服务器完整验收通过。日志自动轮转、15 分钟主机清理和磁盘/inode 保护已实际安装；当时 TEST/RUNNING、LIVE 未启用、钱包 LOCKED，原参数/账本/钱包保留。清理范围、阈值及容量边界见 [自动存储维护](STORAGE_MAINTENANCE.md)。新服务器须安装并验收定时任务；真实资金闭环仍未验证。

**2026-10-19 历史维护：**元数据压缩与扫描调度优化已发布，应用 `6dac94bf6bc9cbc954a22e112a52dd49a798d2a6`，752 项程序/33 项页面及双架构 CI、服务器完整验收通过；原 TEST/RUNNING 已恢复，LIVE 未启用、钱包 LOCKED，参数及历史保留。实际库文件减少 31.95%；新格式库不能直接配旧镜像回滚，见 [本轮格式、维护与验收](METADATA_OPTIMIZATION_2026-10-19.md)。本次维护不代表真实资金闭环已验证。

本指南适用于新电脑、全新系统或独立开发目录。项目公开仓库为 [yvettemiranda/PM-NAUTILUS](https://github.com/yvettemiranda/PM-NAUTILUS)。它是长期有效的开发环境恢复说明，不记录某一次电脑重装或服务器部署阶段；当前运行状态以根目录 [HANDOFF.md](../HANDOFF.md) 为准。

如果目的是买新服务器、放弃旧 TEST 模拟记录、首次启用本程序实盘，请先读 [从 GitHub 到首次实盘](START_FRESH_LIVE.md)。下面的 Python/uv 步骤是开发环境重建细节，不是用户需要独自逐条完成的实盘清单。

重建环境不改变钱包支持范围：完整路径仅 EOA(0)/单签 Safe(2)，EOA 官方允许交易名单资格未验证，默认新建 Deposit Wallet(3) 不支持。先确认类型与准入再准备资金；已有 Safe 自动发现不代替账户准入，也不自动改已有 vault，无交易钱包由用户确认清除后重导。

**2026-10-14 本轮核对修复：**[修复 PR](https://github.com/yvettemiranda/PM-NAUTILUS/pull/12)，应用代码 `3e765ccb067c8012af5f4ea4f23c3bda91d7372f`；回归：705 项 Python / 33 项页面回归、Ruff/JS 与 PR/main 双架构 CI 通过，明细见 [最新验证](VALIDATION.md)；服务器验收：2026-10-14 19:03 CST 通过，TEST/RUNNING、LIVE 未启用、钱包 LOCKED；实际持仓与授权只读核对通过，资金/POL 与地区仍未满足条件。持仓使用本次固定区块的 CTF 原子余额，API 全分页仅发现 token；已知零数量 token 保留身份，严格链上零收益的外部标准二元残仓不再误拦截，不烧毁、赎回或接管旧仓。完整证明及失败关闭条件见 [LIVE_READINESS.md](LIVE_READINESS.md)。已有 Safe 已正确重连，升级前交易所及两个赎回适配器权限只读核对通过，资金/POL 和地区仍须满足条件；真实买卖及正额赎回尚未完成实盘闭环验收。

历史 [PR #10](https://github.com/yvettemiranda/PM-NAUTILUS/pull/10) 应用代码 `fefcdb3963f57ceaea8f0eba93686c6024dc1f0f` 的本地 591/33 项回归、PR/main 双架构 CI 和服务器完整验收通过；2026-10-14 13:36 CST 当时状态 TEST/RUNNING、LIVE 未启用、钱包 LOCKED。旧 `512c8a2` 及本轮最新证据见交接/验证记录，历史状态不能代替当前核对。

## 1. 克隆项目

在新电脑终端执行（公开克隆无需登录）：

```sh
mkdir -p ~/Projects
cd ~/Projects
git clone https://github.com/yvettemiranda/PM-NAUTILUS.git
cd PM-NAUTILUS
git status --short
git log -5 --oneline
```

源码、提交历史、原始需求、设计、UI、测试、锁文件、Docker/CI、交接与运维文档都在仓库中。继续开发不依赖旧聊天、旧电脑绝对路径或 `.reference/`。需要推送修改时使用有仓库权限的 GitHub 账户。

## 2. 重建开发环境

原生 Mac 环境要求 macOS 26+ arm64；项目固定 Python 3.12.14、uv 0.8.22 和 NautilusTrader 1.231.0。不兼容的 Mac 使用仓库 Linux 容器，参见 [DEPLOY.md](DEPLOY.md)，不要复制旧 `.venv`。

依照 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 先安装下载工具 uv 0.12.10，然后重新打开终端。旧版 uv 0.8.22 的内置目录没有 Python 3.12.14，不能直接用它下载该 Python：

```sh
curl -LsSf https://astral.sh/uv/0.12.10/install.sh | sh
```

回到克隆目录执行：

```sh
uv python install 3.12.14
# Python 下载完成后，把依赖管理工具恢复为项目验证过的版本：
curl -LsSf https://astral.sh/uv/0.8.22/install.sh | sh
uv --version  # 应为 0.8.22
uv sync --frozen --extra dev
uv run --frozen ruff check src tests scripts deploy/live-secrets.py
uv run --frozen ruff format --check src tests scripts deploy/live-secrets.py
uv run --frozen pytest -q
uv run --frozen pm-nautilus --offline --data-dir runtime/environment-check
```

首次下载和加载原生依赖可能较慢；保持终端开启并等待服务显示启动完成，不要仅因短时间没有输出就判定失败。

打开 http://127.0.0.1:8765 查看页面。首次使用新目录应为 TEST + PAUSED、100U、每轮1U，LIVE未启用。离线预览没有实时行情；检查完用 Ctrl+C 停止。准备连接公开行情时使用 `uv run --frozen pm-nautilus --data-dir runtime/local`，启动仍为 PAUSED，不自动开始正式长期测试。

前端是静态 HTML/CSS/JS，启动无需 Node 或前端构建。需要前端验收时安装 Node 24.20.0，然后运行：

```sh
node --check src/pm_nautilus/web/app.js
node --test tests/ui-state.test.mjs
```

前端测试会执行实际页面控制处理程序，覆盖模式与异步响应隔离、配置与运行操作互斥、未变化分类焦点、全仓摘要、隐藏页返回刷新和超过一万条的历史分页。GitHub Actions 还会验证 Linux x86_64/ARM64 与 Docker 构建。当前代码的测试数量、发布状态和通过证据以 [VALIDATION.md](VALIDATION.md) 最新章节及对应 CI 为准；历史版本的结果不能替代新版本验收。

## 3. 仓库不保存的内容

`.env`、密码、私钥、钱包/API/RPC凭据、加密凭据包、SQLite账本、runtime、日志、参考项目、虚拟机、虚拟环境以及本地 artifacts/backups 均不上传。GitHub 仓库不保存服务器正式 TEST/LIVE 账本；从仓库恢复会创建新的本地开发账本，不会复制服务器或其他电脑的成交历史。

本仓库只保证项目开发材料可恢复。**若用户明确放弃旧 TEST 模拟历史，且本程序尚未产生真实 LIVE 订单、持仓或在途交易，新服务器可以从空白账本开始；旧模拟账本备份不是首次实盘的前提。**已获用户同意公开的 `config/strategy-profile.json` 保存当前交易设置，但空白账本不会因 GitHub 克隆而自动采用它；应先确认文件仍是想用的版本，再分别应用并读回 TEST、LIVE。钱包以前在本程序之外产生的挂单和持仓也不会因新建账本而消失，仍须在接入时核对。

若要保留旧 TEST 历史，或本程序后来已经有真实 LIVE 交易，应按 [部署指南](DEPLOY.md)保存最新一致性账本及与之配套的加密钱包。网页 LIVE 页可下载、上传单个加密迁移包；新服务器仍须由管理员先配置 Docker、HTTPS 与登录。服务器完整运维备份还应包含 `runtime/server`、`.env` 和本地 `compose.override.yaml`，解锁密码与备份文件分开保管。旧式 `compose.live.yaml`、`/etc/pm-nautilus/live.json.age` 和 `/run/pm-nautilus/live.json` 仅适用于旧部署，不是网页钱包流程。换电脑后克隆 GitHub 可继续开发或远程管理原服务器，不必把签名私钥复制到新电脑。

若决定恢复旧账本，且快照使用 age 接收公钥加密，还须另行安全迁移对应的 identity 私钥；GitHub 无法恢复它。2026-09-23 的服务器快照采用此方式，解密身份文件与服务器备份位置见 `HANDOFF.md`。选择空白账本开始时，不需要该旧账本的解密 identity。

迁移到新服务器时先暂停旧实例的新买、确认在途订单与链上交易，再停止旧实例，制作并验证**停机后的最终一致性备份**。随后在新服务器恢复加密备份和本地配置，核对账本、设置、代次及真实账户，在网页解锁钱包并重新检查；确保旧实例不会再次使用同一钱包后，才由用户手动启用 LIVE、另行 START。运行时下载的网页备份可能漏掉下载至停机之间的卖出或赎回，不能单独替代最终备份。服务器重启后 LIVE 锁定，须在网页重新人工解锁。

当前 `Store` 会原子地将旧业务集合迁移为 v2 `business_parts`，新账本也采用该格式。新电脑继续开发时使用锁定的当前代码；若要运行历史提交，先在隔离副本上确认其能读取业务及原生账本格式，不能让不支持 v2 的旧代码直接打开已升级的生产数据库。账户待确认授权/赎回及 nonce 记录必须完整恢复，不能因换电脑或网页请求取消而清掉。升级、回滚及钱包工作取消的处理见 [DEPLOY.md 第 5、7 节](DEPLOY.md#5-备份升级回滚)。纯文档更新不改变应用版本，无需为文档同步重启服务器；源码 main SHA 可以比镜像/env/runtime 的固定代码 SHA 更新，须按 HANDOFF 核对差异仅为文档。

需要重新生成完整源码文档或离线源码备份时：

```sh
uv run --frozen python scripts/full_source.py
mkdir -p artifacts
git archive --format=zip --prefix=PM-NAUTILUS/ --output=artifacts/PM-NAUTILUS-source.zip HEAD
git bundle create artifacts/PM-NAUTILUS.bundle main
```

## 4. 在新环境接手开发

在 Codex 中打开克隆目录，将以下文字作为接手请求：

> 请完整阅读根目录 README.md、HANDOFF.md、PM-SMALL_Nautilus_Codex_Instructions.md，以及 docs/REINSTALL.md、MIGRATION.md、VALIDATION.md、DEPLOY.md。先核对 git status、版本、当前运行状态和验证记录，恢复锁定的开发环境并执行必要测试。迁移、UI、服务器部署和正式 TEST 已有实现与历史记录，不要从骨架重做或假定需要重新部署。默认启动仍为 TEST + PAUSED；除非我另行明确要求，不启用 LIVE、不读取真实钱包、不真实下单或进行链上写入。维护 HANDOFF.md，区分当前事实、历史记录和未验收部分。

当前状态的权威入口是 HANDOFF.md；历史 `.reference/` 日志不随克隆恢复，已验证结论与复验方法在 VALIDATION.md。可选的本地辅助模板或技能不是应用依赖，缺失时不能阻塞源码恢复。

## 5. 恢复后的工作方式

本地环境通过测试后即可按正常 Git 工作流继续开发。不要把新电脑的 `runtime/local` 当作服务器账本，也不要因为本地环境恢复而自动修改、重启或重新部署服务器；涉及服务器时先读取 HANDOFF.md 的实际目标、版本和运行状态，再按 DEPLOY.md 备份、升级和验收。

真实 LIVE 钱包、授权、资金与链上验收仍是独立阶段。除非用户明确提供对应信息并授权启用，不要将可控测试或正式 TEST 结果解释为实盘已可直接运行。
