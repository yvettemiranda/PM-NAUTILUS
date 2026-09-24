# 从新电脑、新服务器开始首次实盘

这份说明适用于：换了一台电脑、买了新服务器，**不要以前的 TEST 模拟成交记录**，准备把自己用过的钱包首次接入本程序。它不是已有实盘订单或持仓时的迁移办法；那种情况见本文最后一节。

## 将来直接发给 Codex 的话

> 请打开 GitHub 仓库 `https://github.com/yvettemiranda/PM-NAUTILUS`，先读 `README.md`、`docs/START_FRESH_LIVE.md`、`docs/DEPLOY.md`、`docs/REINSTALL.md` 和最新 `HANDOFF.md`。我现在有新电脑和新服务器，打算丢弃旧 TEST 模拟成交，从空白记录安装并首次接入我自己用过的 Polymarket 钱包。请先检查 `config/strategy-profile.json` 是否仍是我想用的设置，在第一次启动应用前把它导入新服务器的 TEST 和 LIVE，再验收 TEST + PAUSED 并在网页复核。然后用 age 加密文件和人工解锁方式带我完成钱包接入、只读预检与 LIVE 规则核对。请让我只在自己信任的终端输入私钥和口令，不要让我贴进聊天、GitHub 或网页。检查新服务器地区、钱包原有订单与持仓、余额和授权；不满足条件就停在检查阶段。最后先让我看到 LIVE + PAUSED 的检查结果，由我决定是否在 LIVE 页面按 START。若旧服务器仍在运行，先确认它已停止，不要让两个实例同时使用这个钱包。

以后可以向 Codex 提供**新服务器的管理入口**和你愿意公开核对的签名地址、资金地址；不要在聊天里提供钱包私钥、加密口令或 API 凭据。你自己还须能在可信设备上恢复该钱包的可导出私钥；GitHub 无法替你找回它。若仍有旧服务器，告知它是否运行过真实交易。

## 先弄清 GitHub 能恢复什么

GitHub 保存程序、安装说明，以及你已同意公开保存的交易设置文件 `config/strategy-profile.json`。它**不保存**服务器的模拟成交、真实交易记录、密码、钱包私钥或加密凭据。

- 当前旧服务器上的 **TEST 模拟成交记录可以丢弃**。在新服务器从空白目录安装即可，不必恢复当前的模拟账本或其备份。
- **你调过的交易设置是另一回事。**`config/strategy-profile.json` 用来保存你同意公开的当前交易设置。换电脑后先检查这份文件是否真的在所下载的版本里，以及它是否还是你想用的最新设置；旧服务器后来改过设置而没有更新 GitHub 时，它就会过期。Codex 会把确认后的设置分别保存到新服务器的 TEST 和 LIVE，再读回来与文件逐项核对。文件只包含交易设置，不会恢复模拟资金、成交或持仓；若文件缺失或已过期，先重新确认设置，不要把程序默认值当成你现用的规则。
- 这个钱包以前用过。新程序不会把你以前手工开的订单、持仓自动变成本程序的记录；首次接入时仍须检查钱包里的旧挂单和持仓。旧模拟账本可以不要，不代表钱包现状可以跳过检查。

## 你实际会经历的五个阶段

1. **准备新服务器和钱包恢复方式。**你提供新服务器的管理入口，并确认自己还能在可信设备上取得所选钱包的私钥。Codex 核对新机器，随后从 GitHub 下载已验证的程序。新电脑是管理入口，不必在它上面长期运行交易程序。
2. **恢复并核对交易规则。**Codex 先请你确认 GitHub 中的 `config/strategy-profile.json` 仍是想用的版本，再在空白账本首次启动前，将它分别写入 TEST 和 LIVE。设置网页密码后启动并验收 `TEST / PAUSED`、`LIVE` 关闭。你在网页看两边的市场类别、价格、周期、每 Event 每轮金额、目标卖出和止损；两边各自保存，不能认为 TEST 改好了，LIVE 就自动一样。文件缺失或过期时先重新确认设置。这里沿用程序现有规则，不额外规定首轮总投入、单笔金额或试单次数。
3. **在自己的电脑上制作加密钱包文件。**在可信终端亲自输入私钥、资金地址和加密口令；如果没有现成的交易所 API 凭据，可用仓库脚本通过锁定的官方 SDK 创建或派生。只把生成的**加密文件**传到服务器，再人工解锁到服务器的临时内存目录。网页、聊天和 GitHub 都不接收私钥。服务器运行实盘时必须临时持有可签名的凭据；关机后解锁文件消失。步骤见 [部署手册第 7.1 节](DEPLOY.md)。
4. **检查钱包与新服务器。**Codex 先运行不会下单的账户预检，核对签名地址、资金账户、余额、手续费币、授权、原有挂单和程序账本；再单独核对钱包原有持仓，以及新服务器出口是否允许新开仓。预检通过只是进入最后核对，不保证真实订单或赎回已验证。若这个钱包有手工持仓，它们仍由你自己管理；程序不会直接接管。检查不合格时先解决原因，不启动自动买入。
5. **最后由你决定开始。**Codex 用明确的 LIVE 配置启动服务，再核对页面确实显示 `LIVE / PAUSED`、资金与规则正确、没有未处理的异常。你在 LIVE 页面按 **START** 后，才允许程序按现有规则真实买入。切换到 LIVE 页面或启动服务本身都不会自动开始新买入。

首次实盘后请备份**新的 LIVE 账本**和加密凭据，并把解锁口令与密文分开保管。此时真实记录已经有用：程序依赖它识别和管理自己开的单、持仓与结算。按 **PAUSE** 只停止新买；已有仓位的卖出、止损或赎回仍可能继续。不要把 PAUSE 理解成完全停止使用钱包。

## 给协助部署者的核对清单

本节供 Codex 或技术协助者执行；你不用自己背命令。以下步骤以[部署手册](DEPLOY.md)的实际命令和当前服务器环境为准。

1. 核对新服务器是受支持的 Linux 架构，装好 Docker/Compose；确认目标目录为空、没有另一实例使用同一钱包或数据目录。克隆仓库并固定经验证的完整 Git SHA。`/opt/pm-nautilus` 只是示例路径，权限和反向代理设置须按新机器处理，不能直接照抄旧服务器的覆盖文件。
2. 创建新的 `runtime/server` 与本机 `.env`，设置 UI 密码、允许访问的主机和准确的 `PM_GIT_REVISION`。**先构建，不启动应用。**检查目标 Git 提交里确实有 `config/strategy-profile.json`，用当前代码的 `Preferences` 模型验证其恰有 15 个规则字段，并请用户确认它仍是想用的版本；存在文件不等于最新。缺失、无效或过期时先修正文件并重新核对，不要静默采用程序默认值。
3. 在应用第一次启动前，用仓库的 `scripts/apply_preferences.py` 将已确认的文件导入两个**空白**账本，然后启动基础 TEST 服务。如下命令在新服务器仓库目录运行；`compose.override.yaml` 只在这台服务器确实配置好本地反代时才加入。脚本遇到已有交易、扫描数据、改过的账本或正在运行的应用会拒绝；绝不可用它覆盖以后产生的 LIVE 记录。

   ```bash
   compose_args=(--env-file .env -f compose.yaml)
   if test -f compose.override.yaml; then compose_args+=(-f compose.override.yaml); fi
   docker compose "${compose_args[@]}" config --quiet
   docker compose "${compose_args[@]}" build
   docker compose "${compose_args[@]}" run --rm --no-deps --entrypoint /app/.venv/bin/python app -c 'from pathlib import Path; from pm_nautilus.store import Store; root=Path("/data"); [Store(root/m/"state.sqlite",m).close() for m in ("TEST","LIVE")]'
   docker compose "${compose_args[@]}" run --rm --no-deps -v "$PWD/scripts/apply_preferences.py:/tmp/apply_preferences.py:ro" -v "$PWD/config/strategy-profile.json:/tmp/strategy-profile.json:ro" --entrypoint /app/.venv/bin/python app /tmp/apply_preferences.py --data-dir /data --profile /tmp/strategy-profile.json
   docker compose "${compose_args[@]}" up -d
   docker compose "${compose_args[@]}" ps
   curl -fsS http://127.0.0.1:8765/api/health
   ```

   核对健康接口为 `TEST / PAUSED`、`liveExecutionEnabled=false`、代码 SHA 正确。再在已认证网页查看 TEST 和 LIVE 的设置，或读取 `/api/TEST/preferences` 与 `/api/LIVE/preferences`；**只比较规则模型的 15 项**，用 `Preferences` 模型比较数值而非小数字符串外观，因为网页接口另带展示用字段，并可能把文件中的 `"1"` 显示成 `1.0`。TEST 的 100U 初始模拟资金与规则文件分开；导入工具按程序现有规则拒绝每轮金额超过这笔初始资金。LIVE 资金由真实账户提供，不写入文件。不要复制旧 TEST 模拟订单、资金或持仓。最后安装适合实际 Compose 文件组合的重启后默认 TEST 单元，见[部署手册第 7.2 节](DEPLOY.md)。
4. 用公开地址运行只读链上检查，核对实际钱包类型与资金账户。随后在可信新电脑按[部署手册第 7.1 节](DEPLOY.md)创建 age 密文；没有 CLOB L2 凭据时使用 `--derive-api-credentials` 和项目锁定依赖。签名私钥由用户在本机交互输入，不能进入命令参数、shell 环境、聊天、网页或 Git；若私钥无法导出，不要假称现有本地签名流程可以直接使用。
5. 把密文安装在服务器受限目录，人工解锁到 `/run/pm-nautilus/live.json`，核对权限。复制 LIVE Compose 样例为本机覆盖文件只用于配置检查和一次性私有预检；**只有预检与后续条件满足，才用这份覆盖文件重建常驻服务**。一次性私有预检须对照用户确认的公开签名地址和资金地址，核对退出码及输出；`0` 只表示它检查的条件通过。另从**新服务器实际交易出口**请求 [Polymarket 官方地区检查接口](https://docs.polymarket.com/api-reference/geoblock) `GET https://polymarket.com/api/geoblock`，确认 `blocked=false` 且所在地允许 API 新开仓；该检查不是私有预检的一部分。单独核对钱包原有持仓、余额、授权和 LIVE 设置；不要用 LIVE + PAUSED 代替只读预检。
6. 用基础 Compose、实际需要的本机覆盖及 LIVE 覆盖启动；确认健康接口的 `liveExecutionEnabled=true`，认证后的 LIVE 页面显示 `PAUSED`，实际账户、设置和账本验证正常，再将 START 留给用户。实际下单、成交、退出与赎回仍需在后续运行中观察和验收，不能由模拟测试推断为已成功。

## 以后已有真实交易，再换电脑或服务器

**只换电脑、不换服务器：**从 GitHub 取得程序和说明即可继续管理原服务器，另行恢复自己的服务器登录方式；无需为了管理网页而把钱包私钥复制到新电脑。若要重新制作凭据，仍按本地可信终端流程操作。

**换服务器且已有本程序的真实订单、持仓或待结算权利：**不能按本文“丢弃旧记录”从空白 LIVE 账本直接启动。先停止旧实例，在停止状态保存和验证**最新 LIVE 账本**及加密凭据，再按[部署手册第 5 节](DEPLOY.md)迁移并与真实账户核对；确保旧服务器不再同时交易。重启或迁移时开机默认 TEST，新服务器不会自动继续管理已有 LIVE 仓位，须人工解锁并重建 LIVE，然后再核对与恢复运行。
