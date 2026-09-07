# PM-NAUTILUS

PM-SMALL 完整业务规则迁移到 NautilusTrader 1.231.0，保留原手机/桌面 UI。
新建独立 TEST/LIVE 账本，不导入旧交易，不接管手动仓位。

**默认 TEST + PAUSED，LIVE 未启用。** 启动服务只连接公开行情；START 才允许新买。
PAUSE 后已有仓位的目标卖出、止损与结算继续。正式长期 TEST 不自动启动。

**电脑重装后请从 [恢复开发与接手指南](docs/REINSTALL.md) 开始。**

## 本地启动

先按恢复指南安装锁定依赖，在克隆目录执行：

```sh
uv run --frozen pm-nautilus --data-dir runtime/local
```

打开 [本地页面](http://127.0.0.1:8765)。新账本为100U、每 Event 每轮1U。
完整安装、认证、GitHub上传、Linux部署和回滚步骤见 [运维手册](docs/DEPLOY.md)。

## 文档与验证

- [整体迁移设计及规则对应表](docs/MIGRATION.md)
- [当前进度、外部缺项及继续入口](HANDOFF.md)
- [真实执行的验证结果及未验证范围](docs/VALIDATION.md)
- [依赖版权声明](NOTICE.md)

```sh
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pytest -q
node --check src/pm_nautilus/web/app.js
```

`scripts/public_smoke.py` 是独立临时账本的公开行情开发短测，使用明确放宽的
开发配置和采样范围，不替代生产全分页发现、不使用钱包、不运行正式长期活动。

LIVE 代码包含官方 CLOB V2 接入、自有订单恢复、EOA/单所有者 Safe 自动赎回。
真实凭据、实际钱包类型和真实链上验收必须另行落实；可控测试不能证明真实链上交易成功。
