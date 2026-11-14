# 版本维护规则

`main` 是当前开发和部署入口，包含单账户 TEST、持续录制、历史盘口回测和受保护的 LIVE。部署固定通过验证的完整应用 SHA，已部署版本见 [HANDOFF](../HANDOFF.md)。

十账户改造前的基准提交 [`4de107a`](https://github.com/yvettemiranda/PM-NAUTILUS/commit/4de107a4dc1a4abaaf08b330eb00803391268e2a) 保留在 `main` 的提交历史中。多账户改造和后续保护也仍可从历史追溯。

只整理分支、标签和文档，不重写或压平 Git 提交历史。老提交、合并记录和历史 CI 属于追溯资料，不是额外维护的版本。清理前的全部引用和完整仓库已制作本地可恢复 bundle；删除分支不等于删除交易账本或服务器备份。

后续下载、开发和维护统一从 `main` 开始。已有源码目录应先检查未提交修改，再获取并切换到当前入口；不要继续使用已删除的旧分支名作为远端拉取参数。

```sh
git clone --branch main https://github.com/yvettemiranda/PM-NAUTILUS.git
```

`main` 的文档提交可以比已部署应用 SHA 更新。2026-10-29 整理仅改 Markdown；2026-10-30 的 [四项保护修复](archive/EGM_FIXES_20261030.md)随后叠加 [Hallmark 界面更新](archive/UI_HALLMARK_20261030.md)，上述固定 SHA 已完成双架构 CI 和服务器验收，后续只同步验收文档时不重启镜像。完整 SHA 和验收证据以 HANDOFF 为准，源码更新不表示镜像自动更新。GitHub 源码不是运行数据备份；换服和回滚按 [部署手册](DEPLOY.md)保全全部账本、模式意图、钱包及受保护配置。

根目录 [README](../README.md)、[HANDOFF](../HANDOFF.md) 和 [VALIDATION](VALIDATION.md)描述当前版本。带日期的审计与设计材料用于解释原始需求或当时的修复，其中的旧数字、旧版本、地区和钱包状态不能覆盖当前证据。

若启用过 `c96feed` 的新策略开关，回退前要核对账本是否已写入新偏好、市场标记或按市场区分的周期键。这些记录需要支持该功能的版本读取。回退前先保全当前账本，再核对目标版本与恢复点。
