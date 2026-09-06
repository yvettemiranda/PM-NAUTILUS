# PM-NAUTILUS 接手记录

## 范围与交付边界
完整执行根目录 PM-SMALL_Nautilus_Codex_Instructions.md。新应用、新账本、仅管理本程序交易；保留原 UI 和完整业务规则；实现 TEST、LIVE 接入及自动赎回。默认 TEST + PAUSED，LIVE 未启用。禁止实际导入钱包、签单、实盘单、approve/redeem 等链上写操作。正式长期 TEST 不自动启动。

## 当前进度
- 已完整读取根指令，工作区初始只有该指令和 .DS_Store。
- 2026-09-06 远端 PM-SMALL main = eb8c6d8a09f9b0427890b7a2d744fc3485d1d3dc，与要求一致。
- 已比较 v1.231.0 与 v2.0.0rc4，选定并安装 v1.231.0，源码 SHA 27a8e54e7ac3c57d6cbf8891f0283dfbaee97317。Mac Python 3.12.14 可运行。
- 已完成 docs/MIGRATION.md 整体设计及逐条迁移表，识别官方模拟/费用/迟到回报与本项目规则的差异。
- 已实现整数规则、逐价位消费、原生 Strategy/DataEngine/RiskEngine/ExecutionEngine/Cache/Portfolio、持久原生事件重放和业务投影。复制原 UI，尚未接通 API。
- 当前针对性验证：17 个纯规则测试、2 个原生运行/重启测试通过。尚未完成整体回归、真实行情端到端、LIVE/赎回及 Linux/UI 验收。
- 当前继续：公开全分页发现、WS 行情、UI/API；随后 LIVE 签名/回报/核对、自动赎回、部署及完整验证。

## 实际缺项
- ~/.codex/templates/agent_memory/ 不存在；不能伪称已按原模板创建三个文件。当前用本文件维护完整进度，模板补齐后按模板建立。
- multi-agent skill 未在现有技能目录找到。尚未委派子代理。
- 已定位 Codex bundled Python/Node，.venv 已建立，uv 已安装。系统无 Docker/gh，后续复核替代运行位置。
- 未提供本次明确 GitHub 上传目标确认、服务器地址/SSH 用户/运行目录。先完成本地交付与部署准备。

## 已运行命令与结果
- git ls-remote https://github.com/yvettemiranda/PM-SMALL.git HEAD refs/heads/main：两者均为指定 SHA。
- git ls-remote https://github.com/nautechsystems/nautilus_trader.git refs/tags/v1.231.0：标签对象 d3e1685e979925d7b0ffacd1b3f442547686e18f（尚须核对 commit）。
- uname -ms：Darwin arm64。

## 下一步
1. 修正现金计价买单在框架 RiskEngine 中的表达，完善回报去重、恢复和增量调度。
2. 实现 market.py、app.py、原 UI 的模式隔离接入，真实公开数据只做短期开发验证。
3. 实现 live.py、redemption.py，使用可控响应验签路径/归属/失败/重启，禁止实际私钥或链上写。
4. 完成锁文件、CI、部署文件、操作手册、完整代码归档；运行规则/UI/Linux/恢复整体验证并记录实际缺项。
