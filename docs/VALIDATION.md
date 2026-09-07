# 验证结果与边界

## 范围与版本

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
| GitHub/自有服务器 | 2026-09-07 已创建公开仓库并推送 main；双架构CI通过，详情见下节。服务器由用户明确推迟至重装电脑后，尚未部署 |

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
