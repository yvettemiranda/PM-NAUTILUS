# PM-NAUTILUS 开发、发布与运维

默认交付：TEST + PAUSED，LIVE 未启用。下列部署步骤不启动正式长期 TEST。代码、TEST 数据、LIVE 数据各自独立；不得复制 PM-SMALL 的数据库、钱包会话或历史运行目录。

非技术用户在新电脑和新服务器上放弃旧 TEST 模拟记录、准备首次实盘时，先按 [从 GitHub 到首次实盘](START_FRESH_LIVE.md)确认顺序，再由执行部署的人使用本手册。若本程序已产生真实 LIVE 订单或持仓，须走本手册的迁移及对账流程，不能按空白安装覆盖。

## 1. Mac 本地开发

要求 macOS 26+ arm64（所选 Nautilus wheel 的最低要求）、Python 3.12.14。其他 Mac 版本使用本地 Linux 容器。新开发环境先按 [环境配置指南](REINSTALL.md) 下载 Python 并安装锁定依赖；在克隆目录运行：

```sh
uv run --frozen pm-nautilus --data-dir runtime/local
```

浏览器打开 http://127.0.0.1:8765 。程序每次启动均为 PAUSED；自动连接的是公开行情，已有本程序持仓的退出/结算继续处理。新目录最初余额 100U、每轮 1U。不要让两台进程使用同一个 SQLite 文件。

从干净环境建立依赖：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install uv==0.8.22
.venv/bin/uv sync --frozen --extra dev
.venv/bin/pytest -q
.venv/bin/ruff check src tests deploy/live-secrets.py
.venv/bin/ruff format --check src tests deploy/live-secrets.py
```

`--offline --data-dir runtime/ui-preview` 只查看本地 UI；该选项不提供行情，已有仓位也无法依靠实时盘口退出，仅用于开发验证。`runtime/` 全部不入 Git。`.python-version` 和 `uv.lock` 为固定依赖依据。

## 2. GitHub 开发流程

公开仓库为 `yvettemiranda/PM-NAUTILUS`。新电脑直接克隆已有仓库，不重复创建仓库、不覆盖远端历史、不强推；环境配置见 [REINSTALL.md](REINSTALL.md)。每次修改在推送前先 fetch，检查本地分支与 `origin/main` 的关系并保护其他开发者的提交。

```sh
git remote -v
git status --short
git push -u origin main
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

推送前检查 `git ls-files`，不得包含 `.env`、凭据 JSON、runtime、私钥、RPC 密钥或 `.reference`。CI只进行无凭据测试，不部署、不启动LIVE。GitHub main 与服务器运行 SHA 可能因纯文档提交暂时不同；实际部署版本和运行状态以 HANDOFF.md 及服务器健康接口共同核对。

## 3. 新 Linux 服务器目录

本节适用于首次部署到新服务器或建立独立的新运行目录。当前已部署服务器的实际目标、版本和入口只记录在 HANDOFF.md；不要仅凭示例地址或历史服务器推断操作目标。新目标开始时先只读执行 `uname -m`、`cat /etc/os-release`、`docker version`、`docker compose version`、`ss -lntup`、`df -h`、现有服务/目录检查。

支持 Linux x86_64 或 aarch64，使用本仓库 Debian bookworm 容器（glibc 2.36）。推荐独立目录 `/opt/pm-nautilus`，该目录仅为示例，须按实际目标选择。不要覆盖现有目录或停止无关服务。

```sh
# 在已授权服务器，使用确认的新目录：
git clone https://github.com/yvettemiranda/PM-NAUTILUS.git /opt/pm-nautilus
cd /opt/pm-nautilus
git checkout --detach <已验证的完整提交SHA>
cp .env.example .env
chmod 600 .env
mkdir -p runtime/server
sudo chown 10001:10001 runtime/server
```

在 `.env` 写入 `PM_GIT_REVISION` 的完整 SHA、随机 UI 密码（至少16字符）、允许的域名。账号固定 `pm`。密码可用 `openssl rand -hex 24` 本地生成；只保存于该文件。不要把真实密码放进聊天、命令参数、Git 或一般文档。当前服务器已有本地 `compose.override.yaml`，它是 HTTPS 控制请求正常工作的组成部分；下面的 Compose 命令均显式包含它。新服务器须先根据自己的反代配置创建并验证覆盖文件；若没有反代，则只使用基础 `compose.yaml`，并相应修改开机单元。基础配置的 `PM_TRUST_HTTPS_PROXY` 默认为 `false`；**只有**应用端口限于回环地址、Nginx 覆写 `X-Forwarded-Proto`、HTTPS 已验证后，才在 `.env` 设为 `true`。**若要从 GitHub 的公开规则文件恢复首次实盘，不要先运行下面的 `up -d`；须在首次启动前离线初始化并导入两个空白账本。**

```sh
docker compose --env-file .env -f compose.yaml -f compose.override.yaml config --quiet
docker compose --env-file .env -f compose.yaml -f compose.override.yaml build
docker compose --env-file .env -f compose.yaml -f compose.override.yaml up -d
docker compose --env-file .env -f compose.yaml -f compose.override.yaml ps
curl -fsS http://127.0.0.1:8765/api/health
```

**首次安装且要采用 GitHub 公开规则时**，在 `build` 与第一次 `up -d` 之间运行以下三条命令。它们只接受全新、未使用的 TEST/LIVE 账本；脚本已随镜像包含，并将两套规则同时写入后保持 PAUSED。若任何数据库已经存在，先查清来源，不能为了让命令通过而删除真实账本。没有 `compose.override.yaml` 的新机器只删除以下命令中的对应 `-f` 片段。

```sh
test ! -e runtime/server/TEST/state.sqlite && test ! -e runtime/server/LIVE/state.sqlite
docker compose --env-file .env -f compose.yaml -f compose.override.yaml run --rm --no-deps --entrypoint /app/.venv/bin/python app -c 'from pathlib import Path; from pm_nautilus.store import Store; root=Path("/data"); [Store(root/m/"state.sqlite",m).close() for m in ("TEST","LIVE")]'
docker compose --env-file .env -f compose.yaml -f compose.override.yaml run --rm --no-deps --entrypoint /app/.venv/bin/python app /app/scripts/apply_preferences.py --data-dir /data --profile /app/config/strategy-profile.json
```

健康结果需包含 TEST、PAUSED、`liveExecutionEnabled=false` 和对应代码 SHA。默认仅绑定服务器回环地址。最小访问方式：在自己的 Mac 上运行 `ssh -L 8765:127.0.0.1:8765 <用户>@<服务器>`，然后访问本机端口并以 `pm` 登录。

**全新服务器没有本地 `compose.override.yaml` 时**，上面命令去掉 `-f compose.override.yaml`，从 `docker compose --env-file .env -f compose.yaml config --quiet`、`build`、`up -d`、`ps` 依次执行；若使用公开规则文件，就在 `build` 与首次 `up -d` 之间按新手指南导入。应用启动后的公开扫描会写入数据，严格的全新账本导入工具会拒绝再次导入。下文所有带该覆盖文件的命令，在新机器没有该文件时同样删去这个 `-f` 参数。

`deploy/nginx.conf.example` 提供可审查的独立虚拟主机样例；替换实际域名和证书路径后先运行 `nginx -t`。如使用公网域名，沿用服务器现有 HTTPS 反代，在新独立配置中转发到 `127.0.0.1:8765`，把域名加入 `PM_ALLOWED_HOSTS`。保留原 Host/Origin，使用 HTTPS。不要直接公开容器端口，不要把 Basic 密码通过明文公网 HTTP 发送。切换任何已有公网入口前必须保存旧配置，明确旧服务、新服务端口和回滚动作。

备份含凭据的文件时，将 `umask 077` 限定在备份子进程内，备份保持0600；Git检出和构建使用正常源码权限（目录0755、公开源码0644）。不要让备份的严格umask影响随后检出的源码，否则Docker中的非root用户可能无法读取新文件。遇到此类错误仅修复具体公开源码文件，不对`.env`、备份或整个部署目录递归放宽权限。构建成功后仍须确认容器healthy及认证后的账本校验通过。

## 4. 部署验收与同库重启

1. `git rev-parse HEAD` 与 GitHub main 的目标 SHA 相同；健康接口 revision 相同。
2. `docker compose --env-file .env -f compose.yaml -f compose.override.yaml ps` 健康；网页设置、五项资金摘要、TEST/LIVE 切换、记录可读取。
3. 启动后 PAUSED；浏览器切至 LIVE 不能启动未启用的实盘，不能填写虚拟实盘资金。
4. 查看数据卷存在 `TEST/state.sqlite`、`LIVE/state.sqlite`，模式互不共用。
5. 在 PAUSED 状态记下配置与资金，用 `docker compose --env-file .env -f compose.yaml -f compose.override.yaml restart` 重启，确认同库配置/资金一致、仍为 PAUSED；网页钱包版本的 LIVE 会锁定，须在网页解锁重查。只有旧式 LIVE 覆盖部署才需额外加入 `-f compose.live.yaml`。
6. 已有仓位时需验证目标、止损、应收与交易记录都恢复，先在开发 TEST 目录验证，不借此擅自启动正式长期活动。

控制 API 需要登录和 `x-pm-csrf`，令牌来自已认证 GET 响应头，浏览器自动处理。不要把未认证 POST 能否成功当作健康检查。正式长期 TEST 应在整体验收后由用户点击 START；PAUSE 只停止新买，保留已有仓位退出和回款。

### 4.1 可选的强制只读 SSH 巡检

`deploy/pm-nautilus-monitor` 是服务器侧固定巡检脚本。将它以 root 所有、`0755` 安装到 `/usr/local/sbin/pm-nautilus-monitor` 后，可为一把独立公钥配置如下 `authorized_keys` 前缀：

```text
restrict,command="/usr/local/sbin/pm-nautilus-monitor"
```

该公钥必须与普通运维密钥分开，私钥不得提交 Git。`restrict` 禁止 PTY、端口/Agent/X11 转发，强制命令忽略调用方传入的 SSH 命令，只返回资源、服务、证书、容器、健康、脱敏面板、账本验证、SQLite quick-check 和近期错误快照。脚本需要目标用户具备其中固定只读命令的免密 sudo；安装后必须实测传入任意命令仍只返回巡检快照。它不能用于部署、重启、修改配置或紧急修复；这些操作仍走单独授权的管理入口。

## 5. 备份、升级、回滚

本节适用于保留旧账本、升级或回滚。若明确放弃旧 TEST 模拟历史，且本程序从未有真实 LIVE 交易，新服务器可建立空白账本；旧 TEST 密文快照不构成首次启用 LIVE 的必备条件。**空白账本不会自动恢复旧服务器上调过的策略设置**，须核对并应用仓库的 `config/strategy-profile.json`，在 LIVE START 前分别读回 TEST 和 LIVE 的设置。该公开文件不会随旧服务器后续改动自动更新。曾经使用过的钱包仍可能有本程序外的真实挂单/持仓：私有预检查原有挂单，原有持仓须另行只读核对。

停机备份可确保两个模式与 SQLite WAL 一致；LIVE 已启用时先 PAUSE 并确认所有在途买单终态，持仓退出中应选择维护时机。**网页钱包版本始终使用基础 Compose 与现有 HTTPS 覆盖文件**；只有仍在使用旧式 `compose.live.yaml` 的历史部署才需在备份与恢复时加入第三份文件。维护期间保持同一组合；`start` 只重启原容器，不会应用新配置或切换模式。

```bash
cd /opt/pm-nautilus
(
  set -euo pipefail
  umask 077
  compose_args=(--env-file .env -f compose.yaml -f compose.override.yaml)
  # 如果当前正在运行 LIVE，在执行 stop 前加入：compose_args+=(-f compose.live.yaml)
  docker compose "${compose_args[@]}" stop
  mkdir -p backups
  stage=$(mktemp -d backups/.state.XXXXXXXX)
  trap 'rm -rf -- "$stage"' EXIT
  files=(runtime/server .env compose.override.yaml)
  if test -f compose.live.yaml; then files+=(compose.live.yaml); fi
  tar -czf - "${files[@]}" | age -p -o "$stage/state.tgz.age"
  age -d -o - "$stage/state.tgz.age" | tar -tzf - >/dev/null
  final="backups/state-$(date +%Y%m%d-%H%M%S).tgz.age"
  ln "$stage/state.tgz.age" "$final"
  git rev-parse HEAD > backups/previous-revision.txt
  docker compose "${compose_args[@]}" start
)
```

备份会让 age 再次询问口令，以解密到管道并用 `tar -t` 验证压缩包；全部成功才将临时密文原子链接到最终文件并重启。若任一步失败，命令立即中止，不能继续升级或恢复旧快照；原服务保持停止，排查后用维护前相同的 Compose 文件组合显式恢复。网页钱包版本的加密文件位于 `runtime/server/wallet/live.vault`，已随 `runtime/server` 一起备份；恢复后仍须在网页解锁并核对。只有旧式 LIVE 覆盖部署才需检查 `/run/pm-nautilus/live.json` 并继续使用第三份 Compose 文件。

也可用 `age -r <接收公钥>` 加密备份，并把对应的 age identity 私钥单独保存在可信电脑上；服务器只需要公钥。先在持有 identity 的可信终端用 `age -d -i <identity路径> -o - <快照> | tar -tzf -` 验证密文与归档，再把解密后的内容通过受认证的传输方式恢复到已停止的目标服务器。identity、密文和服务器不能只保留在同一处；换电脑前须安全转存 identity。2026-09-23 的停机快照采用此方式，具体路径与状态记在根目录 `HANDOFF.md`，不上传 GitHub。

网页钱包版本的 `runtime/server/wallet/live.vault` 应与 LIVE 账本保持同一份备份；若是旧式手工加密凭据部署，另须备份 `/etc/pm-nautilus/live.json.age`，且不得复制 `/run/pm-nautilus/live.json` 明文。账本快照若用 age 口令模式，须保存对应口令；若用 recipient 公钥模式，须保存对应 identity 私钥。备份及解锁文件不能上传 Git。备份时不要把运行中的单个 `.sqlite` 拷贝而遗漏 WAL。LIVE 账本可能含待广播的已签名交易原文，仍按敏感数据处理。

新服务器恢复时，先确保旧实例已停止；克隆并固定目标代码提交，然后在**停止应用**的目录中把最新加密快照通过管道解密并解包，不写出明文压缩包：

```bash
cd /opt/pm-nautilus
(
  set -euo pipefail
  age -d -o - /受保护路径/state-最新.tgz.age | tar -xz
)
chmod 600 .env compose.override.yaml
if test -f compose.live.yaml; then chmod 600 compose.live.yaml; fi
sudo chown -R 10001:10001 runtime/server
```

恢复后先做 SQLite 与应用账本只读校验，核对所有真实在途订单和链上状态；网页钱包版本的加密钱包随数据目录一同恢复，用户再在网页人工解锁。确认旧服务器不会再次运行同一钱包后才能启用新服务器 LIVE。不能把旧 LIVE 快照直接当作最新交易事实。

升级：记录当前 SHA和镜像标签 → PAUSE → 停服务并备份 → 拉取已验证提交 → 更新 `.env` SHA → build/up → 检查健康与同库资金。回滚：停新版本 → checkout 上一个 SHA → `.env` 改回对应镜像标签 → 用该版本启动。数据库格式变更必须先验证兼容；不能拿 TEST reset 解决升级问题。真实钱包发生交易后，严禁恢复旧 LIVE 账本直接启动：必须把旧快照与最新真实回报核对，避免遗失已发生的权利或重复赎回。

2026-10-05 的四项维护修复部署时，额外核对：健康接口返回 `ok`、原 TEST 设置和账本代次不变、`LIVE` 仍未启用、`runtime/server` 中旧数据库在新镜像启动后通过 SQLite `quick_check` 与应用账本验证。新版本会在已有 `intents` 表上自动增加活动订单部分索引，不删除历史订单。LIVE 曾出现结果不明的在途订单时，资金和 Event 占用须保留至对账确认；不可通过重启、恢复旧快照或手工清空记录释放。部署结果及实际 SHA 以 [HANDOFF.md](../HANDOFF.md) 最新章节为准。

## 6. TEST 操作

- 配置通过 UI 保存；新买使用新配置。已有周期的预算、止损参数冻结，已有目标不追溯改变。
- 初始资金只能在 PAUSED、无交易历史时改。盈利不会自动扩大每轮金额。
- 重置需 PAUSED 下点击两次确认；只清 TEST 数据并恢复100U/1U等原始默认值，LIVE不受影响。
- 长时间无成交先看公开扫描、盘口与筛选条件，不能用强制下单验证网络。无买盘估值0，未知盘口估值显示未知。
- 小于交易所最小卖出量的残余仓位不能伪造卖出；保留并等待可合法退出或正式结算。

## 7. 网页 LIVE 钱包与实盘启用

新版本的日常操作入口是同一个 HTTPS 网页。默认 Compose 仍写着 `PM_LIVE_ENABLED=false`；这表示应用开机后不自动连接真实钱包。用户切到 LIVE 页面只是查看，不会下单。**无需运行 `compose.live.yaml`、`deploy/live-secrets.py` 或进入服务器解锁。**这些旧文件仅供尚未迁移的旧式部署参考，不能与网页钱包方式同时启用。

安装者先按前文部署基础 Compose、Nginx HTTPS、至少 16 字符的 UI 登录密码，并保留服务器本地 `compose.override.yaml`。Nginx 必须覆写 `X-Forwarded-Proto`，应用容器端口仅绑定宿主机回环；仅此可信路径在服务器 `.env` 设置 `PM_TRUST_HTTPS_PROXY=true`。公网钱包操作在后端也强制要求 HTTPS。加密备份恢复可能超过 Nginx 默认的 1 MiB 上传限制，反代应按 `deploy/nginx.conf.example` **只对** `/api/live/wallet/restore` 放宽至 180 MiB 并允许较长上传时间。初次服务器安装、证书和 Docker 仍需管理员完成一次；此后用户在网页完成日常钱包操作。

程序目前只支持 Polygon 主网普通钱包（签名地址和资金地址相同）或单签 Safe（签名地址是唯一 owner，阈值 1）。网页会根据私钥或 **12 词 BIP39 助记词**派生签名地址，要求填 Polymarket 显示的公开资金地址，并只读核对两者的归属。它会调用固定版本 SDK 创建或派生 CLOB API 凭据；这一步有账户认证签名，但不签订单或链上交易。其他钱包类型会被拒绝，不能通过修改地址或签名类型强行接入。

### 7.1 用户在网页上的顺序

1. 登录 HTTPS 网页，切到 **LIVE**。先查看并保存 LIVE 规则；TEST 规则独立，切换视图不改变运行模式。仍使用现有策略的每 Event 每轮金额、筛选和止损规则，不加新的投入或笔数限制。
2. 选择 12 个助记词或私钥，填写公开的 Polymarket 资金地址，设置至少 12 字符的解锁密码。助记词/私钥经 HTTPS 到达服务器；助记词不写盘。派生的签名密钥和 API 凭据存入 `/data/wallet/live.vault`，文件 `0600`、目录 `0700`，服务器只保存加密文件。**服务器解锁后内存中持有签名密钥**，这是一台服务器独立运行所必需的。秘密不要发到聊天、GitHub、URL 或截图。
3. 网页进行只读检查：钱包类型及所有权、Polygon 网络、CLOB 余额和授权、手续费余额、现有挂单、钱包旧持仓与 LIVE 账本归属，以及从**服务器交易出口**查询 Polymarket 地区接口。未通过时停在检查页面，不启用 LIVE。已有手动持仓/挂单不会自动算作程序仓位。余额或授权缺失须按钱包/Polymarket 的正常操作补齐，再在网页重查；程序不会悄悄批准或转入资金。
4. 检查全通过后，核对页面的签名和资金地址、确认当前 LIVE 规则，点击“启用实盘”。程序先接通并核对账户，状态仍为 **LIVE / PAUSED**。随后只有用户点击右上角 **▶** 才会开始新买。PAUSE 停止新买，但已有仓位的卖出、止损和赎回仍可能继续。

主机或应用重启后，默认只运行 TEST；LIVE 页显示 **LOCKED**。用户在网页输入解锁密码、重新检查并启用，再自行决定是否点 ▶。锁定期间程序不维护已有 LIVE 仓位，网页不得把不可见持仓显示成已清仓。解锁密码丢失时不能靠 GitHub 找回钱包；请保管原钱包恢复方式和网页加密备份。

美国来源的 API 新开仓受 [Polymarket 官方地区规则](https://docs.polymarket.com/api-reference/geoblock)限制。当前硅谷服务器应在地区核对中阻止启用；换服务器后必须从新服务器自身网络出口重新检查，不能沿用旧服务器或自己电脑的结果。

### 7.2 网页加密备份与换服务器

LIVE 页面“换服务器时备份或恢复”可下载单个 `.pmnb` 加密包，包含**同一时点**的加密钱包和 LIVE SQLite 账本。下载时再次输入解锁密码；备份包用该密码单独加密。把文件与密码分开保管，不上传 GitHub。它不包含 TEST 模拟历史、服务器登录密码、证书或 Nginx 设置；公开策略文件也不会自动记录网页后续调整。较大的 LIVE 账本若超过网页备份上限，应由管理员按第 5 节做一致性停机备份。

迁移时先在旧服务器暂停新买、确认在途订单，下载最新备份，随后停止旧服务器的应用；不要让新旧服务器同时管理同一个钱包。管理员在新服务器安装相同或兼容的已验证版本，配置 HTTPS/登录并保持空白 LIVE 账本。用户进入新网页 LIVE 页，上传 `.pmnb`、输入解锁密码恢复；恢复后状态 **LOCKED**，还需解锁、从新服务器出口核对账户，并手动启用/START。若备份以后旧服务器又有订单、卖出或赎回，新服务器的实际份额核对会阻止直接启用；先处理账本差异，不能覆盖最新真实记录。**GitHub 本身没有真实账本或钱包。**

网页恢复只接受空白 LIVE 账本，拒绝覆盖已有钱包或交易历史。服务器升级和回滚继续遵守第 5 节的停机备份；不能用旧备份抹掉未知结果的真实订单。`deploy/live-secrets.py`、`compose.live.yaml.example` 属于旧式手工接入路径，已配置该路径的部署需先由管理员审查迁移，不得把旧 `/run` 明文和新网页钱包同时提供给一个应用。

### 7.3 开机与版本更新

`deploy/pm-nautilus-test-boot.service.example` 仍可作为开机 TEST 单元；它重建基础 Compose，应用不会自动启用 LIVE。升级时先记录版本与资金、暂停 TEST/LIVE 所需动作、停服务并按第 5 节备份，再拉取固定 SHA、构建和启动基础 Compose。确认 TEST 账本、网页、健康和 LIVE 锁定状态后，由用户在网页解锁与重新核对；升级本身不能自动按 ▶。

## 8. 故障排查

| 现象 | 检查和恢复 |
|---|---|
| 401 | 使用账号pm和服务器UI密码；不要关闭认证绕过 |
| 403 控制来源失败 | 刷新当前同源页面，检查反代Host/Origin；不要去掉CSRF |
| 无可执行盘口 | 检查公网出站、WS连接、扫描错误；重连需完整快照 |
| `streamError=ConnectionClosedError` | 公共行情常规断线会使盘口失效并自动退避重连；不应单独导致 TEST 暂停。若同时存在 `serviceError`，保持 PAUSED 并按持久化的错误类型、详情和时间排查后再显式 START |
| 有行情无成交 | 检查总时长、进度、费用元数据、最小量、Bid/Ask比例、兄弟完整性与预算 |
| 业务投影失败 | 保持PAUSED，检查磁盘/SQLite错误，备份后重启重放；不要手改cursor |
| LIVE核对失败 | 保持PAUSED，检查真实订单/余额/自有份额；临时网络恢复后重新核对，就绪恢复不自动START |
| 赎回SUBMITTED很久 | 查询保存的tx_hash和nonce、RPC与POL，不删除跟踪或手动重复提交 |
| 赎回FAILED | 核对失败收据/余额/混合权利；不要自动重置成未提交 |
| 重复实例错误 | 找到占用同目录的进程，保留唯一实例；不要绕过process.lock |

清理本次 Linux 验证虚拟机：工作区 `.reference/lima/bin/limactl`，实例 `pm-verify`，`LIMA_HOME` 为工作区 `.reference/lima-home`。验证结束停机；保留文件便于复验，不作为生产服务使用。

现有服务器的所有 `config`、`build`、`up`、`ps`、`stop` 命令都须显式包含本地 `compose.override.yaml`。网页 LIVE 钱包沿用基础 Compose，不加入 `compose.live.yaml`；只有尚未迁移的旧式部署才用第三份文件。不要在服务器上用省略 override 的命令覆盖当前 HTTPS 反代信任设置。
