# PM-NAUTILUS

PM-NAUTILUS 是基于 [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) 的 Polymarket 自动交易程序。项目将 PM-SMALL 已确定的扫描、筛选、Event 仲裁、FAK、周期预算、分批目标、主动止损和结算规则迁移到独立的 Python/NautilusTrader 应用，并保留手机与桌面共用的网页操作界面。

程序只管理自身产生的订单、仓位和赎回权利，不导入 PM-SMALL 的历史账本，也不接管手动仓位。TEST 与 LIVE 使用同一套策略规则，执行适配器和资金来源相互隔离。

## 主要功能

- 通过 Gamma keyset 全分页发现开放 Event，同步官方市场类别并持续订阅公开订单簿。
- 按市场类型、类别、价格、总时长、生命周期进度和 Bid/Ask 比例筛选候选市场。
- 在 Event 内进行确定性仲裁，使用共享盘口深度预演和执行 FAK 买卖。
- 冻结每轮预算，为每笔实际 Fill 建立独立目标价，并支持可持久化的主动止损流程。
- TEST 使用模拟资金和成交；LIVE 接入官方 CLOB V2 订单、账户核对、恢复及自动赎回流程。
- 使用原生交易事件日志和 SQLite 业务投影，支持同一账本重启恢复及一致性验证。
- 提供响应式网页 UI、START/PAUSE、配置、资金、持仓、交易记录和市场扫描视图。
- 提供 Docker Compose、Nginx 示例、健康检查、GitHub Actions 双架构验证和只读 SSH 巡检脚本。

## 安全边界

每次进程启动都会进入 `TEST + PAUSED`。连接公开行情不代表开始交易；只有显式 START 才允许新买。PAUSE 会停止新买并取消未完成买单，但已有仓位的目标卖出、止损和结算仍继续处理。

仓库默认 `PM_LIVE_ENABLED=false`，不包含钱包、API、RPC、服务器密码或真实账本。LIVE 代码可供后续受控启用，但实际钱包类型、凭据、余额、授权和链上流程必须在目标环境单独核验。不要把自动化测试通过等同于真实资金验收。

当前服务器、正式 TEST 和最新验证状态会变化，以 [HANDOFF.md](HANDOFF.md) 为准；README 只描述长期有效的程序使用方式。

## 开发环境

项目固定使用：

- Python 3.12.14
- uv 0.8.22
- NautilusTrader 1.231.0
- `uv.lock` 中锁定的完整 Python 依赖

原生 Mac 开发环境要求 macOS 26+ arm64。不兼容的 Mac、Windows 或其他平台应使用 Linux/Docker 环境；生产镜像支持 Linux x86_64 和 ARM64。Node 仅用于检查静态前端语法，应用启动不需要 Node 或前端构建步骤。

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
uv run --frozen ruff check src tests scripts
uv run --frozen ruff format --check src tests scripts
uv run --frozen pytest -q
node --check src/pm_nautilus/web/app.js
```

`scripts/public_smoke.py` 使用独立临时账本进行真实公开行情短测。它会使用明确的开发采样配置，不替代正式全分页扫描或长期 TEST，也不会建立私有交易连接、读取钱包或进行链上操作。

## 部署

生产部署使用 `Dockerfile` 和 `compose.yaml`，应用端口只绑定服务器回环地址，再由 Nginx 提供 HTTPS 与认证。部署、备份、升级、回滚、TEST 操作及后续 LIVE 配置见 [部署与运维手册](docs/DEPLOY.md)。

服务器必须固定运行已经通过验证的完整 Git SHA，并将 `.env`、`runtime/server`、本地 Compose 覆盖和备份留在服务器，不提交仓库。代码升级不会自动恢复 RUNNING；重启后的显式 START 属于独立控制动作。

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
- [依赖与许可证声明](NOTICE.md)

原始迁移要求保存在 `PM-SMALL_Nautilus_Codex_Instructions.md`，用于追溯产品规则，不应被当作每次开发都要重新执行的初始化任务。
