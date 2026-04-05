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

- `/icbc [币种名称]`：实时查询指定币种（如：美元、日元）的汇率，若留空会默认返回常见币种列表。设定了监控的币种会显示历史最高最低价及时间。
- `/icbc_add_buy [币种] [条件(高于/低于)] [数值阈值]`：添加购汇价监控。例如 `/icbc_add_buy 美元 低于 7.0`。
- `/icbc_add_sell [币种] [条件(高于/低于)] [数值阈值]`：添加结汇价监控。例如 `/icbc_add_sell 美元 高于 7.2`。
- `/icbc_del_buy [币种]`：删除指定币种的购汇价监控。例如 `/icbc_del_buy 美元`。
- `/icbc_del_sell [币种]`：删除指定币种的结汇价监控。例如 `/icbc_del_sell 美元`。
- `/icbc_list`：查看当前已配置的汇率监控项与曲线追踪列表。
- `/icbc_cron [cron表达式]`：自定义后台监控更新的频率。默认 `*/30 * * * *`。
- `/icbc_chart_add [币种]`：添加汇率曲线追踪，开启后后台会自动定频储备历史数据。
- `/icbc_chart_del [币种]`：删除汇率曲线追踪。
- `/icbc_chart [币种]`：生成并查看指定币种历史走势的高清图表。
- `/icbc_chart_auto [on/off]`：开启或关闭定时自动推送图表走势。
- `/icbc_chart_cron [cron表达式]`：自定义后台走势图自动推送频率，默认只在工作日中午执行 `0 12 * * 1-5`。
- `/icbc_help`：查看所有的指令帮助说明。

## 注意事项

- 本插件基于 aiohttp 实现异步爬虫。
- 数据监控和配置会自动保存在 `data/plugin_data/astrbot_plugin_exchangerate_icbc/exchangerate_icbc_data.json` 文件中，插件更新时不受影响。

## 更新日志

- 详细更新内容请查看 [CHANGELOG.md](./CHANGELOG.md)。
