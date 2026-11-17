import asyncio
import json
import os
import ssl
from datetime import datetime, timedelta

import aiohttp
import croniter

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

from .chart_generator import ExchangeRateChartGenerator

PLUGIN_NAME = "astrbot_plugin_exchangerate_icbc"
# 记录会话 UMO 的上限，超出后按最近活跃顺序淘汰最早的一条
_KNOWN_SESSION_LIMIT = 500


@register(PLUGIN_NAME, "Yuuu0109", "工商银行汇率监控插件", "1.6.0")
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
            "chart_cron": "0 12 * * 1-5",  # 图表推送默认cron，未单独设置的会话使用
            "chart_cron_by_session": {},  # userId/groupId -> 该会话专属的推送cron
            "price_extremes": {},  # currency -> {"buy_high": {price, time}, "buy_low": {price, time}, "sell_high": {price, time}, "sell_low": {price, time}}
            "known_sessions": {},  # UMO -> 可读名称，记录收到过消息的会话
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
                    self.data.update(
                        {k: v for k, v in file_data.items() if k in self.data}
                    )
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

    @staticmethod
    def _display_name_from_event(event: AstrMessageEvent) -> str:
        """从入站事件取可读名称：群聊取群名，私聊取发送者名。"""
        try:
            group_id = event.get_group_id() or ""
        except Exception:  # noqa: BLE001 - 取不到会话信息时视为无名
            group_id = ""
        if group_id:
            group = getattr(event.message_obj, "group", None)
            group_name = getattr(group, "group_name", None) if group else None
            return group_name.strip() if isinstance(group_name, str) else ""
        try:
            sender_name = event.get_sender_name()
        except Exception:  # noqa: BLE001
            sender_name = ""
        return sender_name.strip() if isinstance(sender_name, str) else ""

    async def _remember_umo_alias(self, umo: str, name: str) -> None:
        """记录会话标识与可读名称，仅在新增或名称变化时写盘。"""
        known = self.data.setdefault("known_sessions", {})
        if umo in known and known[umo] == name:
            return
        # 重新插入到末尾，让最近活跃的会话排在最后，便于按数量裁剪
        known.pop(umo, None)
        known[umo] = name
        overflow = len(known) - _KNOWN_SESSION_LIMIT
        if overflow > 0:
            for stale in list(known)[:overflow]:
                known.pop(stale, None)
        await self.save_data()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def remember_session(self, event: AstrMessageEvent):
        """记录收到过消息的会话，供 WebUI 选择 UMO。

        QQ 官方机器人的会话 ID 是随机 openid，官方接口无法主动列出群聊，
        用户先向机器人发一条消息后，插件才能在这里记下该会话。
        """
        umo = (event.unified_msg_origin or "").strip()
        parts = umo.split(":", 2)
        if len(parts) != 3 or not all(parts):
            return
        await self._remember_umo_alias(umo, self._display_name_from_event(event))

    async def initialize(self):
        logger.info("初始化工商银行汇率监控插件...")
        self._session = aiohttp.ClientSession()
        self.monitor_task = asyncio.create_task(self.monitor_loop())
        self.chart_push_task = asyncio.create_task(self.chart_push_loop())
        self._register_web_apis()

    def _register_web_apis(self):
        """注册供 Dashboard 插件 WebUI 调用的接口。"""
        for route, handler, methods, desc in (
            ("state", self.page_get_state, ["GET"], "获取汇率控制台完整状态"),
            (
                "umo-options",
                self.page_get_umo_options,
                ["GET"],
                "列出可选的会话标识（UMO）",
            ),
            (
                "platform-targets",
                self.page_get_platform_targets,
                ["GET"],
                "列出机器人实例及其群组/好友，用于添加 UMO",
            ),
            ("rates", self.page_get_rates, ["GET"], "获取工商银行实时牌价"),
            ("monitors/save", self.page_save_monitor, ["POST"], "新增或更新监控规则"),
            ("monitors/delete", self.page_delete_monitor, ["POST"], "删除监控规则"),
            ("charts/add", self.page_chart_add, ["POST"], "添加汇率曲线追踪"),
            ("charts/delete", self.page_chart_delete, ["POST"], "移除汇率曲线追踪"),
            ("charts/data", self.page_chart_data, ["GET"], "查询曲线历史数据"),
            ("settings", self.page_save_settings, ["POST"], "修改轮询与推送设置"),
        ):
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{route}", handler, methods, desc
            )

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
                logger.warning(
                    f"获取汇率数据网络异常 (第{attempt}/{max_retries}次): {e}"
                )
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
            if currency == rate.get(
                "currencyCHName", ""
            ) or currency.upper() == rate.get("currencyENName", ""):
                return rate
        # 第二遍：部分匹配
        for rate in rates:
            if currency in rate.get(
                "currencyCHName", ""
            ) or currency.upper() in rate.get("currencyENName", ""):
                return rate
        return None

    async def _resolve_currency_name(self, currency: str) -> str | None:
        rates = await self.fetch_exchange_rates()
        if not rates:
            return None
        matched = self._find_rate_by_currency(rates, currency)
        if matched:
            return matched.get("currencyCHName") or None
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

    def format_rate_info(self, rate_info: dict, session_id: str = None) -> str:
        name = rate_info.get("currencyCHName", "")
        code = rate_info.get("currencyENName", "")
        buy = rate_info.get("foreignBuy", "N/A")
        sell = rate_info.get("foreignSell", "N/A")
        c_buy = rate_info.get("cashBuy", "N/A")
        c_sell = rate_info.get("cashSell", "N/A")
        pub_time = (
            f"{rate_info.get('publishDate', '')} {rate_info.get('publishTime', '')}"
        )

        result = f"【{name}({code})】\n结汇价: {buy}  购汇价: {sell}\n现钞买入: {c_buy}  现钞卖出: {c_sell}\n更新时间: {pub_time}"

        # 根据该会话的监控类型决定显示哪些极值
        if session_id:
            monitors = self.data.get("monitors", {}).get(session_id, [])
            has_buy_monitor = any(
                r["currency"] == name and r.get("type") == "buy" for r in monitors
            )
            has_sell_monitor = any(
                r["currency"] == name and r.get("type") == "sell" for r in monitors
            )

            price_extremes = self.data.get("price_extremes", {})
            extremes = price_extremes.get(name, {})
            if extremes:
                lines = []
                if has_buy_monitor:
                    if extremes.get("buy_high"):
                        high = extremes["buy_high"]
                        lines.append(f"结汇价最高: {high['price']} ({high['time']})")
                    if extremes.get("buy_low"):
                        low = extremes["buy_low"]
                        lines.append(f"结汇价最低: {low['price']} ({low['time']})")
                if has_sell_monitor:
                    if extremes.get("sell_high"):
                        high = extremes["sell_high"]
                        lines.append(f"购汇价最高: {high['price']} ({high['time']})")
                    if extremes.get("sell_low"):
                        low = extremes["sell_low"]
                        lines.append(f"购汇价最低: {low['price']} ({low['time']})")
                if lines:
                    result += "\n\n※ 监控历史极值:\n" + "\n".join(lines)

        return result

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
                session_id = event.unified_msg_origin
                yield event.plain_result(self.format_rate_info(matched, session_id))
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
                r
                for r in monitors[session_id]
                if not (
                    r["currency"] == currency and r.get("type", "sell") == price_type
                )
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

        # 统一规范化为标准中文币种名，确保追踪列表与历史数据命名一致
        standard_name = await self._resolve_currency_name(currency)
        if not standard_name:
            yield event.plain_result(f"未找到关于包含 {currency} 的汇率信息。")
            return

        chart_monitors = self.data.setdefault("chart_monitors", {})
        session_monitors = chart_monitors.setdefault(session_id, [])

        if standard_name in session_monitors:
            yield event.plain_result(
                f"{standard_name} 已在曲线追踪列表中，无需重复添加。"
            )
            return

        session_monitors.append(standard_name)
        await self.save_data()

        yield event.plain_result(
            f"已添加 {standard_name} 到汇率曲线追踪列表。\n"
            f"后台将定时采集数据，使用 /icbc_chart {standard_name} 可查看最近走势图。"
        )

    @filter.command("icbc_chart_del")
    async def chart_del(self, event: AstrMessageEvent, currency: str):
        """删除汇率曲线追踪。用法: /icbc_chart_del 美元"""
        session_id = event.unified_msg_origin
        chart_monitors = self.data.get("chart_monitors", {})
        session_list = chart_monitors.get(session_id, [])

        # 优先按用户输入直接匹配，未命中再尝试解析为标准中文名（兼容英文代码输入与历史数据）
        target_name = currency if currency in session_list else None
        if target_name is None:
            standard_name = await self._resolve_currency_name(currency)
            if standard_name and standard_name in session_list:
                target_name = standard_name

        if target_name is None:
            yield event.plain_result(f"未找到 {currency} 的曲线追踪记录。")
            return

        session_list.remove(target_name)
        chart_monitors[session_id] = session_list
        self.data["chart_monitors"] = chart_monitors
        await self.save_data()
        yield event.plain_result(f"已移除 {target_name} 的汇率曲线追踪。")

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

        # 直接命中失败时回退到币种名解析，兼容英文代码与中文名混用
        if not records:
            standard_name = await self._resolve_currency_name(currency)
            if standard_name:
                fallback = chart_data.get(standard_name, [])
                if fallback:
                    currency = standard_name
                    records = fallback

        if not records:
            yield event.plain_result(
                f"暂无 {currency} 的历史数据。\n"
                f"请先使用 /icbc_chart_add {currency} 添加追踪，等待数据采集后再试。"
            )
            return

        chart_path = await asyncio.to_thread(
            ExchangeRateChartGenerator.generate, currency, records
        )
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

    async def _update_price_extremes(self, rates: list):
        """更新监控币种的最高最低价，仅针对已添加监控规则的币种"""
        monitors = self.data.get("monitors", {})
        if not monitors:
            return

        # 汇总所有监控的币种
        monitored_currencies = set()
        for rules in monitors.values():
            for rule in rules:
                monitored_currencies.add(rule["currency"])

        if not monitored_currencies:
            return

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        price_extremes = self.data.get("price_extremes", {})

        for cur_name in monitored_currencies:
            target_rate = self._find_rate_by_currency(rates, cur_name)
            if not target_rate:
                continue

            try:
                buy_price = float(target_rate.get("foreignBuy", 0))
                sell_price = float(target_rate.get("foreignSell", 0))
            except (ValueError, TypeError):
                continue

            if cur_name not in price_extremes:
                price_extremes[cur_name] = {}

            # 更新结汇价(foreignBuy)最高最低
            if buy_price > 0:
                if (
                    "buy_high" not in price_extremes[cur_name]
                    or buy_price > price_extremes[cur_name]["buy_high"]["price"]
                ):
                    price_extremes[cur_name]["buy_high"] = {
                        "price": buy_price,
                        "time": now_str,
                    }
                if (
                    "buy_low" not in price_extremes[cur_name]
                    or buy_price < price_extremes[cur_name]["buy_low"]["price"]
                ):
                    price_extremes[cur_name]["buy_low"] = {
                        "price": buy_price,
                        "time": now_str,
                    }

            # 更新购汇价(foreignSell)最高最低
            if sell_price > 0:
                if (
                    "sell_high" not in price_extremes[cur_name]
                    or sell_price > price_extremes[cur_name]["sell_high"]["price"]
                ):
                    price_extremes[cur_name]["sell_high"] = {
                        "price": sell_price,
                        "time": now_str,
                    }
                if (
                    "sell_low" not in price_extremes[cur_name]
                    or sell_price < price_extremes[cur_name]["sell_low"]["price"]
                ):
                    price_extremes[cur_name]["sell_low"] = {
                        "price": sell_price,
                        "time": now_str,
                    }

        self.data["price_extremes"] = price_extremes
        await self.save_data()

    def _chart_cron_for(self, session_id: str) -> str:
        """返回某个会话实际生效的走势图推送 cron。

        优先使用该会话单独设置的值，没有设置或值非法时回退到全局默认。

        Args:
            session_id: 会话标识（UMO）。

        Returns:
            合法的 cron 表达式。
        """
        default_cron = self.data.get("chart_cron", "0 12 * * 1-5")
        if not croniter.croniter.is_valid(default_cron):
            default_cron = "0 12 * * 1-5"

        per_session = (self.data.get("chart_cron_by_session") or {}).get(session_id)
        if isinstance(per_session, str) and croniter.croniter.is_valid(per_session):
            return per_session
        return default_cron

    async def _auto_send_charts(self, session_ids=None):
        """向开启了自动推送的会话发送追踪币种的走势图。

        Args:
            session_ids: 只推送给这些会话；为 None 时推送给所有已开启的会话。
        """
        chart_auto_send = self.data.get("chart_auto_send", {})
        chart_monitors = self.data.get("chart_monitors", {})
        chart_data = self.data.get("chart_data", {})

        targets = session_ids if session_ids is not None else list(chart_auto_send)
        for session_id in targets:
            if not chart_auto_send.get(session_id):
                continue

            tracked = chart_monitors.get(session_id, [])
            if not tracked:
                continue

            for currency in tracked:
                records = chart_data.get(currency, [])
                if not records:
                    continue

                chart_path = await asyncio.to_thread(
                    ExchangeRateChartGenerator.generate, currency, records
                )
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

                triggered = (condition == "高于" and price > threshold) or (
                    condition == "低于" and price < threshold
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
                    # 更新监控币种的最高最低价
                    await self._update_price_extremes(rates)
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
                    await asyncio.wait_for(
                        self._monitor_wakeup.wait(), timeout=sleep_seconds
                    )
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
        """独立的图表推送后台任务。

        每个会话可以使用各自的 cron：先算出所有待推送会话的下一个触发时刻，
        取最近的那个作为休眠时长；到点后只推送给同一时刻到期的会话。
        """
        while True:
            try:
                now = datetime.now()
                pending: list[tuple[datetime, str]] = []
                for session_id, enabled in (
                    self.data.get("chart_auto_send") or {}
                ).items():
                    if not enabled:
                        continue
                    if not (self.data.get("chart_monitors") or {}).get(session_id):
                        continue
                    try:
                        next_time = croniter.croniter(
                            self._chart_cron_for(session_id), now
                        ).get_next(datetime)
                    except Exception as e:  # noqa: BLE001 - 单个会话配置异常不应中断调度
                        logger.warning(f"会话 {session_id} 的推送计划无效: {e}")
                        continue
                    pending.append((next_time, session_id))

                if not pending:
                    # 没有需要推送的会话：等唤醒或定期复查，以便及时接上新增的会话
                    try:
                        await asyncio.wait_for(self._chart_wakeup.wait(), timeout=60)
                        self._chart_wakeup.clear()
                    except asyncio.TimeoutError:
                        pass
                    continue

                next_time, _ = min(pending, key=lambda item: item[0])
                sleep_seconds = max((next_time - now).total_seconds(), 1)
                try:
                    await asyncio.wait_for(
                        self._chart_wakeup.wait(), timeout=sleep_seconds
                    )
                    self._chart_wakeup.clear()
                    # 被唤醒说明配置已变更，重新计算
                    continue
                except asyncio.TimeoutError:
                    pass

                # 只推送与最近触发时刻一致（同一分钟到期）的会话
                due = [sid for fire_at, sid in pending if fire_at <= next_time]
                if due:
                    await self._auto_send_charts(due)

            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"图表推送后台任务网络异常: {e}")
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"图表推送后台任务报错: {e}")
                # 退避补偿：防止异常发生在 sleep 之前导致死循环炸 CPU
                await asyncio.sleep(60)

    # ========== Dashboard 插件 WebUI 接口 ==========

    async def page_get_state(self):
        """返回控制台首屏所需的全部配置与统计。"""
        monitors = self.data.get("monitors", {})
        chart_monitors = self.data.get("chart_monitors", {})
        chart_auto_send = self.data.get("chart_auto_send", {})
        chart_data = self.data.get("chart_data", {})
        # 只保留形如 platform:MessageType:id 且确实有内容的键，
        # 避免历史遗留的裸会话 ID（如纯数字）混进下拉与默认选中项
        sessions = sorted(
            umo
            for umo in set(monitors) | set(chart_monitors) | set(chart_auto_send)
            if isinstance(umo, str)
            and len(umo.strip().split(":", 2)) == 3
            and all(umo.strip().split(":", 2))
            and ((monitors.get(umo) or []) or (chart_monitors.get(umo) or []))
        )
        return json_response(
            {
                "cron": self.data.get("cron", "*/30 * * * *"),
                "chart_cron": self.data.get("chart_cron", "0 12 * * 1-5"),
                "chart_cron_by_session": self.data.get("chart_cron_by_session", {}),
                "sessions": sessions,
                "monitors": monitors,
                "chart_monitors": chart_monitors,
                "chart_auto_send": chart_auto_send,
                "chart_points": {
                    name: len(items) for name, items in chart_data.items()
                },
                "price_extremes": self.data.get("price_extremes", {}),
            }
        )

    async def _get_umo_display_names(self) -> dict[str, str]:
        """返回插件记录的会话与 AstrBot UMO 别名，名称可能为空字符串。

        复用 AstrBot 4.28+ 的 `umo_aliases` 表：通过 `/name` 指令设置或自动
        记录的别名会覆盖插件自己记下的名称；旧版本没有该表时直接跳过。
        """
        names: dict[str, str] = {}
        for umo, name in (self.data.get("known_sessions") or {}).items():
            if isinstance(umo, str) and umo:
                names[umo] = name.strip() if isinstance(name, str) else ""

        try:
            from astrbot.core.umo_alias import serialize_umo_alias

            aliases = await self.context.get_db().get_umo_aliases()
        except Exception:  # noqa: BLE001 - 旧版本或数据库不可用时不影响主流程
            return names

        for alias in aliases or []:
            umo = getattr(alias, "umo", "")
            if not isinstance(umo, str) or not umo:
                continue
            display = serialize_umo_alias(alias, umo).get("display_name", "").strip()
            if display and display != umo:
                names[umo] = display
        return names

    async def page_get_umo_options(self):
        """返回数据文件中真正配置过内容的会话标识（UMO）。

        只统计「有监控规则」或「有曲线追踪」的会话，空规则列表与格式非法的键
        （例如缺少冒号的纯用户 ID）会被忽略，保证下拉里只出现有用的会话。
        """
        monitors = self.data.get("monitors") or {}
        chart_monitors = self.data.get("chart_monitors") or {}
        chart_auto_send = self.data.get("chart_auto_send") or {}
        display_names = await self._get_umo_display_names()

        sessions = []
        # 两类配置的键合并去重，再按是否有内容过滤
        for umo in list(monitors) + [k for k in chart_monitors if k not in monitors]:
            if not isinstance(umo, str):
                continue
            parts = umo.strip().split(":", 2)
            if len(parts) != 3 or not all(parts):
                continue

            rules = monitors.get(umo) or []
            charts = chart_monitors.get(umo) or []
            if not rules and not charts:
                continue

            sessions.append(
                {
                    "umo": umo,
                    "name": display_names.get(umo, ""),
                    "platform_id": parts[0],
                    "message_type": parts[1],
                    "session_id": parts[2],
                    "rule_count": len(rules),
                    "chart_count": len(charts),
                    "auto_send": bool(chart_auto_send.get(umo)),
                }
            )

        sessions.sort(key=lambda item: (item["platform_id"], item["session_id"]))
        return json_response({"sessions": sessions})

    async def page_get_platform_targets(self):
        """列出可用于新建监控的机器人实例及其群组/好友。

        数据来自四处并合并去重：
        1. 平台适配器实时返回的群列表与好友列表；
        2. 插件记录过消息的会话与 AstrBot 的 UMO 别名；
        3. AstrBot 历史会话中实际出现过的会话；
        4. 已配置过规则或追踪的会话。
        """
        registry: dict[str, dict] = {}

        def entry_for(platform_id: str, display: str = "") -> dict:
            """取得（或创建）某个机器人实例的登记项。"""
            item = registry.get(platform_id)
            if item is None:
                item = {
                    "platform_id": platform_id,
                    "display": display or platform_id,
                    "targets": {},
                    "can_query": False,
                    "errors": [],
                }
                registry[platform_id] = item
            elif display and item["display"] == item["platform_id"]:
                item["display"] = display
            return item

        def add_target(
            item: dict,
            umo: str,
            message_type: str,
            session_id: str,
            name: str = "",
        ) -> None:
            """登记一个可选会话，重名或重复时不产生第二条。"""
            target = item["targets"].get(umo)
            if target is None:
                item["targets"][umo] = {
                    "umo": umo,
                    "message_type": message_type,
                    "session_id": str(session_id),
                    "name": name,
                }
            elif name and not target["name"]:
                target["name"] = name

        # 1) 平台实例实时返回的群与好友
        for platform in getattr(self.context.platform_manager, "platform_insts", []):
            try:
                meta = platform.meta()
            except Exception:  # noqa: BLE001 - 单个适配器异常不应影响整体列表
                continue
            platform_id = getattr(meta, "id", "") or ""
            if not platform_id:
                continue
            display = (
                getattr(meta, "adapter_display_name", None)
                or getattr(meta, "name", "")
                or platform_id
            )
            item = entry_for(platform_id, display)

            get_client = getattr(platform, "get_client", None)
            if not callable(get_client):
                continue
            try:
                client = get_client()
            except Exception:  # noqa: BLE001
                continue
            call_action = getattr(client, "call_action", None)
            if not callable(call_action):
                if getattr(meta, "name", "") in (
                    "qq_official",
                    "qq_official_webhook",
                ):
                    item["errors"].append(
                        "QQ 官方机器人无法列出群聊/好友；请先在目标群 @机器人 "
                        "或私聊机器人发送一条消息，再点开此对话框"
                    )
                else:
                    item["errors"].append("该平台不支持主动调用接口")
                continue

            # OneBot 反向 WebSocket 在存在多个连接时，必须显式指定 self_id
            # 才能定位到具体的机器人账号（aiocqhttp 内部按 self_id 索引连接）。
            self_ids: list[str] = []
            clients_map = getattr(client, "_wsr_api_clients", None)
            if isinstance(clients_map, dict):
                self_ids = [str(key) for key in clients_map if str(key) != "*"]
            if self_ids:
                item["can_query"] = True
                routed = [{"self_id": self_id} for self_id in self_ids]
            else:
                # 无法枚举账号（例如 HTTP 上报或其它适配器）时，先不带 self_id 尝试
                routed = [{}]

            for action, message_type, id_key, name_keys in (
                ("get_group_list", "GroupMessage", "group_id", ("group_name",)),
                ("get_friend_list", "FriendMessage", "user_id", ("remark", "nickname")),
            ):
                entries: list = []
                last_error = ""
                for extra in routed:
                    try:
                        result = await call_action(action=action, **extra)
                    except Exception as e:  # noqa: BLE001 - 记录原因而非吞掉
                        last_error = f"{type(e).__name__}: {e}"
                        continue
                    item["can_query"] = True
                    if isinstance(result, list):
                        entries.extend(result)

                if not entries and last_error:
                    label = "群列表" if action == "get_group_list" else "好友列表"
                    item["errors"].append(f"获取{label}失败（{last_error}）")
                    continue

                for raw in entries:
                    if not isinstance(raw, dict):
                        continue
                    session_id = raw.get(id_key)
                    if session_id in (None, ""):
                        continue
                    name = ""
                    for key in name_keys:
                        value = raw.get(key)
                        if isinstance(value, str) and value.strip():
                            name = value.strip()
                            break
                    add_target(
                        item,
                        f"{platform_id}:{message_type}:{session_id}",
                        message_type,
                        session_id,
                        name,
                    )

        # 2) 插件记录过消息的会话与 AstrBot 的 UMO 别名。
        # QQ 官方机器人的会话 ID 是随机 openid，官方接口无法列出群聊，
        # 只能等用户先发一条消息，由插件把 UMO 记下来后在这里展示。
        for umo, name in (await self._get_umo_display_names()).items():
            parts = umo.strip().split(":", 2)
            if len(parts) != 3 or not all(parts):
                continue
            add_target(entry_for(parts[0]), umo.strip(), parts[1], parts[2], name)

        # 3) 历史会话中出现过的会话
        try:
            (
                conversations,
                _,
            ) = await self.context.conversation_manager.get_filtered_conversations(
                page=1,
                page_size=200,
                include_history=False,
                group_by_session=True,
            )
            for conv in conversations:
                umo = getattr(conv, "user_id", "")
                if not isinstance(umo, str):
                    continue
                parts = umo.strip().split(":", 2)
                if len(parts) != 3 or not all(parts):
                    continue
                title = getattr(conv, "title", None)
                add_target(
                    entry_for(parts[0]),
                    umo.strip(),
                    parts[1],
                    parts[2],
                    title.strip() if isinstance(title, str) else "",
                )
        except Exception as e:  # noqa: BLE001 - 数据库不可用不应阻塞页面
            logger.warning(f"读取历史会话失败: {e}")

        # 4) 已经配置过规则或追踪的会话
        for source in ("monitors", "chart_monitors"):
            for umo in self.data.get(source) or {}:
                if not isinstance(umo, str):
                    continue
                parts = umo.strip().split(":", 2)
                if len(parts) != 3 or not all(parts):
                    continue
                add_target(entry_for(parts[0]), umo.strip(), parts[1], parts[2])

        platforms = []
        for item in registry.values():
            targets = sorted(
                item["targets"].values(),
                key=lambda t: (t["message_type"], t["name"] or t["session_id"]),
            )
            if targets:
                # 已有可选对象时，仅把失败原因作为补充说明，不阻断选择
                unavailable = "；".join(item["errors"])
            elif item["errors"]:
                unavailable = "；".join(item["errors"])
            elif item["can_query"]:
                unavailable = "机器人未返回任何群组或好友"
            else:
                unavailable = "该机器人不支持列出群组或好友"
            platforms.append(
                {
                    "platform_id": item["platform_id"],
                    "display": item["display"],
                    "targets": targets,
                    "unavailable": unavailable,
                }
            )
        platforms.sort(key=lambda p: p["platform_id"])
        return json_response({"platforms": platforms})

    async def page_get_rates(self):
        """返回工商银行实时牌价原始列表。"""
        rates = await self.fetch_exchange_rates()
        if not rates:
            return error_response("暂时无法获取工商银行牌价，请稍后重试。")
        return json_response(
            {
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "rates": [
                    {
                        "name": item.get("currencyCHName", ""),
                        "code": item.get("currencyENName", ""),
                        "buy": item.get("foreignBuy", ""),
                        "sell": item.get("foreignSell", ""),
                        "cash_buy": item.get("cashBuy", ""),
                        "cash_sell": item.get("cashSell", ""),
                        "published": (
                            f"{item.get('publishDate', '')} "
                            f"{item.get('publishTime', '')}"
                        ).strip(),
                    }
                    for item in rates
                    if item.get("currencyENName")
                ],
            }
        )

    async def page_save_monitor(self):
        """新增或更新一条监控规则，返回最新的规则列表。"""
        payload = await request.json(default={})
        session_id = str(payload.get("session_id", "")).strip()
        currency = str(payload.get("currency", "")).strip()
        price_type = payload.get("type")
        condition = payload.get("condition")

        if not session_id:
            return error_response("请先选择或填写会话标识。")
        if not currency:
            return error_response("请填写币种名称。")
        if price_type not in self._PRICE_TYPE_MAP:
            return error_response("监控类型只能为结汇价或购汇价。")
        if condition not in ("高于", "低于"):
            return error_response("条件只能为高于或低于。")
        try:
            threshold = float(payload.get("threshold"))
        except (TypeError, ValueError):
            return error_response("阈值必须是数字。")
        if threshold <= 0:
            return error_response("阈值必须大于 0。")

        rates = await self.fetch_exchange_rates()
        matched = self._find_rate_by_currency(rates, currency) if rates else None
        if not matched:
            return error_response(f"未找到币种「{currency}」的实时牌价，请核对名称。")

        # 统一使用工商银行返回的中文币种名，避免中英文混写产生重复规则
        currency = matched.get("currencyCHName") or currency

        # 以下修改之间没有 await，内存中的规则列表不会被并发打断
        session_rules = self.data.setdefault("monitors", {}).setdefault(session_id, [])
        for rule in session_rules:
            if (
                rule["currency"] == currency
                and rule["condition"] == condition
                and rule.get("type") == price_type
            ):
                rule["threshold"] = threshold
                rule["last_triggered"] = False
                action = "更新"
                break
        else:
            session_rules.append(
                {
                    "currency": currency,
                    "condition": condition,
                    "threshold": threshold,
                    "type": price_type,
                    "last_triggered": False,
                }
            )
            action = "添加"

        await self.save_data()
        logger.info(
            f"WebUI {action}监控规则: {session_id} {currency} {condition} {threshold}"
        )
        return json_response(
            {
                "action": action,
                "currency": currency,
                "monitors": self.data.get("monitors", {}),
            }
        )

    async def page_delete_monitor(self):
        """删除指定会话下的监控规则，返回删除后的规则列表。"""
        payload = await request.json(default={})
        session_id = str(payload.get("session_id", "")).strip()
        currency = str(payload.get("currency", "")).strip()
        price_type = payload.get("type")

        if not session_id or not currency:
            return error_response("缺少会话标识或币种名称。")

        session_rules = self.data.get("monitors", {}).get(session_id, [])
        remaining = [
            rule
            for rule in session_rules
            if not (
                rule["currency"] == currency
                and (price_type is None or rule.get("type") == price_type)
            )
        ]
        if len(remaining) == len(session_rules):
            return error_response(f"未找到 {currency} 的对应监控规则。")

        self.data.setdefault("monitors", {})[session_id] = remaining
        await self.save_data()
        logger.info(f"WebUI 删除监控规则: {session_id} {currency}")
        return json_response({"monitors": self.data.get("monitors", {})})

    async def page_chart_add(self):
        """把币种加入曲线追踪列表。"""
        payload = await request.json(default={})
        session_id = str(payload.get("session_id", "")).strip()
        currency = str(payload.get("currency", "")).strip()

        if not session_id:
            return error_response("请先选择或填写会话标识。")
        if not currency:
            return error_response("请填写币种名称。")

        standard_name = await self._resolve_currency_name(currency)
        if not standard_name:
            # 牌价接口暂时不可用时，允许继续操作本地已有的币种，
            # 避免因外部接口故障导致无法管理已追踪的币种
            known = set(self.data.get("chart_data") or {})
            known.update(self.data.get("price_extremes") or {})
            for rules in (self.data.get("monitors") or {}).values():
                for rule in rules or []:
                    if rule.get("currency"):
                        known.add(rule["currency"])
            for names in (self.data.get("chart_monitors") or {}).values():
                known.update(names or [])

            if currency in known:
                standard_name = currency
            else:
                return error_response(
                    f"未找到币种「{currency}」的实时牌价，请核对名称。"
                )

        session_charts = self.data.setdefault("chart_monitors", {}).setdefault(
            session_id, []
        )
        if standard_name in session_charts:
            return error_response(f"{standard_name} 已在曲线追踪列表中。")

        session_charts.append(standard_name)
        await self.save_data()
        logger.info(f"WebUI 添加曲线追踪: {session_id} {standard_name}")
        return json_response({"chart_monitors": self.data.get("chart_monitors", {})})

    async def page_chart_delete(self):
        """把币种移出曲线追踪列表。"""
        payload = await request.json(default={})
        session_id = str(payload.get("session_id", "")).strip()
        currency = str(payload.get("currency", "")).strip()

        if not session_id or not currency:
            return error_response("缺少会话标识或币种名称。")

        session_charts = self.data.get("chart_monitors", {}).get(session_id, [])
        target = currency if currency in session_charts else None
        if target is None:
            standard_name = await self._resolve_currency_name(currency)
            if standard_name in session_charts:
                target = standard_name
        if target is None:
            return error_response(f"未找到 {currency} 的曲线追踪记录。")

        session_charts.remove(target)
        self.data.setdefault("chart_monitors", {})[session_id] = session_charts
        await self.save_data()
        logger.info(f"WebUI 移除曲线追踪: {session_id} {target}")
        return json_response({"chart_monitors": self.data.get("chart_monitors", {})})

    async def page_chart_data(self):
        """返回指定币种的历史曲线数据点。"""
        currency = (request.query.get("currency") or "").strip()
        if not currency:
            return error_response("缺少币种名称。")
        records = self.data.get("chart_data", {}).get(currency, [])
        return json_response({"currency": currency, "records": records})

    async def page_save_settings(self):
        """修改轮询频率、推送频率与自动推送开关。"""
        payload = await request.json(default={})
        changed = []

        if "cron" in payload:
            cron_expr = str(payload.get("cron", "")).strip().strip('"').strip("'")
            if not croniter.croniter.is_valid(cron_expr):
                return error_response("监控轮询的 Cron 表达式不合法。")
            self.data["cron"] = cron_expr
            # 唤醒后台任务，让新的 cron 立即生效
            self._monitor_wakeup.set()
            changed.append("监控轮询")

        if "chart_cron" in payload and payload.get("session_id"):
            # 会话级推送时间：传空字符串表示清除覆盖，回退到全局默认
            session_id = str(payload.get("session_id", "")).strip()
            if not session_id:
                return error_response("缺少会话标识。")
            cron_expr = str(payload.get("chart_cron", "")).strip().strip('"').strip("'")
            overrides = self.data.setdefault("chart_cron_by_session", {})
            if not cron_expr:
                if overrides.pop(session_id, None) is not None:
                    changed.append("会话推送频率")
            else:
                if not croniter.croniter.is_valid(cron_expr):
                    return error_response("走势图推送的 Cron 表达式不合法。")
                overrides[session_id] = cron_expr
                changed.append("会话推送频率")
            self._chart_wakeup.set()
        elif "chart_cron" in payload:
            # 全局默认推送时间
            cron_expr = str(payload.get("chart_cron", "")).strip().strip('"').strip("'")
            if not croniter.croniter.is_valid(cron_expr):
                return error_response("走势图推送的 Cron 表达式不合法。")
            self.data["chart_cron"] = cron_expr
            self._chart_wakeup.set()
            changed.append("走势图默认推送频率")

        if "session_id" in payload and "chart_auto_send" in payload:
            session_id = str(payload.get("session_id", "")).strip()
            if not session_id:
                return error_response("缺少会话标识。")
            enabled = bool(payload.get("chart_auto_send"))
            if enabled and not self.data.get("chart_monitors", {}).get(session_id):
                return error_response("该会话还没有曲线追踪，无法开启自动推送。")
            self.data.setdefault("chart_auto_send", {})[session_id] = enabled
            changed.append("自动推送开关")

        if not changed:
            return error_response("没有需要保存的设置项。")

        await self.save_data()
        logger.info(f"WebUI 更新设置: {', '.join(changed)}")
        return json_response(
            {
                "changed": changed,
                "cron": self.data.get("cron", ""),
                "chart_cron": self.data.get("chart_cron", ""),
                "chart_cron_by_session": self.data.get("chart_cron_by_session", {}),
                "chart_auto_send": self.data.get("chart_auto_send", {}),
            }
        )
