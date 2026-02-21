import asyncio
import json
import os
import ssl

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

DATA_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "exchangerate_icbc_data.json",
)


@register(
    "astrbot_plugin_exchangerate_icbc", "Yuuu0109", "工商银行汇率监控插件", "1.0.1"
)
class ICBCExchangeRatePlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.plugin_config = config or {}
        self.data: dict = {
            "monitors": {},  # userId/groupId -> list of monitor rules
            # monitor rule: {"currency": "美元", "condition": "高于", "threshold": 7.2, "last_triggered": False}
        }
        self.last_rates: dict[str, dict] = {}
        self.monitor_task: asyncio.Task | None = None
        self.load_data()

    def load_data(self):
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, encoding="utf-8") as f:
                    file_data = json.load(f)
                    # Support legacy save config
                    if "monitors" in file_data:
                        self.data["monitors"] = file_data["monitors"]
                logger.info(f"成功加载汇率监控数据: {self.data}")
        except Exception as e:
            logger.error(f"加载汇率监控配置失败: {e}")

    def save_data(self):
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存汇率监控配置失败: {e}")

    async def initialize(self):
        logger.info("初始化工商银行汇率监控插件...")
        self.monitor_task = asyncio.create_task(self.monitor_loop())

    async def terminate(self):
        logger.info("销毁工商银行汇率监控插件...")
        if self.monitor_task:
            self.monitor_task.cancel()

    async def fetch_exchange_rates(self) -> list[dict]:
        url = "https://papi.icbc.com.cn/exchanges/ns/getLatest"
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.icbc.com.cn/column/1438058341489590354.html",
            "Origin": "https://www.icbc.com.cn",
        }
        try:
            ssl_context = ssl.create_default_context()
            # OP_LEGACY_SERVER_CONNECT helps mitigate UNSAFE_LEGACY_RENEGOTIATION_DISABLED
            ssl_context.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=headers, json={}, ssl=ssl_context
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("code") == 0:
                            return data.get("data", [])
                        else:
                            logger.error(f"API请求返回错误代码: {data}")
                    else:
                        logger.error(f"API请求失败，状态码: {resp.status}")
        except Exception as e:
            logger.error(f"获取汇率数据异常: {e}")
        return []

    def format_rate_info(self, rate_info: dict) -> str:
        name = rate_info.get("currencyCHName", "")
        code = rate_info.get("currencyENName", "")
        buy = rate_info.get("foreignBuy", "N/A")
        sell = rate_info.get("foreignSell", "N/A")
        c_buy = rate_info.get("cashBuy", "N/A")
        c_sell = rate_info.get("cashSell", "N/A")
        pub_time = (
            f"{rate_info.get('publishDate', '')} {rate_info.get('publishTime', '')}"
        )

        return f"【{name}({code})】\n现汇买入: {buy}  现汇卖出: {sell}\n现钞买入: {c_buy}  现钞卖出: {c_sell}\n更新时间: {pub_time}"

    @filter.command("icbc")
    async def query_rate(self, event: AstrMessageEvent, currency: str = ""):
        """实时查询工商银行外汇牌价。用法: /icbc [币种，如：美元]"""
        rates = await self.fetch_exchange_rates()
        if not rates:
            yield event.plain_result("无法获取最新汇率数据，请稍后重试。")
            return

        if currency:
            for rate in rates:
                if currency in rate.get(
                    "currencyCHName", ""
                ) or currency.upper() in rate.get("currencyENName", ""):
                    yield event.plain_result(self.format_rate_info(rate))
                    return
            yield event.plain_result(f"未找到关于包含 {currency} 的汇率信息。")
        else:
            # 默认展示常见货币
            focus_currencies = ["USD", "HKD", "EUR", "JPY", "GBP"]
            results = []
            for rate in rates:
                if rate.get("currencyENName") in focus_currencies:
                    buy = rate.get("foreignBuy", "N/A")
                    sell = rate.get("foreignSell", "N/A")
                    results.append(
                        f"{rate.get('currencyCHName')}({rate.get('currencyENName')}): 现汇买入 {buy}, 现汇卖出 {sell}"
                    )

            summary = (
                "实时工商银行汇率 (部分):\n"
                + "\n".join(results)
                + "\n* 发送 '/icbc 美元' 获取详细信息"
            )
            yield event.plain_result(summary)

    @filter.command("icbc_add")
    async def add_monitor(
        self, event: AstrMessageEvent, currency: str, condition: str, threshold: float
    ):
        """添加汇率监控规则。用法: /icbc_add 美元 高于 7.2"""
        if condition not in ["高于", "低于"]:
            yield event.plain_result(
                "条件必须为 '高于' 或 '低于'。例如: /icbc_add 美元 高于 7.2"
            )
            return

        session_id = event.get_sender_id()
        # 为了更好地支持群聊，我们可以取群组ID如果是在群里
        # 如果获取不到，就用发送者ID
        if not session_id:
            yield event.plain_result("无法识别当前会话。")
            return

        monitors = self.data.get("monitors", {})
        if session_id not in monitors:
            monitors[session_id] = []

        rule = {
            "currency": currency,
            "condition": condition,
            "threshold": float(threshold),
            "last_triggered": False,
        }
        monitors[session_id].append(rule)
        self.data["monitors"] = monitors
        self.save_data()

        yield event.plain_result(
            f"成功添加监控: 当 {currency} 现汇卖出价 {condition} {threshold} 时将通知您。"
        )

    @filter.command("icbc_rm")
    async def remove_monitor(self, event: AstrMessageEvent, currency: str):
        """删除特定币种的汇率监控。用法: /icbc_rm 美元"""
        session_id = event.get_sender_id()
        monitors = self.data.get("monitors", {})
        if session_id not in monitors:
            yield event.plain_result("当前没有任何监控规则。")
            return

        initial_len = len(monitors[session_id])
        monitors[session_id] = [
            r for r in monitors[session_id] if r["currency"] != currency
        ]

        if len(monitors[session_id]) < initial_len:
            self.data["monitors"] = monitors
            self.save_data()
            yield event.plain_result(f"已删除 {currency} 的监控规则。")
        else:
            yield event.plain_result(f"未找到 {currency} 的监控规则。")

    @filter.command("icbc_ls")
    async def list_monitors(self, event: AstrMessageEvent):
        """查看当前会话的汇率监控列表。用法: /icbc_ls"""
        session_id = event.get_sender_id()
        monitors = self.data.get("monitors", {}).get(session_id, [])
        if not monitors:
            yield event.plain_result("当前没有任何监控规则。")
            return

        results = [
            f"- {r['currency']} 现汇卖出价 {r['condition']} {r['threshold']}"
            for r in monitors
        ]
        yield event.plain_result("当前的监控规则:\n" + "\n".join(results))

    @filter.command("icbc_help")
    async def help_cmd(self, event: AstrMessageEvent):
        """获取工商银行汇率监控插件的使用帮助。用法: /icbc_help"""
        help_text = (
            "工商银行汇率监控插件使用说明：\n"
            "/icbc [币种名称]：实时查询指定币种的汇率（留空返回常用币种）。\n"
            "/icbc_add [币种] [高于/低于] [数值]：添加汇率监控规则。\n"
            "/icbc_rm [币种]：删除特定的汇率监控规则。\n"
            "/icbc_ls：查看当前已配置的汇率监控列表。\n"
            "/icbc_help：查看此帮助信息。\n"
            "※ 后台实时刷新频率可通过 AstrBot 的 WebUI 进行配置。"
        )
        yield event.plain_result(help_text)

    async def monitor_loop(self):
        while True:
            try:
                freq = self.plugin_config.get("frequency_minutes", 30)
                await asyncio.sleep(freq * 60)

                monitors = self.data.get("monitors", {})
                if not monitors:
                    continue

                rates = await self.fetch_exchange_rates()
                if not rates:
                    continue

                # 遍历所有监控规则
                for session_id, rules in monitors.items():
                    messages = []
                    for rule in rules:
                        cur_name = rule["currency"]
                        condition = rule["condition"]
                        threshold = rule["threshold"]

                        # 查找目标汇率
                        target_rate = None
                        for rate in rates:
                            if cur_name in rate.get(
                                "currencyCHName", ""
                            ) or cur_name.upper() in rate.get("currencyENName", ""):
                                target_rate = rate
                                break

                        if not target_rate:
                            continue

                        # 获取现汇卖出价用于触发
                        try:
                            sell_price = float(target_rate.get("foreignSell", 0))
                        except ValueError:
                            continue

                        if sell_price <= 0:
                            continue

                        triggered = False
                        if condition == "高于" and sell_price > threshold:
                            triggered = True
                        elif condition == "低于" and sell_price < threshold:
                            triggered = True

                        if triggered and not rule.get("last_triggered", False):
                            messages.append(
                                f"⚠️ 汇率预警: {target_rate['currencyCHName']} 现汇卖出价为 {sell_price}，已{condition}设定的阈值 {threshold}！"
                            )
                            rule["last_triggered"] = True
                            self.save_data()
                        elif not triggered and rule.get("last_triggered", False):
                            # 汇率回落/升回，重置触发状态
                            rule["last_triggered"] = False
                            self.save_data()

                    # 推送通知
                    for msg in messages:
                        logger.info(f"触发推送给 {session_id}: {msg}")
                        try:
                            # 尝试使用 context 的 send_message 推送
                            await self.context.send_message(session_id, [Plain(msg)])
                        except Exception as e:
                            logger.error(f"消息推送失败: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"汇率监控后台任务报错: {e}")
