import asyncio
import json
import os
import ssl
from datetime import datetime

import aiohttp
import croniter

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
    "astrbot_plugin_exchangerate_icbc", "Yuuu0109", "工商银行汇率监控插件", "1.0.5"
)
class ICBCExchangeRatePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data: dict = {
            "cron": "0 * * * *",
            "monitors": {},  # userId/groupId -> list of monitor rules
        }
        self.last_rates: dict[str, dict] = {}
        self.monitor_task: asyncio.Task | None = None
        self.load_data()

    def load_data(self):
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, encoding="utf-8") as f:
                    file_data = json.load(f)
                    if "monitors" in file_data:
                        self.data["monitors"] = file_data["monitors"]
                    if "cron" in file_data:
                        self.data["cron"] = file_data["cron"]
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

        return f"【{name}({code})】\n结汇价: {buy}  购汇价: {sell}\n现钞买入: {c_buy}  现钞卖出: {c_sell}\n更新时间: {pub_time}"

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
                        f"{rate.get('currencyCHName')}({rate.get('currencyENName')}): 结汇价 {buy}, 购汇价 {sell}"
                    )

            summary = (
                "实时工商银行汇率 (部分):\n"
                + "\n".join(results)
                + "\n* 发送 '/icbc 美元' 获取详细信息"
            )
            yield event.plain_result(summary)

    @filter.command("icbc_add_buy")
    async def add_monitor_buy(
        self, event: AstrMessageEvent, currency: str, condition: str, threshold: float
    ):
        """添加购汇价监控规则。用法: /icbc_add_buy 美元 低于 7.0"""
        async for res in self._add_monitor_impl(
            event, currency, condition, threshold, "sell"
        ):
            yield res

    @filter.command("icbc_add_sell")
    async def add_monitor_sell(
        self, event: AstrMessageEvent, currency: str, condition: str, threshold: float
    ):
        """添加结汇价监控规则。用法: /icbc_add_sell 美元 高于 7.2"""
        async for res in self._add_monitor_impl(
            event, currency, condition, threshold, "buy"
        ):
            yield res

    async def _add_monitor_impl(
        self, event, currency, condition, threshold, price_type
    ):
        if condition not in ["高于", "低于"]:
            yield event.plain_result("条件必须为 '高于' 或 '低于'。")
            return

        session_id = event.get_sender_id()
        if not session_id:
            yield event.plain_result("无法识别当前会话。")
            return

        monitors = self.data.get("monitors", {})
        if session_id not in monitors:
            monitors[session_id] = []

        for rule in monitors[session_id]:
            if (
                rule["currency"] == currency
                and rule["condition"] == condition
                and rule["threshold"] == float(threshold)
                and rule["type"] == price_type
            ):
                yield event.plain_result(
                    f"检测到重复的监控规则: {currency} {condition} {threshold}，请勿重复添加。"
                )
                return

        rule = {
            "currency": currency,
            "condition": condition,
            "threshold": float(threshold),
            "type": price_type,
            "last_triggered": False,
        }
        monitors[session_id].append(rule)
        self.data["monitors"] = monitors
        self.save_data()

        type_name = "结汇价" if price_type == "buy" else "购汇价"

        cron_expr = self.data.get("cron", "0 * * * *")
        try:
            now = datetime.now()
            cron = croniter.croniter(cron_expr, now)
            next1 = cron.get_next(datetime)
            next2 = cron.get_next(datetime)
            interval_minutes = max(1, int((next2 - next1).total_seconds() / 60))
        except Exception:
            interval_minutes = 60

        yield event.plain_result(
            f"成功添加监控: 当 {currency} {type_name} {condition} {threshold} 时将通知您。\n(当前后台约每 {interval_minutes} 分钟获取一次数据)"
        )

    @filter.command("icbc_del")
    async def remove_monitor(self, event: AstrMessageEvent, currency: str):
        """删除特定币种的汇率监控。用法: /icbc_del 美元"""
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

    @filter.command("icbc_list")
    async def list_monitors(self, event: AstrMessageEvent):
        """查看当前会话的汇率监控列表。用法: /icbc_list"""
        session_id = event.get_sender_id()
        monitors = self.data.get("monitors", {}).get(session_id, [])
        if not monitors:
            yield event.plain_result("当前没有任何监控规则。")
            return

        results = []
        for r in monitors:
            type_name = "结汇价" if r.get("type", "sell") == "buy" else "购汇价"
            results.append(
                f"- {r['currency']} {type_name} {r['condition']} {r['threshold']}"
            )

        cron_expr = self.data.get("cron", "0 * * * *")
        try:
            now = datetime.now()
            cron = croniter.croniter(cron_expr, now)
            next1 = cron.get_next(datetime)
            next2 = cron.get_next(datetime)
            interval_minutes = max(1, int((next2 - next1).total_seconds() / 60))
        except Exception:
            interval_minutes = 60

        yield event.plain_result(
            f"当前的监控规则 (后台约每 {interval_minutes} 分钟获取一次数据):\n"
            + "\n".join(results)
        )

    @filter.command("icbc_cron")
    async def set_cron(self, event: AstrMessageEvent, cron_expr: str):
        """设置后台监控刷新频率的Cron表达式。用法: /icbc_cron "*/30 * * * *\""""
        if not croniter.croniter.is_valid(cron_expr):
            yield event.plain_result(
                "Cron表达式格式不正确，请查看 /icbc_help 获取常用格式示例。"
            )
            return

        self.data["cron"] = cron_expr
        self.save_data()

        # 唤醒现有的 monitor_loop 以应用新 cron
        if self.monitor_task:
            self.monitor_task.cancel()
            self.monitor_task = asyncio.create_task(self.monitor_loop())

        warnings = ""
        fast_crons = ["*/1 ", "*/2 ", "*/3 ", "*/4 ", "*/5 "]
        if any(f in cron_expr for f in fast_crons):
            warnings = "\n\n⚠️ 建议：您设置的频率过快，为了避免触发银行的反爬虫机制导致获取数据失败，建议将轮询间隔设为 60 分钟或更长。"

        yield event.plain_result(
            f"汇率后台监控频率已成功修改为: {cron_expr}。{warnings}"
        )

    @filter.command("icbc_help")
    async def help_cmd(self, event: AstrMessageEvent):
        """获取工商银行汇率监控插件的使用帮助。用法: /icbc_help"""
        help_text = (
            "工商银行汇率监控插件使用说明：\n"
            "/icbc [币种名称]：实时查询汇率。\n"
            "/icbc_add_buy [币种] [高于/低于] [数值]：添加购汇价监控规则。\n"
            "/icbc_add_sell [币种] [高于/低于] [数值]：添加结汇价监控规则。\n"
            "/icbc_del [币种]：删除特定的汇率监控规则。\n"
            "/icbc_list：查看当前已配置的监控。\n"
            "/icbc_cron [cron表达式]：自定义后台监控刷新频率。\n"
            "/icbc_help：查看此帮助信息。\n\n"
            "※ Cron 表达式简易教程：\n"
            "建议：后台轮询间隔尽量设置在 60 分钟或更长，防止过于频繁请求导致数据获取失败。\n"
            "格式: 分 时 日 月 周\n"
            "举例:\n"
            "0 * * * *     (每小时的第0分执行一次，约每60分钟)\n"
            "*/30 * * * *  (每30分钟执行一次)\n"
            "0 * * * *     (每小时的第0分执行一次)\n"
            "0 8 * * *     (每天早上8点执行)\n"
            "0 9,13,18 * * * (每天9、13、18点执行)"
        )
        yield event.plain_result(help_text)

    async def monitor_loop(self):
        while True:
            try:
                cron_expr = self.data.get("cron", "0 * * * *")
                if not croniter.croniter.is_valid(cron_expr):
                    cron_expr = "0 * * * *"

                now = datetime.now()
                cron = croniter.croniter(cron_expr, now)
                next_time = cron.get_next(datetime)
                sleep_seconds = (next_time - now).total_seconds()

                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)

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

                        # 获取现汇价用于触发
                        try:
                            # 根据 type 获取相应价格，如果没有设置（旧版配置）默认使用 selling
                            v = (
                                target_rate.get("foreignBuy", 0)
                                if rule.get("type", "sell") == "buy"
                                else target_rate.get("foreignSell", 0)
                            )
                            price = float(v)
                        except ValueError:
                            continue

                        if price <= 0:
                            continue

                        triggered = False
                        if condition == "高于" and price > threshold:
                            triggered = True
                        elif condition == "低于" and price < threshold:
                            triggered = True

                        if triggered and not rule.get("last_triggered", False):
                            type_name = (
                                "结汇价"
                                if rule.get("type", "sell") == "buy"
                                else "购汇价"
                            )
                            messages.append(
                                f"⚠️ 汇率预警: {target_rate['currencyCHName']} {type_name}为 {price}，已{condition}设定的阈值 {threshold}！"
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
