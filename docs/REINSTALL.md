# 重装电脑后恢复开发

本项目公开仓库：https://github.com/yvettemiranda/PM-NAUTILUS 。2026-09-07 用户明确授权新建公开仓库并上传；服务器部署推迟到电脑重装之后。此决定优先于原始指令中拟定私有仓库的旧安排。

## 1. 找回项目

在新电脑终端执行（公开克隆无需登录）：

```sh
mkdir -p ~/Projects
cd ~/Projects
git clone https://github.com/yvettemiranda/PM-NAUTILUS.git
cd PM-NAUTILUS
git status --short
git log -5 --oneline
```

源码、提交历史、原始需求、设计、UI、测试、锁文件、Docker/CI、交接与运维文档都在仓库中。继续开发不依赖旧聊天、旧电脑绝对路径或 `.reference/`。以后推送修改时再登录自己的 GitHub 账户。

## 2. 重建开发环境

原生 Mac 环境要求 macOS 26+ arm64；项目固定 Python 3.12.14、uv 0.8.22 和 NautilusTrader 1.231.0。不兼容的 Mac 使用仓库 Linux 容器，参见 DEPLOY.md，不要复制旧 `.venv`。

依照 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装固定版本，然后重新打开终端：

```sh
curl -LsSf https://astral.sh/uv/0.8.22/install.sh | sh
```

回到克隆目录执行：

```sh
uv --version
uv python install 3.12.14
uv sync --frozen --extra dev
uv run --frozen ruff check src tests scripts
uv run --frozen ruff format --check src tests scripts
uv run --frozen pytest -q
uv run --frozen pm-nautilus --offline --data-dir runtime/reinstall-check
```

打开 http://127.0.0.1:8765 查看页面。首次使用新目录应为 TEST + PAUSED、100U、每轮1U，LIVE未启用。离线预览没有实时行情；检查完用 Ctrl+C 停止。准备连接公开行情时使用 `uv run --frozen pm-nautilus --data-dir runtime/local`，启动仍为 PAUSED，不自动开始正式长期测试。

前端是静态 HTML/CSS/JS，启动无需 Node 或前端构建。需要语法检查时安装 Node 24.20.0，再执行 `node --check src/pm_nautilus/web/app.js`。GitHub Actions 也会检查前端语法及 Linux 双架构测试/镜像构建。

## 3. 仓库不保存的内容

`.env`、密码、私钥、钱包/API/RPC凭据、SQLite账本、runtime、日志、参考项目、虚拟机、虚拟环境以及本地 artifacts/backups 均不上传。现有运行目录是开发样本，没有已交付生产账本；从仓库恢复会创建新开发账本，不会恢复样本成交历史。

如果另有自己需要保留的电脑文件或凭据，需自行单独备份；本仓库只保证项目开发材料可恢复。未来正式运行产生的账本与服务器凭据按 DEPLOY.md 加密备份，不能提交公开仓库。

需要重新生成完整源码文档或离线源码备份时：

```sh
uv run --frozen python scripts/full_source.py
mkdir -p artifacts
git archive --format=zip --prefix=PM-NAUTILUS/ --output=artifacts/PM-NAUTILUS-source.zip HEAD
git bundle create artifacts/PM-NAUTILUS.bundle main
```

## 4. 交给重装后的 Codex

在 Codex 中打开克隆目录，将以下文字作为接手请求：

> 请完整阅读根目录 PM-SMALL_Nautilus_Codex_Instructions.md、HANDOFF.md，以及 docs/REINSTALL.md、MIGRATION.md、VALIDATION.md、DEPLOY.md。先核对 git status、版本和当前验证记录，恢复锁定的开发环境并执行必要测试。整体迁移、UI与可控验证已有实现，不要从骨架重做或重复询问已确定规则。GitHub公开上传已获授权；服务器部署留待我提供重装后的目标信息，不推断旧服务器就是目标。默认 TEST + PAUSED，LIVE未启用，不读取真实钱包、不真实下单或链上写入。维护 HANDOFF.md，区分已实测和未验收部分。

当前权威入口是 HANDOFF.md；历史 `.reference/` 日志不随克隆恢复，已验证结论与复验方法在 VALIDATION.md。`agent_memory` 模板原机缺失，记录在 HANDOFF；新环境若提供原始模板再按原样建立文件，不能让这一可选环境依赖阻塞源码恢复。

可选技能：交接整理用 handoff，后续代码审查用 code-review，实际出现故障再用 diagnosing-bugs；这些技能不随仓库分发，也不是应用运行依赖。

## 5. 下一阶段

本轮只上传 GitHub和验证恢复能力，不部署服务器。重装完成后再提供服务器地址、SSH用户、新应用目录和入口要求，按 DEPLOY.md 做独立部署与验收。真实 LIVE 钱包、授权、资金与链上验收仍未完成；不要将可控测试通过等同于实盘可直接启用。
