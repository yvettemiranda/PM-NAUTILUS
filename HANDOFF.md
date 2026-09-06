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

## 交付与继续入口
- README.md：启动入口；docs/DEPLOY.md：完整操作步骤；docs/VALIDATION.md：结果、覆盖与未验收部分。
- artifacts/FULL_SOURCE.md：所有受控文本文件完整内容（含锁文件，无省略）；SOURCE_MANIFEST.json逐文件SHA256及最终提交。
- artifacts/PM-NAUTILUS-source.zip为完整源码；PM-NAUTILUS.bundle保留分段Git历史。以git log及manifest为最终交付版本，不把代码验证SHA误认为后续纯文档提交SHA。
- .reference/下保存原项目、公开短测、缺陷复现及Linux证据，不入Git。runtime/ui-preview、runtime/ui-acceptance为开发样本，不作为正式账本。
- 临时UI服务已在确认PAUSED后SIGTERM停止；Linux验收容器已healthy验证后停止，临时Lima pm-verify已请求停机，保留镜像/卷供复验，不作为生产服务。

## 实际外部缺项
1. 根指令仅拟定首次私有上传目标yvettemiranda/PM-NAUTILUS。已核对gh登录yvettemiranda(id169894659)，当时同名仓库不存在；尚缺首次目标确认，未创建远端/推送，未运行远端CI。
2. 尚缺自有服务器地址、SSH用户、新应用目录和公网入口切换要求。未部署任何自有服务器，也未停止/更改旧项目或旧服务。
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
