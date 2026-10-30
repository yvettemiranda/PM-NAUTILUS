# PM-NAUTILUS

基于 NautilusTrader 的 Polymarket 交易程序。TEST 使用真实公开行情模拟成交，LIVE 使用同一套策略、风控、目标价、退出与赎回业务，通过独立的真实执行器操作账户。

## 仅保留两个版本入口

| 版本 | GitHub 入口 | 用途 |
| --- | --- | --- |
| 当前最新版 | [`main`](https://github.com/yvettemiranda/PM-NAUTILUS/tree/main) | 十个独立 TEST 账户、通用性能优化、账户故障隔离及 TEST/LIVE 后台互斥；后续开发从这里继续。 |
| 十账户改造前基准 | [`baseline/pre-test-10-accounts-20261027`](https://github.com/yvettemiranda/PM-NAUTILUS/tree/baseline/pre-test-10-accounts-20261027) | 固定源码 `4de107a4dc1a4abaaf08b330eb00803391268e2a`，仅供比较和追溯，保持原样。 |

已验收应用代码为 [`2f6d6f6`](https://github.com/yvettemiranda/PM-NAUTILUS/commit/2f6d6f6c860d87bd289300af9ce532f0f2045c74)。本次仓库整理只更新 Markdown 和版本入口，`main` 的程序、依赖、策略配置及测试文件与该应用相同。服务器应用未因文档整理重启。旧工作分支、重复标签及已被取代的 PR 不再作为版本入口；Git 提交历史仍可追溯。

2026-10-29 应用验收：**1,221 项 Python、114 项页面回归、AMD64/ARM64 CI 与 Docker 验证全部通过**，服务器十个 TEST 和一个 LIVE 账本保全核对通过。[验证结果](docs/VALIDATION.md)与[当前交接](HANDOFF.md)分别记录证据和运行边界。

## TEST 十账户

- 页面选择 1—10 号，只切换查看和操作对象，不改变其他账户的运行状态。
- 账户分别保存初始资金、参数、模拟盘口消耗、订单、持仓、交易记录、收益曲线和启停状态。
- 1 号保留原 `TEST/state.sqlite`；2—10 号分别使用 `TEST/accounts/N/state.sqlite`。新账户首次复制 1 号的初始设置，不复制交易和资金历史，之后互不跟随。
- 公共市场发现和行情连接共享，各账户模拟成交独立。去重报价解码、正在进行的相同公开查询及不可变元数据，不共享资金或业务状态。
- 正常启动后所有 TEST 均 PAUSED；需要逐账户明确 START。账户局部故障只影响该账户，整机、公共网络和共享磁盘故障仍可能影响多个账户。

说明、API 和目标卖价步长例子见 [TEST 多账户](docs/TEST_ACCOUNTS.md)；优化与隔离边界见 [通用优化](docs/TEST_OPTIMIZATION_20261029.md)。优化不按服务器型号分支，不减少市场、历史、精度或原交易检查，换服务器可直接沿用。

## TEST 与 LIVE 切换

TEST、LIVE 后台只运行其一。启用 LIVE 前必须先暂停全部 TEST，程序等待 TEST 行情、扫描、结算和采样任务退出，并持久化模式意图，再激活 LIVE 私有客户端。LIVE 启用后仍为 PAUSED，需要另行明确 START。

PAUSE 沿用原含义：停止新买、取消未完成买单，已有仓位的卖出、止损和结算仍维护；因此 PAUSE 不等于后台完全停止。模式切换由程序完成后台排空，不能用只关闭网页代替。

保存过 LIVE 模式意图的程序重启后，钱包保持锁定，LIVE 不自动启用，TEST 后台也保持停止。明确选择“恢复TEST后台（保持暂停）”后，账户仍需分别 START。不存在故障后自动恢复交易。

LIVE 钱包、资金、订单和账本独立，TEST 优化不进入真实签名或执行逻辑。当前完整接入路径限程序已支持的钱包类型；地区、资金、手续费和账户准入必须当次核对。TEST、CI、镜像或健康检查通过均不代表真实买卖及正额赎回闭环已验收。详见 [LIVE 准备检查](docs/LIVE_READINESS.md)。

## 本地使用

固定依赖：Python 3.12.14、uv 0.8.22、NautilusTrader 1.231.0；全部依赖以 `uv.lock` 为准。原生 Mac 使用兼容的 arm64/macOS 环境，生产镜像支持 Linux AMD64、ARM64。完整安装步骤见 [新环境配置](docs/REINSTALL.md)。

```sh
git clone --branch main https://github.com/yvettemiranda/PM-NAUTILUS.git
cd PM-NAUTILUS
uv sync --frozen --extra dev
uv run --frozen pm-nautilus --data-dir runtime/local
```

打开 [本地页面](http://127.0.0.1:8765)。全新目录建立十个 PAUSED 模拟账户，默认模拟资金为 100U，检查配置后再决定是否 START。已有目录保留原账户设置，不套用这个初始值。只预览页面、不连接公开行情：

```sh
uv run --frozen pm-nautilus --offline --data-dir runtime/ui-preview
```

离线预览不能验证已有仓位的实时退出。公开的[策略规则快照](config/README.md)不会自动覆盖任何账本；新服务器导入规则及真实账本迁移按部署文档操作。

## 验证与部署

```sh
uv run --frozen ruff check src tests scripts deploy/live-secrets.py
uv run --frozen ruff format --check src tests scripts deploy/live-secrets.py
uv run --frozen pytest -q
node --check src/pm_nautilus/web/app.js
node --test tests/*.test.mjs
```

Node 24.20.0 用于页面验证，程序运行不需要 Node。部署固定已经验证的完整 Git SHA，通过 Docker Compose 和自己的 HTTPS/认证入口提供服务。克隆、拉取源码或切换分支不会自动同步现有服务器。

钱包、`.env`、真实账本、WAL/SHM、服务器覆盖配置和备份不提交 GitHub。升级或换服务器时保全全部十个 TEST 账本、LIVE 账本、模式意图和钱包；停止旧写入后制作一致性备份，确保新旧服务器不同时管理真实账户。不要把基准镜像直接套到较新账本上。

## 文档入口

- [当前交接与最新部署验收](HANDOFF.md)
- [两个版本的维护规则](docs/VERSIONS.md)
- [TEST 多账户](docs/TEST_ACCOUNTS.md)
- [通用优化与模式互斥](docs/TEST_OPTIMIZATION_20261029.md)
- [启停、关闭、钱包取消与历史分批修复](docs/EGM_FIXES_20261030.md)
- [验证结果与未验收范围](docs/VALIDATION.md)
- [部署、备份、升级与回滚](docs/DEPLOY.md)
- [新开发环境](docs/REINSTALL.md)、[首次 LIVE](docs/START_FRESH_LIVE.md)
- [架构与规则对应](docs/MIGRATION.md)
- [界面与收益曲线](docs/UI_REFRESH.md)、[磁盘保护](docs/STORAGE_MAINTENANCE.md)
- [依赖与许可证](NOTICE.md)

带日期的审计文档用于解释当时的修复和证据，不能覆盖当前交接状态。原始需求保存在 [迁移指令](PM-SMALL_Nautilus_Codex_Instructions.md)，用于追溯，不应重新执行其中的历史初始化任务。
