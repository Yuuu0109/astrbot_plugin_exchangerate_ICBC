# 工商银行汇率监控插件 (astrbot_plugin_exchangerate_icbc)

这是一个用于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的插件，通过工商银行官方实时数据来提供汇率查询和监控功能。

## 功能特性

- **实时汇率查询**：查询工商银行最新的各种外币现汇、现钞买入/卖出价。
- **自定义汇率监控**：设置特定币种和触发条件（如：“高于”或“低于”某阈值），当汇率达到要求时，主动通过机器人通知您。
- **监控频率配置**：允许配置后台扫描更新汇率数据的频率。

## 安装方法

1. 在 AstrBot 的 `data/plugins` 目录下，克隆本项目或者将其放置于此。
2. 安装所需依赖库：

   ```bash
   pip install -r requirements.txt
   ```

3. 启动（或重启）AstrBot 即可加载插件。

## 指令说明

- `/icbc [币种名称]`：实时查询指定币种（如：美元、日元）的汇率，若留空会默认返回常见币种列表。
- `/icbc_add_buy [币种] [条件(高于/低于)] [数值阈值]`：添加购汇价监控。例如 `/icbc_add_buy 美元 低于 7.0`。
- `/icbc_add_sell [币种] [条件(高于/低于)] [数值阈值]`：添加结汇价监控。例如 `/icbc_add_sell 美元 高于 7.2`。
- `/icbc_del [币种]`：删除已有监控。例如 `/icbc_del 美元`。
- `/icbc_list`：查看当前已配置的汇率监控项。
- `/icbc_cron [cron表达式]`：自定义后台监控更新的频率。默认 `*/30 * * * *`。
- `/icbc_help`：查看指令帮助信息。

## 注意事项

- 本插件基于 aiohttp 实现异步爬虫。
- 数据监控和配置会自动保存在 `data/plugin_data/astrbot_plugin_exchangerate_icbc/exchangerate_icbc_data.json` 文件中，插件更新时不受影响。

## 更新日志

- 详细更新内容请查看 [CHANGELOG.md](./CHANGELOG.md)。
