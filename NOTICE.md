# 代码来源与第三方声明

本应用为 PM-SMALL 的独立迁移实现。Web UI 由用户指定原仓库
`yvettemiranda/PM-SMALL` 的 `eb8c6d8a09f9b0427890b7a2d744fc3485d1d3dc`
版本复用并适配；原项目源代码及账本没有作为依赖或历史数据库导入。

NautilusTrader 1.231.0 作为独立、未修改的 Python wheel 依赖安装；其代码版权属于
Nautech Systems Pty Ltd 及贡献者，许可证为 LGPL-3.0。
原始许可证随本项目保留于 `LICENSES/NautilusTrader-LGPL-3.0.txt`。
对应完整上游源代码：
https://github.com/nautechsystems/nautilus_trader/tree/27a8e54e7ac3c57d6cbf8891f0283dfbaee97317

本项目使用其公开扩展接口，不复制或静态嵌入框架核心。其余依赖及精确来源见
`pyproject.toml`、`uv.lock` 与各安装包自带许可证。发布包不包含第三方 wheel。
