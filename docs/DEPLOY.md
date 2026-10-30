# PM-NAUTILUS 开发、发布与运维

当前版本统一从 [`main`](https://github.com/yvettemiranda/PM-NAUTILUS/tree/main) 获取。十账户改造前的固定基准为 [`baseline/pre-test-10-accounts-20261027`](https://github.com/yvettemiranda/PM-NAUTILUS/tree/baseline/pre-test-10-accounts-20261027)，提交 `4de107a4dc1a4abaaf08b330eb00803391268e2a`，仅供回看和对照，不是默认安装版本。旧开发分支不再作为入口；完整提交历史仍保留。

已验收、已部署应用为 `8ab1937e84b1f63db3db46a07eca8a4d17876a58`，后续纯文档提交可使 `main` 比应用 SHA 更新；须核对差异只涉及文档，不因此重启镜像。当前验证与服务器采样见 [HANDOFF](../HANDOFF.md) 和 [VALIDATION](VALIDATION.md)。

## 当前运行与数据范围

全新数据目录建立 1—10 号独立 TEST 账户，均为 PAUSED，LIVE 未启用。TEST 与 LIVE 后台互斥：启用 LIVE 前先暂停全部 TEST，程序等待 TEST 行情、扫描、结算与采样退出后才进入 LIVE 私有激活。切换页面只切换查看对象。详见 [TEST 多账户](TEST_ACCOUNTS.md) 与 [通用优化及运行模式边界](TEST_OPTIMIZATION_20261029.md)。

运行模式意图保存在 `PM_DATA_DIR/execution-mode.json`。曾选择 LIVE 的目录重启后，钱包仍锁定、LIVE 不自动启用，TEST 后台也保持停止。确认没有 LIVE 活动且后端允许时，可明确“恢复TEST后台（保持暂停）”，之后仍需逐账户 START。新目录或 TEST 意图重启时，TEST 后台按原 PAUSE 含义维护已有退出和结算，不自动开始买入。

迁移和备份应完整包含数据根目录：`TEST/state.sqlite`、`TEST/accounts/2/state.sqlite` 至 `TEST/accounts/10/state.sqlite`、`LIVE/state.sqlite` 共 11 份账本，以及运行模式文件、钱包目录及其他持久状态。Compose 默认对应宿主机 `runtime/server/`；不要仅复制 1 号或 LIVE 数据库。停机一致性备份仍须核对 WAL、原参数、generation 与所有历史。基准版不能直接当作当前十账户数据卷的回滚方案，须先在隔离副本验证兼容性。

部署本身不启用 LIVE，不启动真实交易。后文历史发布折叠保留，操作以本页当前步骤为准。

## 1. Mac 本地开发

要求 macOS 26+ arm64（所选 Nautilus wheel 的最低要求）、Python 3.12.14。其他 Mac 版本使用本地 Linux 容器。新开发环境先按 [环境配置指南](REINSTALL.md) 下载 Python 并安装锁定依赖；在克隆目录运行：

```sh
uv run --frozen pm-nautilus --data-dir runtime/local
```

浏览器打开 http://127.0.0.1:8765 。新目录十个 TEST 均为 PAUSED，每个账户最初余额 100U、每轮 1U；切换账户只改变查看和操作对象。TEST 后台启用时，暂停账户已有持仓的退出和结算继续处理。恢复旧目录须按运行模式意图决定是否启用 TEST 后台；LIVE 意图不会因重启改回 TEST。不要让两台进程使用同一个数据根目录或 SQLite 文件。

从干净环境建立依赖：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install uv==0.8.22
.venv/bin/uv sync --frozen --extra dev
.venv/bin/pytest -q
.venv/bin/ruff check src tests scripts deploy/live-secrets.py
.venv/bin/ruff format --check src tests scripts deploy/live-secrets.py
```

`--offline --data-dir runtime/ui-preview` 只查看本地 UI；该选项不提供行情，已有仓位也无法依靠实时盘口退出，仅用于开发验证。`runtime/` 全部不入 Git。`.python-version` 和 `uv.lock` 为固定依赖依据。

## 2. GitHub 开发流程

公开仓库为 `yvettemiranda/PM-NAUTILUS`，`main` 是当前唯一开发和发布入口，固定基准分支仅供回看。新电脑直接克隆已有仓库，不重复创建仓库、不覆盖远端历史、不强推；环境配置见 [REINSTALL.md](REINSTALL.md)。每次修改在推送前先 fetch，检查本地分支与 `origin/main` 的关系并保护其他开发者的提交。

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
git clone --branch main https://github.com/yvettemiranda/PM-NAUTILUS.git /opt/pm-nautilus
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

以下工具只初始化 TEST 1 和 LIVE；应用首次启动再以 TEST 1 的初始资金及参数建立 2—10 号空白账户，已有 2—10 号不被改写。**首次安装且要采用 GitHub 公开规则时**，在 `build` 与第一次 `up -d` 之间运行以下三条命令。它们只接受全新、未使用的 TEST/LIVE 账本；脚本已随镜像包含，并将两套规则同时写入后保持 PAUSED。若任何数据库已经存在，先查清来源，不能为了让命令通过而删除真实账本。没有 `compose.override.yaml` 的新机器只删除以下命令中的对应 `-f` 片段。

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

1. 核对 GitHub 目标 SHA、服务器源码和健康接口 revision。镜像须对应固定的已验证应用提交；若 GitHub main 或服务器源码随后只有文档更新，允许 SHA 不同，但须确认应用代码、依赖、策略文件和部署配置没有差异，不能把未部署的程序修改当作已生效。
2. `docker compose --env-file .env -f compose.yaml -f compose.override.yaml ps` 健康；网页设置、五项资金摘要、TEST/LIVE 切换、记录可读取。
3. 十个 TEST 启动后均为 PAUSED；浏览器切至 LIVE 不能启动未启用的实盘，不能填写虚拟实盘资金。保存 LIVE 意图时，TEST 后台还应保持停用。
4. 查看数据卷的 `TEST/state.sqlite`、`TEST/accounts/2/state.sqlite` 至 `TEST/accounts/10/state.sqlite`、`LIVE/state.sqlite` 共 11 份独立账本，并保全 `execution-mode.json`（已经选择过模式时）、钱包和其他持久状态，不共用或遗漏账户文件。
5. 在 PAUSED 状态记下各账户配置与资金，再明确进行同库重启，确认全部参数、资金及历史保留、均未恢复 RUNNING；网页钱包版本的 LIVE 会锁定，须在网页解锁重查。若之前保存 LIVE 意图，确认 TEST 后台仍停止，明确恢复也只到 PAUSED。只有旧式 LIVE 覆盖部署才需额外加入 `-f compose.live.yaml`。
6. 已有仓位时需验证目标、止损、应收与交易记录都恢复，先在开发 TEST 目录验证，不借此擅自启动正式长期活动。资金摘要须与全部持仓/权利一致，不能只按展开的前 20 条相加；交易历史的分页、迟到更新及超过一万条后的继续读取应按实际规模核对。

控制 API 需要登录和 `x-pm-csrf`，令牌来自已认证 GET 响应头，浏览器自动处理。不要把未认证 POST 能否成功当作健康检查。正式长期 TEST 应在整体验收后由用户点击 START；PAUSE 只停止新买，保留已有仓位退出和回款。

### 4.1 可选的强制只读 SSH 巡检

`deploy/pm-nautilus-monitor` 是服务器侧固定巡检脚本。将它以 root 所有、`0755` 安装到 `/usr/local/sbin/pm-nautilus-monitor` 后，可为一把独立公钥配置如下 `authorized_keys` 前缀：

```text
restrict,command="/usr/local/sbin/pm-nautilus-monitor"
```

该公钥必须与普通运维密钥分开，私钥不得提交 Git。`restrict` 禁止 PTY、端口/Agent/X11 转发，强制命令忽略调用方传入的 SSH 命令，只返回资源、服务、证书、容器、健康、脱敏面板、账本验证、SQLite quick-check 和近期错误快照。脚本需要目标用户具备其中固定只读命令的免密 sudo；安装后必须实测传入任意命令仍只返回巡检快照。它不能用于部署、重启、修改配置或紧急修复；这些操作仍走单独授权的管理入口。

## 5. 备份、升级、回滚

本节适用于保留旧账本、升级或回滚。若明确放弃旧 TEST 模拟历史，且本程序从未有真实 LIVE 交易，新服务器可建立空白账本；旧 TEST 密文快照不构成首次启用 LIVE 的必备条件。**空白账本不会自动恢复旧服务器上调过的策略设置**，须核对并应用仓库的 `config/strategy-profile.json`，在 LIVE START 前分别读回 TEST 和 LIVE 的设置。该公开文件不会随旧服务器后续改动自动更新。曾经使用过的钱包仍可能有本程序外的真实挂单/持仓：私有预检查原有挂单，原有持仓须另行只读核对。

停机备份须覆盖全部 11 份账本、运行模式文件及 SQLite WAL；TEST 模式先逐账户暂停所有 RUNNING 账户。LIVE 已启用时先 PAUSE 并确认所有在途买单终态，持仓退出中应选择维护时机。**网页钱包版本始终使用基础 Compose 与现有 HTTPS 覆盖文件**；只有仍在使用旧式 `compose.live.yaml` 的历史部署才需在备份与恢复时加入第三份文件。维护期间保持同一组合；`start` 只重启原容器，不会应用新配置或切换模式。

```bash
cd /opt/pm-nautilus
(
  set -euo pipefail
  umask 077
  compose_args=(--env-file .env -f compose.yaml -f compose.override.yaml)
  # 只有历史旧式 LIVE 部署才加入：compose_args+=(-f compose.live.yaml)
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

也可用 `age -r <接收公钥>` 加密备份，并把对应的 age identity 私钥单独保存在可信电脑上；服务器只需要公钥。先在持有 identity 的可信终端用 `age -d -i <identity路径> -o - <快照> | tar -tzf -` 验证密文与归档，再把解密后的内容通过受认证的传输方式恢复到已停止的目标服务器。identity、密文和服务器不能只保留在同一处；换电脑前须安全转存 identity。旧 recipient 快照的具体位置及对应 identity 应由持有人在私有运维记录中确认；本仓库不能恢复该私钥，也不保证当前 HANDOFF 仍记录旧快照位置。

网页钱包版本的 `runtime/server/wallet/live.vault` 应与 LIVE 账本保持同一份备份；若是旧式手工加密凭据部署，另须备份 `/etc/pm-nautilus/live.json.age`，且不得复制 `/run/pm-nautilus/live.json` 明文。账本快照若用 age 口令模式，须保存对应口令；若用 recipient 公钥模式，须保存对应 identity 私钥。备份及解锁文件不能上传 Git。备份时不要把运行中的单个 `.sqlite` 拷贝而遗漏 WAL。LIVE 账本可能含待广播的已签名交易原文，仍按敏感数据处理。

网页 `.pmnb` 会压缩并加密同一时点的 LIVE 账本，当前接受最多 4 GiB 原始数据库、2 GiB 加密包；旧版小文件 `.pmnb` 仍可恢复。网页下载在支持的浏览器中直接写入用户选择的文件，上传则在服务器数据卷中暂存。下载前服务器需容纳快照和加密包；上传恢复时还需容纳上传包、解密内容和解压后的账本。空间不足或超过上述上限时，请由管理员停旧实例，使用本节的一致性停机备份迁移。

**有真实 LIVE 记录的迁移必须以停止旧实例后制作的最终一致性备份为准。**网页 `.pmnb` 虽是内部一致的时间点快照，但网页备份时服务仍运行；PAUSE 只停止新买，卖出和赎回仍可写入账本。先下载 `.pmnb` 再停旧服务，可能漏掉这段时间已完成的交易，甚至在没有未结仓位时被误认为迁移成功。当前版本的最终迁移应由管理员确认在途订单、停旧实例、制作并验证包含 `runtime/server` 和加密钱包的停机备份，然后在新服务器恢复并核对。**上方升级备份命令结尾的 `docker compose ... start` 在换服时不得执行；还要确认旧服的自动启动单元不会将它重新拉起。**日常网页 `.pmnb` 可另存作恢复参考，不能单独替代最终停机备份。

停旧实例后，也可用镜像内的 `pm-nautilus-final-export` 生成最终 `.pmnb`。它独占应用的 `process.lock`，若旧实例仍在运行便拒绝导出；目标文件只创建一次，不覆盖已有备份，并返回 SHA-256。示例中的文件位于持久化数据目录，导出后应连同校验值转存到受保护的位置，且保持旧实例停止，直到新服恢复和核对完成：

```bash
docker compose --env-file .env -f compose.yaml -f compose.override.yaml stop
docker compose --env-file .env -f compose.yaml -f compose.override.yaml run --rm --no-deps -it \
  --entrypoint /app/.venv/bin/pm-nautilus-final-export app \
  --data-dir /data --output "/data/LIVE/final-live-$(date +%Y%m%d-%H%M%S).pmnb"
```

该工具保证导出期间本机应用不能写入账本；它不能代替停机前对在途订单、链上交易及旧服自动重启的核对。最终文件原子发布后会同步父目录，快照连接显式关闭；这些持久化保障仍不能替代备份校验及恢复演练。若账本超过网页包上限，仍使用前述停机加密归档。首次实盘之前没有 LIVE 历史时，不需要迁移旧 TEST 模拟账本。

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

未确认链上授权或赎回的 raw/hash/nonce 与账户 nonce 下限同属恢复资料，须完整保留。新版本按账户串行处理待确认交易；广播结果不明时只恢复原交易，不替另一项权利构造相同 nonce。发现旧账本存在冲突时先核对原链上事务，不删除 pending、手改 nonce 或回滚账本来解除占用。

升级：记录当前 SHA 和镜像标签 → 构建已验证的新镜像 → PAUSE 并确认在途状态 → 停服务并制作最终一致性备份 → 固定目标提交和 `.env` SHA → up → 核对健康、设置、同库资金、历史与账本代次。部署不会自动 START；恢复维护前已获授权的 TEST 运行状态仍须在校验通过后显式执行。

**当前业务账本采用 v2 分行存储。**启动时，`Store` 会在单个 SQLite 事务内将旧 `meta.business` 集合迁移到 `business_parts`，保留周期、目标、结算权利、原生事件、订单、设置及账本代次；新库也使用该格式。迁移失败会回滚并拒绝继续，未知版本或混合格式不会被当作空白账本。SQLite 自动回滚后也会清掉未提交的比较缓存，重试须从已提交数据重建。部署前必须保存与旧镜像匹配的完整停机快照，首次用新版本打开数据卷后按上述项目逐项对比；不要删除表、清空游标或 TEST reset 来让升级通过。

回滚前先停新版本并保存当前账本，确认目标代码能读取当前业务和原生账本格式；**不能只 checkout 旧 SHA，再让不认识 v2 的版本打开已经迁移的数据卷。**兼容版本可在核对后使用当前数据；不兼容版本必须先在隔离副本上验证转换或使用与旧版本配套的停机快照，且不得覆盖之后新增的交易事实。真实钱包发生交易后，严禁恢复旧 LIVE 账本直接启动：必须把账本与最新真实回报核对，避免遗失已发生的权利或重复赎回。

2026-10-05 的四项维护修复部署时，额外核对：健康接口返回 `ok`、原 TEST 设置和账本代次不变、`LIVE` 仍未启用、`runtime/server` 中旧数据库在新镜像启动后通过 SQLite `quick_check` 与应用账本验证。新版本会在已有 `intents` 表上自动增加活动订单部分索引，不删除历史订单。LIVE 曾出现结果不明的在途订单时，资金和 Event 占用须保留至对账确认；不可通过重启、恢复旧快照或手工清空记录释放。部署结果及实际 SHA 以 [HANDOFF.md](../HANDOFF.md) 最新章节为准。

## 6. TEST 操作

- 在 TEST 选择 1—10 号后，配置、资金、记录和启停均只作用于选中账户；其他账户继续按各自状态运行。配置通过 UI 保存；新买使用新配置。已有周期的预算、止损参数冻结，已有目标不追溯改变。
- 选中账户的初始资金只能在该账户 PAUSED、无交易历史时改。盈利不会自动扩大每轮金额。
- 重置需选中账户 PAUSED 并双重确认；只清该账户的模拟数据并恢复 100U／1U 等原始默认值，其他 TEST 和 LIVE 不受影响。TEST 后台因 LIVE 意图停用时，先按明确恢复流程处理，不能保存或重置来绕过互斥。
- START 仍会完整核对账本，较大数据库检查在后台执行；检查完成才进入 RUNNING。等待期间运行状态发生变化时，原 START 不会迟到覆盖新状态；检查耗时不表示已开始交易。
- 长时间无成交先看公开扫描、盘口与筛选条件，不能用强制下单验证网络。无买盘估值0，未知盘口估值显示未知。
- 小于交易所最小卖出量的残余仓位不能伪造卖出；保留并等待可合法退出或正式结算。

## 7. 网页 LIVE 钱包与实盘启用

新版本的日常操作入口是同一个 HTTPS 网页。默认 Compose 仍写着 `PM_LIVE_ENABLED=false`；这表示应用开机后不自动连接真实钱包。用户切到 LIVE 页面只是查看，不会下单。**无需运行 `compose.live.yaml`、`deploy/live-secrets.py` 或进入服务器解锁。**这些旧文件仅供尚未迁移的旧式部署参考，不能与网页钱包方式同时启用。

下列流程描述网页操作顺序。费用、份额和赎回授权的代码修复已部署；钱包网页的“就绪”只覆盖当前列出的检查，真实买卖及链上赎回仍需实际回报验收。

安装者先按前文部署基础 Compose、Nginx HTTPS、至少 16 字符的 UI 登录密码，并保留服务器本地 `compose.override.yaml`。Nginx 必须覆写 `X-Forwarded-Proto`，应用容器端口仅绑定宿主机回环；仅此可信路径在服务器 `.env` 设置 `PM_TRUST_HTTPS_PROXY=true`。公网钱包操作在后端也强制要求 HTTPS。加密备份恢复采用流式传输，反代应按 `deploy/nginx.conf.example` **只对** `/api/live/wallet/restore` 放宽上传上限和时间，并关闭代理请求缓冲；`/api/live/wallet/backup` 也应单独延长下载等待时间。服务器需要有足够剩余磁盘容纳临时备份和账本快照。初次服务器安装、证书和 Docker 仍需管理员完成一次；此后用户在网页完成日常钱包操作。

程序目前只支持 Polygon 主网普通钱包（签名地址和资金地址相同）或单签 Safe（签名地址是唯一 owner，阈值 1）。网页会根据私钥或 **12 词 BIP39 助记词**派生签名地址，要求填 Polymarket 显示的公开资金地址，并只读核对两者的归属。它会调用固定版本 SDK 创建或派生 CLOB API 凭据；这一步有账户认证签名，但不签订单或链上交易。其他钱包类型会被拒绝，不能通过修改地址或签名类型强行接入。

完整支持范围是 EOA(0) 与单签 Safe(2)；新建官方账号默认的 Deposit Wallet(3) 当前不支持。[官方 EOA 交易路径](https://docs.polymarket.com/trading/wallets-auth#set-up-eoa-trading)要求允许交易名单，本次尚未验证该资格。部署者应先核对资金账户类型与官方准入，之后再让用户决定准备资金；安装成功、余额授权通过或新建一个钱包都不能替代交易资格确认。

当前连接流程允许导入时留空资金地址，只读发现已有平台 Safe：Gamma profile、官方 factory 派生、已部署合约、唯一 owner 和阈值 1 须一致，异常或不支持即拒绝，不猜测 EOA。明确地址仍走原核对语义。签名 EOA 和平台资金 Safe 可以不同，不能凭密钥派生地址自行替代资金账户；未创建新链上钱包，也不自动重绑定现有 vault。

### 7.1 用户在网页上的顺序

1. 登录 HTTPS 网页，切到 **LIVE**。先查看并保存 LIVE 规则；TEST 规则独立，切换视图不改变运行模式。仍使用现有策略的每 Event 每轮金额、筛选和止损规则，不加新的投入或笔数限制。
2. 选择 12 个助记词或私钥，设置至少 12 字符的解锁密码。公开资金地址可留空查找已有 Safe，也可明确填写平台显示的资金地址；请亲自核对显示的 signer/funder。助记词/私钥经 HTTPS 到达服务器；助记词不写盘。派生的签名密钥和 API 凭据存入 `/data/wallet/live.vault`，文件 `0600`、目录 `0700`，服务器只保存加密文件。**服务器解锁后内存中持有签名密钥**，这是一台服务器独立运行所必需的。秘密不要发到聊天、GitHub、URL 或截图。
3. 网页进行只读检查：钱包类型及所有权、Polygon 网络、CLOB 余额与交易授权、手续费余额、CTF 赎回适配器授权、现有挂单、钱包旧持仓与 LIVE 账本归属，以及从**服务器交易出口**查询 Polymarket 地区接口。资金账户用 pUSD 交易，签名账户（Safe 为 owner）用 Polygon POL 支付链上手续费。缺少 pUSD/CTF 交易所权限时，按 [Polymarket 官方交易授权流程](https://docs.polymarket.com/trading/wallets-auth#set-up-trading-approvals)完成同一资金账户授权，再重新核对；不要用试单代替授权检查。网页“授权赎回”只设置标准与 Neg-Risk 的 CTF 赎回适配器权限，是明确的真实链上交易，需由用户确认并支付 gas；不会在只读检查中自动广播，也不代办交易所权限。已有权限通过时无需重复授权。程序外持仓/挂单仍须核对；仅严格证明零收益的外部标准二元残仓不再要求先清理，其余旧仓继续阻止启用。程序不接管、烧毁或自动赎回旧仓，本程序份额精确核对保留。
4. 先将所有 TEST 账户暂停。核对页面的签名和资金地址、确认当前 LIVE 规则及全部检查后，由用户决定是否点击“启用实盘”。程序拒绝仍有 TEST 运行的切换，停止并等待全部 TEST 后台任务、持久化 LIVE 意图后才接通并核对私有账户，状态仍为 **LIVE / PAUSED**。随后只有用户点击右上角 **▶** 才会开始新买。PAUSE 停止新买，但已有仓位的卖出、止损和赎回仍可能继续。首次真实交易后的扣款、份额、卖出和结算仍须据实际回报核验。

主机或应用重启不自动恢复 RUNNING。若运行模式意图为 LIVE，TEST 后台保持停止，LIVE 钱包显示 **LOCKED**；用户需在网页输入解锁密码、重新检查并明确启用，再自行决定是否点 ▶。若要返回 TEST，须确认没有 LIVE 活动并使用“恢复TEST后台（保持暂停）”，随后逐账户明确 START；不能只切换页面恢复。锁定期间程序不维护已有 LIVE 仓位，网页不得把不可见持仓显示成已清仓。解锁密码丢失时不能靠 GitHub 找回钱包；请保管原钱包恢复方式和网页加密备份。

持仓就绪核对完整读取 Data API OPEN 全分页，仅用结果发现 token；本程序原子份额取自本次新取固定区块的 CTF 余额，不因 API 四位显示截断而放宽六位核对。已知零数量 token 保留归属，若 API 出现它仍核对零余额；正数量权利即使 API 缺行也查链上。外部例外须链上证明官方 pUSD/USDC.e、空父集合的标准二元 Condition/结果/token 身份一致、已结算、收益分子严格为零且另一结果余额为零；不读取显示价格作证明，不接管或写入这些残仓。正收益、未结算、Neg-Risk、身份不明或 RPC 失败保持阻止或 UNKNOWN，详细条件见 [LIVE_READINESS.md](LIVE_READINESS.md)。启动报告按 token 汇总未结算自有权利并保留读取后的最新权利检查及 ready 前最终新查；每批余额固定区块、有界四并行，无跨笔缓存。每笔 BUY 的账户、挂单和同 Condition 核对保留。

美国来源的 API 新开仓受 [Polymarket 官方地区规则](https://docs.polymarket.com/api-reference/geoblock)限制。硅谷服务器在 **2026-10-07 22:47 CST** 的出口核对返回 `blocked=true`、`US/CA`；2026-10-12 部署验收没有重新查询地区接口。2026-10-14 本轮升级前只读账户检查仍显示当前出口不允许 API 下单。启用前必须从届时服务器自身交易出口重新检查，换服务器后也不能沿用旧服务器或自己电脑的结果。

### 7.2 网页加密备份与换服务器

LIVE 页面“钱包备份与管理”可下载单个 `.pmnb` 加密包，包含**同一时点**的加密钱包和 LIVE SQLite 账本。下载时再次输入解锁密码；备份包用该密码单独加密。把文件与密码分开保管，不上传 GitHub。它不包含 TEST 模拟历史、服务器登录密码、证书或 Nginx 设置；公开策略文件也不会自动记录网页后续调整。它是日常时间点备份，不能保证下载后到停机前没有新增交易。较大的 LIVE 账本若超过网页备份上限，应由管理员按第 5 节做一致性停机备份。

迁移已有真实 LIVE 记录时，先在旧服务器 PAUSE 新买、确认在途订单，再由管理员停止旧应用，按第 5 节制作并验证**停机后的最终一致性加密备份**。管理员在新服务器安装相同或兼容的已验证版本，配置 HTTPS/登录并恢复最终账本及加密钱包，随后从新服务器出口核对真实账户；不可让新旧服务器同时管理同一钱包。当前网页 `.pmnb` 可作另一个时间点备份，但“先下载再停机”的文件可能缺少期间的卖出或赎回，不能保证为最终账本；实际份额核对也未必能发现已经完成的回合。恢复后状态 **LOCKED**，仍需解锁、核对并手动启用/START。**GitHub 本身没有真实账本或钱包。**

网页恢复只接受空白 LIVE 账本，拒绝覆盖已有钱包或交易历史。服务器升级和回滚继续遵守第 5 节的停机备份；不能用旧备份抹掉未知结果的真实订单。`deploy/live-secrets.py`、`compose.live.yaml.example` 属于旧式手工接入路径，已配置该路径的部署需先由管理员审查迁移，不得把旧 `/run` 明文和新网页钱包同时提供给一个应用。

恢复开始后，后台工作线程会在钱包操作锁内完成钱包和 LIVE 账本替换，再清理上传文件；浏览器超时或请求取消不会提前释放锁。遇到这种情况先刷新并核对恢复结果，再解锁或做下一步钱包操作，不能把超时视为恢复已停止。备份下载取消时也先等待后台导出结束，再删除本次临时包；正常下载完成后清理。重启取得进程锁后只清理本程序专用且权限/内容合法的遗留临时文件，不触碰账本、钱包或最终导出。应用关闭会等待这些工作结束再释放进程锁。

“授权赎回”后台签名及广播不会因网页取消而立刻停止；操作锁保留到线程结束。待确认授权或赎回继续占用原账户交易，不能重复授权、删除钱包或用恢复包覆盖。请刷新查看原交易检查结果，再操作下一步。启用过程中请求被取消时，尚未挂接的 LIVE 运行时也会完成清理，避免留下未登记的账本连接和执行队列。

10-14 已部署代码保留准备失败的阶段和安全错误码，重启或账户查询失败仍显示同一钱包的可信 FAILED 诊断。仅在 LIVE 未启用、账本无活动，且失败记录身份匹配、明确 prepare 未签名、pending=None、错误码可信时，清除空白钱包可一并清理该失败记录及旧钱包绑定；nonce 消费下限保留。未知提交、身份不符或不可信 FAILED 仍拒绝，不能用清钱包绕过待确认状态。上述删除边界已由受控回归验证，未在正式钱包执行删除验收。

已部署连接修复：从未有任何 LIVE 交易、未启用且无在途时，用户在“钱包备份与管理”输入原密码并明确确认“清除当前钱包”，可取消身份匹配、字段规范且 pending=None 的 ACTIVE 准备记录，再用同一密钥重新导入；合法 FAILED/prepare 清理继续保留。清理旧绑定及授权记录采用同一事务，nonce 消费下限保留；未知提交、不可信字段或有交易历史仍拒绝。不是在原 vault 上直接改资金地址。

### 7.3 开机与版本更新

`deploy/pm-nautilus-test-boot.service.example` 仍可作为开机 TEST 单元；它重建基础 Compose，应用不会自动启用 LIVE，也不覆盖已保存的运行模式意图。升级时先记录版本、全部 TEST 状态与资金，暂停所有运行中的 TEST 或当前 LIVE 所需动作、停服务并按第 5 节备份，再拉取固定 SHA、构建和启动基础 Compose。确认全部 11 份账本、模式意图、网页、健康和 LIVE 锁定状态后，由用户在网页解锁与重新核对；升级本身不能自动按 ▶。

## 8. 故障排查

| 现象 | 检查和恢复 |
|---|---|
| 401 | 使用账号pm和服务器UI密码；不要关闭认证绕过 |
| 403 控制来源失败 | 刷新当前同源页面，检查反代Host/Origin；不要去掉CSRF |
| 无可执行盘口 | 检查公网出站、WS连接、扫描错误；重连需完整快照 |
| `streamError=ConnectionClosedError` | 公共行情常规断线会使盘口失效并自动退避重连；不应单独导致 TEST 暂停。若同时存在 `serviceError`，保持 PAUSED 并按持久化的错误类型、详情和时间排查后再显式 START |
| 行情恢复后仍有 `serviceError` | 当前共享行情源将 TEST 账户局部故障限制在相关账户，正常兄弟账户不被暂停。可恢复错误须在该账户显式 START 确认；已关闭并移出行情源的故障账户拒绝 START，需排查并重启程序。正常扫描不重放已确认错误；LIVE 沿用自身核对流程，TEST 不替 LIVE 确认 |
| 缩小筛选范围后仍有很多行情连接 | TEST 模式的共享行情源按十个账户的监控并集订阅，缩小范围时会合并多余连接；仍被其他 TEST 使用的连接保留。LIVE 启用时 TEST 后台退出，仅保留 LIVE 所需订阅。迁移 token 在旧连接关闭等待前先失效，须取得新完整快照。不要手工把未就绪盘口改成 READY |
| 有行情无成交 | 检查总时长、进度、费用元数据、最小量、Bid/Ask比例、兄弟完整性与预算 |
| 业务投影失败 | 保持PAUSED，检查磁盘/SQLite错误，备份后重启重放；不要手改cursor |
| LIVE核对失败 | 保持PAUSED，检查真实订单/余额/自有份额；临时网络恢复后重新核对，就绪恢复不自动START |
| 已结算输方旧仓仍显示 token | 仅链上严格零收益、派生身份一致且另一结果余额为零的外部标准二元残仓可不阻止启用；不靠显示价格判断，不需要为这类残仓付 gas 清理。不满足证明条件时先核对，不删除账本或手改持仓 |
| 赎回SUBMITTED很久 | 查询保存的tx_hash和nonce、RPC与POL，不删除跟踪或手动重复提交 |
| 授权或另一笔赎回暂时不能推进 | 先核对账户原待确认交易；结果未知时保留原 raw/hash/nonce。网页取消不等于撤销链上交易，不能靠重复点击、删除钱包或清除占用来处理 |
| 赎回FAILED | 核对失败收据/余额/混合权利；不要自动重置成未提交 |
| 重复实例错误 | 找到占用同目录的进程，保留唯一实例；不要绕过process.lock |

清理本次 Linux 验证虚拟机：工作区 `.reference/lima/bin/limactl`，实例 `pm-verify`，`LIMA_HOME` 为工作区 `.reference/lima-home`。验证结束停机；保留文件便于复验，不作为生产服务使用。

现有服务器的所有 `config`、`build`、`up`、`ps`、`stop` 命令都须显式包含本地 `compose.override.yaml`。网页 LIVE 钱包沿用基础 Compose，不加入 `compose.live.yaml`；只有尚未迁移的旧式部署才用第三份文件。不要在服务器上用省略 override 的命令覆盖当前 HTTPS 反代信任设置。

<details>
<summary>十账户改造前的历史发布与验收记录</summary>

以下内容按原日期保留，旧应用 SHA、测试数量、两库结构及当时服务器状态仅用于追溯，不是当前部署指令或最新验收结论。当前入口为 `main`，现行状态见 [HANDOFF](../HANDOFF.md) 和 [VALIDATION](VALIDATION.md)。

## 2026-10-21 执行与查询恢复优化（已发布部署验收）

本轮已完成构建、完整回归、双架构 CI、一致性备份及服务器保全验收，逐项核对两库、钱包、原参数、历史及主机维护，并恢复原 TEST/RUNNING。LIVE 不启用，不操作真实资金。无依赖、配置、数据库格式或主机维护脚本变更。范围见 [执行与恢复优化](EXECUTION_RESILIENCE.md)，部署验收以 HANDOFF 最新记录为准。

固定应用 `257b1fbe205cea083d6a9a80ac739fcfea2c17b4` 已部署，2026-10-21 17:40:41 CST 最终验收 PASS；TEST/RUNNING、LIVE=false、钱包 LOCKED，原参数和两库账本保留。实际证据、备份及源码／应用版本区分见 [HANDOFF 最新记录](../HANDOFF.md)；后续纯文档同步不重启应用。

## 2026-10-20 缺报价仓位按成本暂计（已发布部署验收）

本轮按用户确认口径调整展示：正常持仓继续显示报价、市值、剩余成本与浮盈亏；`UNAVAILABLE`／`NOT_READY` 仓位稳定排到列表底部，并保留数量、成本、均价、目标和原因。先对全部持仓排序，再截取前 20 笔或展开全部，纯摘要与各页的全仓汇总一致。

资产估算为账面现金（含已冻结现金）＋可估值持仓市值＋缺报价持仓剩余成本＋原有已证明待赎回应收；失败赎回权利仅在未计入现金时以原剩余成本暂计，诊断回款金额不作为现金或应收。盈亏估算保留原已实现盈亏、可估值持仓浮盈亏和原已知待赎回盈亏；缺报价持仓及失败赎回权利的当前浮盈亏暂按 0 计入，并注明数量、成本暂计金额及“浮盈亏暂记 0，恢复报价后重算”。成本暂计不代表可兑现市值，暂记 0 不是已确认零收益，也不抹掉原已实现盈亏。

这些值只用于只读展示，交易报价、原严格完整估值字段、账本、权益、参数和收益历史不改写。新的 `estimatedTotalFunds`／`estimatedPositionValue`、`knownUnrealizedPnl`／`knownTotalPnl` 和逐仓 `estimatedUnrealizedPnl` 明确区分展示估算与真实报价；锁定或未核对的 LIVE 账户继续隐藏账户资料。存储维护保留，LIVE 不启用。

本地最终 886 项 Python（25.30 秒）、41 项页面测试、Ruff 检查／格式（106 文件）通过；14 项部署门禁专项测试通过，代码已做独立复核。[PR #17](https://github.com/yvettemiranda/PM-NAUTILUS/pull/17) 已合并，固定应用为 `2095d1427b6aaf3eae886f1172b2b1c10c8981d0`；代码 push、PR 和 [main CI 39444708014](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/39444708014) 的 x86_64／ARM64 回归及 Docker verify 全部通过。2026-10-20 23:19:15 CST（15:19:15 UTC）服务器最终验收 PASS：容器 healthy、restarts=0、OOM=false，原 TEST/RUNNING 已恢复，LIVE=false、钱包 LOCKED，后台错误为空。完整在线 TEST validation PASS（62.018 秒）、真实 TEST 账本隔离副本 Runtime 验证及 TEST/LIVE 两库全部停机逻辑数据保留通过；原参数、代次、native／equity 历史、钱包、schema／codec v2、依赖／配置／安全设置与自动存储维护保留。 本次现场 94 笔持仓中 93 笔正常估值，1 笔归档持仓稳定排在第 94 笔；其他未就绪持仓为 0，剩余 702/702 盘口、4/4 连接组 READY。资产估算包含该笔精确剩余成本，行内浮盈亏显示 `0.00U（暂计）`，既有已实现盈亏保留；完整、摘要、前 20 笔的全仓聚合一致，正常仓排序保留。真实 Chrome 生产页已确认资产和盈亏均显示数字、94 笔可展开及异常项末位成本／零浮盈亏；行情变动会更新估算数字。本轮未启用 LIVE 或操作真实资金，短时验收不等于实盘闭环或长期稳定性证明。 源码／文档 SHA 以本次纯 Markdown 提交及私有 `docs-sync.json` 记录为准；镜像、env 和 runtime 保持固定应用代码，纯文档同步无需重启。下方保留前轮历史证据。

备份与完整保全：`/opt/pm-nautilus/backups/pre-costvaluation-20261020T150747Z`，密文 SHA-256 `c285732230ad31c196a6ae7b72d186803207e7ce92d5e516d34db430c50ff162`；一致性私有回滚、recipient 加密及哈希通过，未演练解密恢复。两库无迁移、压缩或 VACUUM；原 native／equity 历史前缀、意图、成本、参数和钱包保留，主机维护脚本／units／enabled-active timer 内容和权限及 Docker 日志 10m×3 保持。摘要与分页历史（验收时 796 条）、HTTPS 证书／认证、未认证钱包接口 401、app.js／ui-state.js／index.html 三份部署文件哈希通过。

重启后完成新市场扫描：容器 StartedAt=`2026-10-20T15:14:55.886024473Z`，lastScanAt=`2026-10-20T15:16:34.438587+00:00`；现场 scan／category／stream／service 当前错误均为 null。成本暂估和零浮盈亏仅用于展示，不作为可卖报价、退款、结算或链上回款证明。

市场归档、停单与缺失盘口的识别、订阅隔离、恢复和未知估值规则见 [市场可用性处理](MARKET_AVAILABILITY.md)；具体发布验收以 [HANDOFF](../HANDOFF.md) 为准。

**2026-10-20 历史部署（停用识别）：**[PR #16](https://github.com/yvettemiranda/PM-NAUTILUS/pull/16) 已合并，固定应用代码 `ebbe888a56213f3c976fdb5291b045354cb821e9`；代码 push、PR 和 [main CI 39397396195](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/39397396195) 的 x86_64／ARM64 回归及 Docker verify 全部通过。本地 880 项程序、37 项页面、Ruff（105 文件）及 JS/shell 语法通过。源码／文档 SHA 以本次纯 Markdown 提交及私有 `docs-sync.json` 记录为准；纯文档同步仅推进源码，应用镜像、env、runtime 保持上述固定代码，无需重启。

2026-10-20 17:14:13 CST（09:14:13 UTC）服务器最终 proof 的 `acceptance=true`：容器 healthy、restarts=0、OOM=false，原 TEST/RUNNING 已恢复，LIVE=false、钱包 LOCKED，后台错误为空。在线完整 TEST validation PASS（38.999 秒），真实 TEST 账本隔离副本离线 Runtime 完整验证通过；停机 TEST/LIVE 两库全部逻辑值、schema 和 codec v2 原样保留。原参数、generation、native／equity 历史、加密钱包及绑定、依赖／配置／安全设置保留；既有主机存储工具、units、enabled／active timer 和 Docker 日志 10m×3 保持。本轮没有数据迁移、压缩或 VACUUM。

一致性私有回滚与 age 加密归档位于 `/opt/pm-nautilus/backups/pre-marketavailability-20261020T090449Z`；密文 SHA-256 `46526d65e1df00dd89206671d9b3ae2a9129fac976fe4ce7ba7e621da3e879e7`。私有回滚一致性、recipient 加密及哈希通过，未演练解密恢复。恢复交易后不得用旧快照覆盖新增记录。 全仓摘要／分页历史（790 条）、HTTPS／认证／未认证钱包 401 与部署 JS 哈希通过；认证 dashboard 的报价和估值语义通过，本轮未做生产浏览器登录后的视觉验收。

本次现场为 92 笔持仓，1 笔因 `archived` 报价不可用，其他未就绪持仓为 0；剩余 4/4 连接组 READY、767/767 盘口完整。停用仓位的旧价格已屏蔽，已知持仓小计精确并与完整总额分开；完整资产和未实现盈亏仍为 `null`／未知，数量、成本与权利保留，处置安排待核查。归档不证明零价值、退款、结算或一定恢复。新扫描和后续 PONG／错误复查通过；仅代表本次短时采样，具体时间和结果见 [最新验证](VALIDATION.md)。可用性在内存观察，重启后重新核实，不用旧盘口恢复交易。停用持仓仍随账本、钱包和备份保留。本轮没有启用 LIVE、解锁钱包、签订单或链上资金操作，也未重新解决地区、pUSD／POL 或真实资金买卖／正额赎回闭环；TEST、CI 和服务器验收不等于实盘就绪。

**2026-10-20 历史部署（空间维护）：**应用 `640d1b1dbc58f2e61ef9cf3e131cd37a1e163de9` 已完成备份、升级、TEST/LIVE Store 两库逻辑核对、TEST 隔离副本 Runtime 与在线 TEST 完整核对，当时 TEST/RUNNING、LIVE=false、钱包 LOCKED，参数/历史/钱包保留。自动日志轮转、15 分钟主机清理及运行时磁盘/inode 保护已安装并验收，详见 [自动存储维护](STORAGE_MAINTENANCE.md) 与 [验证记录](VALIDATION.md)。新服务器须安装定时任务并验收，不仅复制源码；日常维护无需定时停机。纯文档源码同步不重建或重启应用。

**2026-10-19 元数据格式维护：**新版本写入可离线无损还原的压缩 Token，兼容旧库读取；3e765cc 及更早的压缩格式前镜像不能读取新格式，6dac94b 与 640d1b1 的 Token 编码兼容。升级、显式离线整理、完整双库回滚副本和可选 VACUUM 的前提见 [本轮维护说明](METADATA_OPTIMIZATION_2026-10-19.md)。不能仅回退到压缩格式前镜像而继续用新格式库；恢复运行后也不能回退旧快照丢失新增交易。正式服务器当时已完成整理与验收，运行应用 `6dac94bf6bc9cbc954a22e112a52dd49a798d2a6`，结果见 [最新验证](VALIDATION.md)。

**2026-10-19 历史实例验收：**2026-10-19 20:40:33 CST 恢复 TEST/RUNNING，LIVE=false、钱包 LOCKED；全部逻辑数据与配置保留，实测文件减少 31.95%。新装默认行为仍如下。

默认交付：TEST + PAUSED，LIVE 未启用。下列部署步骤不启动正式长期 TEST。代码、TEST 数据、LIVE 数据各自独立；不得复制 PM-SMALL 的数据库、钱包会话或历史运行目录。

非技术用户在新电脑和新服务器上放弃旧 TEST 模拟记录、准备首次实盘时，先按 [从 GitHub 到首次实盘](START_FRESH_LIVE.md)确认顺序，再由执行部署的人使用本手册。若本程序已产生真实 LIVE 订单或持仓，须走本手册的迁移及对账流程，不能按空白安装覆盖。

2026-10-07 [逐环节审计](END_TO_END_HANDOFF_2026-10-07.md)中的费用、份额、赎回授权和最终导出问题已修复；实际部署与未验收范围见 [HANDOFF.md](../HANDOFF.md)。本手册说明部署机制；服务器健康、网页钱包就绪或 TEST 模拟通过，均不表示真实买卖及赎回闭环已验收。

**2026-10-14：本轮精确持仓核对修复。**[修复 PR](https://github.com/yvettemiranda/PM-NAUTILUS/pull/12) 的固定应用代码 `3e765ccb067c8012af5f4ea4f23c3bda91d7372f`；回归记录：705 项 Python / 33 项页面回归、Ruff/JS 与 PR/main 双架构 CI 通过，明细见 [最新验证](VALIDATION.md)；服务器验收：2026-10-14 19:03 CST 通过，TEST/RUNNING、LIVE 未启用、钱包 LOCKED；实际持仓与授权只读核对通过，资金/POL 与地区仍未满足条件。已有 Safe 重连后的身份、交易所及两个赎回适配器权限已完成升级前只读核对，资金/POL 和地区仍未满足启用条件。持仓核对改用固定区块链上原子余额，严格零收益外部残仓的误拦截修复边界见第 7 节。参数和规则保留，真实买卖及正额赎回仍待实际验收。

**2026-10-14 Safe 连接历史验收：**[PR #10](https://github.com/yvettemiranda/PM-NAUTILUS/pull/10) 的代码 `fefcdb3963f57ceaea8f0eba93686c6024dc1f0f` 经 591/33 项回归、Ruff（85 文件）、PR/main CI 和服务器完整核对通过；参数、两库历史、钱包及安全设置保留。13:36 CST 当时状态 TEST/RUNNING、LIVE 未启用、钱包 LOCKED。新导入可留空查找已有 Safe，旧空白绑定由用户确认清除后重导，不自动更换。旧证据见 [HANDOFF.md](../HANDOFF.md)/[VALIDATION.md](VALIDATION.md)。纯文档合并可更新服务器源码 main SHA，镜像/env/runtime 继续固定最新已验收应用代码，无需为纯文档构建或重启。

</details>
