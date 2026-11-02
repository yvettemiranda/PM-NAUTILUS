# PM-NAUTILUS 部署与运维手册

本页只写现在要用的操作步骤。2026-11-01 之前的完整说明和历史发布记录在 [归档](archive/DEPLOY_FULL_20261101.md)，当前运行状态以 [HANDOFF](../HANDOFF.md) 为准，实盘前清单见 [LIVE 准备检查](LIVE_READINESS.md)。

## 1. 当前服务器速查

| 项目 | 值 |
| --- | --- |
| 程序目录 | `/opt/pm-nautilus`（源码由 root 持有，git 命令需 `sudo git -c safe.directory=/opt/pm-nautilus`） |
| Compose 文件 | 只有基础 `compose.yaml`，没有 `compose.override.yaml` |
| 数据目录 | `/opt/pm-nautilus/runtime/server`，容器内为 `/data`，属主 UID 10001 |
| 版本 | `.env` 中的 `PM_GIT_REVISION`（完整 SHA），镜像标签同名 |
| 健康接口 | `curl -H 'Host: localhost' http://127.0.0.1:8765/api/health`（无需登录） |
| 部署备份 | `/var/lib/pm-nautilus-deploy/`（部署脚本自动生成，保留最近 5 份） |
| 服务器外备份 | 未启用（COS 未开通）；可选方案见第 3.2 节。部署前手动快照 `pre-deploy-8eafbeb`（2026-11-02；免费配额 2 个，下次部署前再建一个，成功后删较旧的） |

数据目录里有 11 份账本：`TEST/state.sqlite`、`TEST/accounts/2…10/state.sqlite`、`LIVE/state.sqlite`，另有 `execution-mode.json`（运行模式意图）和 `wallet/live.vault`（加密钱包）。不要只复制其中一部分，也不要在程序运行时直接复制单个 `.sqlite` 文件（会漏掉 WAL 中的数据）。

## 2. 日常更新：一键部署

在服务器上：

```sh
cd /opt/pm-nautilus
sudo git -c safe.directory=/opt/pm-nautilus fetch -q origin
sudo deploy/deploy.sh <完整的 40 位提交 SHA>
```

脚本先显示当前账户状态，输入 `yes` 后依次执行：

1. 预检：服务是否响应、磁盘空间是否足够；LIVE 已启用或钱包已解锁（状态不是未配置/锁定）时拒绝执行，停机前最后一步会再查一次；程序目录有未提交或未跟踪文件时拒绝；提交不在 `origin/main` 上，或 GitHub CI 对这个提交没有全部通过（含两种架构）时拒绝。确需部署须加 `--allow-unverified`
2. 检出目标提交，并在旧版本继续运行时构建新镜像
3. 记录停机前每个 TEST 账户的运行状态和持仓数
4. 正常停机（最多 45 秒），完整复制数据目录和 `.env` 到 `/var/lib/pm-nautilus-deploy/<时间>-<SHA>/`
5. 改写 `PM_GIT_REVISION` 并启动新版本
6. 等待健康检查通过，确认版本号和可用账户数正确
7. 确认重启后所有账户都是 PAUSED，然后只 START 停机前在运行的账户
8. 核对运行状态与停机前一致，等待行情恢复

任何一步失败都会立即停下，并打印对应的回滚命令。脚本不会启用 LIVE、解锁钱包，也不会启动原本暂停的账户。每次的状态记录（`accounts-before`、`accounts-restarted`、`accounts-after`）保存在对应备份目录里。

可选环境变量：`PM_APP_DIR`、`PM_BACKUP_ROOT`、`PM_KEEP_BACKUPS`（默认 5）、`PM_HEALTH_TIMEOUT`、`PM_MARKET_TIMEOUT`、`PM_COMPOSE_FILES`（默认 `compose.yaml`，以空格分隔多个文件）。

## 3. 备份

共三层，用途各不相同。

### 3.1 部署时自动备份

由第 2 节的部署脚本在停机状态下完整复制，是每次升级前的一致性快照，保留最近 5 份。它和账本在同一块盘上，只能防止升级出错，防不了服务器损坏。

### 3.2 服务器外备份：每日上传腾讯云 COS（可选，当前未启用）

腾讯云轻量应用服务器不支持定期自动快照，所以用 `deploy/offsite-backup.py` 做每日备份。它不停机：用 SQLite 官方在线备份接口从只读连接复制每份账本，得到一致的副本并做 `quick_check`；再连同模式文件和加密钱包打包，附带 SHA-256 清单，上传到 COS。它必须以 UID 10001 运行（脚本会检查），否则可能在数据目录里留下 root 属主的 SQLite 临时文件，导致程序打不开账本。

一次性设置：

1. **COS 存储桶**：在腾讯云控制台新建私有存储桶，建议选和服务器不同的地域（如香港），开启服务端加密；再加一条生命周期规则，30 天后自动删除旧备份。
2. **子账号密钥**：在访问管理（CAM）新建子用户，只授予该存储桶的上传权限（`cos:PutObject` 及分块上传相关权限），生成 SecretId/SecretKey。不要使用主账号密钥。
3. **服务器上安装**（密钥只写入服务器上的配置文件，不发到聊天或 GitHub）：

```sh
# coscli：官方命令行工具，从 https://github.com/tencentyun/coscli/releases 下载对应架构
sudo install -m 0755 coscli-linux-amd64 /usr/local/bin/coscli
sudo install -d -m 0750 -o root -g 10001 /etc/pm-nautilus
sudo install -d -m 0700 -o 10001 -g 10001 /var/lib/pm-nautilus-offsite
# 写入 coscli 配置（SecretId/SecretKey/桶/地域），属主 root、组 10001、权限 0640
sudo coscli config init -c /etc/pm-nautilus/cos.yaml
sudo chown root:10001 /etc/pm-nautilus/cos.yaml && sudo chmod 0640 /etc/pm-nautilus/cos.yaml
sudo tee /etc/pm-nautilus/offsite.env >/dev/null <<'EOF'
PM_OFFSITE_COS_URI=cos://<桶名-APPID>/pm-nautilus/
PM_OFFSITE_COSCLI=/usr/local/bin/coscli
PM_OFFSITE_COSCLI_CONFIG=/etc/pm-nautilus/cos.yaml
EOF
sudo cp deploy/pm-nautilus-offsite-backup.service.example /etc/systemd/system/pm-nautilus-offsite-backup.service
sudo cp deploy/pm-nautilus-offsite-backup.timer.example /etc/systemd/system/pm-nautilus-offsite-backup.timer
sudo systemctl daemon-reload
sudo systemctl start pm-nautilus-offsite-backup.service   # 先手动跑一次
sudo journalctl -u pm-nautilus-offsite-backup --no-pager -n 20
sudo systemctl enable --now pm-nautilus-offsite-backup.timer  # 之后每天 04:17 左右
```

恢复时从 COS 下载一份 `.tar.gz`，在**程序停止**的目录中解压，把 `data/` 下的内容放回 `runtime/server/`，再执行 `chown -R 10001:10001`，先启动核对，然后才决定是否 START。不要用旧备份覆盖之后已经产生新交易的账本。

另外，大改动之前可以在轻量服务器控制台手动打一个快照（每台 2 个免费），作为整机回退的额外保险。

### 3.3 换服务器或有真实 LIVE 记录时：停机最终备份

COS 每日备份和网页 `.pmnb` 都是在程序运行时做的，PAUSE 期间仍可能发生卖出和赎回。所以有真实 LIVE 记录时，迁移必须以**停止旧实例后**制作的最终备份为准，见第 5 节。

## 4. 回滚

部署脚本失败时会打印回滚命令。新版本已经正常运行之后想回到旧版本：

```sh
sudo deploy/deploy.sh <旧版本 SHA>   # 会再做一次停机备份
```

旧镜像还在本机，构建很快。注意旧版本不一定能读取新版本写过的账本（例如业务账本 v2 格式迁移之后，不认识 v2 的旧版本不能直接打开）。有真实交易之后，禁止用旧 LIVE 账本覆盖当前账本再启动，必须先和真实回报核对。

## 5. 换服务器迁移

1. 新服务器按第 6 节装好，但**先不要启动**（不要执行 `up -d`）。
2. 旧服务器：确认没有在途订单，执行 `sudo docker compose --env-file .env -f compose.yaml stop`，并确认开机单元不会再把它拉起来。
3. 旧服务器：完整打包停机状态的 `runtime/server` 和 `.env`，传到新服务器。如需加密，用 `tar -czf - runtime/server .env | age -p > state.tgz.age`；有 LIVE 记录时，还可以用镜像里的 `pm-nautilus-final-export` 导出最终 `.pmnb`。
4. 新服务器：解压到停止状态的程序目录，执行 `sudo chown -R 10001:10001 runtime/server`，`.env` 权限设为 0600。按新域名或 IP 修改 `PM_ALLOWED_HOSTS`，并确认 HTTPS 反代配置。
5. 新服务器：`up -d` 后核对健康状态、11 份账本、各账户设置和历史；LIVE 钱包是锁定状态，需在网页解锁并重新检查，再手动启用。
6. 确认旧服务器不会再运行同一个钱包之后，才在新服务器上启用 LIVE。两台服务器不能同时管理同一个钱包。

## 6. 首次部署新服务器

支持 Linux x86_64 和 aarch64。需要 Docker 及 Compose 插件，以及 git、curl、python3。

```sh
sudo git clone --branch main https://github.com/yvettemiranda/PM-NAUTILUS.git /opt/pm-nautilus
cd /opt/pm-nautilus
sudo git -c safe.directory=/opt/pm-nautilus checkout --detach <完整 SHA>
sudo cp .env.example .env && sudo chmod 600 .env
sudo mkdir -p runtime/server && sudo chown 10001:10001 runtime/server
```

编辑 `.env`：
- `PM_GIT_REVISION` 填完整 SHA。
- `PM_UI_PASSWORD` 填至少 16 个字符的随机密码（`openssl rand -hex 24`），登录账号固定为 `pm`。
- `PM_ALLOWED_HOSTS` 填访问用的域名或 IP，并保留 `localhost`，部署脚本靠它访问本机接口。
- 只有在应用端口只绑定回环地址、并且 Nginx 会覆写 `X-Forwarded-Proto` 的前提下，才把 `PM_TRUST_HTTPS_PROXY` 设为 `true`。

如果要用仓库里的公开策略规则初始化账本（只适用于全新、从未启动过的数据目录），在 `build` 和第一次 `up -d` 之间执行：

```sh
C="sudo docker compose --env-file .env -f compose.yaml"
$C build
test ! -e runtime/server/TEST/state.sqlite && test ! -e runtime/server/LIVE/state.sqlite
$C run --rm --no-deps --entrypoint /app/.venv/bin/python app -c 'from pathlib import Path; from pm_nautilus.store import Store; root=Path("/data"); [Store(root/m/"state.sqlite",m).close() for m in ("TEST","LIVE")]'
$C run --rm --no-deps --entrypoint /app/.venv/bin/python app /app/scripts/apply_preferences.py --data-dir /data --profile /app/config/strategy-profile.json
$C up -d && $C ps
curl -fsS -H 'Host: localhost' http://127.0.0.1:8765/api/health
```

HTTPS：参照 `deploy/nginx.conf.example` 配一个独立虚拟主机，反代到 `127.0.0.1:8765`，先执行 `nginx -t` 再重载。不要直接公开容器端口，也不要通过明文 HTTP 传登录密码。钱包的备份和恢复接口需要样例中单独放宽的上传大小和超时设置。

可选组件（样例都在 `deploy/` 下）：
- `pm-nautilus-test-boot.service.example`：开机启动单元；不会自动启用 LIVE 或 START。
- `pm-nautilus-storage.*.example`：磁盘保护定时任务，见 [磁盘保护](STORAGE_MAINTENANCE.md)。
- `pm-nautilus-monitor`：只读巡检脚本，可配合 `restrict,command=...` 的独立 SSH 公钥使用。

## 7. LIVE 网页钱包

日常的钱包操作都在 HTTPS 网页的 LIVE 页完成，不需要 `compose.live.yaml` 或 `deploy/live-secrets.py`（那是旧式手工接入路径，不能和网页钱包同时启用）。默认 `PM_LIVE_ENABLED=false`，开机不会自动连接钱包。

### 7.1 用户在网页上的顺序

1. 切到 LIVE，查看并保存 LIVE 规则（和 TEST 规则相互独立）。
2. 导入 12 个助记词或私钥，设置至少 12 个字符的解锁密码。资金地址可以留空让程序查找已有 Safe，也可以手动填写；请亲自核对页面显示的签名地址（signer）和资金地址（funder）。秘密只在这个 HTTPS 页面输入，不要发到聊天或 GitHub，也不要截图。
3. 点“检查账户”：程序会核对钱包类型、余额和授权、手续费余额、赎回授权、现有挂单和持仓，并从服务器的出口 IP 查询地区限制。缺少交易授权时，按官方流程完成。“授权赎回”是一笔真实的链上交易，需要你确认。
4. 暂停全部 TEST 后，再点“启用实盘”，启用后状态为 LIVE / PAUSED；之后只有你点 ▶ 才会开始买入。PAUSE 只停止新的买入，已有仓位的卖出、止损和赎回仍会继续。

重启后钱包会显示 LOCKED，需要解锁、重新检查、明确启用。解锁密码丢失无法从 GitHub 找回，请保管好原钱包的恢复方式，以及网页导出的 `.pmnb` 加密备份。

## 8. 故障排查

| 现象 | 处理 |
| --- | --- |
| 401 | 用账号 `pm` 和服务器 UI 密码登录；不要关闭认证 |
| 403 控制来源失败 | 刷新页面，检查反代的 Host 和 Origin 设置；不要去掉 CSRF |
| 部署脚本提示 LIVE 已启用 | 先在网页处理 LIVE（暂停、核对），脚本不在 LIVE 期间部署 |
| 部署后行情一直 recovering | 重启后首次扫描约 1 分钟；一直不恢复时，查看健康接口的 `serviceFreshness` 和容器日志 |
| 有行情但没有成交 | 检查筛选条件（总时长、进度、买卖盘比例）、手续费元数据、最小份额、市价买单最低 1U、预算 |
| 业务投影失败 | 保持 PAUSED，检查磁盘和 SQLite 错误，备份后重启重放；不要手改游标（cursor） |
| LIVE 核对失败 | 保持 PAUSED，核对真实订单、余额和自有份额；恢复后不会自动 START |
| 赎回长时间 SUBMITTED / FAILED | 查已保存的 tx_hash 和 nonce、RPC 以及 POL 余额；不要删除记录或手动重复提交 |
| 赎回提示份额与程序记录不一致 | 检查 Polymarket 是否开启了自动赎回（Auto redeem），或钱包里有程序外的操作 |
| 重复实例错误 | 同一数据目录只能有一个进程；不要绕过 `process.lock` |
| COS 备份失败 | `journalctl -u pm-nautilus-offsite-backup`；检查 coscli 配置、子账号权限、磁盘空间，以及是否以 UID 10001 运行 |
