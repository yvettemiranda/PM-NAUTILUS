# 历史盘口回测

打开页面右上角「回测」，下载人工构造的样例行情包，上传后选择时段、初始资金和参数。样例可用 1U 初始资金、1U 每轮金额、同时允许 2¢ 买入，复现 1 次买入和 2 次部分卖出；把最高买价降至 1.9¢ 可对照。上传真实行情包后，采用相同步骤。网页能叠加同一行情包、同一时段和同一起始资金的上一次结果，也能导出交易明细和曲线 JSON。

网页任务从空仓开始。复制 TEST 设置会读取当前参数，运行及修改回测参数在独立的工作目录和进程内完成。曲线缺估值时留空。回测采用 TEST 的买卖档位、剩余深度、价格档位、手续费、部分成交、持仓退出和模拟结算规则。模拟不包含下单及资金操作。参数比较固定行情包、时段和初始资金，先逐笔核对再比较净值。

## 准备真实历史盘口

当前转换器读取本地 [Pendulum Flow v3.1 盘口档案](https://archive.pendulumflow.com/v3.1/) 的分类 Parquet 和同小时同分类的 provenance Parquet。档案按 UTC 小时和类别分段，可只下载所需范围；两份输入应先同发布清单核对 SHA-256。请遵守档案许可和服务的下载限制。网页接收转换后的 NDJSON。大量数据在有空间的机器处理，程序服务器留给小范围试验。

同时准备策略可能看到的两边合约及同一事件的候选市场。使用对应时段保存的 Polymarket Gamma **完整事件响应**，包括该事件当时包含的市场和规则。市场和结算分别记录当时可见或核验的时刻。最终状态的 Gamma 响应里可能含结束时间、费率和其他后来改变的字段，不能据此断定以前已看到相同字段。官方历史成交可核对盘口，历史价格可核对走势。缺档不能用成交价补成虚构盘口。[Polymarket 市场资料](https://docs.polymarket.com/api-reference/markets/get-market-by-id) · [官方成交](https://docs.polymarket.com/api-reference/feeds/list-trades) · [官方结算](https://docs.polymarket.com/api-reference/markets/get-resolution-state)

在与行情文件同一目录新建清单 `manifest.json`，填入实际市场和文件。时间用带时区的 ISO 8601；同一份清单不必重复记录未变化的 metadata。主动标注已知的断流 `gaps`。缺少某小时所有文件时，转换器会标记全体断流，此后必须有新快照才能继续使用该盘口。档案的市场解决通知关闭该市场，结算现金必须来自与市场身份匹配的、注明观察时间的官方确认。

```json
{
  "source": "Pendulum Flow 短窗口",
  "start": "2026-11-13T05:00:00Z",
  "end": "2026-11-13T05:59:59.999Z",
  "conditions": ["0x...真实的64位条件ID..."],
  "metadata": [{"observedAt": "2026-11-13T05:00:00Z", "events": ["gamma-event.json"]}],
  "officialTags": [],
  "hours": [{
    "hour": "2026-11-13T05:00:00Z",
    "files": [{
      "lean": "2026-11-13T05.pop-culture.parquet",
      "provenance": "2026-11-13T05.pop-culture.provenance.parquet"
    }]
  }],
  "settlements": [],
  "gaps": [],
  "notes": ["在此记录档案版本、与策略候选名单核对的结果、缺口或明确假设。"]
}
```

如需按栏目回测，`officialTags` 填对应时期的官方栏目 ID；每次结算在 `settlements` 加 `{"observedAt":"…","market":"gamma-resolved-market.json"}`。转换器需要来自观测时间的市场快照。尽量纳入回测起点之前已有行情的预热区间，等到快照后再模拟。仅有价格变化而没有初始化盘口时，更新会被跳过。输出覆盖标记为部分覆盖。使用了哪些市场、有效盘口数、已知断流和未知估值采样可在结果里检查。覆盖比例以当时的候选市场与时段校验，成交量占比不能代替。

```sh
uv run --frozen python -m pm_nautilus.backtest_archive /path/to/manifest.json /path/to/market.ndjson
uv run --frozen python -m pm_nautilus.backtest_cli /path/to/market.ndjson /path/to/run.json \
  --preferences /path/to/parameters.json --cash 100 \
  --start 2026-11-13T05:10:00Z --end 2026-11-13T05:59:59Z
```

参数文件使用 [策略快照](../config/strategy-profile.json) 的字段格式。日期范围以行情包覆盖为限；单次最长 366 天。生产网页每次运行一个任务，最多保存八份 256 MiB 行情包，超出空间、时间或内存预算时拆小范围，或改在独立机器用上述命令运行。日期、净值缺口、数据缺口及资金口径应随结果一起保留。

## 和已有 TEST 1 对照

挑 TEST 1 原参数稳定的一段，用期初资金和当时的状态。如果窗口起始已有持仓，只在本地用与回测起点一致的、停机备份中的 TEST 1 数据库副本，命令增加 `--checkpoint /path/to/copied-state.sqlite --checkpoint-at <起点时间>`。副本须有清楚的采集时刻，不能用窗口结束时或最新的数据库代替。`--cash` 填原账本的初始本金，回测自动读取副本在起点的可用资金和持仓。保持源数据只读；不上传账本或凭证到网页、GitHub 或行情提供方。实际参数曾变动时，先分段验证。

比较两边每笔买卖的合约、方向、策略动作、时间、价格和数量，也看净值采样、无成交和未配对的交易。从目标周期另取一段验证，再扩大到 30 天或半年。短样本上对得上不代表早期档案有同样覆盖。回测可辅助比较参数和模拟成交，实盘还会受送单延迟、盘口变化及真实流动性影响。

可用本地对照工具导出同期实际 TEST 的原始成交与净值。导出用包含整个对照窗口的**期末副本**，与回测使用的期初副本分别保存。初始数据和结果均留在本地。时间参数为 Unix 毫秒。

```sh
uv run --frozen python -m pm_nautilus.backtest_compare export /path/to/end-copy.sqlite \
  --start-ms 1794546600000 --end-ms 1794547500000 > observed.json
uv run --frozen python -m pm_nautilus.backtest_compare compare run.json observed.json \
  --tolerance-ms 5000 > comparison.json
```
