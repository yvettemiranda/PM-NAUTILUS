# PM-NAUTILUS 开发、发布与运维

默认交付：TEST + PAUSED，LIVE 未启用。下列部署步骤不启动正式长期 TEST。代码、TEST 数据、LIVE 数据各自独立；不得复制 PM-SMALL 的数据库、钱包会话或历史运行目录。

## 1. Mac 本地开发

要求 macOS 26+ arm64（所选 Nautilus wheel 的最低要求）、Python 3.12.14。其他 Mac 版本使用本地 Linux 容器。当前工作区 `.venv` 已安装，通常直接运行：

```sh
cd /Users/d4clt/PM-NAUTILUS
.venv/bin/pm-nautilus --data-dir runtime/local
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

## 2. GitHub 首次上传

2026-09-07 用户明确授权公开仓库 `yvettemiranda/PM-NAUTILUS`，覆盖原始指令的拟定私有安排。重装恢复见 [REINSTALL.md](REINSTALL.md)。首次创建由本轮执行；后续克隆已有仓库，不重复创建、不强推。

```sh
git remote -v
git status --short
git push -u origin main
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

推送前检查 `git ls-files`，不得包含 `.env`、凭据 JSON、runtime、私钥、RPC 密钥或 `.reference`。CI只进行无凭据测试，不部署、不启动LIVE。用户明确推迟服务器部署到重装后，以下章节是后续操作手册，不是本轮已部署的声明。

## 3. 新 Linux 服务器目录

服务器目标、SSH 用户和运行目录尚未提供。不要推断旧 VPS 或原 PM-SMALL 机器就是本次目标。收到目标后，先只读执行 `uname -m`、`cat /etc/os-release`、`docker version`、`docker compose version`、`ss -lntup`、`df -h`、现有服务/目录检查。

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

在 `.env` 写入 `PM_GIT_REVISION` 的完整 SHA、随机 UI 密码（至少16字符）、允许的域名。账号固定 `pm`。密码可用 `openssl rand -hex 24` 本地生成；只保存于该文件。不要把真实密码放进聊天、命令参数、Git 或一般文档。

```sh
docker compose --env-file .env config --quiet
docker compose --env-file .env build
docker compose --env-file .env up -d
docker compose ps
curl -fsS http://127.0.0.1:8765/api/health
```

健康结果需包含 TEST、PAUSED、`liveExecutionEnabled=false` 和对应代码 SHA。默认仅绑定服务器回环地址。最小访问方式：在自己的 Mac 上运行 `ssh -L 8765:127.0.0.1:8765 <用户>@<服务器>`，然后访问本机端口并以 `pm` 登录。

`deploy/nginx.conf.example` 提供可审查的独立虚拟主机样例；替换实际域名和证书路径后先运行 `nginx -t`。如使用公网域名，沿用服务器现有 HTTPS 反代，在新独立配置中转发到 `127.0.0.1:8765`，把域名加入 `PM_ALLOWED_HOSTS`。保留原 Host/Origin，使用 HTTPS。不要直接公开容器端口，不要把 Basic 密码通过明文公网 HTTP 发送。已有公网入口的切换必须先保存旧配置、明确旧服务/新服务端口及回滚动作；本次未指定或切换任何入口。

## 4. 部署验收与同库重启

1. `git rev-parse HEAD` 与 GitHub main 的目标 SHA 相同；健康接口 revision 相同。
2. `docker compose ps` 健康；网页设置、五项资金摘要、TEST/LIVE 切换、记录可读取。
3. 启动后 PAUSED；浏览器切至 LIVE 不能启动未启用的实盘，不能填写虚拟实盘资金。
4. 查看数据卷存在 `TEST/state.sqlite`、`LIVE/state.sqlite`，模式互不共用。
5. 在 PAUSED 状态记下配置与资金，用 `docker compose restart` 重启，确认同库配置/资金一致、仍为 PAUSED。
6. 已有仓位时需验证目标、止损、应收与交易记录都恢复，先在开发 TEST 目录验证，不借此擅自启动正式长期活动。

控制 API 需要登录和 `x-pm-csrf`，令牌来自已认证 GET 响应头，浏览器自动处理。不要把未认证 POST 能否成功当作健康检查。正式长期 TEST 应在整体验收后由用户点击 START；PAUSE 只停止新买，保留已有仓位退出和回款。

## 5. 备份、升级、回滚

停机备份可确保两个模式与 SQLite WAL 一致；LIVE 已启用时先 PAUSE 并确认所有在途买单终态，持仓退出中应选择维护时机：

```sh
cd /opt/pm-nautilus
docker compose stop
mkdir -p backups
tar -czf backups/state-$(date +%Y%m%d-%H%M%S).tgz runtime/server .env
git rev-parse HEAD > backups/previous-revision.txt
docker compose start
```

备份含身份信息，必须限制访问与加密存放，不能上传 Git。备份时不要把运行中的单个 `.sqlite` 拷贝而遗漏 WAL。

升级：记录当前 SHA和镜像标签 → PAUSE → 停服务并备份 → 拉取已验证提交 → 更新 `.env` SHA → build/up → 检查健康与同库资金。回滚：停新版本 → checkout 上一个 SHA → `.env` 改回对应镜像标签 → 用该版本启动。数据库格式变更必须先验证兼容；不能拿 TEST reset 解决升级问题。真实钱包发生交易后，严禁恢复旧 LIVE 账本直接启动：必须把旧快照与最新真实回报核对，避免遗失已发生的权利或重复赎回。

## 6. TEST 操作

- 配置通过 UI 保存；新买使用新配置。已有周期的预算、止损参数冻结，已有目标不追溯改变。
- 初始资金只能在 PAUSED、无交易历史时改。盈利不会自动扩大每轮金额。
- 重置需 PAUSED 下点击两次确认；只清 TEST 数据并恢复100U/1U等原始默认值，LIVE不受影响。
- 长时间无成交先看公开扫描、盘口与筛选条件，不能用强制下单验证网络。无买盘估值0，未知盘口估值显示未知。
- 小于交易所最小卖出量的残余仓位不能伪造卖出；保留并等待可合法退出或正式结算。

## 7. 后续 LIVE 配置与启用

本次未读取真实钱包、未签署真实订单、未进行 approve/redeem 或其他链上写。未来由用户完成测试验收，确认实际钱包类型及独立资金账户，再明确启用。

实现支持 Polygon 主网 EOA（signature_type=0，signer=funder）和单签 Safe（signature_type=2，signer 是 owner、threshold=1）。Magic/PolyProxy、Deposit Wallet、多签 Safe 不能冒充这两种路径；若实际账户属于其他类型，须按其官方路径补齐并验证后再启用。当前钱包类型尚未提供。

凭据文件示例结构如下，所有值仅在服务器本地填写；文件权限600、属主为服务运行用户：

```json
{
  "private_key": "在服务器填写",
  "api_key": "在服务器填写",
  "api_secret": "在服务器填写",
  "passphrase": "在服务器填写",
  "funder": "实际资金账户地址",
  "signature_type": 0,
  "rpc_url": "实际Polygon RPC",
  "auto_approve_redemption": false
}
```

CLOB V2 客户端、当前 pUSD、标准/neg-risk 抵押适配器地址固定在所选实现。EOA 需备 POL 支付手续费；Safe 由受支持 owner 支付外层交易手续费。启动前只读核对链ID、合约代码、资金账户与 Safe 权限。CLOB 下单授权需要账户预先完成；赎回授权未满足时保留应收并显示原因。只有用户明确启用 LIVE 且将 `auto_approve_redemption` 设为 true，后台才会为自有赎回所需适配器提交授权。

如使用 Compose，将 `deploy/compose.live.yaml.example` 复制到仓库根目录为本地 `compose.live.yaml`，将服务器凭据文件只读挂载到容器，把环境变量明确设为 `PM_LIVE_ENABLED: "true"`、`PM_LIVE_CREDENTIALS_FILE: /run/secrets/live.json`，保持 TEST/LIVE 目录分离。主 `compose.yaml` 永远保留 false；实际挂载文件权限需允许 UID10001读取且其他用户不可读。不要将该本地覆盖文件或凭据提交仓库。

启动 LIVE 上下文仅连接和核对；状态仍 PAUSED。用户切换 LIVE 视图并按 START 才允许真实新买。未完成核对、未知订单、资金/份额不一致均阻止新买。PAUSE 后目标退出、止损和赎回继续；不得通过关掉 LIVE 门来代替安全暂停现有仓位。

自动赎回分为 IDENTIFIED → SUBMITTED → CONFIRMED → CREDITED；TEST先模拟确认，再模拟入账。LIVE提交前保存交易哈希、nonce与同一签名原文；重启仅查询或重发同一交易，不创建新身份。链上收据需成功且至少32块确认，核对pUSD回款与自有权利后刷新实际账户余额。应收不占可下单现金。同 Condition 若存在额外手动资产，整Condition赎回会影响它们，因此自动赎回拒绝该混合权利，等待人工核对，不接管手动资产。

## 8. 故障排查

| 现象 | 检查和恢复 |
|---|---|
| 401 | 使用账号pm和服务器UI密码；不要关闭认证绕过 |
| 403 控制来源失败 | 刷新当前同源页面，检查反代Host/Origin；不要去掉CSRF |
| 无可执行盘口 | 检查公网出站、WS连接、扫描错误；重连需完整快照 |
| 有行情无成交 | 检查总时长、进度、费用元数据、最小量、Bid/Ask比例、兄弟完整性与预算 |
| 业务投影失败 | 保持PAUSED，检查磁盘/SQLite错误，备份后重启重放；不要手改cursor |
| LIVE核对失败 | 保持PAUSED，检查真实订单/余额/自有份额；临时网络恢复后重新核对，就绪恢复不自动START |
| 赎回SUBMITTED很久 | 查询保存的tx_hash和nonce、RPC与POL，不删除跟踪或手动重复提交 |
| 赎回FAILED | 核对失败收据/余额/混合权利；不要自动重置成未提交 |
| 重复实例错误 | 找到占用同目录的进程，保留唯一实例；不要绕过process.lock |

清理本次 Linux 验证虚拟机：工作区 `.reference/lima/bin/limactl`，实例 `pm-verify`，`LIMA_HOME` 为工作区 `.reference/lima-home`。验证结束停机；保留文件便于复验，不作为生产服务使用。

未来已明确授权 LIVE 后，使用 `docker compose -f compose.yaml -f compose.live.yaml config --quiet` 检查合并配置，再以相同 `-f` 参数执行 build/up。未启用时只使用主 compose.yaml。
