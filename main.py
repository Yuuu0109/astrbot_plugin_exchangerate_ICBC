import asyncio
import json
import os
import ssl
import tempfile
from datetime import datetime, timedelta

import aiohttp
import croniter
from .chart_generator import ExchangeRateChartGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr



@register(
    "astrbot_plugin_exchangerate_icbc", "Yuuu0109", "工商银行汇率监控插件", "1.4.2"
)
class ICBCExchangeRatePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._session: aiohttp.ClientSession | None = None
        self.data: dict = {
            "cron": "*/30 * * * *",
            "monitors": {},  # userId/groupId -> list of monitor rules
            "chart_monitors": {},  # userId/groupId -> list of currency names
            "chart_data": {},  # currency -> list of {time, buy, sell}
            "chart_auto_send": {},  # userId/groupId -> bool
            "chart_cron": "0 12 * * 1-5",  # 图表推送cron，默认工作日中午12点
        }

        self.monitor_task: asyncio.Task | None = None
        self.chart_push_task: asyncio.Task | None = None
        self._monitor_wakeup = asyncio.Event()
        self._chart_wakeup = asyncio.Event()
        self._lock = asyncio.Lock()

        data_dir = StarTools.get_data_dir(
            plugin_name="astrbot_plugin_exchangerate_icbc"
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = str(data_dir / "exchangerate_icbc_data.json")

        self.load_data()

    def load_data(self):
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, encoding="utf-8") as f:
                    file_data = json.load(f)
                    self.data.update({k: v for k, v in file_data.items() if k in self.data})
                logger.info(f"成功加载汇率监控数据: {self.data_file}")
        except Exception as e:
            logger.error(f"加载汇率监控配置失败: {e}")

    async def save_data(self):
        try:
            async with self._lock:
                with open(self.data_file, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存汇率监控配置失败: {e}")

    async def initialize(self):
        logger.info("初始化工商银行汇率监控插件...")
        self._session = aiohttp.ClientSession()
        self.monitor_task = asyncio.create_task(self.monitor_loop())
        self.chart_push_task = asyncio.create_task(self.chart_push_loop())

    async def terminate(self):
        logger.info("销毁工商银行汇率监控插件...")
        if self._session and not self._session.closed:
            await self._session.close()
        if self.monitor_task:
            self.monitor_task.cancel()
        if self.chart_push_task:
            self.chart_push_task.cancel()

    async def fetch_exchange_rates(self) -> list[dict]:
        url = "https://papi.icbc.com.cn/exchanges/ns/getLatest"
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.icbc.com.cn/column/1438058341489590354.html",
            "Origin": "https://www.icbc.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        ssl_context = ssl.create_default_context()
        ssl_context.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
                async with self._session.post(
                    url,
                    headers=headers,
                    json={},
                    ssl=ssl_context,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("code") == 0:
                            return data.get("data", [])
                        else:
                            logger.error(f"API请求返回错误代码: {data}")
                    else:
                        logger.error(f"API请求失败，状态码: {resp.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"获取汇率数据网络异常 (第{attempt}/{max_retries}次): {e}")
                if self._session and not self._session.closed:
                    await self._session.close()
                self._session = None
                if attempt < max_retries:
                    await asyncio.sleep(2 * attempt)
                else:
                    logger.error(f"获取汇率数据失败，已重试{max_retries}次")
            except Exception as e:
                logger.warning(f"获取汇率数据异常 (第{attempt}/{max_retries}次): {e}")
                if self._session and not self._session.closed:
                    await self._session.close()
                self._session = None
                if attempt < max_retries:
                    await asyncio.sleep(2 * attempt)
                else:
                    logger.error(f"获取汇率数据失败，已重试{max_retries}次")
        return []

    @staticmethod
    def _find_rate_by_currency(rates: list, currency: str) -> dict | None:
        """在汇率列表中查找匹配的币种，返回匹配的条目或 None。"""
        # 第一遍：精准匹配
        for rate in rates:
            if currency == rate.get("currencyCHName", "") or currency.upper() == rate.get("currencyENName", ""):
                return rate
        # 第二遍：部分匹配
        for rate in rates:
            if currency in rate.get("currencyCHName", "") or currency.upper() in rate.get("currencyENName", ""):
                return rate
        return None

    @staticmethod
    def _get_cron_interval_minutes(cron_expr: str) -> int:
        """根据 cron 表达式计算相邻两次执行之间的分钟数。"""
        try:
            now = datetime.now()
            cron = croniter.croniter(cron_expr, now)
            next1 = cron.get_next(datetime)
            next2 = cron.get_next(datetime)
            return max(1, int((next2 - next1).total_seconds() / 60))
        except Exception:
            return 60


    @staticmethod
    def format_rate_info(rate_info: dict) -> str:
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
            matched = self._find_rate_by_currency(rates, currency)
            if matched:
                yield event.plain_result(self.format_rate_info(matched))
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

        session_id = event.unified_msg_origin
        if not session_id:
            yield event.plain_result("无法识别当前会话。")
            return

        monitors = self.data.setdefault("monitors", {})
        session_monitors = monitors.setdefault(session_id, [])

        # 检查是否已存在相同 币种+条件+类型 的规则，若存在则更新阈值
        for rule in session_monitors:
            if (
                rule["currency"] == currency
                and rule["condition"] == condition
                and rule["type"] == price_type
            ):
                old_threshold = rule["threshold"]
                if old_threshold == float(threshold):
                    yield event.plain_result(
                        f"检测到重复的监控规则: {currency} {condition} {threshold}，请勿重复添加。"
                    )
                    return
                rule["threshold"] = float(threshold)
                rule["last_triggered"] = False
                await self.save_data()

                type_name = "结汇价" if price_type == "buy" else "购汇价"
                yield event.plain_result(
                    f"已更新监控: {currency} {type_name} {condition} {old_threshold} → {threshold}"
                )
                return

        rule = {
            "currency": currency,
            "condition": condition,
            "threshold": float(threshold),
            "type": price_type,
            "last_triggered": False,
        }
        session_monitors.append(rule)
        await self.save_data()

        type_name = "结汇价" if price_type == "buy" else "购汇价"

        cron_expr = self.data.get("cron", "*/30 * * * *")
        interval_minutes = self._get_cron_interval_minutes(cron_expr)

        yield event.plain_result(
            f"成功添加监控: 当 {currency} {type_name} {condition} {threshold} 时将通知您。\n(当前后台约每 {interval_minutes} 分钟获取一次数据)"
        )

    @filter.command("icbc_del_buy")
    async def remove_monitor_buy(self, event: AstrMessageEvent, currency: str):
        """删除特定币种的购汇价监控。用法: /icbc_del_buy 美元"""
        async for res in self._del_monitor_impl(event, currency, "sell"):
            yield res

    @filter.command("icbc_del_sell")
    async def remove_monitor_sell(self, event: AstrMessageEvent, currency: str):
        """删除特定币种的结汇价监控。用法: /icbc_del_sell 美元"""
        async for res in self._del_monitor_impl(event, currency, "buy"):
            yield res

    async def _del_monitor_impl(self, event, currency, price_type=None):
        """删除监控规则的共享实现。price_type 为 None 时删除该币种所有规则。"""
        session_id = event.unified_msg_origin
        monitors = self.data.get("monitors", {})
        if session_id not in monitors:
            yield event.plain_result("当前没有任何监控规则。")
            return

        initial_len = len(monitors[session_id])
        if price_type:
            monitors[session_id] = [
                r for r in monitors[session_id]
                if not (r["currency"] == currency and r.get("type", "sell") == price_type)
            ]
        else:
            monitors[session_id] = [
                r for r in monitors[session_id] if r["currency"] != currency
            ]

        if len(monitors[session_id]) < initial_len:
            self.data["monitors"] = monitors
            await self.save_data()
            if price_type:
                type_name = "结汇价" if price_type == "buy" else "购汇价"
                yield event.plain_result(f"已删除 {currency} 的{type_name}监控规则。")
            else:
                yield event.plain_result(f"已删除 {currency} 的所有监控规则。")
        else:
            if price_type:
                type_name = "结汇价" if price_type == "buy" else "购汇价"
                yield event.plain_result(f"未找到 {currency} 的{type_name}监控规则。")
            else:
                yield event.plain_result(f"未找到 {currency} 的监控规则。")

    @filter.command("icbc_list")
    async def list_monitors(self, event: AstrMessageEvent):
        """查看当前会话的监控规则和曲线追踪。用法: /icbc_list"""
        session_id = event.unified_msg_origin
        parts = []

        # ===== 监控规则部分 =====
        monitors = self.data.get("monitors", {}).get(session_id, [])
        if monitors:
            cron_expr = self.data.get("cron", "*/30 * * * *")
            interval_minutes = self._get_cron_interval_minutes(cron_expr)
            results = []
            for r in monitors:
                type_name = "结汇价" if r.get("type", "sell") == "buy" else "购汇价"
                results.append(
                    f"- {r['currency']} {type_name} {r['condition']} {r['threshold']}"
                )
            parts.append(
                f"※ 监控规则 (后台约每 {interval_minutes} 分钟获取一次数据):\n"
                + "\n".join(results)
            )

        # ===== 曲线追踪部分 =====
        tracked = self.data.get("chart_monitors", {}).get(session_id, [])
        if tracked:
            chart_data = self.data.get("chart_data", {})
            info_parts = []
            for c in tracked:
                count = len(chart_data.get(c, []))
                info_parts.append(f"- {c}: {count} 条数据")

            auto_send = self.data.get("chart_auto_send", {}).get(session_id, False)
            auto_status = "已开启" if auto_send else "未开启"

            parts.append(
                "※ 汇率曲线追踪:\n"
                + "\n".join(info_parts)
                + f"\n定时推送走势图: {auto_status}"
            )

        if not parts:
            yield event.plain_result("当前没有任何监控规则或曲线追踪。")
            return

        yield event.plain_result("\n\n".join(parts))

    @filter.command("icbc_cron")
    async def set_cron(self, event: AstrMessageEvent, cron_expr: GreedyStr):
        """设置后台监控刷新频率的Cron表达式。用法: /icbc_cron 0 * * * *"""
        cron_expr = cron_expr.strip().strip('"').strip("'")
        if not croniter.croniter.is_valid(cron_expr):
            yield event.plain_result(
                "Cron表达式格式不正确，请查看 /icbc_help 获取常用格式示例。"
            )
            return

        self.data["cron"] = cron_expr
        await self.save_data()

        # 唤醒现有的 monitor_loop 以应用新 cron
        self._monitor_wakeup.set()

        yield event.plain_result(f"汇率后台监控频率已成功修改为: {cron_expr}。")

    # ========== 汇率曲线追踪 ==========

    @filter.command("icbc_chart_add")
    async def chart_add(self, event: AstrMessageEvent, currency: str):
        """添加汇率曲线追踪。用法: /icbc_chart_add 美元"""
        session_id = event.unified_msg_origin
        if not session_id:
            yield event.plain_result("无法识别当前会话。")
            return

        chart_monitors = self.data.setdefault("chart_monitors", {})
        session_monitors = chart_monitors.setdefault(session_id, [])

        if currency in session_monitors:
            yield event.plain_result(f"{currency} 已在曲线追踪列表中，无需重复添加。")
            return

        session_monitors.append(currency)
        await self.save_data()

        yield event.plain_result(
            f"已添加 {currency} 到汇率曲线追踪列表。\n"
            f"后台将定时采集数据，使用 /icbc_chart {currency} 可查看最近走势图。"
        )

    @filter.command("icbc_chart_del")
    async def chart_del(self, event: AstrMessageEvent, currency: str):
        """删除汇率曲线追踪。用法: /icbc_chart_del 美元"""
        session_id = event.unified_msg_origin
        chart_monitors = self.data.get("chart_monitors", {})
        if (
            session_id not in chart_monitors
            or currency not in chart_monitors[session_id]
        ):
            yield event.plain_result(f"未找到 {currency} 的曲线追踪记录。")
            return

        chart_monitors[session_id].remove(currency)
        self.data["chart_monitors"] = chart_monitors
        await self.save_data()
        yield event.plain_result(f"已移除 {currency} 的汇率曲线追踪。")


    @staticmethod
    async def _delayed_remove_file(path: str, delay: int = 60):
        """延迟删除生成的图片文件，避免发送冲突"""
        await asyncio.sleep(delay)
        try:
            os.unlink(path)
        except OSError:
            pass

    @filter.command("icbc_chart")
    async def chart_view(self, event: AstrMessageEvent, currency: str):
        """查看指定币种的汇率走势图。用法: /icbc_chart 美元"""
        chart_data = self.data.get("chart_data", {})
        records = chart_data.get(currency, [])
        if not records:
            yield event.plain_result(
                f"暂无 {currency} 的历史数据。\n"
                f"请先使用 /icbc_chart_add {currency} 添加追踪，等待数据采集后再试。"
            )
            return

        chart_path = await asyncio.to_thread(ExchangeRateChartGenerator.generate, currency, records)
        if chart_path:
            try:
                yield event.image_result(chart_path)
            finally:
                # 挂起异步延迟清理任务，取代立即删除
                asyncio.create_task(self._delayed_remove_file(chart_path))
        else:
            yield event.plain_result("图表生成失败，请稍后重试。")

    @filter.command("icbc_chart_auto")
    async def chart_auto(self, event: AstrMessageEvent, switch: str):
        """开启/关闭定时自动推送汇率走势图。用法: /icbc_chart_auto on 或 /icbc_chart_auto off"""
        session_id = event.unified_msg_origin
        if not session_id:
            yield event.plain_result("无法识别当前会话。")
            return

        if switch.lower() not in ["on", "off"]:
            yield event.plain_result(
                "参数错误，请使用 on 或 off。\n用法: /icbc_chart_auto on"
            )
            return

        enabled = switch.lower() == "on"

        # 检查是否有追踪的币种
        tracked = self.data.get("chart_monitors", {}).get(session_id, [])
        if enabled and not tracked:
            yield event.plain_result(
                "请先使用 /icbc_chart_add [币种] 添加追踪后再开启自动推送。"
            )
            return

        chart_auto_send = self.data.get("chart_auto_send", {})
        chart_auto_send[session_id] = enabled
        self.data["chart_auto_send"] = chart_auto_send
        await self.save_data()

        chart_cron = self.data.get("chart_cron", "0 12 * * 1-5")
        if enabled:
            yield event.plain_result(
                f"已开启定时推送汇率走势图。\n"
                f"当前推送频率: {chart_cron}\n"
                f"使用 /icbc_chart_cron 可修改推送频率。"
            )
        else:
            yield event.plain_result("已关闭定时推送汇率走势图。")

    @filter.command("icbc_chart_cron")
    async def chart_cron_cmd(self, event: AstrMessageEvent, cron_expr: GreedyStr):
        """设置走势图定时推送频率。用法: /icbc_chart_cron 0 12 * * 1-5"""
        cron_expr = cron_expr.strip().strip('"').strip("'")
        if not croniter.croniter.is_valid(cron_expr):
            yield event.plain_result(
                "Cron表达式格式不正确，请查看 /icbc_help 获取常用格式示例。"
            )
            return

        self.data["chart_cron"] = cron_expr
        await self.save_data()

        # 唤醒现有的 chart_push_loop 以应用新 cron
        self._chart_wakeup.set()

        yield event.plain_result(f"走势图定时推送频率已修改为: {cron_expr}")

    async def _collect_chart_data(self, rates: list):
        """采集追踪币种的历史数据，保留最近7个工作日（约9自然天）的数据。"""
        now = datetime.now()

        chart_monitors = self.data.get("chart_monitors", {})
        # 汇总所有会话中追踪的币种（去重）
        all_currencies = set()
        for currencies in chart_monitors.values():
            all_currencies.update(currencies)

        chart_data = self.data.get("chart_data", {})
        now_str = now.strftime("%Y-%m-%d %H:%M")

        # 采集新数据
        for cur_name in all_currencies:
            target_rate = self._find_rate_by_currency(rates, cur_name)
            if not target_rate:
                continue

            try:
                buy_price = float(target_rate.get("foreignBuy", 0))
                sell_price = float(target_rate.get("foreignSell", 0))
            except (ValueError, TypeError):
                continue

            record = {
                "time": now_str,
                "buy": buy_price,
                "sell": sell_price,
            }

            chart_data.setdefault(cur_name, []).append(record)

        # 清理超期数据：遍历 chart_data 所有 key（而非仅 all_currencies），
        # 避免已取消追踪的币种数据残留导致存储泄漏
        cutoff = (now - timedelta(days=9)).strftime("%Y-%m-%d %H:%M")
        stale_keys = []
        for cur_name in list(chart_data.keys()):
            chart_data[cur_name] = [
                r for r in chart_data[cur_name] if r.get("time", "") >= cutoff
            ]
            # 如果清理后为空且已不在追踪列表中，则直接删除该 key
            if not chart_data[cur_name] and cur_name not in all_currencies:
                stale_keys.append(cur_name)
        for key in stale_keys:
            del chart_data[key]

        self.data["chart_data"] = chart_data
        await self.save_data()

    async def _auto_send_charts(self):
        """向开启了自动推送的会话发送追踪币种的走势图。"""
        chart_auto_send = self.data.get("chart_auto_send", {})
        chart_monitors = self.data.get("chart_monitors", {})
        chart_data = self.data.get("chart_data", {})

        for session_id, enabled in chart_auto_send.items():
            if not enabled:
                continue

            tracked = chart_monitors.get(session_id, [])
            if not tracked:
                continue

            for currency in tracked:
                records = chart_data.get(currency, [])
                if not records:
                    continue

                chart_path = await asyncio.to_thread(ExchangeRateChartGenerator.generate, currency, records)
                if not chart_path:
                    continue

                try:
                    await self.context.send_message(
                        session_id,
                        MessageChain(chain=[Image.fromFileSystem(chart_path)]),
                    )
                    logger.info(f"自动推送 {currency} 走势图给 {session_id}")
                except Exception as e:
                    logger.error(f"自动推送走势图失败 ({session_id}, {currency}): {e}")
                finally:
                    # 挂起异步延迟清理任务，取代立即删除
                    asyncio.create_task(self._delayed_remove_file(chart_path))

    @filter.command("icbc_help")
    async def help_cmd(self, event: AstrMessageEvent):
        """获取工商银行汇率监控插件的使用帮助。用法: /icbc_help"""
        help_text = (
            "工商银行汇率监控插件使用说明：\n"
            "/icbc [币种名称]：实时查询汇率。\n"
            "/icbc_add_buy [币种] [高于/低于] [数值]：添加/更新购汇价监控规则。\n"
            "/icbc_add_sell [币种] [高于/低于] [数值]：添加/更新结汇价监控规则。\n"
            "/icbc_del_buy [币种]：删除特定币种的购汇价监控规则。\n"
            "/icbc_del_sell [币种]：删除特定币种的结汇价监控规则。\n"
            "/icbc_list：查看当前监控规则和曲线追踪。\n\n"
            "※ 汇率曲线追踪：\n"
            "/icbc_chart_add [币种]：添加汇率曲线追踪。\n"
            "/icbc_chart_del [币种]：删除汇率曲线追踪。\n"
            "/icbc_chart [币种]：查看汇率走势折线图。\n"
            "/icbc_chart_auto [on/off]：开启/关闭定时推送走势图。\n"
            "/icbc_chart_cron [cron表达式]：设置走势图推送频率（默认工作日中午12点）。\n\n"
            "※ 其他设置：\n"
            "/icbc_cron [cron表达式]：自定义后台监控刷新频率。\n"
            "/icbc_help：查看此帮助信息。\n\n"
            "※ Cron 表达式简易教程：\n"
            "建议：后台轮询间隔尽量设置在 60 分钟或更长，防止过于频繁请求导致数据获取失败。\n"
            "格式: 分 时 日 月 周\n"
            "举例:\n"
            "0 * * * *        (每小时的第0分执行一次，约每60分钟)\n"
            "*/30 * * * *     (每30分钟执行一次)\n"
            "0 8 * * *        (每天早上8点执行)\n"
            "0 9,13,18 * * *  (每天9、13、18点执行)\n"
            "0 12 * * 1-5     (工作日中午12点执行)"
        )
        yield event.plain_result(help_text)

    # 监控类型 -> (价格字段, 中文名) 的映射
    _PRICE_TYPE_MAP = {
        "buy": ("foreignBuy", "结汇价"),
        "sell": ("foreignSell", "购汇价"),
    }

    async def _check_and_notify_monitors(self, rates: list):
        """分离出来的规则阈值检查与消息触发逻辑。"""
        monitors = self.data.get("monitors", {})
        if not monitors:
            return

        is_changed = False
        for session_id, rules in monitors.items():
            messages = []
            for rule in rules:
                cur_name = rule["currency"]
                condition = rule["condition"]
                threshold = rule["threshold"]
                price_type = rule.get("type", "sell")

                target_rate = self._find_rate_by_currency(rates, cur_name)
                if not target_rate:
                    continue

                # 使用映射获取价格字段和中文名
                field_key, type_name = self._PRICE_TYPE_MAP.get(
                    price_type, ("foreignSell", "购汇价")
                )

                try:
                    price = float(target_rate.get(field_key, 0))
                except ValueError:
                    continue

                if price <= 0:
                    continue

                triggered = (
                    (condition == "高于" and price > threshold)
                    or (condition == "低于" and price < threshold)
                )

                if triggered:
                    messages.append(
                        f"汇率提醒: {target_rate['currencyCHName']} {type_name}为 {price}，已{condition}设定的阈值 {threshold}！"
                    )
                    if not rule.get("last_triggered", False):
                        rule["last_triggered"] = True
                        is_changed = True
                elif not triggered and rule.get("last_triggered", False):
                    # 汇率回落/升回，重置触发状态
                    rule["last_triggered"] = False
                    is_changed = True

            # 推送通知
            for msg in messages:
                logger.info(f"触发推送给 {session_id}: {msg}")
                try:
                    await self.context.send_message(
                        session_id, MessageChain(chain=[Plain(msg)])
                    )
                except Exception as e:
                    logger.error(f"消息推送失败: {e}")

        # 所有规则遍历完毕后统一保存一次
        if is_changed:
            await self.save_data()

    async def monitor_loop(self):
        while True:
            try:
                rates = await self.fetch_exchange_rates()

                if rates:
                    # 采集汇率曲线数据
                    await self._collect_chart_data(rates)
                    # 检查阈值并推送通知
                    await self._check_and_notify_monitors(rates)

                # 无论 rates/monitors 是否为空，都必须执行休眠
                cron_expr = self.data.get("cron", "*/30 * * * *")
                if not croniter.croniter.is_valid(cron_expr):
                    cron_expr = "*/30 * * * *"

                now = datetime.now()
                cron = croniter.croniter(cron_expr, now)
                next_time = cron.get_next(datetime)
                sleep_seconds = (next_time - now).total_seconds()

                sleep_seconds = max(sleep_seconds, 1)
                try:
                    await asyncio.wait_for(self._monitor_wakeup.wait(), timeout=sleep_seconds)
                    self._monitor_wakeup.clear()
                except asyncio.TimeoutError:
                    pass

            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"汇率监控后台任务网络异常: {e}")
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"汇率监控后台任务报错: {e}")
                # 退避补偿：防止异常发生在 sleep 之前导致死循环炸 CPU
                await asyncio.sleep(60)

    async def chart_push_loop(self):
        """独立的图表推送后台任务，按 chart_cron 定时执行。"""
        while True:
            try:
                # 先计算到下次触发时间的休眠秒数，确保启动时不会立即推送
                chart_cron = self.data.get("chart_cron", "0 12 * * 1-5")
                if not croniter.croniter.is_valid(chart_cron):
                    chart_cron = "0 12 * * 1-5"

                now = datetime.now()
                cron = croniter.croniter(chart_cron, now)
                next_time = cron.get_next(datetime)
                sleep_seconds = (next_time - now).total_seconds()

                sleep_seconds = max(sleep_seconds, 1)
                try:
                    await asyncio.wait_for(self._chart_wakeup.wait(), timeout=sleep_seconds)
                    self._chart_wakeup.clear()
                    # 被唤醒说明 cron 配置已变更，重新计算
                    continue
                except asyncio.TimeoutError:
                    pass

                await self._auto_send_charts()

            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"图表推送后台任务网络异常: {e}")
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"图表推送后台任务报错: {e}")
                # 退避补偿：防止异常发生在 sleep 之前导致死循环炸 CPU
                await asyncio.sleep(60)
