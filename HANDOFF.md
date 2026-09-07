# PM-NAUTILUS 接手记录

## 当前有效范围与状态
完整执行根目录 PM-SMALL_Nautilus_Codex_Instructions.md。独立新应用、新账本，只管理本程序交易；保留原UI及交易规则；实现TEST、LIVE接入、自动赎回。交付默认TEST + PAUSED，LIVE未启用。未导入旧账本或钱包，未做真实签名/下单/approve/redeem/链上写入，未启动正式长期TEST。

## 已完成
- 原项目HEAD/main核对为 eb8c6d8a09f9b0427890b7a2d744fc3485d1d3dc。只读参考 .reference/PM-SMALL 排除Git；有效文档及关键代码已完整核对，原324测试在当前环境全部通过。
- 选定NautilusTrader 1.231.0（源码27a8e54e7ac3c57d6cbf8891f0283dfbaee97317），对比过2.0.0rc4；Python3.12.14、uv0.8.22、精确依赖与uv.lock已固定。
- docs/MIGRATION.md提供整体设计及逐条旧来源/新模块/职责/验证表。框架承担Strategy、DataEngine、RiskEngine、ExecutionEngine、Cache、Portfolio；应用补充PM的Event仲裁、周期、目标、止损、费用与赎回权利。
- 全分页Gamma、公开WS、静态资格、FAK、六级仲裁、共享深度、首Fill预算/止损冻结、逐Fill目标、30秒止损、暂停/重置/模式隔离均已接通原UI/API。
- 原生事件唯一成交事实源；先持久化事件再投影，带Fill当时规则快照。崩溃重放恢复预算/目标/消费；LIVE临时额度和Event互斥持续到真实确认终态。
- LIVE使用所选官方CLOB V2客户端及LiveExecutionEngine扩展；现金FAK与合法tick、不提高预算、提交前确定性身份、CONFIRMED-only真实回报、自有归属与重启核对、实际余额接通。
- 自动赎回覆盖标准/neg-risk pUSD、EOA/单所有者Safe、实际权利核对、提交前raw/hash/nonce持久化、同身份重查/重播、32块及Transfer/烧毁核对。到账标记和实际现金原子提交，未本地归账时不重复加总资金；本地结算意图重启可继续。
- 两处旧深度缺陷已在原处理器+数据库实际复现；新逐价位消费修复且回归验证。多档FAK记录按订单归组，各笔实际Fill与目标仍独立。
- 真实公开行情短测产生14笔模拟Fill、10持仓，现金4.37U，同库重启核对通过，PAUSED。独立临时目录和明确开发设置，不是产品默认或正式活动。
- Mac完整68测试通过；Linux aarch64/bookworm生产依赖实际构建，68测试通过。代码验证提交 e91563f5723609f126ed540d9bd3d3d36f795de6；之后仅文档交付提交。
- Linux生产base镜像通过非root/只读rootfs、认证/API/HTML/JS、健康和同库重启：2U设置保留、100U现金不变、同generation、LIVE独立1U；最后恢复TEST每轮1U，始终PAUSED。
- 浏览器实测320px配置/持仓页无横向溢出、桌面布局、草稿保存、START/PAUSE、LIVE余额只读与模式配置隔离、21项展开和排序、记录读取。临时视口已恢复。
- Dockerfile、Compose、CI、环境样例、Nginx样例、未来LIVE覆盖样例、许可证、Mac/上传/部署/备份/更新/回滚手册齐备。

- 2026-09-07 从实际公开仓库克隆至新目录，独立下载Python3.12.14并重建虚拟环境；68测试、Ruff和JS语法检查通过。离线服务健康为TEST/PAUSED/LIVE=false，HTML/JS可读，验证后停止。详见docs/VALIDATION.md与REINSTALL.md。

## 本次恢复核验（2026-09-07）
- 在 `/Users/d4clt/Documents/Codex/2026-09-07/zhe/PM-NAUTILUS` 完成真实 Git 克隆；`origin` 为 `https://github.com/yvettemiranda/PM-NAUTILUS.git`，本地 `main`、`HEAD` 与远端 `main` 均为 `cf910d25aedb380ab88e45a104ed057a525d715d`。克隆后工作树干净，`git fsck --no-dangling` 通过；随后仅维护本文件，未提交或推送。
- 本机为 macOS 26.6.2 arm64，已安装 Apple Command Line Tools 26.6（Git 2.50.1）；按 REINSTALL 使用 Python 3.12.14、uv 0.8.22 和 `uv sync --frozen --extra dev` 重建 `.venv`（75 个锁定包）。Ruff check/format 通过；当前真实工作树 `pytest -q` 为 68 项通过、2 项既有依赖弃用警告（1.77 秒）。已使用校验过的 Node 24.20.0 执行 `node --check src/pm_nautilus/web/app.js`，通过。
- 显式 `PM_LIVE_ENABLED=false` 的离线服务验收返回 `mode=TEST`、`strategyStatus=PAUSED`、`liveExecutionEnabled=false`、100U/每轮1U，首页和 API 可读后已 SIGINT 停止。本机没有 `.env`、真实凭据或 LIVE 启用；未进行真实签名/下单/approve/redeem/链上写入。
- 随后在用户确认的独立 Ubuntu 24.04 amd64 服务器上，以固定 SHA `cf910d25aedb380ab88e45a104ed057a525d715d` 部署主 Compose。服务器 `.env` 为本机生成、`0600` 保护且不记录密码；容器只监听 `127.0.0.1:8765`，Nginx 仅通过 HTTPS 反代，HTTP 不承载应用而是跳转 HTTPS（ACME 验证路径除外）。保留服务器本地 `compose.override.yaml`：它仅信任回环和 Docker 网桥的转发来源，使 HTTPS 控制请求保留正确协议；已用不启用 LIVE 的 POST 验收 CSRF/Origin 路径返回预期 400。无邮箱签发的短期 IP 证书及 Certbot 定时续期/成功后重载 Nginx 均已配置并演练通过。三地独立公网 HTTPS 探测均为 200；服务器健康检查、Basic 认证页面和容器重启后均确认 `TEST`、`PAUSED`、`liveExecutionEnabled=false`。未启用 LIVE，未接触钱包或链上写入。地址、密码、证书序列号等敏感运行细节不写入交接文件。

## 扫描与分类线上修复（2026-09-07）
- 用户页面的“扫描失败”不是网络或筛选规则问题：旧 Gamma `GET /events` 的 offset 分页在 `offset=2100` 必定返回 HTTP 422。扫描循环因此在第 22 页中断，保留旧结果；首页分类也仍依赖主站的 filtered tags 路径。
- 本地提交 `ce96a05bc6ddbaabd6f902f232bcc118bf8326fe` 将正式扫描改为官方 Gamma `/events/keyset` 的不透明 cursor 分页，校验 wrapper、事件前进和 cursor 前进；分类改为 Gamma related-tags 接口（`status=active`、`omit_empty=true`），并在成功同步后清除过期 `categoryError`。公开行情短测也同步迁移至 keyset。
- 回归结果：Ruff check/format、前端 JS 语法检查均通过，`pytest -q` 为 69 通过（2 个既有第三方弃用警告）。真实公开行情短测抽取 100 个事件、选中 10 个、监控 20 个 Token，模拟成交及重启核对均通过，结束后仍为 TEST + PAUSED，私有连接数为 0。
- 本机没有 GitHub HTTPS 推送凭据，因此该提交尚未推送至 `origin`（公开远端仍在 `cf910d25aedb380ab88e45a104ed057a525d715d`）。已将经过本地校验的精确 Git bundle 导入服务器，并将服务器检出、镜像标签和 `PM_GIT_REVISION` 都固定为 `ce96a05bc6ddbaabd6f902f232bcc118bf8326fe`；服务器的 `compose.override.yaml` 保持未改。以后取得该仓库的授权推送凭据后，先核对远端是否快进，再推送这两个本地提交，绝不强推。
- 部署后容器 healthy；实际首次扫描完成且 `lastError=null`、`categoryError=null`、14 个分类、10,922 个监控 Token、46 个可交易事件；随后一轮定时全量扫描同样完成且所有扫描/服务/行情流错误字段均为空（公开市场数据会动态变化）。Chrome 页面已实测显示“扫描完成”、候选市场列表、14 个市场类别及其下方筛选项。带有效认证、CSRF 和同源头的 `POST /api/live/start` 明确返回 400 “LIVE尚未在服务器配置并明确启用”，因此没有启动 TEST 或 LIVE，亦未触碰钱包、签名、下单、approve、redeem 或链上写入。

## 交付与继续入口
- README.md：启动入口；docs/DEPLOY.md：完整操作步骤；docs/VALIDATION.md：结果、覆盖与未验收部分。
- artifacts/FULL_SOURCE.md：所有受控文本文件完整内容（含锁文件，无省略）；SOURCE_MANIFEST.json逐文件SHA256及最终提交。
- artifacts/PM-NAUTILUS-source.zip为完整源码；PM-NAUTILUS.bundle保留分段Git历史。以git log及manifest为最终交付版本，不把代码验证SHA误认为后续纯文档提交SHA。
- .reference/下保存原项目、公开短测、缺陷复现及Linux证据，不入Git。runtime/ui-preview、runtime/ui-acceptance为开发样本，不作为正式账本。
- 临时UI服务已在确认PAUSED后SIGTERM停止；Linux验收容器已healthy验证后停止，临时Lima pm-verify已确认停止，保留镜像/卷供复验，不作为生产服务。

## 发布状态与后续外部事项
1. 2026-09-07 用户明确授权新建公开仓库 yvettemiranda/PM-NAUTILUS 并上传，覆盖原指令的拟定私有安排。公开仓库已创建并推送完整 main 历史：https://github.com/yvettemiranda/PM-NAUTILUS 。GitHub Actions run 34071046061 在 Linux x86_64 与 ARM64 均通过68测试、前端语法检查及Docker verify构建。恢复指南见 docs/REINSTALL.md，不再询问首次上传授权。
2. 2026-09-07 用户提供并确认独立服务器后，已完成固定 SHA 的 TEST 部署；公网入口为 HTTPS IP 反代，应用内部端口未公开，HTTP 不承载 Basic 认证。服务器本地 `compose.override.yaml` 是 HTTPS POST 的必要信任边界，更新时保留并用禁用 LIVE 的 POST 复验 CSRF/Origin。IP 证书为约六天的短期证书，Certbot 定时续期和 Nginx 重载钩子已通过 dry-run 演练。继续维护时先核对 `certbot renew` 定时服务、Nginx 和 Compose 健康；不得改为明文 HTTP 或启用 LIVE。未停止/更改任何旧项目或旧服务。
3. 实际LIVE钱包类型、服务器本地凭据、RPC、授权及资金未提供；当前代码支持EOA和单所有者Safe，其他类型须补对应路径。真实钱包/链上验收另行明确授权，不把可控测试当真实成功。
4. ~/.codex/templates/agent_memory/仍不存在。按用户AGENTS要求不能伪造模板结构，因此只建立agent_memory/archive/，暂由本文件承载当前上下文/进度/风险。模板提供后原样建立三个.md再填当前事实。

## 复验命令
```sh
cd /Users/d4clt/PM-NAUTILUS
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/pytest -q
.reference/node-v24.20.0-darwin-arm64/bin/node --check src/pm_nautilus/web/app.js
```

Linux临时环境使用 `.reference/lima/bin/limactl`，LIMA_HOME=$PWD/.reference/lima-home，实例pm-verify；Compose生产部署流程见DEPLOY。所有重启默认PAUSED。中断恢复先读本文件、MIGRATION/VALIDATION/DEPLOY、git status/log；不要重新设计已确定规则。外部条件补齐后按根指令流程推进，保持实盘未启用。
