# TEST 账户与旧账本

TEST 只提供原来的 1 号账户，继续使用 `TEST/state.sqlite`。已有本金、参数、持仓、订单、交易历史、收益采样和模拟盘口消耗都沿用原账本。新目录以 100U 初始资金和 PAUSED 状态创建 TEST 1。

## 从十账户版本升级

旧 TEST 2–10 的账本留在 `TEST/accounts/N/state.sqlite`，继续纳入完整数据备份和回滚。新版本移除它们的页面和 API 入口，不创建或运行这些账户。删除旧 Git 分支与这些账本无关。

归档前，旧账户必须已暂停且没有持仓、未完成订单、待结算或待恢复记录。新版在取得数据目录的进程锁后核对停机的数据，拒绝忽略非空 WAL、回滚日志或者未经确认的账本。核对用只读连接，不建立旧账户的 SQLite WAL／SHM 文件。升级、保留旧账本和回滚步骤见[部署手册](DEPLOY.md#从十账户版本升级)。

旧环境中的 `PM_TEST_ACTIVE_ACCOUNT=1` 可以保留以便回滚。新版本只接受空值或 1。切换到过去的十账户版本须走正常备份和回滚流程，不能只把旧镜像直接套在生产目录上。

## 启停、资金与重置

- 进程重启后，TEST 以 PAUSED 启动。若保存过 LIVE 模式意图，TEST 后台暂停，LIVE 也不自动启用。核对后可明确“恢复 TEST 后台（保持暂停）”，再单独 START。
- PAUSE 停止新买入，已有持仓的目标卖出、止损和结算仍继续处理。START 会用当前完整候选盘口和原有排序判断买入。
- 排队或核对中的 START 绑定发起时的运行实例及账本代次。之后的 PAUSE、RESET 或服务关闭会使它失效。
- 初始资金只在暂停且无历史时能直接修改。重置需要两次确认，会清空 TEST 1 的模拟历史并恢复默认设置。

TEST 与 LIVE 的自动执行后台互斥。两者使用各自账本。启用 LIVE 仍须暂停 TEST 并通过既有钱包和账户检查。

## API 兼容

TEST 的 dashboard、preferences、start、pause、reset、trade-records、validation、performance 和 restore-background 仍接受 `account_id=1`，省略时默认 TEST 1。其它账户号返回参数错误。LIVE 接口仍使用独立账本，不接受 TEST 账户号。

健康接口报告 `testAccountCount=1` 与 `activeTestAccountId=1`。实际加载和可用状态分别见 `loadedTestAccountCount` 和 `availableTestAccountCount`。

## 目标卖价与价格步长

目标卖价先取“实际买价＋加价”与“实际买价×倍数”中较高者，再向上对齐市场允许的最小价格步长，保留 99¢ 上限。举例，买价 3¢、加价 1¢、倍数 1.67，计算出 5.01¢。若市场步长是 1¢，合法目标价为 6¢。市场步长是 0.1¢ 时，目标价是 5.1¢。每笔买入记录自己的卖出目标，后来修改参数不会重算旧持仓。
