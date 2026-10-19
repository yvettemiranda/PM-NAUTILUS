# 元数据存储与扫描调度优化（2026-10-19）

[PR #14](https://github.com/yvettemiranda/PM-NAUTILUS/pull/14) 已合并，正式应用固定为 `6dac94bf6bc9cbc954a22e112a52dd49a798d2a6`；[main CI 39224291130](https://github.com/yvettemiranda/PM-NAUTILUS/actions/runs/39224291130) 的 x86_64/ARM64 回归与 Docker verify 全部通过。后续纯文档提交可更新服务器源码，镜像/env/runtime 继续固定该应用 SHA，无须为文档暂停或重启。正式整理与恢复验收结果见本页末节。

本轮实现无损 Token 压缩、活跃身份索引、扫描与控制维护解耦及完整校验独立等待。保留全部 Token、原生事件、订单、策略参数、业务状态和 TEST 消费深度。TEST 延迟对照、完整周期收益诊断及 tick 定向恢复留待后续，不在本轮实现范围。

## 格式与兼容

保留 `tokens(token_id,payload)` 两列。新 payload 是 v1 JSON 外壳，含 `_token_storage`、严格布尔 `open`、UTF-8 原文 `size` 和 base64/zlib `data`；完整 Token 字段可离线还原。公共压缩字典字节冻结，改变须增加 codec 版本并继续兼容旧格式。reader 同时接受旧平面 JSON，保留缺省字段语义。

重复字段、未知版本、损坏或尾随压缩流、主键身份/内外 open 不一致均拒绝，不跳行或截断。完整 Token JSON 上限 1 MiB、外壳上限 2 MiB，远大于生产最大 1,128 字节；这不限制扫描市场数量。活跃部分索引与历史 intent 主键读取保留关闭交易身份；恢复缺索引的旧包时正确降级，重新打开或离线整理会建索引。首次建索引仍有扫描成本，正常启动不自动批量转码旧数据。

**新数据库不能直接由旧镜像读取。** 维护回滚须使用旧镜像配对应的完整旧数据库、配置和钱包快照；恢复运行产生新交易后，不得回退旧快照丢失新增记录。

## 离线整理

`scripts/compact_metadata.py --data-dir DATA_ROOT --backup-dir NEW_PRIVATE_DIR [--vacuum]` 仅供停止应用后运行。取得实际 `process.lock`，核对两库身份，先创建 TEST/LIVE 的 `0600` SQLite 回滚副本（目录 `0700`），不构造 Store/Runtime、不改状态或钱包。工具已随生产镜像放入 `/app/scripts/`，可在 `--network none`、覆盖 entrypoint 的独立维护容器运行。

每库独立事务逐批转码、建索引，提交前比较所有表/列的逻辑值与完整 Token，并执行 full integrity_check。失败回滚该库、保留备份和 incomplete manifest。两库不是跨文件原子提交，第二库失败时第一库可能已验证完成，须按 manifest 处理。两库回滚副本不代替含配置和加密钱包的完整部署备份。

`--vacuum` 显式重建文件、需要额外空间和维护时间，完成后再次比较全部逻辑摘要并 full integrity_check。未选该项时负载已压缩但物理文件可能未缩小。最终 checkpoint，并在私有 manifest 保存备份 SHA-256、行数、逻辑摘要、前后大小和完成状态。整理后仍须真实账本副本的 Runtime 重放及完整 validation，文件检查不代替业务验收。

维护容器应使用实际数据目录的 UID/GID；`--user 0` 配 `--cap-drop ALL` 不会获得绕过文件权限的能力。私有整理目录由同一 UID/GID 持有并直接挂载，外层完整备份目录继续仅 root 可访问。大库 VACUUM 不应使用 64 MiB 的 `/tmp`：指定 `SQLITE_TMPDIR=/data` 或另一个有足够磁盘空间且该 UID 可写的私有挂载，保留只读根目录、无网络、drop ALL 与 no-new-privileges。不要通过放宽账本或钱包权限解决维护问题。

## 扫描和巡检

每个 MarketService 只管理一个扫描任务，300 秒从上次完成计；5 秒控制维护不再等待网络分页。首次扫描尝试结束前不主动订阅持久化旧元数据。完整分页/成功后应用、共享源发现锁、tick 版本保护、TEST/LIVE 隔离保留。常规请求/格式失败保留旧数据与 lastError，未预期失败仍记录 serviceError 并安全暂停；关闭取消并等待任务，拒绝迟到结果。同步 SQL 提交和订阅工作仍可能占用事件循环，不宣称所有处理均已异步化。

巡检对普通请求保持 20 秒，完整 validation 单独等待 180 秒并输出耗时；超时标为结果未知，不以 quick_check/缓存代替完整校验。

## 本地证据

752 项 Python（26.12 秒、两条既有第三方弃用警告）、33 项页面、Ruff check/format（93 文件）、JS/shell 语法通过；独立复核无确定 P1/P2 问题。新增 47 项涵盖旧新格式、字典版本、损坏拒绝、历史身份、旧包降级、双库备份、锁/事务/非 Token 污染、Native 重放、TEST 消费深度、慢扫描及关闭/共享竞态。

实际实现的合成基准：936,800 个 Token、23,976 open 和 100 个历史引用，未使用生产私有数据。原库 1,053,872,128 字节；Token 负载 813,745,257→518,375,212 字节（减少 36.3%），事务压缩 34.33 秒；未 VACUUM 文件 1,056,043,008 字节，可选 VACUUM 后 725,295,104 字节（较原库减少 31.2%）。首次 open 索引 0.756 秒，active 查询及解码 1.287→0.381 秒（约 3.38 倍）。没有证明 full integrity 提速；以上不保证服务器同等比例。

生产基线：10-19 13:32–13:35 CST 应用仍 `3e765cc`、源码 `c83d79c`，TEST/RUNNING、LIVE disabled、healthy/零重启，765/765 盘口 READY；完整 validation PASS（28.958 秒），SQLite quick_check=ok。主库 1,067,622,400 字节，tokens 占 962,498,560 字节，936,802 行中 23,976 open。本段为升级前基线；正式整理与恢复结果如下，不以基线或合成测试代替。

## 服务器整理与最终验收

服务器实际整理 941,620 条 TEST Token，文件 1,073,287,168→730,410,672 字节，减少 342,876,496 字节（31.95%）；LIVE 无 Token，69,632→73,728 字节，增加的 4 KiB 来自新索引。两库全表逻辑摘要及完整 integrity_check 通过，没有删除历史。此比例是本次停机整理结果，恢复扫描后文件仍会随新增数据增长。

2026-10-19 20:40:33 CST 最终服务器验收 PASS：源码/镜像/env/runtime 在验收时一致，容器 healthy、restarts=0、OOM=false、后台错误为空；已恢复原 TEST/RUNNING，LIVE=false、钱包因重启 LOCKED。在线完整 TEST validation PASS（57.873 秒），真实 TEST 副本离线 Runtime full validation、全仓摘要和分页历史核对通过。原参数、代次、两库全部停机逻辑数据、native/equity 历史、加密钱包及依赖/配置/安全设置保留；部署 JS 哈希、HTTPS/认证及未认证钱包接口 401 通过。

恢复后的公开行情采样：748/748 盘口、4/4 连接组 READY，扫描/分类/当前流与服务错误为空；lastScanAt=2026-10-19T12:38:08.486703+00:00。这是本次短时验收，不构成长时间稳定性或收益保证。只读巡检已同步新版，完整 validation 单独等待 180 秒；旧脚本在私有备份内保留，权限未扩大。

一致性私有回滚及完整 age 加密备份保存在 `/opt/pm-nautilus/backups/pre-metadataopt-20261019T120207Z`；密文 SHA-256 `40dfa36bd7e1acba8c3a04379e9289a1afc84983a0c8123519d534102e2998d0`。两库回滚副本、recipient 加密及密文哈希通过；未演练解密恢复。旧镜像回滚必须配维护前完整旧库/配置/钱包快照，恢复交易后不得用旧快照丢失新增记录。

维护时首次容器因 UID/文件权限不匹配在转码前退出；后续在识别小容量临时目录风险后，只停止了该次离线维护容器，再以原数据 UID/GID、磁盘临时目录和新的私有整理目录重试。重试前后全部逻辑核对通过，未放宽生产账本/钱包权限。维护中 TEST 停机，恢复时间以本节验收为准，此次维护打断旧连续运行区间。

本轮没有解锁钱包、启用 LIVE、签订单或链上交易。地区限制、pUSD/POL 与真实买卖/正额赎回闭环仍未在本轮解决或实测；不能以 TEST/CI 通过认定实盘就绪。
