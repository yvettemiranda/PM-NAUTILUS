# 自动存储维护与磁盘保护

2026-10-20 实现。发布和服务器验收状态以最新 [HANDOFF](../HANDOFF.md) 与 [VALIDATION](VALIDATION.md) 为准。机制减少人工清理频率，并在空间不足时提前限制新增风险；不承诺磁盘、文件系统或其他硬件故障时仍能无中断交易。

## 自动执行的工作

| 范围 | 触发与频率 | 行为 |
| --- | --- | --- |
| 应用容器日志 | Docker 自动轮转 | 单文件 10 MiB，最多 3 份；不重启 Docker、不手工截断其日志文件 |
| 主机可回收文件 | systemd 每 15 分钟，随机延迟最多 60 秒 | 只处理专属登记目录的过期文件；保留最新两份及 pinned 文件 |
| 旧程序镜像 | 使用率达到 80% 或可用空间不超过 6 GiB 时 | 只删除超过 14 天、无任何容器引用、未固定的 `pm-nautilus:<40位SHA>` 镜像；保留当前、最新两份回滚镜像及显式固定版本 |
| 运行时容量检查 | 每 30 秒，文件系统探测在线程执行 | 检查应用账户真正可用的字节和 inode；健康接口及页面显示当前保护状态 |
| 冷公共市场缓存 | WARNING 状态、每分钟至多一批 | 检查最多 128 行，协作工作预算 25 ms；仅删除结束超过 90 天且没有任何所有权引用的关闭市场元数据 |

主机清理使用独占锁，单次最多处理 2 项、45 秒预算，并以低 CPU/IO 优先级运行。容量恢复到使用率不超过 70% 且可用空间至少 8 GiB 后解除主机清理滞回。使用率达到 90% 或可用不超过 2 GiB 时标记严重；没有可安全回收内容时明确记录需处理，不扩大删除范围。

正常新增 Token 已压缩保存。冷缓存删除产生的 SQLite 空页供后续写入复用，**不会立即缩小操作系统看到的库文件**。不在线执行全库 VACUUM，不自动停机或重启程序。SQLite 对删除与重建文件的语义见 [官方 VACUUM 文档](https://www.sqlite.org/lang_vacuum.html)；容器日志选项见 [Docker 官方说明](https://docs.docker.com/engine/logging/drivers/json-file/)。

## 运行时保护边界

磁盘 WARNING 为可用空间不超过 `max(3 GiB, 总量的15%)`；恢复至 `max(4 GiB,20%)` 才清除。inode WARNING 为不超过 `max(5000,5%)`，恢复至 `max(7500,7.5%)`。WARNING 仍允许原交易规则运行，且在不扫描时小批维护公共缓存。

磁盘 CRITICAL 为可用空间不超过 `max(1 GiB,5%)`；恢复至 `max(2 GiB,10%)` 才解除。inode CRITICAL 为不超过 `max(1000,1%)`，恢复至 `max(2000,2%)`。检测失败、采样超过 90 秒或检测任务停止也阻止新增买入。

- 在 START、BUY 意图创建前和 LIVE 最终 HTTP POST/重试边界检查；等待期间容量变化不能绕过保护。
- 暂缓批量扫描落库、公共缓存清理和收益采样，保留当前行情及已有订单/持仓维护的资源。扫描未完整成功时不伪造 lastScanAt，不因存储保护写 PAUSED 或清除其他故障。
- 已提交订单的回报、对账、卖出及赎回仍按原所有权和先持久化后发送规则处理。已有未明结果及资金预留不会因磁盘压力释放或自动重下；实际落账失败时不能继续产生无法持久化的新外部交易。
- 不改变用户的 RUNNING/PAUSED 选择；容量恢复到安全余量后，原本 RUNNING 的实例可恢复新买入，PAUSED 不会自动启动。START 仍需正常账本和账户准入检查。
- `/api/health` 和 dashboard 的 `storage` 包含容量、inode、采样新鲜度、保护原因和冷缓存维护计数，不依赖新增数据库写入。存储保护导致健康接口 503，但应用仍响应，Docker 不因 unhealthy 自动重启。页面保留 PAUSE 操作，阻止受保护状态的 START。

## 永不自动删除的数据

钱包、密钥、运行数据库/WAL/SHM、交易意图（含历史终态）、原生事件、账本、收益历史、未结算及赎回权利、旧部署/回滚备份都不属于主机清理范围。

冷公共缓存清理只删 `tokens` 行。任何 intents、持久化或内存 books、monitored/native instruments、cycles、targets、claims/rights 以及关联 Event/Condition 引用均保护对应身份；无法识别、缺索引、数据异常、外部写锁或超时均回滚整批。`intents_token` 索引避免扫描完整订单历史。删除后在同一 owner loop 立即移除对应内存缓存；重新出现的公开市场仍可被完整发现。

历史账本持续增长且不能随意删除。如果空间被保留数据或其他服务占满，机制会进入保护并提示处理，不能靠无限删除保证永远不扩容。这里的“无人工清理”适用于正常可回收内容，不等于无限磁盘容量。

## 安装与维护

先部署通过验收的新镜像，使日志轮转与运行时保护生效。容器重建会应用日志配置；不用重启 Docker。随后以 root 安装已审核工具和定时单元：

```sh
install -d -m 0700 /var/lib/pm-nautilus-maintenance /opt/pm-nautilus/backups/managed-space
install -d -m 0755 /usr/local/libexec
install -m 0755 scripts/storage_maintenance.py /usr/local/libexec/pm-nautilus-storage.py
install -m 0644 deploy/pm-nautilus-storage.service.example /etc/systemd/system/pm-nautilus-storage.service
install -m 0644 deploy/pm-nautilus-storage.timer.example /etc/systemd/system/pm-nautilus-storage.timer
```

在实际 service 的 ExecStart 加上所需的 `--protect-revision FULL_SHA`，固定每个仍需使用的回滚镜像；先确认镜像存在，缺失会拒绝镜像清理。保留 service 的资源和权限限制。首次先试运行确认输出，再启用：

```sh
python3 /usr/local/libexec/pm-nautilus-storage.py run
systemctl daemon-reload
systemctl enable --now pm-nautilus-storage.timer
systemctl start pm-nautilus-storage.service
systemctl status pm-nautilus-storage.timer --no-pager
```

工具默认 dry-run，service 使用 `run --apply`。状态为 `/var/lib/pm-nautilus-maintenance/status.json`（0600）；巡检脚本会读取状态和 timer。没有目录内文件会被隐式登记。

只有确认可过期的专属归档才可先移入新建的 `backups/managed-space`（0700），再登记：

```sh
python3 /usr/local/libexec/pm-nautilus-storage.py register disposable.log.gz --expires-days 30
```

仅接受普通单链接 `.log`、`.log.gz`、`.tgz.age` 文件，单个不超过 512 MiB，登记及删除前核对身份、大小、修改时间和 SHA-256；拒绝符号链接及路径穿越。这个命令不适用于唯一备份、钱包/数据库或既有回滚目录。需要保留的归档登记时加 `--pinned`，最新两份也始终保留。

## 验证

本地 847 项 Python、34 项页面、Ruff 检查/格式（101 文件）、JS/shell 语法通过。新增测试以受控 statvfs、真实 Runtime/SQLite 与模拟网络验证阈值滞回、过期/失败探测、START/签名后 BUY、已有 SELL 和迟到成交、共享扫描隔离、只删无归属公共缓存、锁/回滚/时间预算及路径和镜像保护。未填满实际用户磁盘，没有用 LIVE 真实交易验证压力场景。CI、服务器定时任务及现场结果另记最新验收。
