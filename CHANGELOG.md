# Changelog

## [1.0.1] - 2026-02-21
### Added & Fixed
- 采用 `_conf_schema.json` 图形化配置后台刷新频率，废弃原有的频率查询和设置指令。
- 实现 `/icbc_help` 指令：获取帮助和指令一览。
- 修复并跳过 `UNSAFE_LEGACY_RENEGOTIATION_DISABLED` SSL报错问题解决 API 请求失败。

## [1.0.0] - 2026-02-21
### Added
- 初始版本发布。
- 实现 `/icbc` 指令：实时获取工商银行汇率数据。
- 实现 `/icbc_add` 和 `/icbc_rm` 指令：根据设定的阈值监控汇率波动。
- 实现 `/icbc_freq` 指令：设置后台刷新的时间间隔。
- 实现 `/icbc_ls` 指令：查看当前的监控规则列表。
- 集成 aiohttp 异步请求，遵循 AstrBot 插件开发规范。
