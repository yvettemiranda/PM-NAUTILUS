# 新开发环境配置

本指南适用于新电脑、全新系统或独立开发目录。项目公开仓库为：https://github.com/yvettemiranda/PM-NAUTILUS 。它是长期有效的开发环境恢复说明，不记录某一次电脑重装或服务器部署阶段；当前运行状态以根目录 `HANDOFF.md` 为准。

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

原生 Mac 环境要求 macOS 26+ arm64；项目固定 Python 3.12.14、uv 0.8.22 和 NautilusTrader 1.231.0。不兼容的 Mac 使用仓库 Linux 容器，参见 DEPLOY.md，不要复制旧 `.venv`。

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
uv run --frozen ruff check src tests scripts
uv run --frozen ruff format --check src tests scripts
uv run --frozen pytest -q
uv run --frozen pm-nautilus --offline --data-dir runtime/environment-check
```

首次下载和加载原生依赖可能较慢；保持终端开启并等待服务显示启动完成，不要仅因短时间没有输出就判定失败。

打开 http://127.0.0.1:8765 查看页面。首次使用新目录应为 TEST + PAUSED、100U、每轮1U，LIVE未启用。离线预览没有实时行情；检查完用 Ctrl+C 停止。准备连接公开行情时使用 `uv run --frozen pm-nautilus --data-dir runtime/local`，启动仍为 PAUSED，不自动开始正式长期测试。

前端是静态 HTML/CSS/JS，启动无需 Node 或前端构建。需要语法检查时安装 Node 24.20.0，再执行 `node --check src/pm_nautilus/web/app.js`。GitHub Actions 也会检查前端语法及 Linux 双架构测试/镜像构建。

## 3. 仓库不保存的内容

`.env`、密码、私钥、钱包/API/RPC凭据、SQLite账本、runtime、日志、参考项目、虚拟机、虚拟环境以及本地 artifacts/backups 均不上传。GitHub 仓库不保存服务器正式 TEST 账本；从仓库恢复会创建新的本地开发账本，不会复制服务器或其他电脑的成交历史。

如果另有自己需要保留的电脑文件或凭据，需自行单独备份；本仓库只保证项目开发材料可恢复。未来正式运行产生的账本与服务器凭据按 DEPLOY.md 加密备份，不能提交公开仓库。

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
