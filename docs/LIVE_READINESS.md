# 实盘准备检查（2026-09-08）

本次为代码、公开文档及 Polygon 只读检查，不是实盘验收。未读取钱包秘密信息、签名、下单、授权或赎回。继续保持服务器 LIVE 禁用。

## 已核实

- 固定依赖为 NautilusTrader 1.231.0、py-clob-client-v2 1.0.1。已安装 SDK 的 Polygon `exchange_v2`、`neg_risk_exchange_v2` 与官方 Contracts 页面一致；未升级依赖。
- 重新读取官方[英文](https://docs.polymarket.com/resources/contracts)与[中文](https://docs.polymarket.com/cn/resources/contracts)页面，CTF、pUSD、标准与 Neg-Risk collateral adapter 地址均与 `redemption.py` 一致。此前聊天中的地址冲突判断不应作为改地址的依据。
- 公共 RPC 返回 chain ID 137；上述四个合约及两个 V2 Exchange 均有非空代码。这只证明目标已部署，不能代替 ABI、实际授权、交易与回款验证。
- `load_settings` 显式检查 LIVE 开关、秘密文件权限、必要字段及签名类型；当前完整路径仅支持 EOA(0) 和单签 Safe(2)。Safe 检查 threshold=1 且 signer 属于 owners。实际用户公开地址与历史交易不写入公开仓库。
- TEST/LIVE 分库；实盘账户初始化依赖实际连接、账户同步与自有订单核对。历史人工持仓不会被整体导入；正常对账检查本程序仓位是否被实际数量覆盖。赎回时要求同 Condition 的实际份额与本程序权利精确一致，以免覆盖手动资产。
- 赎回先持久化 raw/hash/nonce 再广播；查询确认深度、到账 Transfer、余额烧毁和预期回款。当前 Safe 路径由 owner 发送外层 Polygon 交易，因此 owner 仍需 POL；不能因 Safe 支持平台中继就假定本程序免费代付。

## 必须纠正的操作说明

`LIVE + PAUSED` **不等于只读模式**。按既定规则，PAUSE 停止新买，但已有仓位仍可卖出、止损和赎回；`app.py` 在 LIVE attach 时挂接 RedemptionService，且没有独立的“关闭自动赎回”配置。`auto_approve_redemption=false` 只关闭自动授权，不能阻止已授权仓位赎回。

真正只读预检应独立运行，不启动应用 LIVE runtime、不构造签名钱包、不导入私钥，仅使用公开地址和只读 RPC/HTTP 方法核对。需要认证的账户检查应在独立、限定只读调用的工具中进行。当前尚未交付这样的独立预检工具；不能用应用 START/PAUSE 代替它。

## 尚未验收

- 签名类型 3 不在当前完整路径内；是否开发取决于最终选定账户，不因用户使用 OKX 就推断为类型 3。
- 实际 CLOB 凭据、签名、交易 allowance、余额、私有行情/回报、真实部分成交与重启核对尚未验收。
- 标准/Neg-Risk 的真实授权、赎回与 32 块确认闭环尚未验收；合约有代码不等于流程正确。
- 正式 TEST 的卖出、结算及回款需继续积累实际证据；仅运行满 72 小时不能替代这些验收项。

## 本次本地验证

`pytest -q`：70 passed，2 个既有第三方弃用警告；Ruff check 与 format --check 通过。本次没有修改运行代码或依赖，没有重启服务器。
