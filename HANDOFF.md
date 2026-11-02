# PM-NAUTILUS 当前交接

## 版本与仓库入口（2026-10-30 更新）

只维护两个入口：

| 入口 | 含义 |
| --- | --- |
| [`main`](https://github.com/yvettemiranda/PM-NAUTILUS/tree/main) | 当前十账户最新版及现行文档，默认开发和下载入口。 |
| [`baseline/pre-test-10-accounts-20261027`](https://github.com/yvettemiranda/PM-NAUTILUS/tree/baseline/pre-test-10-accounts-20261027) | 十账户改造前的固定源码 `4de107a4dc1a4abaaf08b330eb00803391268e2a`，不继续修改。 |

当前已部署应用为 `7d47f50cbacba33d603ac1f7ed1370c8171f8337`（2026-11-02，见下节）；此前为 `8eafbeb06792accd85a555f99b01ee61be369838`、`8d13d084544b1d94d558c52f03b0bc1931bd0946`、`ff5da9bb6722eb0e5a8925140631a154af7c4c9e`（同日），再前为 `99eada51405358ec82b7b5bc6329c3141c1a8d04`（2026-11-01 第二次部署），同日上一版为 `6ae9fcaadff86db9405d22adf95b5e7677d01cce`，再前为 `8ab1937e84b1f63db3db46a07eca8a4d17876a58`。在此前启停、异常关闭、钱包取消和历史分批修复之上，本轮仅更新 HTML／CSS 界面，见 [Hallmark 界面说明](docs/archive/UI_HALLMARK_20261030.md)。此前业务保护保留，见 [修复说明](docs/archive/EGM_FIXES_20261030.md)。后续验收文档提交仅改 Markdown，服务器源码可同步到该文档提交；运行镜像及环境版本仍固定为上述应用 SHA，不为同步文档再次重启。

旧工作分支和重复基准标签已从 GitHub 入口移除，旧 PR #18 已关闭，未将它的原地重签方案合入当前交易逻辑；相关问题已经由后续 PR #19 的终结未提交意图及重新评估机制覆盖。提交历史继续保留。后续操作以 `main` 和完整应用 SHA 为准，旧部署脚本中的历史分支名不再作为拉取入口。规则见 [版本维护](docs/VERSIONS.md)。

## 最近一次部署：2026-11-02（7d47f50，恢复串行最终账户核对）

- Codex 复核指出：8eafbeb 的 50 ms 判定以整个账户检查的结束时间为准，较慢的份额查询会掩盖已过时的挂单查询（挂单查询很快返回、签名期间出现外部挂单、份额查询稍后结束→不重查）。成立。已删除该判定：钱包检查与签名仍并行（批量 RPC 保留），完整账户核对（挂单 + 份额）在两者都完成后执行，之后再做计划/控制复核，与提速前顺序一致。代价是账户核对的约 1—2 次往返不再与签名重叠。
- 其他：部署脚本加互斥锁（同一时间只允许一个部署）；服务器外备份失败时删除 `.partial` 并清理旧残留；其余文档中的“当前已部署版本”统一指向本文件；部署手册约定部署期间不操作网页。应用层维护锁暂缓。
- [CI 41399599027](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/41399599027) 双架构通过（各 1,317 项）。部署前快照 `pre-deploy-7d47f50`（lhsnap-j5pykcyd，14:35）；备份 `/var/lib/pm-nautilus-deploy/20261102T063635Z-7d47f50/`；停机前 TEST 1、2、3 RUNNING（持仓 30/42/53），4—10 PAUSED；健康 ok、修订 7d47f50、10 个账户、后台错误为空；恢复后 10 个账户状态一致，行情全部就绪。删除镜像 ff5da9b（保留 7d47f50 与回滚 8eafbeb、8d13d08）和旧快照 `pre-deploy-8eafbeb`；磁盘 30%。

## 2026-11-02 部署（8eafbeb，审计修复）

- 依据：独立审查与 Codex 审计。采纳：
  - 部署脚本拒绝有未提交或未跟踪文件的程序目录，检出后再核对 HEAD；目标提交须在 `origin/main` 上，且 GitHub 对该 SHA 的全部检查（两种架构）通过，否则须显式 `--allow-unverified`；LIVE／钱包安全检查移到停机前最后一步（账户查询之后）。
  - LIVE 买入：并行的账户核对如果比其他准备早结束超过 50 ms（签名或钱包检查较慢），签名后重做一次，最终挂单／份额门禁不比原先串行时旧；账户核对出错时一律撤销两个就绪标记；签名前后均拒绝低于 1U 的金额。
  - 服务器外备份失败路径也做有界清理（未启用）。
  - 文档：删除“TEST 与 LIVE 并行对比”的建议（违反互斥），统一当前版本说明（以本文件为准），修复归档中 46 处断链；部署脚本测试仅在 Linux 运行。
- 未采纳：应用层“维护互斥锁”。停机前最后一步复查后只剩毫秒级窗口，且部署由你本人执行，收益不抵改动。
- 1U 规则改变 TEST 结果属有意为之：LIVE 必被交易所拒绝的买单，TEST 不应成交。
- [CI 41395563254](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/41395563254) 双架构各 1,309 项通过。部署前快照 `pre-deploy-8eafbeb`（lhsnap-lo2894sf，13:54）。新脚本首次实跑：干净检查通过，“GitHub CI（此提交）: 2/2 项检查通过”；停机前 TEST 1、2、3 RUNNING（持仓 30/42/51），4—10 PAUSED；备份 `/var/lib/pm-nautilus-deploy/20261102T055515Z-8eafbeb/`（12 个文件，`.complete`）；健康 ok、修订 8eafbeb、10 个账户、后台错误为空；恢复后 10 个账户状态一致，行情全部就绪。
- 清理：删除镜像 99eada5（保留 8eafbeb 与回滚 8d13d08、ff5da9b）和旧快照 `pre-deploy-8d13d08`；磁盘 28%。

## 2026-11-02 部署（8d13d08，LIVE 买入延迟优化）

- 原则：安全优先，每笔买入的每项检查照做、不缓存任何结论，只减少等待。
- 钱包检查批量化：链 ID、官方合约/签名账户/资金地址代码、Safe 所有者与门限、全部 CTF 授权合成一次 JSON-RPC 批量请求（原先 Safe 钱包需 4—6 次往返）。校验函数与报错文字不变；批量结果有效即为最终结论。RPC 不支持批量或批量任一项失败时，逐项重新读取并以其结果为准；连续两次“批量失败而逐项成功”后改为只走逐项读取（重启后重试）。
- 同 Condition 份额：先读固定区块号，再一次批量读取该区块的余额（原先每个余额还要额外查一次链 ID）。
- 账户核对（开放挂单 + 份额）与钱包检查、签名三者并行。三项都必须通过；计划/控制复核仍在三项全部完成后、账本闸门和 POST 之前执行。
- 新测试：真实 Web3 对本地 JSON-RPC 服务（一次往返、相同结论、回退、注入延迟）以及 LIVE 三项并行（注入延迟，任一失败都不 POST）。[CI 41386349887](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/41386349887) AMD64/ARM64 通过，1,297 项。
- TEST 模式不涉及链上与 CLOB 账户读取，交易决策逻辑未改，TEST/LIVE 一致性不变。
- 部署前快照 `pre-deploy-8d13d08`（lhsnap-ofn5tc9n，12:15）。一键脚本全程自动完成：停机前 TEST 1、2、3 RUNNING（持仓 30/42/51），4—10 PAUSED；备份 `/var/lib/pm-nautilus-deploy/20261102T041659Z-8d13d08/`（12 个文件，自检通过）；健康 ok、修订 8d13d08、10 个账户、后台错误为空；恢复 1、2、3 后全部 10 个账户状态与停机前一致。
- 清理（12:30）：GitHub 删除已合并的 `review/live-latency-20261102`，仅保留 `main` 与基准分支 `baseline/pre-test-10-accounts-20261027`（CI 历史作为追溯资料保留）。服务器删除 10 个旧的手动停机备份目录（`/var/lib/pm-nautilus-{egm-fixes,readonly-audit,review,review-…b,test-*,ui-hallmark,universal-test-optimization}`，均早于脚本备份）、`/home/ubuntu` 下两个验证目录、`/tmp` 旧脚本与日志；旧镜像只留当前 8d13d08 与两个回滚镜像 ff5da9b、99eada5（与存储维护规则一致），删除 verify 镜像和 hello-world，清理 24 小时前的构建缓存；云端删除旧快照 `pre-deploy-ff5da9b`。保留：`/var/lib/pm-nautilus-deploy/` 两份脚本备份、`/var/lib/pm-nautilus-maintenance`（存储维护定时任务状态）、快照 `pre-deploy-8d13d08`。磁盘占用 25G→12G（26%），应用健康。
- 独立审查（12:35，另一检查程序只读审查今天全部改动）：未发现可在检查失败、出错、跳过或过时情况下下单的路径。修复 `53783a9`（[CI 41390416125](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/41390416125) 通过）：部署脚本停机前再查 LIVE/钱包、拒绝非 main 提交、只清理带 `.complete` 标记的完整备份；服务器外备份跳过任意层级的 `.pm-*` 临时目录；LIVE 买入在账户核对出错时总是撤销两个就绪标记，签名前后都拒绝低于 1U 的金额。脚本已同步到服务器（无需重启），现有两份备份已补 `.complete` 标记；LIVE 部分随下次部署生效。

## 2026-11-02 部署（ff5da9b）

- 内容：一键部署脚本 `deploy/deploy.sh`、可选的每日 COS 服务器外备份 `deploy/offsite-backup.py`、精简后的 [部署手册](docs/DEPLOY.md)、`app.py` 中无状态的网页安全与钱包记录校验移到 `web_security.py`、`live_wallet_records.py`（行为不变）。交易逻辑未改。CI 1,277 项通过。
- 部署前在腾讯云控制台创建手动快照 `pre-deploy-ff5da9b`（lhsnap-8bducs9f，2026-11-02 11:03，状态正常）。
- 首次使用部署脚本：预检、构建、停机前记录（TEST 1、2、3 RUNNING，持仓 28/40/51；4—10 PAUSED）、正常停机和完整备份均成功，备份在 `/var/lib/pm-nautilus-deploy/20261102T030603Z-ff5da9b/`。随后脚本的备份大小自检误报并按设计停在停止状态：它拿停机前测的大小作比较，而正常停机会合并并删除 SQLite WAL，数据目录因此变小。人工以 `diff -rq` 确认备份与停机后数据目录逐文件一致后，手动完成切换 `PM_GIT_REVISION`、`up -d`、健康检查、恢复 1、2、3。停机约 3 分钟。
- 上线后：健康 ok、修订 ff5da9b、10 个账户可用、后台错误为空；重启后全部 PAUSED 且持仓与停机前一致，START 1、2、3 后为 RUNNING，4—10 保持 PAUSED。
- 脚本修复 `4c12793`：改为与停机后的数据目录比较文件数和大小，测试替身在停机时删除 WAL 以覆盖此情况。只改脚本与测试，服务器源码更新到该提交即可，运行镜像不变。
- 用户决定暂不开通 COS，服务器外备份未启用。

## 2026-11-01 部署（99eada5）

- TEST/LIVE 一致性审查后修复：Polymarket 拒绝签名金额低于 1 美元的可成交 BUY，原策略会计划此类订单（例如收费市场 1U 预算只签 0.96U，或一轮剩余预算不足 1U 的补仓），TEST 会成交而 LIVE 必被拒。现在 `Runtime.evaluate`（两种模式共用的买入决策点）跳过此类机会，LIVE `buy_valid` 对竞争结果采用同一规则。配置页手续费说明改为“买入手续费以 pUSD 另付并计入预算”（依据官方 SDK）。另将带日期的审计文档移入 `docs/archive/`。
- [GitHub CI 41310180368](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/41310180368)：AMD64、ARM64 及 Docker verify 通过，1,266 项 Python 测试通过。
- 部署前只读核对：TEST 1、2、3 RUNNING（持仓 26/35/46），4—10 PAUSED、无持仓；各账户每事件预算 100U，新规则主要影响剩余不足 1U 的补仓。构建 `pm-nautilus:99eada5` 后正常 stop（27 秒），停机状态下完整复制 `runtime/server` 至 `/var/lib/pm-nautilus-review-20261101b/runtime-server`，保留原 `.env` 副本，再改 `PM_GIT_REVISION` 并 `up -d`。
- 上线后：健康 ok、修订 99eada5、10 个账户可用、后台错误为空；明确 START 1、2、3，持仓数一致，4—10 保持 PAUSED；全部账户行情组 5/5、盘口全部 READY、无未就绪持仓，结算检查正常；页面已显示新手续费说明。
- 外部规则核对与上线前清单见 [LIVE 准备检查](docs/LIVE_READINESS.md)。要点：当前硅谷服务器所在的美国为“仅可平仓”地区，程序的地区检查会阻止启用 LIVE，须换到允许地区的服务器。

## 同日较早部署：2026-11-01（6ae9fca）

- 代码审查后的小范围加固，仅改 `app.py`、`books.py` 并新增 `tests/test_review_hardening.py`：Basic 认证与 CSRF 头按 UTF-8 字节比较（原先非 ASCII 输入触发 500，非 ASCII 的 UI 密码无法登录）；配置请求体非 JSON 对象返回 400；`/api/dashboard` 的 limit 限 1–10000；盘口快照/增量先整批校验再写入，非法档位不再留下半更新盘口。未改策略、目标价、费用、模拟成交、资金、止损、赎回或数据库结构。
- [GitHub CI 41301770739](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/41301770739)：AMD64、ARM64 宿主验证及 Docker verify 通过，1,263 项 Python 测试通过。
- 服务器 `/opt/pm-nautilus` 仅使用基础 `compose.yaml`（目录中没有 `compose.override.yaml`）。部署前只读核对：TEST 1、2、3 为 RUNNING（持仓 26/35/45），4—10 PAUSED、无持仓，LIVE 未启用、钱包 UNCONFIGURED。
- 构建 `pm-nautilus:6ae9fca` 后正常 stop（28 秒），停机状态下完整复制 `runtime/server` 至 `/var/lib/pm-nautilus-review-20261101/runtime-server`，并保留原 `.env` 副本；随后改 `PM_GIT_REVISION` 并 `up -d`。
- 上线后：健康 ok、修订 6ae9fca、10 个账户可用、后台错误为空；按部署前状态明确 START 1、2、3，持仓数一致，4—10 保持 PAUSED。全部账户行情组 5/5、盘口全部 READY、无未就绪持仓；非 ASCII 错误登录返回 401；容器 healthy。

## 上一次应用验收：2026-10-30

- 本地 1,256 项 Python 回归、122 项页面回归全部通过，无失败、错误或跳过；Ruff 检查、145 文件格式检查及 JavaScript 语法检查通过。
- [GitHub CI 41040472149](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/41040472149)：AMD64、ARM64 的宿主验证及 Docker verify 全部成功，绑定上述应用 SHA，包含完整程序、页面及静态检查。
- 本轮程序改动限 `web/index.html`、`web/styles.css`。`app.js`、全部 Python、配置、依赖及测试文件与前版字节一致；143 个静态 ID、30 个输入及原属性、脚本入口保留，没有改动交易规则、盘口、目标价、费用、资金或数据库结构。
- 真实 Chrome／Cua 在隔离临时 TEST 上检查八种屏宽、持仓／配置／记录／钱包展示、空的 10 号与返回 1 号、记录 20→40 条；无横向溢出。Hallmark 静态检查通过，独立视觉复核为 ship。预览收益及持仓为模拟测试数据，不是服务器成绩。
- 服务器 `43.159.133.129`、目录 `/opt/pm-nautilus`：固定镜像构建、十账户当前副本及停机冻结副本验证通过。十个 TEST 和一个 LIVE 一致性备份、模式意图、钱包与受保护配置均保全，未用旧快照覆盖账本。
- 十个上线 TEST 的账本、历史与收益接口、增量分批检查通过；原历史前缀、订单、参数、初始资金设置和 generation 保留。LIVE 账本精确一致，钱包、模式意图及部署配置未变，五个页面资源与源码哈希一致。
- 上线后只读采样：仅恢复维护前运行的 TEST 1、2，3—10 为 PAUSED；LIVE 未启用、钱包 UNCONFIGURED；四个行情组及续查当次 783 个订阅盘口全部 READY、无不可用持仓，后台错误为空，容器 healthy、重启次数 0。服务器本机 HTTPS 通过有效证书验证。
- 桌面最初出现代理隧道失败及 CLI 连接失败；续查时真实浏览器已成功通过公网 HTTPS 健康接口核对上述应用 SHA、十个可用账户及后台状态。生产主页面仍要求登录，本轮未完成公网生产登录页面交互验收；隔离页面检查与健康接口分别记录，未改代理、SSH、证书或防火墙配置。

这些运行状态是该次验收的时间点记录；后续接手应先只读核对实际状态，保留用户正在运行的 TEST。恢复 1、2 号是本次维护后明确执行的状态恢复，不是程序重启或故障后自动恢复 RUNNING。

## 本版已经完成

十个账户共用策略、风控、目标价、退出和赎回业务，分别保存参数、资金、订单、持仓、完整历史和运行状态。1 号原账本保留；其他账户不能消耗它的模拟盘口。

共享行情源，TEST 去重解码、合并进行中的相同公开查询、弱引用复用不可变公共元数据；缓存满及不确定状态走完整原路径。无服务器型号判断，无市场、历史、精度或交易频率降级。关闭和重置释放本账户资源。

单账户构造、检查点、诊断、订阅及可选元数据清理错误局部处理，不能让正常兄弟账户一并暂停。公共断网、进程、内存耗尽和共享磁盘保护仍是共享条件。健康接口区分账户错误与全局故障，部署验收另要求十个账户全部可用。

启用 LIVE 前要求全部 TEST 暂停，等待所有 TEST 行情、扫描、结算和采样任务退出，并先持久化 LIVE 模式意图。启用失败先排空私有客户端；清理或模式保存未确认时保持停止。重启不自动解锁、启用 LIVE 或恢复交易；保留 LIVE 意图时 TEST 后台也停止。明确恢复 TEST 后台后，所有账户仍 PAUSED。没有故障后自动恢复 RUNNING。

此前 EGM 修复另加固 LIVE 等待期间的 START／PAUSE 代次、异常与取消时的关闭收尾、钱包导入线程及发布阶段的取消所有权；历史增量按固定覆盖水位分批，兼容旧页面。没有修改策略判断、目标价／tick、费用、份额、模拟成交、成本/PnL、止损或赎回金额规则，也没有数据库迁移、服务器规格特例或降低交易维护频率。

目标卖价沿用原公式和市场步长：3¢、加价 1¢、倍数 1.67 得到 5.01¢，若市场步长 1¢，向上对齐后的合法目标价是 6¢。已有持仓保留成交时存下的目标价，不因之后改参数重算。

## 账本与换服

相对 `PM_DATA_DIR`：`TEST/state.sqlite`、`TEST/accounts/2/state.sqlite` 至 `TEST/accounts/10/state.sqlite`、`LIVE/state.sqlite`，以及 `execution-mode.json` 和 `wallet/live.vault`。服务器实际持久目录见 Compose 挂载配置；GitHub 不保存这些文件。

本次界面升级的一致性备份、模式文件、配置及门禁记录保留于服务器私有目录 `/var/lib/pm-nautilus-ui-hallmark-20261030/8ab1937e84b1f63db3db46a07eca8a4d17876a58/`。此前 EGM 修复及 2026-10-29 的私有恢复资料继续保留。这些不是 GitHub 的第三个版本入口；私有状态文件含部署凭据，不复制到 GitHub 或交接对话。未用旧快照覆盖交易账本。

换服保留全部账本、钱包、模式文件和配置，停止旧实例写入后再制作最终一致性备份。PAUSE 仍可能继续卖出及赎回，不能把 PAUSE 当作最终冻结；含新交易的数据不能配基准版直接回滚。详见 [部署](docs/DEPLOY.md)、[新环境](docs/REINSTALL.md)及[首次 LIVE](docs/START_FRESH_LIVE.md)。

## 尚未完成的实盘验收

服务器地区资格、实际账户类型、pUSD/POL、授权、挂单及份额均需在拟用环境重新核验。真实资金 BUY/SELL、实际扣款和份额、正额赎回及实盘故障恢复闭环尚未在本轮实测。TEST／CI／镜像／服务器健康不能替代这些证据，也不构成启用 LIVE 或资金操作的授权。

后续先读 [README](README.md)、[验证范围](docs/VALIDATION.md)与[TEST 说明](docs/TEST_ACCOUNTS.md)。旧日期文档是追溯资料，其中“当前”仅指当时；当前运行与授权以用户后续指令和当次只读核查为准。
