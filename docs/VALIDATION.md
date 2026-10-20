# 验证结果与边界

## 2026-10-20 市场停用识别与恢复（已发布部署验收）

[PR #16](https://github.com/yvettemiranda/PM-NAUTILUS/pull/16) 已合并，固定应用代码 `ebbe888a56213f3c976fdb5291b045354cb821e9`；代码 push、PR 和 [main CI 39397396195](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/39397396195) 的 x86_64／ARM64 回归及 Docker verify 全部通过。源码／文档 SHA 以本次纯 Markdown 提交及私有 `docs-sync.json` 记录为准；纯文档同步仅推进源码，应用镜像、env、runtime 保持上述固定代码，无需重启。

880 项 Python（26.00 秒）、37 项页面、Ruff 检查／格式（105 文件）、JS/shell 语法通过，保留两条既有第三方弃用警告。新增回归覆盖严格身份和状态证明、404／失败拒绝、同市场双向与同 Event 其他市场隔离、低频复查、共享重新开放及时间戳下限、旧缓存和隐藏持仓估值、公开健康接口聚合隐私、排队 BUY／SELL 的预期延期、可能 POST 的未知结果与正式结算豁免。独立复核发现的共享静默恢复及旧快照问题均已修正并回归通过。

2026-10-20 17:14:13 CST（09:14:13 UTC）服务器最终 proof 的 `acceptance=true`，`checkedAt=2026-10-20T09:14:13.529303+00:00`：容器 healthy、restarts=0、OOM=false，原 TEST/RUNNING 已恢复，LIVE=false、钱包 LOCKED，后台错误为空。在线完整 TEST validation PASS（38.999 秒），真实 TEST 账本隔离副本离线 Runtime 完整验证通过；停机 TEST/LIVE 两库全部逻辑值、schema 和 codec v2 原样保留。原参数、generation、native／equity 历史、加密钱包及绑定、依赖／配置／安全设置保留；既有主机存储工具、units、enabled／active timer 和 Docker 日志 10m×3 保持。本轮没有数据迁移、压缩或 VACUUM。

本次现场为 92 笔持仓，1 笔因 `archived` 报价不可用，其他未就绪持仓为 0；剩余 4/4 连接组 READY、767/767 盘口完整。停用仓位的旧价格已屏蔽，已知持仓小计精确并与完整总额分开；完整资产和未实现盈亏仍为 `null`／未知，数量、成本与权利保留，处置安排待核查。归档不证明零价值、退款、结算或一定恢复。

09:15:19 UTC 复查时四组 PONG 新鲜、各组 snapshotRetries=0，当前 scan／category／stream／service error 均为 null；lastScanAt=`2026-10-20T09:13:38.724659+00:00`，晚于容器 StartedAt=`2026-10-20T09:12:01.904732102`，确认重启后完成新扫描。这是短时采样，不是跨夜稳定性或实盘成交／收益保证。

09:17:19 UTC 的真实低频复查样本：该停用观察的 since=`09:12:17.679330`、checkedAt=`09:17:19.549873`、nextCheckAt=`09:22:19.549873`（均为当天 UTC），lastError=null，符合约 300 秒一次的复查间隔；其余四组 PONG 均为 09:17:19，snapshotRetries 与 missing 各为 0。停用权利继续保留，未通过快速重订阅假造恢复。

全仓摘要、分页历史（当时 790 条记录）、HTTPS 证书及认证、未认证钱包接口 401、部署 JS 哈希均通过。认证 dashboard 的停用原因、旧价屏蔽和未知总额／已知小计语义通过；本轮未做生产浏览器登录后的视觉验收。

一致性私有回滚与 age 加密归档位于 `/opt/pm-nautilus/backups/pre-marketavailability-20261020T090449Z`；密文 SHA-256 `46526d65e1df00dd89206671d9b3ae2a9129fac976fe4ce7ba7e621da3e879e7`。私有回滚一致性、recipient 加密及哈希通过，未演练解密恢复。恢复交易后不得用旧快照覆盖新增记录。

机制与边界见 [市场可用性处理](MARKET_AVAILABILITY.md)。本轮没有启用 LIVE、解锁钱包、签订单或链上资金操作，也未重新解决地区、pUSD／POL 或真实资金买卖／正额赎回闭环；TEST、CI 和服务器验收不等于实盘就绪。

## 2026-10-20 自动空间维护（历史发布部署验收）

[PR #15](https://github.com/yvettemiranda/PM-NAUTILUS/pull/15) 已合并，固定应用代码 `640d1b1dbc58f2e61ef9cf3e131cd37a1e163de9`；[main CI 39292103375](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/39292103375) 的 x86_64/ARM64 回归及 Docker verify 全部通过。本地 847 项 Python、34 项页面、Ruff 检查/格式（101 文件）及 JS/shell 语法通过，保留两条既有第三方弃用警告。独立复核及真实外层 submit 回归覆盖尚未 POST 的 BUY 保护不影响已有 SELL，可能已 POST 的未知结果继续隔离。纯文档同步可推进服务器源码，镜像/env/runtime 保持上述固定应用版本，无需重启。

新增回归覆盖磁盘/inode 阈值与滞回、失败/过期采样、START 与签名后最终 POST 边界、已有 SELL/迟到回报、未知提交资金占用、共享扫描隔离、冷元数据所有权、锁/SQL 回滚/预算、目录与镜像保留及页面 START/PAUSE。机制与边界见 [自动存储维护](STORAGE_MAINTENANCE.md)。

2026-10-20 01:27:53 CST 服务器部署验收采样：容器 healthy、restarts=0、OOM=false，TEST/RUNNING、LIVE=false、钱包因升级重启 LOCKED，后台错误为空；存储 guard=OK、canOpen=true，采样新鲜。在线完整 TEST validation PASS（12.097 秒），真实账本隔离副本 Runtime full validation、停机 Store 两库全部逻辑值精确比对、原生/权益历史前缀及参数/代次保留通过；加密钱包、绑定及依赖/配置/安全设置保留。HTTPS/认证、未认证钱包 401、JS 哈希及全仓摘要/分页历史核对通过。

systemd timer 已 enabled/active，首次 service Result=success，实际 apply=true、level=normal、errors/actions 均为空，无需删除；Docker 实际日志配置为 10m/3。已固定 `6dac94b` 与 `3e765cc` 两个回滚镜像，现有私有回滚备份不在自动清理目录内。当前正常余量下冷缓存未删除，压力阈值、失败/过期与交易边界由受控回归验证，未人为填满实际磁盘。

恢复后的公开行情为 748/748 盘口、4/4 连接组 READY，扫描/分类/当前流/服务错误为空，lastScanAt=2026-10-19T17:26:08.540528+00:00；本次重启后已完成新的公开市场扫描。这仅为本次短时采样，不宣称跨夜稳定性或真实收益/成交保证。

一致性私有回滚与 age 加密归档保存在 `/opt/pm-nautilus/backups/pre-storageguard-20261019T171727Z`；密文 SHA-256 `9f825ca518d5f0b9c60c75f0ba8716a17ebf52ab583797da464a7d178063b3ff`。两库回滚副本、recipient 加密与哈希通过，未演练解密恢复。本次未变更 Token 编码；上一应用 `6dac94b` 兼容现有格式，`3e765cc` 等压缩格式前镜像仍须配对应旧格式完整快照。恢复交易后不得用旧快照丢失新增记录。

钱包、账本、全部交易意图/原生事件/收益历史、持仓/赎回权利和旧备份不自动删除。正常 Token 继续压缩存储，冷缓存删除只形成可复用 SQLite 空页，不在线 VACUUM，不自动停机或重启。若不可删除数据长期增长或其他服务占满磁盘，仍可能需要扩容；机制不能保证无限容量或硬件故障时无中断。

本轮没有解锁钱包、启用 LIVE、签订单或链上交易，也未重新验收地区/资金/POL。真实资金买卖、正额赎回及完整 LIVE 闭环仍未实测，不能以 TEST/CI 通过认定实盘就绪。

## 2026-10-19 元数据与扫描优化（已发布部署验收）

[PR #14](https://github.com/yvettemiranda/PM-NAUTILUS/pull/14) 已合并，正式应用固定为 `6dac94bf6bc9cbc954a22e112a52dd49a798d2a6`；[main CI 39224291130](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/39224291130) 的 x86_64/ARM64 回归与 Docker verify 全部通过。后续纯文档提交可更新服务器源码，镜像/env/runtime 继续固定该应用 SHA，无须为文档暂停或重启。

752 项 Python 回归（26.12 秒）、33 项页面回归、Ruff 检查/格式（93 文件）、JS/shell 语法通过，独立复核无确定 P1/P2。新增 47 项覆盖编解码/旧包/历史身份、损坏拒绝、锁/双库备份/事务、全部逻辑值污染检测、Native 重放、TEST 深度、慢扫描及关闭/共享竞态。两条既有第三方弃用警告保留。

服务器实际整理 941,620 条 TEST Token，文件 1,073,287,168→730,410,672 字节，减少 342,876,496 字节（31.95%）；LIVE 无 Token，69,632→73,728 字节，增加的 4 KiB 来自新索引。两库全表逻辑摘要及完整 integrity_check 通过，没有删除历史。此比例是本次停机整理结果，恢复扫描后文件仍会随新增数据增长。

2026-10-19 20:40:33 CST 最终服务器验收 PASS：源码/镜像/env/runtime 在验收时一致，容器 healthy、restarts=0、OOM=false、后台错误为空；已恢复原 TEST/RUNNING，LIVE=false、钱包因重启 LOCKED。在线完整 TEST validation PASS（57.873 秒），真实 TEST 副本离线 Runtime full validation、全仓摘要和分页历史核对通过。原参数、代次、两库全部停机逻辑数据、native/equity 历史、加密钱包及依赖/配置/安全设置保留；部署 JS 哈希、HTTPS/认证及未认证钱包接口 401 通过。

恢复后的公开行情采样：748/748 盘口、4/4 连接组 READY，扫描/分类/当前流与服务错误为空；lastScanAt=2026-10-19T12:38:08.486703+00:00。这是本次短时验收，不构成长时间稳定性或收益保证。只读巡检已同步新版，完整 validation 单独等待 180 秒；旧脚本在私有备份内保留，权限未扩大。

一致性私有回滚及完整 age 加密备份保存在 `/opt/pm-nautilus/backups/pre-metadataopt-20261019T120207Z`；密文 SHA-256 `40dfa36bd7e1acba8c3a04379e9289a1afc84983a0c8123519d534102e2998d0`。两库回滚副本、recipient 加密及密文哈希通过；未演练解密恢复。旧镜像回滚必须配维护前完整旧库/配置/钱包快照，恢复交易后不得用旧快照丢失新增记录。

936,800 行合成基准负载减少 36.3%、可选 VACUUM 后库文件减少 31.2%、active 查询解码约 3.38 倍；没有证明完整 integrity 提速，也没有把合成查询速度当作服务器实测。格式、维护及边界见 [本轮记录](METADATA_OPTIMIZATION_2026-10-19.md)。

本轮没有解锁钱包、启用 LIVE、签订单或链上交易。地区限制、pUSD/POL 与真实买卖/正额赎回闭环仍未在本轮解决或实测；不能以 TEST/CI 通过认定实盘就绪。

本页按日期保留当时的实测记录。旧章节中的“当前”只描述对应日期的版本；最新部署状态以本节及 [HANDOFF.md](../HANDOFF.md) 为准。

## 2026-10-14 精确持仓核对与零收益旧仓修复（已发布部署验收）

[本轮修复](https://github.com/yvettemiranda/PM-NAUTILUS/pull/12) 的固定应用代码为 `3e765ccb067c8012af5f4ea4f23c3bda91d7372f`。本地及 CI 回归：705 项 Python / 33 项页面测试、Ruff 检查与格式（89 文件）、JS 语法及 diff 检查通过；[PR CI 38525112524](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/38525112524) 与 [main CI 38525489607](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/38525489607) 的 x86_64/ARM64 测试及 Docker verify 均成功。服务器最终验收：2026-10-14 19:03 CST 通过，TEST/RUNNING、LIVE 未启用、钱包 LOCKED；实际持仓与授权只读核对通过，资金/POL 与地区仍未满足条件。策略规则、参数和依赖范围保留；本轮核对不会签订单或链上清理交易。

**生产只读核对（本轮升级前）：**用户已重新连接正确的已有单签 Safe，钱包元数据、API 身份与 LIVE 账本绑定一致。私有 CLOB/账户查询完整返回，交易所及标准/Neg-Risk 赎回适配器权限核对通过、无待确认授权，LIVE 未启用。当前资金/POL 不足且服务器地区受限；旧版另将严格零收益的外部输方残仓视为阻碍。公开文档不记录地址、资产金额、具体仓位或私有查询资料。

**已复现的两项精度/归属问题：**公开持仓 API 的四位显示数量会小于真实六位 CTF 余额，差值不能用容差或修改账本掩盖；已结算输方 token 仍可能为正且显示 `redeemable=true`，该标记不证明有回款。链上 payout 分子严格为零时，烧毁也不会拿回资金。新生产核对模块已用实际完整公开持仓分页及 fresh CTF 读取取得 positions=pass，前后精确余额不变。另一本地集成仅明确模拟资金充足与地区允许，真实持仓及 RPC 保持真实，三项检查通过、ready=true；这证明没有该两项之外的软件拦截，不改变真实生产就绪状态。首次发生公共端点连接超时返回 UNKNOWN，端点恢复后重试通过，失败证据保留。没有订单签名、广播或仓位操作。

本轮回归覆盖：

- 完整 Data API OPEN 分页仅用于发现 token；本程序正数量权利在 API 缺行或显示截断时仍用固定区块 CTF 原子余额精确核对。无容差、无 API 数量回退；读取缺项、异常数值及 RPC 失败拒绝放行。
- 保留本程序历史零数量 token 的身份；API 中出现时仍要求实际零余额，不能误用外部残仓例外。API 未出现的已知零数量历史不触发全量历史 RPC。
- 外部例外仅限标准二元、已结算、收益分子严格为零、受支持抵押资产及派生身份一致、另一结果余额为零的 CTF 残仓。两种受支持 collateral 和两个 outcome 分别覆盖；极小正收益、错误身份/父集合/Condition/index/token、未知抵押资产、Neg-Risk、未结算、非法 payout 及 RPC 故障不能被忽略。API 价格、价值、数量和 `redeemable` 不充当链上证明。
- 原生启动报告按 token 汇总未结算正数量自有权利，链上余额覆盖全部权利后仅报告本程序总份额；等待后重新读取当前权利，新增未读取 token、期间份额增加超过链上余额及非法响应均拒绝。订单恢复完成、ready 前最终新查保留，手动资产和已结算权利不导入报告。
- 钱包每批余额在本次新取区块读取，最多四个并行请求；规范 uint256 token/余额、拒绝重复 token 与空集合不访问 RPC 均有回归。无跨笔缓存；每笔 BUY 的账户、全部开放挂单和同 Condition 精确核对保留。

**服务器最终验收（2026-10-14 19:03 CST / 11:03 UTC）：**固定应用 3e765cc 的源码/镜像/env/runtime 一致、healthy、零重启/未 OOM、后台错误为空；原 TEST/RUNNING 已恢复，LIVE=false、钱包 LOCKED。完整在线 TEST validation PASS，59.655 秒；真实离线 TEST 副本 Runtime full validation 和停机 Store 两库全部逻辑数据精确保留通过。参数、代次、native/equity 历史、已重连的加密钱包及绑定、依赖/配置/安全设置保留；全仓摘要及分页历史（当时 664 条）、部署 JS 哈希、HTTPS/认证与未认证钱包接口 401 通过。本轮一致性备份及 rollback、recipient 加密/密文哈希核对通过，目录见 HANDOFF，解密恢复未演练。新镜像 11:03:40 UTC 公共元数据/账本、Safe 所有权、交易及两个赎回授权只读通过，positions=pass；地区 blocked、资金/POL 不足仍保留，LIVE 意图/原生事件为零。此次核对不读取密钥或构造 signer，也没有链上清理。公开链上证明、私有账户查询及部署验收仍不代表实际买卖、正额赎回、滑点或真实延迟已验收。

## 2026-10-14 已有 Safe 连接修复（历史部署验收）

公开只读核对确认签名 EOA 与已有平台单 owner Safe 资金账户不同；不公开用户地址、资产金额或现场映射。新版本不会自动改旧绑定，地区、资金、手续费及外部旧仓核对仍须通过，真实链上全流程仍待实际验收。

[PR #10](https://github.com/yvettemiranda/PM-NAUTILUS/pull/10) 的应用代码 `fefcdb3963f57ceaea8f0eba93686c6024dc1f0f` 实现资金地址留空时的严格发现：官方 Gamma profile、factory 派生地址、已部署 Safe、唯一 owner 与阈值 1 必须一致，缺失、不支持或异常拒绝，不猜测 EOA。明确填地址仍走原核对路径。无任何 LIVE 交易且未启用时，用户密码加明确确认可取消规范 ACTIVE/pending=None 的未签名准备，或清理合法 FAILED/prepare 未形成待确认交易的记录，随空白钱包清除旧绑定；已消费 nonce 下限保留，未知提交及不可信记录继续拒绝。

本地 **591 项 Python、33 项 Node 页面**及 Ruff（**85 个 Python 文件**）通过，覆盖严格 Safe 发现、手填兼容、异常/不支持拒绝、导入失败不落钱包、未签名准备取消及未知状态拒绝。[PR CI 38494911007](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/38494911007) 与 [main CI 38495374302](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/38495374302) 的 x86_64/ARM64 及 Docker verify 均通过。生产公开资料的发现 helper 只读核对通过；未在正式钱包删除/重导或验收真实交易闭环。操作见 [首次实盘指南](START_FRESH_LIVE.md)。

**服务器最终 proof（2026-10-14 13:36 CST / 05:36 UTC）：**源码、镜像、env 与运行 revision 均为 `fefcdb3963f57ceaea8f0eba93686c6024dc1f0f`，容器 healthy、restarts=0、OOM=false、后台错误为空，TEST 恢复 RUNNING、LIVE=false、钱包 LOCKED；05:36:52 UTC 公网健康接口再次确认。完整在线 TEST validation PASS，**59.319 秒**；真实离线副本 Runtime full validation 与停机 Store 两库全部旧逻辑数据精确保留均通过。参数、代次、native/equity 历史、钱包、依赖/配置/安全设置保留；全仓摘要及分页历史（当时 **658 条**）、部署 JS 哈希、HTTPS 证书/认证及未认证钱包接口 401 通过。新备份一致性回滚比对、recipient 加密与哈希验证通过，目录和密文 SHA 见 [HANDOFF](../HANDOFF.md)；未演练解密恢复。

## 2026-10-14 钱包授权与页面后续修复（512 已部署验收）

已只读确认前次授权失败发生在链上交易准备阶段，该次未签名或广播授权交易。新容器启动时钱包 LOCKED、LIVE 未启用；用户随后解锁，账户 BLOCKED，资金地址连接问题见上一节。08:49 CST 和 10-13 的 LOCKED 都是当时快照。用户地址、资产金额、钱包诊断和私有 RPC 信息不发布到仓库。

[PR #9](https://github.com/yvettemiranda/PM-NAUTILUS/pull/9) 已合并，固定代码 `512c8a2b0147bd12321f07fa3a008d57adf8c166`。本地 **545 项 Python、32 项 Node 页面**及 JS 语法、Ruff check/format（**83 个 Python 文件**）通过；[PR CI 38490241684](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/38490241684) 与 [main CI 38490656664](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/38490656664) 的 x86_64/ARM64 及 Docker verify 均通过。服务器 verify 构建、TEST/LIVE Store 全逻辑数据精确保留、真实 TEST 离线副本 Runtime 完整 validation 均通过。现场最终容器 healthy、restarts=0、OOM=false、TEST/RUNNING、LIVE=false、用户解锁后钱包 BLOCKED、后台错误为空；完整 TEST validation PASS，58.614 秒。参数、代次、native/equity 历史、钱包、依赖及安全设置保留，全仓摘要/分页历史（当时 656 条）、部署 JS 哈希、HTTPS 证书与认证门控通过。08:49 CST 的 LOCKED 是重启初始快照，不能当作当前解锁状态。备份及回滚一致性、加密/哈希验证通过，目录与密文哈希见 [HANDOFF](../HANDOFF.md)，未做解密恢复演练。

已查 [官方钱包说明](https://docs.polymarket.com/trading/wallets-auth)：本程序完整支持 EOA(0)/单签 Safe(2)，但没有实测 EOA 允许交易名单资格，Deposit Wallet(3) 不在支持范围；准备资金及首次实盘须先解决类型与官方准入。

新增回归覆盖签名前 POL 与当次 gas 检查、准备失败持久化与安全错误分类、未知提交保留原交易、两类 Exchange 的 CTF 交易权限准入及每笔新买重新核对四项 CTF 授权、同钱包 PENDING 不被查询 UNKNOWN 覆盖、页面错误跨自动读取保留、同时展示全部账户阻碍，以及摘要持仓标签。`test_live_approval_api.py` 另覆盖 FAILED 准备诊断重启后保留；无活动账本只允许清除身份匹配、prepare 未形成待确认交易、pending=None 且错误码可信的失败授权及旧钱包绑定，nonce 消费下限保留，未知或不可信 FAILED 拒绝删除。以上使用受控账户/RPC，并未完成正式钱包删除、真实订单或链上授权闭环；不以部署验收代替真实账户闭环。授权范围及操作见 [实盘准备检查](LIVE_READINESS.md)。

## 2026-10-13 全面优化验收（已发布部署）

固定应用代码 `b805c4f11bffe09c0bc5dc610e106223a94025aa` 已经 [PR #8](https://github.com/yvettemiranda/PM-NAUTILUS/pull/8) 合并到 main；[PR CI](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/38383623330)、[main CI](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/38384005105) 的 Ubuntu x86_64/ARM64 测试及 Docker verify 全部成功。服务器已更新到该固定代码；本节记录本轮结果，下方旧章节保留有日期的历史验收。

本地最终完整回归：**Python 486 项通过（17.00 秒、两条既有第三方弃用警告），Node 页面 24/24 通过，JS 语法、Ruff check 与 format --check（83 个 Python 文件）通过。**独立复核已结束，发现的启动钱包核对并发竞态已修复；固定发布提交与双架构 CI 已核对，服务器验收见下一段。

**服务器现场验收（2026-10-13 18:51 CST / 10:51 UTC）：**

- `43.159.133.129` 的源码/镜像/runtime revision 均为 `b805c4f11bffe09c0bc5dc610e106223a94025aa`；healthy、零重启、未 OOM、后台错误为空。TEST 已恢复原 RUNNING，LIVE 未启用、钱包 LOCKED。升级前完整 validation 通过，部署后现场完整 validation 再次通过，耗时 57.653 秒。
- 私有回滚两库完整比对通过；新 Store 打开实际 TEST/LIVE 两库后，v2 全部逻辑数据、规则参数、generation、native/equity 历史、钱包、依赖/配置/安全设置精确保留。真实 TEST 账本离线副本的 Runtime 完整验证、全仓 summary=full 及 history 数量检查通过。
- 现场全仓摘要与分页历史通过，当时 649 条记录；页面 JS 哈希匹配部署源码；HTTPS 证书/认证与未认证钱包接口 401 通过。维护前后两份核对脚本最终 PASS。
- 停机备份目录及密文 SHA 见 [HANDOFF](../HANDOFF.md)。已检查加密头和哈希，未解密并演练恢复。后续仅同步文档时，源码 main SHA 可更新，应用镜像及运行 revision 保持固定代码，无需重建或重启。

已取得的局部验收：Chrome headless 使用受控 API，在 1280px 与 320px 下无页面错误或横向溢出；1,000 个持仓初始只展示 20 个、总数仍为 1,000，分类焦点在 500ms 自动刷新后保持，10,005 条交易记录可继续“更多”，未接钱包的 LIVE START 禁用。这是本地浏览器实测，不是生产页面或真实账户现场结果。

隔离投影对照（同一组合成 1,000 持仓，全仓资金精确一致）：完整持仓响应 864,048 字节，前 20 明细 15,968 字节，纯摘要 1,316 字节；对应中位时间 7.149ms、1.136ms、1.037ms。收益采样从完整明细计算的 5.721ms 降至纯汇总 0.494ms。这些数值只说明本地投影/页面负载变化，不等价于实盘下单延迟或滑点。

- **BUY 精度及预算：**`test_live_full_optimization.py`、`test_rules_full_optimization.py` 覆盖 0.1/0.01/0.001/0.0001 tick 的 canonical 向下量化、5.02U 及浮点临界值、最小量、官方余额/手续费保护、原始限价及拒绝下一合法价格档；受控 LIVE POST 与 TEST 相同输入对比。BUY 从旧粗取整改为 SDK 精度是正确性修复，单独解释预期数量变化，不将旧误差当成必须保持的结果。没有真实资金签名、提交或成交。
- **回报与原生日志：**写入失败、写成后原生分发失败、份额转换失败分别注入，验证同一回报可恢复且只计一次；晚到取消不能越过已持久化 Fill。错误 Condition/token/方向及非精确正份额不能作为终态证据；FAILED 单笔、累计确认加失败份额及 REST matched 必须不超过持久化原生上限，超限保留占用并暂停，CONFIRMED 不回退；重复确认跳过无变更写盘和 Fill 构造。
- **账户与元数据：**Condition 索引覆盖替换、删除、重启；12 个自有持仓的执行客户端完整启动由 13 次整钱包读取减为 2 次：报告批次 1 次，完成订单恢复后最终核对 1 次。`test_live_activation_final_custody.py` 两项回归复现 custody 先读、订单随后恢复另一 Fill 的竞态，并验证 ready 前新查最终份额不足会拒绝、正常路径仍新查；之后没有 await 再发布 ready。更优价格的 BUY 数量可超出签名最低接收量，仍受原生数量/含费预算约束。下一轮及实际新买仍获取新结果。手动资产、已结算权利不被冒充为本程序当前 CLOB 仓位。独立只读预检的并行查询固定同一区块，全部账户准入条件保留。
- **链上事务与取消：**`test_redemption_ownership.py` 覆盖未知原交易跨重启、授权与赎回 nonce 互斥、确认下限原子提交、写入失败不广播、反复取消后保持所有权及先保存再解锁。实际标准/Neg-Risk 合约结果、实际回款和确认深度检查保留；测试使用受控钱包/RPC，不是实盘验收。
- **备份与关闭：**`test_backup_lifecycle.py` 覆盖导出/授权线程重复取消、取消期间删除钱包被拒绝、快照连接在 GC 前关闭、专用临时目录安全清理、最终文件及旧式凭据父目录 fsync、应用关闭前等待工作线程。待确认授权不能被空白恢复覆盖。
- **行情与规则：**`test_market_full_optimization.py` 验证旧 socket 关闭等待期间已不可交易、TEST 缩范围/重置不影响仍被 LIVE 使用的订阅、公开结算不延迟定时控制且停止时被等待。`test_rules_full_optimization.py` 覆盖整数预算、首个 SELL 批次/共享深度/尾仓、Bid 哈希与止损版本、进度边界；`test_store_historical_tokens.py` 验证历史身份 SQL 查询保留。
- **START 与页面：**`test_strategy_full_optimization.py` 验证完整异步校验与返回后当前状态检查；`test_full_optimization_views.py` 和实际 `app.js` 页面回归覆盖全仓深度估值/未知状态、摘要与明细隔离、格式器/焦点、隐藏页停止轮询/返回更新，以及超过 10,000 条后的稳定分页、迟到成交、代次和模式隔离。收益采样不构造页面明细，但保留全部资金计算。

既有策略设置与发布安全条件保留。局部基准、离线模拟或服务器 TEST 结果均不能证明真实成交延迟和滑点改善；真实钱包 BUY/SELL、实际费用与份额、正额赎回及故障恢复仍待实际验收。最新状态见 [HANDOFF.md](../HANDOFF.md)。

## 2026-10-12 代码与服务器验收（历史）

代码 `dbf36d40ca1f24c1286e85efc1409b89f53e6bfb`，[PR #6](https://github.com/yvettemiranda/PM-NAUTILUS/pull/6) 已合并；[main CI](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/38261461594) 的 Ubuntu x86_64/ARM64 均通过测试及 Docker verify。现有配置、交易规则、依赖锁和部署安全设置未改变。

- **完整回归：**405 项 Python、17 项 JavaScript 页面测试；Ruff 检查/格式、JS 语法通过。页面测试执行实际 `app.js` 控制处理函数，覆盖延迟响应、模式切换和旧市场缓存。
- **TEST/LIVE 执行一致性：**锁定 SDK 的精确 SELL size 到受控 LIVE 提交及 CONFIRMED Fill；5.02 份在最小量 5.02 下完整卖出，5.029 份保留 .009 份尾仓，TEST 与 LIVE 份额及原生持仓一致。价格下限、最小量和实际拥有数量检查继续执行。结果未知的订单继续占用；缺字段、错误身份、负数或非有限成交量不能确认终态。
- **退出及重试：**未变盘口下的 BUY/TARGET/STOP 零成交终态，每次控制检查最多重新评估一次，不在同步处理循环中空转；在途订单继续保持 Event 锁。恢复行情后用户确认 START，后续扫描不重新锁存旧 fatal，新故障仍会触发安全暂停。
- **持久化与生命周期：**实际 SQLite `RAISE(ROLLBACK)`、延迟外键造成的失败 COMMIT 后，重试更新持仓并删除目标；重复取消 LIVE 启用后 native queues/Store 正常关闭；取消恢复期间钱包锁和上传文件保留至真实工作线程完成。
- **共享行情：**未知错误及 HTTP 401/403 只暂停受影响的订阅模式；另一模式保留健康盘口。10,000→50 token 后 50 条连接压缩为 1 条；旧连接已失去归属并停止，移动 token 的增量须等待新完整快照。
- **正常场景对照：**相对 `7307f33`，720 次行情输入、111 份状态、26 个订单的现金、占用、业务账本、原生事件、持仓、盘口消费、资格及参数一致。该对照验证既有正常规则保留；单独回归验证错误场景得到修正。
- **服务器：**完整 TEST validation `ok=true`、`errors=[]`，容器 healthy、零重启、后台错误为空；恢复 TEST/RUNNING，LIVE 关闭且钱包 LOCKED。更新前后两库业务和原生历史、参数、代次及钱包文件保留；页面脚本匹配已部署源码，HTTPS/认证核对通过。备份及版本记录见 [HANDOFF](../HANDOFF.md)。

以上为离线/受控执行与服务器 TEST 验收，未完成正式钱包买入、卖出、实际费用、正额赎回及实盘故障恢复。运行速度的优化没有经过真实交易所成交延迟或滑点测量；不将模拟一致性解释为成交价格和收益相同。旧服务器地区检查仍引用 2026-10-07 的历史结果，新服务器须从新出口重新检查。

## 2026-10-07 实盘准入修复与部署后验收

- 本地固定依赖执行 246 项 Python 测试、9 项前端测试、Ruff 检查及格式、JavaScript 语法，均通过；`pm-nautilus-final-export --help` 可运行。离线本机应用的健康、TEST/LIVE 面板、钱包状态、TEST 验证和锁定的 LIVE 记录接口均返回 200。测试没有使用真实钱包或提交真实交易。
- [PR #1](https://github.com/yvettemiranda/PM-NAUTILUS/pull/1) 的 Ubuntu x86_64/ARM64 四项 CI 检查均通过并合并；服务器运行同一代码提交 `273c80e7711221b7f532633710d6be381f1f4e42`。
- 停机加密备份后升级，服务器 TEST 完整 `/api/TEST/validation` 返回 `ok=true`、`errors=[]`，SQLite `quick_check=ok`。升级前后 83 个模拟持仓、现金 `117.346484U` 及 TEST/LIVE 各 15 项交易设置一致。升级后恢复原 TEST/RUNNING；LIVE 钱包文件已保存但锁定、未启用、无订单意图或原生事件。公网 HTTPS 浏览器读回 TEST 资金/持仓/记录/市场；LIVE 视图不显示 TEST 资金，锁定的记录不可读且 START 禁用。
- 同一服务器出口的 Polymarket 地区检查返回 `blocked=true`、`US/CA`。真实账户余额、赎回授权、CLOB 订单、链上份额、真实成交费用、卖出和赎回没有进行正式钱包验收；这些结果不能从离线测试或 TEST 账本推断。停机备份成功生成，但本轮没有独立设备上的解密恢复演练。

## 2026-10-07 全流程审计（只读、未验收真实交易）

完整结论与下一轮交接见 [END_TO_END_HANDOFF_2026-10-07.md](END_TO_END_HANDOFF_2026-10-07.md)。本轮本地锁定依赖下 207 项 Python 测试、Ruff 检查/格式、JS 语法、7 项前端测试通过；独立公开行情短测用临时开发规则取得 10 个模拟 Fill，并在同库重启后验证通过。服务器 10:54 CST 只读巡检为 TEST/RUNNING、LIVE 钱包未配置、容器 healthy、SQLite `quick_check=ok`；`/api/TEST/validation` 在本轮巡检客户端 20 秒内超时，因此**没有本轮完整 TEST validation 的成功结果**。没有真实签名、真实订单或链上操作。发现实盘买单含费预算、成交份额/费用入账、首次钱包赎回授权和运行中网页备份用于迁移等缺口；此前章节的模拟测试和网页就绪不能替代修复及真实实盘验收。

## 2026-10-06 LIVE 设置齿轮修复与地区检查

- LIVE 页面原先点击右上角齿轮会展开位于钱包区域下方的交易设置，但不会滚动到该区域。`7802c181a4966add09b8045cce686a5ccbfbae45` 增加展开后的滚动，并更换 `app.js` 资源版本；没有改动钱包、账本或策略逻辑。本地 JS 语法、7 项前端测试和 [GitHub Actions run 37467946285](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/37467946285) 通过。
- 腾讯云服务器已从该提交构建并启动新镜像；停机时生成加密备份，标识和大小见 [HANDOFF.md](../HANDOFF.md)。21:23 CST 本机健康接口确认 `status=ok`、`TEST/RUNNING`、`LIVE=false`、钱包未配置、版本 SHA 匹配、后台错误为空；容器 healthy、零重启。浏览器强制刷新服务器网页、切到 LIVE 视图再点击右上角齿轮后，页面实测自动滚动到交易设置且内容可见；后台仍是 TEST/RUNNING。以上不等于真实实盘验收。
- 同一服务器直接请求 Polymarket 官方地区接口，得到 `blocked=true`、`US/CA`。这验证了**当前出口**无法通过 LIVE 新开仓地区检查；未来迁移后的服务器需单独复测。没有使用真实钱包或执行真实订单。

## 2026-10-06 网页 LIVE 钱包与 H/D 曲线（已发布及部署）

- 新增仅在 LIVE 页出现的钱包流程：12 词助记词或私钥导入、公开资金地址核对、加密保存、网页解锁、就绪检查、明确启用；启用后先停在 PAUSED，另行点击 START 才开始新买。TEST 不需要钱包。应用重启后 LIVE 锁定，未核对的真实资金和仓位显示为未知，不能把未展示的持仓解释为零。
- 只读就绪检查覆盖账户认证、资金与授权、开放挂单、官方持仓接口的完整分页和服务器网络出口的地区结果。首次导入有原有持仓会阻止启用；恢复后按各 token 的实际份额对账。任何查询失败或数据不完整都不能放行。启用前不做真实下单、取消或链上写入。
- 网页 `.pmnb` 加密备份包含同一时点的加密钱包和 LIVE SQLite 账本；恢复仅接受空白 LIVE 账本。正式 TEST 数据库已约 714 MB，促使将首轮 128 MiB 加密包上限升级为分块压缩和流式上传，当前上限为 4 GiB 原始 LIVE 数据库、2 GiB 加密包。离线测试覆盖 100 MiB 原始数据库跨线程导出/恢复、旧版包恢复、错误密码、损坏文件、私有临时文件权限及下载后清理。该包不含 TEST 模拟历史，也不替代有在途订单时的实际账户核对；尚未使用真实 LIVE 账本做网页迁移演练。
- 收益曲线保留原始独立采样，展示可选 **H（每小时一点）**或 **D（按 UTC 日期每天一点）**；这两个按钮改变曲线的数据粒度，不是切换最近一小时或最近一天的时间窗口。未知估值、重启及采样断层仍使曲线断开。去掉重复的每分钟说明字样。本地页面预览已核对 TEST→LIVE 切换、未接钱包时 START 禁用、H/D 按钮与恢复入口；这些 UI 检查没有接入真实钱包。
- 本轮本地测试使用受控钱包、CLOB、RPC、地区与持仓响应；没有输入真实助记词或私钥，没有真实签名订单、资金转移或链上操作。首轮提交 `d4446f1a4818fa781cf701535c9c73aa18460f74` 与备份补强提交 `0844bc5da1153da729d9d369e046a8820ea9b8ae` 均已发布；后者本地 **207 项 Python 测试、7 项前端测试**及 Ruff/JS 语法检查通过，[Actions run 37463286536](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/37463286536) 的 Ubuntu x86_64/ARM64 与 Docker verify 均成功。服务器镜像也已更新到 `0844bc5`；停机加密备份、TEST 账本逐项指纹、应用验证、恢复 RUNNING、HTTPS 页面和钱包未连接时 START 禁用的现场结果见 [HANDOFF.md](../HANDOFF.md) 最新章节。真实 LIVE 验收仍未进行。

## 2026-10-05—06 四项维护修复与部署验证

- LIVE 发单失败区分确定未发出与请求结果不明：签名准备、身份生成和重试管理器获取失败会结束本地订单并释放资金/Event 占用；进入可能发出请求的边界后，网络异常或取消会保留占用、暂停新买并等待对账。回归包含真实异步工作线程取消窗口；没有使用真实钱包或交易所私有接口。
- 开放挂单核对在远端查询后读取最新持久化订单；首次未知时再做一次完整远端查询，排除查询期间已结束的旧响应，持续未知挂单仍阻止新买。
- `/api/health` 增加交易运行时故障、恢复故障、LIVE 账户错误和 LIVE 维护任务状态；正常人工 PAUSED 保持健康。健康响应使用稳定错误码，不输出私有错误详情。
- 全部活动订单查询增加 SQLite 部分索引，旧库启动时自动创建；保留按 Event 查询索引和全部历史数据。
- 本地完整 Python 测试 **151 项通过**；Ruff 检查与格式检查、前端 JS 语法检查及 **3 项前端测试**通过。代码提交 `552b02d8aaf2719cb5230403c01cba8d41dad38e` 已发布到 GitHub `main`；[Actions run 37335756478](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/37335756478) 的 Ubuntu x86_64/ARM64 作业及 Docker verify 均通过。
- 腾讯云服务器使用该代码构建并启动镜像 `sha256:6f3cce552bacba4b72ef7429ee783debb87bbca98fc9b3a27faa3a3fa20b1035`。停机前制作 age 加密备份，TEST 的 2,355 条原生事件内容哈希、486 条订单记录的数量、账本代次和规则设置哈希在升级前后保持一致；LIVE 仍为 0 条事件及订单。两个模式的 SQLite `quick_check` 均为 `ok`，新 `intents_open` 索引均存在；`/api/TEST/validation` 返回 `ok=true`、`errors=[]`。
- 2026-10-06 约 00:03 CST 恢复 TEST 后，`/api/health` 返回 HTTP 200、`status=ok`、TEST/RUNNING、LIVE=false、`backgroundErrors={}`；容器 healthy、零重启且未 OOM。6/6 行情组与 1,161/1,161 个盘口 READY，最近完成的扫描含 21,712 个 Event，当前扫描/行情/服务错误为空。现场采样不等于长期稳定性或真实实盘验收；未使用真实钱包签名、下单、批准、赎回或链上写入。备份标识和完整核对值见 [HANDOFF.md](../HANDOFF.md)。

## 2026-09-20 长期 TEST 稳定性修复

- 最终代码 `a4947c5c884cf48b2e500e9ddec73bc628a7d414`：本地89测试、Ruff/格式通过，前端未改且Node语法检查通过；GitHub Actions [35515717722](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/35515717722) 的x86_64/ARM64均成功。用户服务器真实Linux verify镜像89测试通过（9.55秒），以非root身份导入生产模块成功。
- 新增回归覆盖临时500/502/503/504重连与401/403安全暂停、650次订阅增加的有界分组及全覆盖、按需盘口/native缓存、关闭元数据释放及历史持仓重放；1000次无持仓与1000次有持仓但无业务变化的报价均不增加SQLite变更计数。模拟深度缩减持久化、下单前写失败不生成订单、Fill投影写失败安全暂停且只重放一次均通过。
- 再次运行独立公开行情短测：100个Event样本，10个选中Event，48个监控Token，10笔原生模拟Fill，10持仓；运行和同库重启核对均通过，现金112000微单位一致。开发样本最终PAUSED、私有连接0，不是服务器正式账本或实盘验收。
- 服务器保留848条原生历史事件（seq<=1759）的完整序号/ID/类型/payload哈希，升级前后相同；SQLite完整性、原生持仓/周期/目标/预算/现金校验通过，未reset或删表。原配置与账本代次保留。
- 22:16:53 CST在6组1017盘口全部READY、pending=0及账本验证通过后，通过认证HTTPS/CSRF/Origin恢复用户已授权的正式TEST。22:17复核容器healthy、零重启，TEST/RUNNING、LIVE=false，扫描/分类/流/服务当前错误均为空。历史故障时间保留，不伪装成从未发生。
- 同一2GB主机采样：故障时容器约1.364GiB、available127MiB、swap约1.3GiB、I/O wait46–52%；恢复后容器646.5MiB、available966MiB、swap163MiB，采样I/O wait为0%。这是现场前后采样，不是相同市场负载的严格基准，也不等于已经通过长期无人值守验收。持续运行区间和后续复核见HANDOFF.md。

## 范围与版本

2026-09-21 11:50 CST补充跨夜采样：同一a4947c5镜像healthy、零重启/OOM，TEST/RUNNING、LIVE=false；6组1011盘口全部READY、pending=0，32持仓31 READY/1 NO_BID，账本校验通过。内存642.8MiB、available约1GiB、swap183MiB、采样I/O wait=0%。最新完成扫描11:44:12并已进入下一轮，当前四类错误为空。距恢复约13.5小时，但不把未采样区间当连续全量监控或72小时验收；管理网络间歇连通问题见HANDOFF。

验证日期：2026-09-06。原 PM-SMALL `eb8c6d8a09f9b0427890b7a2d744fc3485d1d3dc`；NautilusTrader 1.231.0，源码 `27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`。所有账本为本次新建开发数据，不导入旧仓位。

此次没有真实钱包导入、真实订单签名、实盘单、approve/redeem 或其他链上写入。LIVE 测试明确使用可控 CLOB/账户/RPC 响应；不能将其表述为真实交易或链上验收。

## 已有真实结果

| 项目 | 实际结果与证据 |
|---|---|
| 原项目完整测试 | 当前 Node 24.20.0 环境：32个测试文件、324项通过；`.reference/original-tests.log`。此前权限失败是历史结果，本次重新实测 |
| 两处原缺陷复核 | 在原 `PaperMarketProcessor` + `PaperDatabase` 上另外执行2个缺陷复现用例，确认低Bid变化会重复卖掉未补充的高Bid深度，getOrderBook的Ask版本导致已消费Bid再被预演计入；`.reference/original-defects.log`。该2项是证实错误存在，不是声称旧实现正确 |
| Mac 新程序回归 | 68项通过，2项依赖弃用警告，1.38秒；Ruff check/format与Node语法检查通过。包括原生运行、LIVE回报、赎回、API和生命周期，不只测试纯规则 |
| 前端 | Node `--check src/pm_nautilus/web/app.js` 通过；无单独前端编译步骤，原生静态HTML/CSS/JS由FastAPI提供 |
| 公开行情到模拟成交 | 实际 Gamma/WS：采样100个公开Event，选10个完整Event、108个合格Token；10个READY，14笔原生模拟Fill，10个持仓，现金4.37U；同库重启现金4.37U，数量/预算/目标/现金核对通过，结束PAUSED。日志 `.reference/public-smoke.log`，重跑 `scripts/public_smoke.py` |
| Linux | Linux aarch64 / Debian bookworm / Python3.12.14：最终代码 e91563f5723609f126ed540d9bd3d3d36f795de6 镜像68项通过（2.84秒），生产base镜像启动成功；`.reference/linux-verified-68.log` |
| GitHub/自有服务器 | 公开仓库已创建并持续同步 main；双架构 CI 通过，详情见下节。服务器后续已完成独立 TEST 部署、HTTPS 入口、认证、备份、只读巡检与正式 TEST 启动；动态运行状态和最新 SHA 见 HANDOFF.md |

公开行情短测使用独立临时目录、100U初始资金，每轮10U、进度100%、最长365天、比例1%、全部市场类型的明确开发设置，便于在有限时间内产生可验证成交。这不是产品默认配置，也不是正式长期TEST；生产发现仍为全部分页，无10个Event/108Token上限。此前默认设置的短测只有真实盘口、没有成交，不计为成交端到端成功。

## 回归覆盖

- `test_rules.py` / `test_arbitration.py`：参数边界、费用净份额、tick及99¢上限、多档目标、共享深度、小目标聚合、逐价位消费与重连；六级仲裁及稳定ID逐层确定预期，输入顺序不影响Winner；生命周期ASC/DESC；30秒不同Bid版本、等值/空值/倒退/不可逆退出。
- `test_runtime.py`：真实Nautilus下单/成交/账户/仓位；部分卖出与重启；1U现金计价多档买入；投影写失败后暂停并只重放一次；Fill当时规则快照；当前READY先排序；旧周期预算/止损冻结、新Fill用新目标；多档FAK归组但目标仍逐Fill独立。
- `test_live.py` / `test_live_recovery.py`：真实Nautilus LIVE执行队列及所选官方REST/WS schema，MATCHED不记可支配成交、CONFIRMED去重、恢复签名订单归属与base quantity、未刷新现金不再融资、合法tick下取整且不提高预算。HTTP与签名结果为可控响应。
- `test_live_redemption.py` / `test_settlement.py`：0/0.5/1结果，等待、失败、错误回款、同一raw/hash重播、重启继续、实际余额刷新失败、32块/Transfer/烧毁权利核验、手动混合权利拒绝；到账但尚未本地结算的窗口不重复计总资金；本地结算意图中断后不重复链上赎回、不重复记账。
- `test_market.py`：完整原始N、正式结果与不明确等待、303个Event分页到末页、静态兄弟未知阻断/已收到空书不阻断，生产无隐藏总数截断。
- `test_app.py` / `test_live_health.py` / `test_lifecycle.py`：CSRF/Host/认证、TEST/LIVE配置与reset隔离；未知LIVE现金；活动意图SQL索引；瞬时核对失败恢复但不自动START；激活失败清理、关闭等回报与适配器任务后关SQLite；旧回调失效、后台异常暂停并健康503。

测试中注入的模拟响应和时钟只是证据生成手段，实际业务使用原生Nautilus对象及同一份规则。测试未删除旧断言或导入旧账本。

## UI 实际操作

通过本机浏览器操作真实API：

- 320px配置面板与含持仓页面均无横向溢出；桌面维持紧凑单列。
- 修改最低买价的未保存草稿跨刷新保留，保存后生效；START/PAUSE按钮正确转换。
- LIVE初始余额只读/未知显示“—”，未启用时START禁用；LIVE保存每轮2U后切回TEST仍1U。
- 使用独立21Event/21持仓模拟账本：默认20项，展开为21/21；扫描展开全部、ASC/DESC切换正确；记录默认折叠、展开读取21总条数，按原UI显示最近20条。
- 该离线样本重启后行情未知，估值显示未知，现金79U；没有假设旧快照仍可成交。浏览器控制台未发现error/warn。
- 临时视口覆盖已恢复。所有UI样本均为开发目录，不作为正式TEST账本交付。

## 剩余真实环境验收

实际钱包类型/签名及资金账户、CLOB权限/授权、POL余额、RPC、标准及neg-risk真实链上交易、真实服务器架构/HTTPS入口均未实际验收。现有代码仅支持EOA和单所有者Safe；其他钱包不能冒充已支持。缺这些条件不影响本地源码、部署配置和可控验证交付。

依赖目前产生2项弃用警告（Starlette/AnyIO别名、websockets legacy）；不影响本次通过结果，固定依赖升级时复核，不在迁移中随意换版本。

## Linux 生产镜像与同库重启实测

使用真实Docker、非root UID10001、只读rootfs、临时/tmp、全部capability删除、no-new-privileges及独立持久卷。启动检查版本SHA与代码一致、健康ok、TEST/PAUSED/LIVE=false；未认证dashboard返回401，认证后HTML/JS/API可读。首次保存每轮2U，再restart同一容器同一数据卷，仍读取2U、现金100U、相同generation、LIVE独立1U，最后恢复TEST默认1U，未START。证据 `.reference/container-first.json`、`.reference/container-restart.json`。

Compose在缺PM_GIT_REVISION时明确拒绝，补完整SHA后config --quiet通过。2026-09-07 又通过 GitHub Actions 实测 Linux x86_64 与 ARM64，见下节。该临时Linux环境不等于已部署用户自有服务器。


## 2026-09-07 公开发布与恢复验证

用户明确授权公开上传、服务器延后。仓库为 [yvettemiranda/PM-NAUTILUS](https://github.com/yvettemiranda/PM-NAUTILUS)，默认分支 main。首次发布提交 `f39c3de95de6a514c3e0555e1774ccd789cb0a73` 的 [GitHub Actions run 34071046061](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/34071046061) 全部通过：

- Ubuntu 24.04 x86_64：宿主68测试通过（7.22秒），Docker verify内68测试通过（6.20秒）。
- Ubuntu 24.04 ARM64：宿主68测试通过（6.87秒），Docker verify内68测试通过（5.49秒）。
- 两组均通过 Ruff、格式及 Node 语法检查；均有相同2项依赖弃用警告。

公开范围检查覆盖首次发布前 main 历史74个唯一blob：禁止路径和常见私钥/token模式扫描无告警。只推送 main，不上传 `.reference/`、runtime、实际环境文件、凭据或本机其他Git引用。该检查不声称能识别任意形式的秘密。

恢复演练发现 uv 0.8.22 的内置Python下载目录没有3.12.14；已在 REINSTALL.md 改为先用 uv 0.12.10 下载Python，再用锁定的 uv 0.8.22 安装项目依赖。新下载的 Python 3.12.14 已实测安装成功，不依赖旧 Codex 内置 Python 路径。

干净恢复验证：从实际 GitHub 公开 URL 克隆到新目录，使用独立下载的 Python3.12.14 和新 `.venv`，uv0.8.22按锁文件安装75个包；Ruff check/format通过，68测试通过（首次冷启动82.46秒，2警告），JS语法检查通过。未复制旧虚拟环境或参考源码。首次服务探测20秒超时，等待首次依赖加载后健康成功；未绕过系统安全检查。离线服务实测 `status=ok, mode=TEST, strategyStatus=PAUSED, liveExecutionEnabled=false`，首页及app.js均HTTP200，随后停止临时服务。此验证恢复的是开发环境，不是运行账本或真实钱包。
# 2026-09-13 心跳与订阅恢复回归

- 77项本地测试通过；包括繁忙WS仍定期PING、PONG超时、仅重订阅缺失完整盘口、新增Token保持旧连接，以及未变化元数据不重复入库。既有逐Fill目标、共享深度、持久化恢复回归仍通过。
- 官方依据：[市场WebSocket](https://docs.polymarket.com/api-reference/wss/market)明确要求每10秒文本PING。真实公开探针35秒收到4次PONG，取消再订阅同一公开Token后再次收到book。没有真实签名或资金动作。
- 独立公开行情模拟短测产生14笔Fill，10个native持仓，验证与同库重启均通过，结束TEST/PAUSED；不是正式服务器长期TEST验收。
- 服务器部署、原账本保留及新运行区间见HANDOFF.md。没有通过减弱持久化或引入未知盘口估值来隐藏故障。
- 补充跨连接完整快照时间基线回归：新快照允许重新建立基线但保留双侧消耗量，已就绪后继续拒绝旧时间戳，未就绪不接受增量。78项测试通过，GitHub Actions 34755364023 在 Linux x86_64/ARM64 均测试及Docker verify成功；最终代码提交bcec57d。
- 19:48只读采样：第一轮心跳修复版本已运行约5小时，5组905盘口均READY、待定事件0、当前扫描/分类/流/服务错误为空，22个持仓，账本和SQLite校验通过。历史最近流事件为17:29的tick变更触发受控重连，不是新的心跳超时。此为按需采样，不宣称全时段零中断或长期验收完成。
- 最终bcec57d服务器部署复验：新容器healthy，扫描21427事件后5组READY、缺失盘口事件0，22持仓20 READY/2 NO_BID；362条维护前原生事件逐条与备份一致、preferences一致。20:13:19 CST认证控制请求恢复TEST，LIVE仍false。部署过程中源码权限问题已修复，过程和剩余容量风险详见HANDOFF。

## 紧凑首页与真实收益采样（2026-09-22，部署前）
- 用户批准以赚亏与运行状态为主的紧凑 UI，并授权推送 GitHub 与服务器：TEST/LIVE + 状态点、小收益曲线、资金明细、持仓/记录/市场分页及可展开详情，适配手机。
- 累计净盈亏沿用已实现+未实现口径；每60秒独立采样到 equity_samples，按模式/generation隔离，最近1440个样本展示。所有旧交易事件保留；不反推历史行情，未知估值/重启/缺采样处断开曲线。采样故障单独展示，不改变策略状态。
- 保留500毫秒面板刷新，展开状态保持；页面连接失败不显示全绿，NO_BID不冒充断线；LIVE仍关闭，策略参数不变。
- 本地91项Python测试、3项JS状态/曲线测试、Ruff与JS语法通过；320/390px浏览器无横向溢出，持仓展开、记录、LIVE隔离与禁用已实测。服务器及GitHub完成情况将在部署后补记。

## UI正式部署验收（2026-09-22 15:10 CST）
- 应用最终提交及运行镜像：`27146fb1c2bca490a714faac89012932a564217a`。GitHub Actions `35697922799` 的 Linux x86_64/ARM64 全部通过（91项Python测试、3项JS测试、Ruff、语法、Docker verify）；服务器前一应用提交 `1cf3aae` 亦实际完成91项Linux测试，最终差异仅HTML资源版本和文档。
- 服务器先完成镜像构建，再暂停TEST并停机备份。保留两份0600完整备份：`backups/pre-ui-20260922.tgz`（55,367,071字节）和 `backups/pre-ui-cache-20260922.tgz`，含runtime/server、.env、compose.override.yaml。未覆盖旧备份，未reset。
- 首次版本写入命令因换行转义失败而安全停止，修正后启动；正式浏览器随后发现历史JS/CSS缓存混用，补加资源版本标识并于15:07:58启动最终镜像。此维护打断此前连续运行区间，不声明72小时稳定性验收通过。
- 停机备份与新实例逐条核对：955条原生事件的序号、ID、类型和原始payload全部一致；generation、preferences、initial_capital、business完全一致（含持仓周期、目标与盈亏）；.env除镜像SHA外不变，compose.override.yaml逐字节一致。SQLite quick_check和应用账本validation均通过；没有清理books、模拟消耗或历史。
- 确认新实例PAUSED、完整行情恢复、账本校验通过后，通过正常HTTPS认证与CSRF接口于15:10:06恢复此前已授权TEST。15:10:21只读复验：TEST/RUNNING、LIVE=false、healthy、restarts=0、OOM=false、backgroundErrors为空。
- 全量扫描15:09:09完成，8组1429盘口全部READY、pending=0，扫描/分类/行情/服务当前错误均为空。41持仓，现金91.567528U、总资产104.242650U、已实现23.099122U、未实现-18.856472U。以上为瞬时模拟值。
- 容器约489.5MiB；主机available990MiB、swap170MiB。Nginx配置检查通过，HTTPS原配置保留，.env与两份备份仍0600。
- 正式Chrome已验证新UI、真实收益采样、195条交易记录入口、持仓展开、运行绿点；无浏览器脚本错误。本地320/390px无横向溢出；模拟断网明确标记旧快照并禁用控制。开发服务已停止，开发数据与正式账本分离。
- 本段为交付文档补记；后续文档同步不重启运行镜像。曲线每分钟采样，只展示最近1440条，旧采样仍保留；09-22部署前的历史没有被伪造为曲线。最大回撤/分类表现等扩展报告未在本次实现。
