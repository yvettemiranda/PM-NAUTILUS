# 公开的交易规则快照

`strategy-profile.json` 是用户同意公开保存的 15 项交易设置。2026-09-24 从旧服务器当前 **TEST** 账本只读提取，并由项目 `Preferences` 模型校验；同日又从服务器只读确认 TEST 与空白 LIVE 的设置完全相同，公开文件与服务器 TEST 设置的规范 JSON SHA-256 也相同。这个文件不含模拟成交、真实交易记录、资金、钱包地址、私钥、API 凭据或服务器密码。

新服务器的空白 TEST 与 LIVE 账本**不会自动读取**此文件。首次安装、首次启动前按 [新手指南](../docs/START_FRESH_LIVE.md)确认它仍是想用的规则，再用 `scripts/apply_preferences.py` 导入两个全新账本，并启动后分别读回核对。导入工具拒绝用过的账本；已有真实交易时须迁移完整 LIVE 账本。

如果以后在服务器网页改了规则，这个 GitHub 文件不会自动更新。要更新给未来新服务器使用的版本，应在有本项目锁定依赖、并能读取相应账本的可信环境只读导出；下例适用于已经安装 `uv` 依赖的环境：

```sh
uv run --frozen python scripts/export_preferences.py --db runtime/server/TEST/state.sqlite --mode TEST
```

先核对输出只有 `Preferences` 的 15 项，再替换本文件旁的 JSON、运行测试并提交推送 GitHub。也可以选择 LIVE 模式导出，但应先确认所选模式正是要留给未来的规则。不要把整个数据库、网页完整面板或凭据文件提交到仓库。
