# PM-NAUTILUS

基于 NautilusTrader 的 Polymarket 交易程序。TEST 使用真实公开行情模拟成交，LIVE 使用同一套策略、风控、目标价、退出与赎回业务，通过独立的真实执行器操作账户。

## TEST 与历史回测

当前版本从 [main](https://github.com/yvettemiranda/PM-NAUTILUS/tree/main) 获取。TEST 使用原 1 号账户和原账本 `TEST/state.sqlite`。升级前须确认旧 2–10 号没有未完成事项，已有账本按原路径归档保留。见 [TEST 账户](docs/TEST_ACCOUNTS.md)。

「回测」接受按历史观测时间整理的公开盘口数据，在独立模拟账本上应用指定参数和起始资金，生成成交明细和净值曲线。可在同一段行情上重跑、比较参数，也可在本地和已运行的 TEST 1 逐笔对照。见 [历史回测](docs/BACKTEST.md)。

当前部署及验证记录见 [HANDOFF](HANDOFF.md)。应用镜像与文档提交分别记录。实盘启用前的核对项目见 [LIVE 准备检查](docs/LIVE_READINESS.md)。

策略设置提供四个默认关闭的选项，用于纳入独立二元市场、纳入增强负风险的具名选项、按市场分周期和只买最优卖档。TEST 与 LIVE 使用相同的规则，各账户分别保存设置。成交结果和买卖手续费可用账户账本生成分组报告。见 [可选策略与账本分析](docs/STRATEGY_PREFERENCES.md)。

## TEST 与 LIVE 切换

TEST、LIVE 后台只运行其一。启用 LIVE 前必须先暂停 TEST，程序等待 TEST 行情、扫描、结算和采样任务退出，并持久化模式意图，再激活 LIVE 私有客户端。LIVE 启用后仍为 PAUSED，需要另行明确 START。

PAUSE 沿用原含义：停止新买、取消未完成买单，已有仓位的卖出、止损和结算仍维护；因此 PAUSE 不等于后台完全停止。模式切换由程序完成后台排空，不能用只关闭网页代替。

保存过 LIVE 模式意图的程序重启后，钱包保持锁定，LIVE 不自动启用，TEST 后台也保持停止。明确选择“恢复TEST后台（保持暂停）”后，仍需手动启动可运行的账户。

LIVE 钱包、资金、订单和账本独立。TEST 专用优化不改变真实签名与订单提交流程；共用策略的等价性能改动及验证边界见 [当前交接](HANDOFF.md)。当前完整接入路径限程序已支持的钱包类型；地区、资金、手续费和账户准入必须当次核对。TEST、CI、镜像或健康检查通过均不代表真实买卖及正额赎回闭环已验收。详见 [LIVE 准备检查](docs/LIVE_READINESS.md)。

## 本地使用

固定依赖：Python 3.12.14、uv 0.8.22、NautilusTrader 1.231.0；全部依赖以 `uv.lock` 为准。原生 Mac 使用兼容的 arm64/macOS 环境，生产镜像支持 Linux AMD64、ARM64。完整安装步骤见 [新环境配置](docs/REINSTALL.md)。

```sh
git clone --branch main https://github.com/yvettemiranda/PM-NAUTILUS.git
cd PM-NAUTILUS
uv sync --frozen --extra dev
uv run --frozen pm-nautilus --data-dir runtime/local
```

打开 [本地页面](http://127.0.0.1:8765)。全新目录建立一个 PAUSED 模拟账户，默认模拟资金为 100U，检查配置后再决定是否 START。已有目录保留原账户设置，不套用这个初始值。只预览页面、不连接公开行情：

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

服务器更新使用一键部署脚本 `sudo deploy/deploy.sh <完整 SHA>`：自动备份、切换版本、核对健康，并只恢复更新前正在运行的 TEST 账户；可选的每日服务器外备份 `deploy/offsite-backup.py` 上传到腾讯云 COS（当前未启用）。步骤见 [部署与运维手册](docs/DEPLOY.md)。

钱包、`.env`、真实账本、WAL/SHM、服务器覆盖配置和备份不提交 GitHub。升级或换服务器时保全全部既有 TEST 和 LIVE 账本、模式意图和钱包；停止旧写入后制作一致性备份，确保新旧服务器不同时管理真实账户。不要把基准镜像直接套到较新账本上。

## 文档入口

- [当前交接与最新部署验收](HANDOFF.md)
- [版本维护规则](docs/VERSIONS.md)
- [TEST 账户](docs/TEST_ACCOUNTS.md)
- [历史盘口回测](docs/BACKTEST.md)
- [可选策略与账本分析](docs/STRATEGY_PREFERENCES.md)
- [执行保护、行情诊断与性能优化](docs/EXECUTION_RESILIENCE.md)
- [通用优化与模式互斥](docs/archive/TEST_OPTIMIZATION_20261029.md)
- [启停、关闭、钱包取消与历史分批修复](docs/archive/EGM_FIXES_20261030.md)
- [验证结果与未验收范围](docs/VALIDATION.md)
- [部署、备份、升级与回滚](docs/DEPLOY.md)
- [新开发环境](docs/REINSTALL.md)、[首次 LIVE](docs/START_FRESH_LIVE.md)
- [架构与规则对应](docs/MIGRATION.md)
- [界面与收益曲线](docs/UI_REFRESH.md)、[磁盘保护](docs/STORAGE_MAINTENANCE.md)
- [依赖与许可证](NOTICE.md)
- [归档文档](docs/archive/README.md)（带日期的审计与原始需求）

带日期的审计文档用于解释当时的修复和证据，不能覆盖当前交接状态。原始需求保存在 [迁移指令](docs/archive/PM-SMALL_Nautilus_Codex_Instructions.md)，用于追溯，不应重新执行其中的历史初始化任务。
