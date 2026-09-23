# PM-NAUTILUS 开发、发布与运维

默认交付：TEST + PAUSED，LIVE 未启用。下列部署步骤不启动正式长期 TEST。代码、TEST 数据、LIVE 数据各自独立；不得复制 PM-SMALL 的数据库、钱包会话或历史运行目录。

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
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
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

在 `.env` 写入 `PM_GIT_REVISION` 的完整 SHA、随机 UI 密码（至少16字符）、允许的域名。账号固定 `pm`。密码可用 `openssl rand -hex 24` 本地生成；只保存于该文件。不要把真实密码放进聊天、命令参数、Git 或一般文档。当前服务器已有本地 `compose.override.yaml`，它是 HTTPS 控制请求正常工作的组成部分；下面的 Compose 命令均显式包含它。新服务器须先根据自己的反代配置创建并验证覆盖文件；若没有反代，则只使用基础 `compose.yaml`，并相应修改开机单元，不能照搬旧服务器的代理信任设置。

```sh
docker compose --env-file .env -f compose.yaml -f compose.override.yaml config --quiet
docker compose --env-file .env -f compose.yaml -f compose.override.yaml build
docker compose --env-file .env -f compose.yaml -f compose.override.yaml up -d
docker compose --env-file .env -f compose.yaml -f compose.override.yaml ps
curl -fsS http://127.0.0.1:8765/api/health
```

健康结果需包含 TEST、PAUSED、`liveExecutionEnabled=false` 和对应代码 SHA。默认仅绑定服务器回环地址。最小访问方式：在自己的 Mac 上运行 `ssh -L 8765:127.0.0.1:8765 <用户>@<服务器>`，然后访问本机端口并以 `pm` 登录。

`deploy/nginx.conf.example` 提供可审查的独立虚拟主机样例；替换实际域名和证书路径后先运行 `nginx -t`。如使用公网域名，沿用服务器现有 HTTPS 反代，在新独立配置中转发到 `127.0.0.1:8765`，把域名加入 `PM_ALLOWED_HOSTS`。保留原 Host/Origin，使用 HTTPS。不要直接公开容器端口，不要把 Basic 密码通过明文公网 HTTP 发送。切换任何已有公网入口前必须保存旧配置，明确旧服务、新服务端口和回滚动作。

备份含凭据的文件时，将 `umask 077` 限定在备份子进程内，备份保持0600；Git检出和构建使用正常源码权限（目录0755、公开源码0644）。不要让备份的严格umask影响随后检出的源码，否则Docker中的非root用户可能无法读取新文件。遇到此类错误仅修复具体公开源码文件，不对`.env`、备份或整个部署目录递归放宽权限。构建成功后仍须确认容器healthy及认证后的账本校验通过。

## 4. 部署验收与同库重启

1. `git rev-parse HEAD` 与 GitHub main 的目标 SHA 相同；健康接口 revision 相同。
2. `docker compose --env-file .env -f compose.yaml -f compose.override.yaml ps` 健康；网页设置、五项资金摘要、TEST/LIVE 切换、记录可读取。
3. 启动后 PAUSED；浏览器切至 LIVE 不能启动未启用的实盘，不能填写虚拟实盘资金。
4. 查看数据卷存在 `TEST/state.sqlite`、`LIVE/state.sqlite`，模式互不共用。
5. 在 PAUSED 状态记下配置与资金，用 `docker compose --env-file .env -f compose.yaml -f compose.override.yaml restart` 重启；若已启用 LIVE，再加入 `-f compose.live.yaml`，确认同库配置/资金一致、仍为 PAUSED。
6. 已有仓位时需验证目标、止损、应收与交易记录都恢复，先在开发 TEST 目录验证，不借此擅自启动正式长期活动。

控制 API 需要登录和 `x-pm-csrf`，令牌来自已认证 GET 响应头，浏览器自动处理。不要把未认证 POST 能否成功当作健康检查。正式长期 TEST 应在整体验收后由用户点击 START；PAUSE 只停止新买，保留已有仓位退出和回款。

### 4.1 可选的强制只读 SSH 巡检

`deploy/pm-nautilus-monitor` 是服务器侧固定巡检脚本。将它以 root 所有、`0755` 安装到 `/usr/local/sbin/pm-nautilus-monitor` 后，可为一把独立公钥配置如下 `authorized_keys` 前缀：

```text
restrict,command="/usr/local/sbin/pm-nautilus-monitor"
```

该公钥必须与普通运维密钥分开，私钥不得提交 Git。`restrict` 禁止 PTY、端口/Agent/X11 转发，强制命令忽略调用方传入的 SSH 命令，只返回资源、服务、证书、容器、健康、脱敏面板、账本验证、SQLite quick-check 和近期错误快照。脚本需要目标用户具备其中固定只读命令的免密 sudo；安装后必须实测传入任意命令仍只返回巡检快照。它不能用于部署、重启、修改配置或紧急修复；这些操作仍走单独授权的管理入口。

## 5. 备份、升级、回滚

停机备份可确保两个模式与 SQLite WAL 一致；LIVE 已启用时先 PAUSE 并确认所有在途买单终态，持仓退出中应选择维护时机。备份前先按**当前实际模式**选择文件组合：TEST 使用基础两份，LIVE 额外加入 `compose.live.yaml`。维护期间保持同一组合；`start` 只重启原容器，不会应用新配置或切换模式。

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

备份会让 age 再次询问口令，以解密到管道并用 `tar -t` 验证压缩包；全部成功才将临时密文原子链接到最终文件并重启。若任一步失败，命令立即中止，不能继续升级或恢复旧快照；原服务保持停止，排查后用维护前相同的 Compose 文件组合显式恢复。若维护前运行 LIVE，恢复前检查 `/run/pm-nautilus/live.json` 仍存在，否则先人工解锁；`compose_args` 必须在 `stop` 前加入第三份文件，并沿用到 `start`。若想从 LIVE 切为 TEST，须按 7.3 停止并以基础两份文件执行 `up -d --force-recreate`，不能用 `start` 切换模式。

如已有 LIVE 加密凭据，再单独备份 `/etc/pm-nautilus/live.json.age` 到受限的离线位置；加密密文、age 口令和账本备份不要放在同一个可直接访问的位置。不要复制 `/run/pm-nautilus/live.json` 明文。上述账本快照本身也要保存其 age 口令。备份及解锁文件不能上传 Git。备份时不要把运行中的单个 `.sqlite` 拷贝而遗漏 WAL。LIVE 账本可能含待广播的已签名交易原文，仍按敏感数据处理。

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

恢复后先做 SQLite 与应用账本只读校验，核对所有真实在途订单和链上状态，再从单独备份安装加密凭据并人工解锁。确认旧服务器不会再次运行同一钱包后才能启动新服务器 LIVE。不能把旧 LIVE 快照直接当作最新交易事实。

升级：记录当前 SHA和镜像标签 → PAUSE → 停服务并备份 → 拉取已验证提交 → 更新 `.env` SHA → build/up → 检查健康与同库资金。回滚：停新版本 → checkout 上一个 SHA → `.env` 改回对应镜像标签 → 用该版本启动。数据库格式变更必须先验证兼容；不能拿 TEST reset 解决升级问题。真实钱包发生交易后，严禁恢复旧 LIVE 账本直接启动：必须把旧快照与最新真实回报核对，避免遗失已发生的权利或重复赎回。

## 6. TEST 操作

- 配置通过 UI 保存；新买使用新配置。已有周期的预算、止损参数冻结，已有目标不追溯改变。
- 初始资金只能在 PAUSED、无交易历史时改。盈利不会自动扩大每轮金额。
- 重置需 PAUSED 下点击两次确认；只清 TEST 数据并恢复100U/1U等原始默认值，LIVE不受影响。
- 长时间无成交先看公开扫描、盘口与筛选条件，不能用强制下单验证网络。无买盘估值0，未知盘口估值显示未知。
- 小于交易所最小卖出量的残余仓位不能伪造卖出；保留并等待可合法退出或正式结算。

## 7. 后续 LIVE 配置与启用

仓库默认不读取真实钱包、不签署真实订单，也不进行 approve、redeem 或其他链上写。只有完成 TEST 验收、确认实际钱包类型与独立资金账户，并获得明确 LIVE 启用授权后，才进入本节流程。

实现支持 Polygon 主网 EOA（signature_type=0，signer=funder）和单签 Safe（signature_type=2，signer 是 owner、threshold=1）。Magic/PolyProxy、Deposit Wallet、多签 Safe 不能冒充这两种路径；若实际账户属于其他类型，须按其官方路径补齐并验证后再启用。仓库不记录实际钱包类型或凭据，当前外部准备状态以 HANDOFF.md 为准。

LIVE 凭据不经过网页、聊天、Git、命令参数或 `.env`。推荐在可信的本地电脑运行 `deploy/live-secrets.py create`：它逐项隐藏输入，在内存中组装下列 JSON，调用 [age 的口令模式](https://github.com/FiloSottile/age/blob/main/doc/age.1.html)加密后才写文件，只把密文传到服务器。age 口令由 age 直接从终端读取，应单独保存在自己的密码管理器中。仓库只保存脚本，不保存加密文件或口令。字段结构为：

```json
{
  "private_key": "创建时交互式输入",
  "api_key": "创建时交互式输入",
  "api_secret": "创建时交互式输入",
  "passphrase": "创建时交互式输入",
  "funder": "实际资金账户地址",
  "signature_type": 2,
  "rpc_url": "实际Polygon RPC",
  "auto_approve_redemption": false
}
```

CLOB V2 客户端、当前 pUSD、标准/neg-risk 抵押适配器地址固定在所选实现。EOA 需备 POL 支付手续费；Safe 由受支持 owner 支付外层交易手续费。启动前只读核对链ID、合约代码、资金账户与 Safe 权限。CLOB 下单授权需要账户预先完成；赎回授权未满足时保留应收并显示原因。创建脚本固定 `auto_approve_redemption=false`；如以后需要自动授权，须另行审查并修改加密配置。服务器进程运行期间仍可在内存和 `/run` 读取私钥，root/Docker 管理员也能读取；加密存储主要保护关机后的磁盘和备份。

### 7.1 创建、解锁并接入本地签名

先在可信本地电脑安装 age 和 Python 3.9+，核对脚本来自目标提交，再创建私有目录并交互式输入凭据。创建操作只用于新凭据；若加密文件已存在，脚本会拒绝覆盖。不要在聊天、shell 命令或文本文件中粘贴私钥。

```sh
age --version
mkdir -m 700 ~/pm-nautilus-private
python3 deploy/live-secrets.py create --vault ~/pm-nautilus-private/live.json.age
scp ~/pm-nautilus-private/live.json.age <管理员>@<服务器>:~/live.json.age
```

服务器安装 age 后，把收到的密文安装为 root 所有的 `0600` 文件。也可以直接在服务器运行 `sudo python3 deploy/live-secrets.py create`，但本地创建只传输密文，更符合私钥不离开本地电脑的要求。

```sh
sudo apt-get install age
sudo install -d -o root -g root -m 0700 /etc/pm-nautilus
sudo install -o root -g root -m 0600 ~/live.json.age /etc/pm-nautilus/live.json.age
rm ~/live.json.age
sudo stat -c '%U %a %n' /etc/pm-nautilus/live.json.age
```

在每次主机重启后人工解锁。脚本先确认 `/run` 是 tmpfs，校验加密文件属主与权限，再通过 age 交互式询问口令、校验 JSON，并原子创建 `/run/pm-nautilus/live.json`（UID 10001、`0600`）。失败时不会留下新的明文文件。不要通过 shell 重定向、环境变量或命令参数传递私钥/口令。

```sh
sudo python3 deploy/live-secrets.py unlock
sudo stat -c '%u %a %n' /run/pm-nautilus/live.json
cp deploy/compose.live.yaml.example compose.live.yaml
docker compose --env-file .env -f compose.yaml -f compose.override.yaml -f compose.live.yaml config --quiet
docker compose --env-file .env -f compose.yaml -f compose.override.yaml -f compose.live.yaml run --rm --no-deps --entrypoint /app/.venv/bin/python app -m pm_nautilus.private_preflight --expected-signer 0x你的公开签名地址 --expected-funder 0x你的Safe资金地址 --data-dir /data
```

这条私有预检在一次性容器中只读检查凭据对应地址、链上余额与授权、CLOB 账户和已有挂单、本程序 LIVE 账本；不下单、不取消旧挂单。运行前须确认应用镜像已从包含 `private_preflight` 的目标提交构建。退出码 `0` 表示可进入新买验证，`2` 表示认证成功但资金、授权或未知挂单仍阻止新买，`1` 表示检查失败。旧手动挂单需本人处理，程序不会自动取消。真实凭据导入前不能运行此命令。只有预检返回 `0`，且首次启用前已完成下文 7.2 的开机 TEST 单元，再启动服务并核对 LIVE：

```sh
docker compose --env-file .env -f compose.yaml -f compose.override.yaml -f compose.live.yaml up -d --force-recreate
```

`compose.live.yaml` 只读挂载 `/run` 中的明文文件，缺失时直接失败，不会自动创建目录。基础 Compose 始终保持 `PM_LIVE_ENABLED=false`；只有显式加入 LIVE 覆盖文件才连接钱包。单机 Compose 的文件挂载并不提供加密存储或 UID 重映射，故这里由脚本在主机上设置属主和权限。[Docker Compose 文件 secret 说明](https://docs.docker.com/compose/how-tos/use-secrets/) · [文件挂载权限说明](https://docs.docker.com/reference/compose-file/services/)

启动 LIVE 上下文仅连接和核对；状态仍 PAUSED。TEST 和 LIVE 设置存于两套账本，不会自动继承。首次启动后，在已认证的 UI 分别切到 TEST/LIVE 读取完整设置，或在同一已认证浏览器中只读打开 `/api/TEST/preferences` 和 `/api/LIVE/preferences`，对照各自 `preferences` 的类别、筛选、每 Event 每轮金额、目标与止损。随后在 LIVE 保存确认的参数，切回 TEST 和 LIVE 各复查一次。`orderAmount` 是现有的每 Event 每轮预算；不另加首轮总投入、单笔金额或最多笔数限制。用户切换 LIVE 视图并按 START 才允许真实新买。未完成核对、未知订单、资金/份额不一致均阻止新买。PAUSE 后目标退出、止损和赎回继续；不得通过关掉 LIVE 门来代替安全暂停现有仓位。

### 7.2 主机重启后默认回到 TEST

在首次启用 LIVE 前，将 `deploy/pm-nautilus-test-boot.service.example` 的 `WorkingDirectory` 与 Docker 路径改为实际服务器值，确认本地 `compose.override.yaml` 存在，再安装开机单元：

```sh
sudo install -m 0644 deploy/pm-nautilus-test-boot.service.example /etc/systemd/system/pm-nautilus-test-boot.service
sudo systemctl daemon-reload
sudo systemctl enable pm-nautilus-test-boot.service
```

开机时 `/run` 明文已消失，旧 LIVE 容器因挂载源缺失不能运行；该单元用 `compose.yaml + compose.override.yaml` 重建 TEST。**不要在已有 LIVE 仓位时把 TEST 运行误认为仓位仍被管理**：主机重启后须人工解锁，并再次使用三份 Compose 文件重建 LIVE，核对账户、订单和账本；重启后策略仍为 PAUSED。

### 7.3 手动结束 LIVE 并重新锁定

先核对所有在途订单和仓位退出安排，再停 LIVE、用基础两份 Compose 文件重建 TEST；确认旧 LIVE 容器已结束且健康接口 `liveExecutionEnabled=false`，最后才删除 `/run` 明文文件：

```sh
docker compose --env-file .env -f compose.yaml -f compose.override.yaml -f compose.live.yaml stop app
docker compose --env-file .env -f compose.yaml -f compose.override.yaml up -d --force-recreate
docker compose --env-file .env -f compose.yaml -f compose.override.yaml ps
curl -fsS http://127.0.0.1:8765/api/health
sudo rm -- /run/pm-nautilus/live.json
sudo test ! -e /run/pm-nautilus/live.json
```

删除宿主机路径本身不会使**仍在运行**的 LIVE 容器、其 bind mount 或进程内存失去私钥，因此不能把单独执行 `rm` 当成锁定。重新启用 LIVE 须再次人工解锁并完成私有预检。TEST 运行时不会管理既有 LIVE 仓位。

自动赎回分为 IDENTIFIED → SUBMITTED → CONFIRMED → CREDITED；TEST先模拟确认，再模拟入账。LIVE提交前保存交易哈希、nonce与同一签名原文；重启仅查询或重发同一交易，不创建新身份。链上收据需成功且至少32块确认，核对pUSD回款与自有权利后刷新实际账户余额。应收不占可下单现金。同 Condition 若存在额外手动资产，整Condition赎回会影响它们，因此自动赎回拒绝该混合权利，等待人工核对，不接管手动资产。

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

未来已明确授权 LIVE 后，所有 `config`、`build`、`up`、`ps`、`stop` 命令都须显式包含现有的 `compose.override.yaml`；LIVE 时再加入第三份 `compose.live.yaml`。不要在服务器上用省略 override 的命令覆盖当前 HTTPS 反代信任设置。
