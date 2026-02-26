import asyncio
import json
import os
import ssl
import tempfile
from datetime import datetime, timedelta

import aiohttp
import croniter
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr



@register(
    "astrbot_plugin_exchangerate_icbc", "Yuuu0109", "工商银行汇率监控插件", "1.0.9"
)
class ICBCExchangeRatePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data: dict = {
            "cron": "*/30 * * * *",
            "monitors": {},  # userId/groupId -> list of monitor rules
            "chart_monitors": {},  # userId/groupId -> list of currency names
            "chart_data": {},  # currency -> list of {time, buy, sell}
            "chart_auto_send": {},  # userId/groupId -> bool
        }
        self.last_rates: dict[str, dict] = {}
        self.monitor_task: asyncio.Task | None = None

        data_dir = StarTools.get_data_dir(
            plugin_name="astrbot_plugin_exchangerate_icbc"
        )
        self.data_file = os.path.join(data_dir, "exchangerate_icbc_data.json")

        self.load_data()

    def load_data(self):
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, encoding="utf-8") as f:
                    file_data = json.load(f)
                    if "monitors" in file_data:
                        self.data["monitors"] = file_data["monitors"]
                    if "cron" in file_data:
                        self.data["cron"] = file_data["cron"]
                    if "chart_monitors" in file_data:
                        self.data["chart_monitors"] = file_data["chart_monitors"]
                    if "chart_data" in file_data:
                        self.data["chart_data"] = file_data["chart_data"]
                    if "chart_auto_send" in file_data:
                        self.data["chart_auto_send"] = file_data["chart_auto_send"]
                logger.info(f"成功加载汇率监控数据: {self.data_file}")
        except Exception as e:
            logger.error(f"加载汇率监控配置失败: {e}")

    def save_data(self):
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        ssl_context = ssl.create_default_context()
        ssl_context.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
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
            except Exception as e:
                logger.warning(f"获取汇率数据异常 (第{attempt}/{max_retries}次): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 * attempt)
                else:
                    logger.error(f"获取汇率数据失败，已重试{max_retries}次")
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

        session_id = event.unified_msg_origin
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

        cron_expr = self.data.get("cron", "*/30 * * * *")
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
        session_id = event.unified_msg_origin
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
        session_id = event.unified_msg_origin
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

        cron_expr = self.data.get("cron", "*/30 * * * *")
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
    async def set_cron(self, event: AstrMessageEvent, cron_expr: GreedyStr):
        """设置后台监控刷新频率的Cron表达式。用法: /icbc_cron 0 * * * *"""
        cron_expr = cron_expr.strip().strip('"').strip("'")
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

        yield event.plain_result(f"汇率后台监控频率已成功修改为: {cron_expr}。")

    # ========== 汇率曲线追踪 ==========

    @filter.command("icbc_chart_add")
    async def chart_add(self, event: AstrMessageEvent, currency: str):
        """添加汇率曲线追踪。用法: /icbc_chart_add 美元"""
        session_id = event.unified_msg_origin
        if not session_id:
            yield event.plain_result("无法识别当前会话。")
            return

        chart_monitors = self.data.get("chart_monitors", {})
        if session_id not in chart_monitors:
            chart_monitors[session_id] = []

        if currency in chart_monitors[session_id]:
            yield event.plain_result(f"{currency} 已在曲线追踪列表中，无需重复添加。")
            return

        chart_monitors[session_id].append(currency)
        self.data["chart_monitors"] = chart_monitors
        self.save_data()

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
        self.save_data()
        yield event.plain_result(f"已移除 {currency} 的汇率曲线追踪。")

    @filter.command("icbc_chart_list")
    async def chart_list(self, event: AstrMessageEvent):
        """查看当前会话的汇率曲线追踪列表。用法: /icbc_chart_list"""
        session_id = event.unified_msg_origin
        tracked = self.data.get("chart_monitors", {}).get(session_id, [])
        if not tracked:
            yield event.plain_result(
                "当前没有汇率曲线追踪，使用 /icbc_chart_add [币种] 添加。"
            )
            return

        lines = [f"- {c}" for c in tracked]
        chart_data = self.data.get("chart_data", {})
        info_parts = []
        for c in tracked:
            count = len(chart_data.get(c, []))
            info_parts.append(f"{c}: {count} 条数据")

        auto_send = self.data.get("chart_auto_send", {}).get(session_id, False)
        auto_status = "已开启" if auto_send else "未开启"

        yield event.plain_result(
            "当前追踪的汇率曲线：\n"
            + "\n".join(lines)
            + f"\n\n定时推送走势图：{auto_status}"
            + "\n\n数据采集情况：\n"
            + "\n".join(info_parts)
            + "\n\n使用 /icbc_chart [币种] 查看走势图"
        )

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

        chart_path = self._generate_chart(currency, records)
        if chart_path:
            yield event.image_result(chart_path)
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
        self.save_data()

        if enabled:
            yield event.plain_result(
                "已开启定时推送汇率走势图。\n"
                "每次后台采集数据时，将自动发送追踪币种的走势图。"
            )
        else:
            yield event.plain_result("已关闭定时推送汇率走势图。")

    def _get_font_prop(self):
        """获取可用的中文字体 FontProperties，跨平台兼容。"""
        if hasattr(self, "_cached_font_prop"):
            return self._cached_font_prop

        # 策略1: 按名称搜索常见中文字体
        chinese_fonts = [
            "Microsoft YaHei",
            "SimHei",
            "SimSun",
            "PingFang SC",
            "Hiragino Sans GB",
            "WenQuanYi Micro Hei",
            "WenQuanYi Zen Hei",
            "Noto Sans CJK SC",
            "Noto Sans SC",
            "Source Han Sans SC",
            "Source Han Sans CN",
            "Droid Sans Fallback",
            "AR PL UMing CN",
        ]
        for name in chinese_fonts:
            try:
                path = fm.findfont(
                    fm.FontProperties(family=name), fallback_to_default=False
                )
                if (
                    path
                    and os.path.exists(path)
                    and not path.endswith("DejaVuSans.ttf")
                ):
                    self._cached_font_prop = fm.FontProperties(fname=path)
                    logger.info(f"汇率图表使用中文字体: {path}")
                    return self._cached_font_prop
            except Exception:
                continue

        # 策略2: 按文件名关键词搜索系统字体
        font_keywords = [
            "simhei",
            "msyh",
            "yahei",
            "notosanscjk",
            "notosanssc",
            "wqy",
            "simsun",
            "heiti",
            "songti",
            "droidsans",
            "sourcehan",
            "pingfang",
        ]
        try:
            for f in fm.findSystemFonts():
                fname_lower = os.path.basename(f).lower()
                if any(k in fname_lower for k in font_keywords):
                    self._cached_font_prop = fm.FontProperties(fname=f)
                    logger.info(f"汇率图表使用中文字体: {f}")
                    return self._cached_font_prop
        except Exception:
            pass

        # 策略3: 检查插件目录下的 fonts/ 文件夹
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        fonts_dir = os.path.join(plugin_dir, "fonts")
        if os.path.isdir(fonts_dir):
            for f in os.listdir(fonts_dir):
                if f.lower().endswith((".ttf", ".otf", ".ttc")):
                    path = os.path.join(fonts_dir, f)
                    try:
                        self._cached_font_prop = fm.FontProperties(fname=path)
                        logger.info(f"汇率图表使用插件自带字体: {path}")
                        return self._cached_font_prop
                    except Exception:
                        continue

        logger.warning(
            "未找到可用的中文字体，图表中文可能显示异常。\n"
            "解决方案: 1) 安装系统中文字体 (如 apt install fonts-noto-cjk);\n"
            "         2) 在插件 fonts/ 目录下放置中文字体文件 (.ttf/.otf)。"
        )
        self._cached_font_prop = fm.FontProperties()
        return self._cached_font_prop

    def _generate_chart(self, currency: str, records: list) -> str | None:
        """根据历史数据生成高质量汇率走势图，返回图片文件路径。"""
        try:
            plt.rcParams["axes.unicode_minus"] = False
            font_prop = self._get_font_prop()

            # 创建不同大小的字体副本
            def make_font(size, weight="normal"):
                fp = font_prop.copy()
                fp.set_size(size)
                fp.set_weight(weight)
                return fp

            fp_title = make_font(18, "bold")
            fp_label = make_font(12, "bold")
            fp_legend = make_font(11, "bold")
            fp_tick = make_font(9, "bold")
            fp_anno = make_font(9, "bold")
            fp_watermark = make_font(8, "bold")

            # 解析数据
            times, buy_prices, sell_prices = [], [], []
            for r in records:
                try:
                    t = datetime.strptime(r["time"], "%Y-%m-%d %H:%M")
                    times.append(t)
                    buy_prices.append(float(r.get("buy", 0)))
                    sell_prices.append(float(r.get("sell", 0)))
                except (ValueError, KeyError):
                    continue

            if not times:
                return None

            # ===== 主题配色 =====
            bg_color = "#ffffff"
            card_color = "#f8f9fa"
            text_color = "#1a1a2e"
            text_secondary = "#6c757d"
            grid_color = "#e9ecef"
            border_color = "#dee2e6"
            buy_color = "#1976D2"
            sell_color = "#E65100"
            color_green = "#2e7d32"
            color_red = "#c62828"

            # 创建图表
            fig, ax = plt.subplots(figsize=(12, 6))
            fig.set_facecolor(bg_color)
            ax.set_facecolor(card_color)

            # 边框样式
            for spine in ax.spines.values():
                spine.set_color(border_color)
                spine.set_linewidth(0.8)

            has_buy = any(p > 0 for p in buy_prices)
            has_sell = any(p > 0 for p in sell_prices)

            # 计算填充区域的底部基准
            all_valid = [p for p in (buy_prices + sell_prices) if p > 0]
            if all_valid:
                price_range = max(all_valid) - min(all_valid)
                fill_bottom = min(all_valid) - price_range * 0.05
            else:
                fill_bottom = 0

            # 绘制结汇价
            if has_buy:
                ax.plot(
                    times,
                    buy_prices,
                    linewidth=2.2,
                    label="结汇价",
                    color=buy_color,
                    zorder=3,
                    solid_capstyle="round",
                )
                ax.fill_between(
                    times,
                    buy_prices,
                    fill_bottom,
                    alpha=0.08,
                    color=buy_color,
                    zorder=2,
                )
                # 最新值标记点 (发光效果)
                ax.scatter(
                    [times[-1]],
                    [buy_prices[-1]],
                    color=buy_color,
                    s=80,
                    zorder=5,
                    edgecolors=bg_color,
                    linewidth=1.5,
                )
                ax.scatter(
                    [times[-1]],
                    [buy_prices[-1]],
                    color=buy_color,
                    s=200,
                    zorder=4,
                    alpha=0.12,
                )

            # 绘制购汇价
            if has_sell:
                ax.plot(
                    times,
                    sell_prices,
                    linewidth=2.2,
                    label="购汇价",
                    color=sell_color,
                    zorder=3,
                    solid_capstyle="round",
                )
                ax.fill_between(
                    times,
                    sell_prices,
                    fill_bottom,
                    alpha=0.08,
                    color=sell_color,
                    zorder=2,
                )
                ax.scatter(
                    [times[-1]],
                    [sell_prices[-1]],
                    color=sell_color,
                    s=80,
                    zorder=5,
                    edgecolors=bg_color,
                    linewidth=1.5,
                )
                ax.scatter(
                    [times[-1]],
                    [sell_prices[-1]],
                    color=sell_color,
                    s=200,
                    zorder=4,
                    alpha=0.12,
                )

            # ===== 最高价 / 最低价标注 =====
            for prices, color, has_data, label_name in [
                (buy_prices, buy_color, has_buy, "结汇价"),
                (sell_prices, sell_color, has_sell, "购汇价"),
            ]:
                if not has_data:
                    continue
                valid = [(t, p) for t, p in zip(times, prices) if p > 0]
                if len(valid) < 2:
                    continue

                valid_prices = [p for _, p in valid]
                max_val = max(valid_prices)
                min_val = min(valid_prices)

                # 最高价和最低价相同则跳过
                if max_val == min_val:
                    continue

                # 最高价水平参考虚线 + 右侧文字标签
                ax.axhline(
                    y=max_val, color=color, linestyle=":",
                    linewidth=0.8, alpha=0.45, zorder=1,
                )
                ax.text(
                    1.01, max_val,
                    f"▲ 高 {max_val:.4f}",
                    transform=ax.get_yaxis_transform(),
                    fontproperties=fp_anno,
                    color=color,
                    va="center", ha="left",
                    bbox={
                        "boxstyle": "round,pad=0.2",
                        "facecolor": bg_color,
                        "edgecolor": color,
                        "alpha": 0.85,
                        "linewidth": 0.6,
                    },
                    zorder=7,
                )

                # 最低价水平参考虚线 + 右侧文字标签
                ax.axhline(
                    y=min_val, color=color, linestyle=":",
                    linewidth=0.8, alpha=0.45, zorder=1,
                )
                ax.text(
                    1.01, min_val,
                    f"▼ 低 {min_val:.4f}",
                    transform=ax.get_yaxis_transform(),
                    fontproperties=fp_anno,
                    color=color,
                    va="center", ha="left",
                    bbox={
                        "boxstyle": "round,pad=0.2",
                        "facecolor": bg_color,
                        "edgecolor": color,
                        "alpha": 0.85,
                        "linewidth": 0.6,
                    },
                    zorder=7,
                )

            # 标题
            ax.set_title(
                f"{currency} 汇率走势",
                fontproperties=fp_title,
                color=text_color,
                pad=20,
                loc="left",
            )

            # 轴标签
            ax.set_xlabel(
                "时间",
                fontproperties=fp_label,
                color=text_secondary,
                labelpad=10,
            )
            ax.set_ylabel(
                "汇率",
                fontproperties=fp_label,
                color=text_secondary,
                labelpad=10,
            )

            # 网格
            ax.grid(True, alpha=0.6, color=grid_color, linestyle="-", linewidth=0.5)
            ax.set_axisbelow(True)

            # 刻度样式
            ax.tick_params(colors=text_secondary, labelsize=9, length=0)
            for label in ax.get_xticklabels() + ax.get_yticklabels():
                label.set_fontproperties(fp_tick)

            # 图例
            ax.legend(
                prop=fp_legend,
                facecolor=bg_color,
                edgecolor=border_color,
                labelcolor=text_color,
                loc="upper left",
                framealpha=0.95,
            )

            # X 轴时间格式
            if len(times) > 1:
                span = (times[-1] - times[0]).total_seconds()
                if span > 86400:  # > 1 天
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
                else:
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            else:
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))

            fig.autofmt_xdate(rotation=30)

            # 重新设置旋转后的 tick 字体
            for label in ax.get_xticklabels():
                label.set_fontproperties(fp_tick)

            # 最新值涨跌幅标注
            anno_offsets = [(18, 12), (18, -18)]
            for idx, (prices, color, has_data) in enumerate(
                [
                    (buy_prices, buy_color, has_buy),
                    (sell_prices, sell_color, has_sell),
                ]
            ):
                if not has_data or len(prices) < 1:
                    continue
                latest = prices[-1]
                first_val = prices[0]
                if first_val > 0:
                    change = latest - first_val
                    pct = change / first_val * 100
                    c = color_green if change >= 0 else color_red
                    symbol = "+" if change >= 0 else "-"
                    text = f"{latest:.4f}  {symbol}{abs(pct):.2f}%"
                else:
                    text = f"{latest:.4f}"
                    c = text_color

                ax.annotate(
                    text,
                    xy=(times[-1], latest),
                    xytext=anno_offsets[idx],
                    textcoords="offset points",
                    fontproperties=fp_anno,
                    color=c,
                    bbox={
                        "boxstyle": "round,pad=0.4",
                        "facecolor": bg_color,
                        "edgecolor": c,
                        "alpha": 0.95,
                        "linewidth": 1.2,
                    },
                    zorder=6,
                )

            # 底部数据来源水印
            fig.text(
                0.99,
                0.01,
                "数据来源: 中国工商银行",
                fontproperties=fp_watermark,
                color=text_secondary,
                ha="right",
                va="bottom",
                alpha=0.4,
            )

            # 右上角更新时间
            update_time = times[-1].strftime("%Y-%m-%d %H:%M") if times else ""
            fig.text(
                0.99,
                0.97,
                f"更新: {update_time}",
                fontproperties=fp_watermark,
                color=text_secondary,
                ha="right",
                va="top",
                alpha=0.5,
            )

            plt.tight_layout(rect=[0, 0.02, 1, 0.98])

            # 保存到临时文件
            tmp = tempfile.NamedTemporaryFile(
                suffix=".png", prefix="icbc_chart_", delete=False
            )
            fig.savefig(
                tmp.name,
                dpi=150,
                bbox_inches="tight",
                facecolor=fig.get_facecolor(),
                edgecolor="none",
            )
            plt.close(fig)
            return tmp.name

        except Exception as e:
            logger.error(f"生成汇率图表失败: {e}")
            return None

    def _collect_chart_data(self, rates: list):
        """采集追踪币种的历史数据，保留最近7天。"""
        chart_monitors = self.data.get("chart_monitors", {})
        # 汇总所有会话中追踪的币种（去重）
        all_currencies = set()
        for currencies in chart_monitors.values():
            all_currencies.update(currencies)

        if not all_currencies:
            return

        chart_data = self.data.get("chart_data", {})
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")

        for cur_name in all_currencies:
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

            if cur_name not in chart_data:
                chart_data[cur_name] = []

            chart_data[cur_name].append(record)

            # 清理超过7天的旧数据
            chart_data[cur_name] = [
                r for r in chart_data[cur_name] if r.get("time", "") >= cutoff
            ]

        self.data["chart_data"] = chart_data
        self.save_data()

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

                chart_path = self._generate_chart(currency, records)
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

    @filter.command("icbc_help")
    async def help_cmd(self, event: AstrMessageEvent):
        """获取工商银行汇率监控插件的使用帮助。用法: /icbc_help"""
        help_text = (
            "工商银行汇率监控插件使用说明：\n"
            "/icbc [币种名称]：实时查询汇率。\n"
            "/icbc_add_buy [币种] [高于/低于] [数值]：添加购汇价监控规则。\n"
            "/icbc_add_sell [币种] [高于/低于] [数值]：添加结汇价监控规则。\n"
            "/icbc_del [币种]：删除特定的汇率监控规则。\n"
            "/icbc_list：查看当前已配置的监控。\n\n"
            "※ 汇率曲线追踪：\n"
            "/icbc_chart_add [币种]：添加汇率曲线追踪。\n"
            "/icbc_chart_del [币种]：删除汇率曲线追踪。\n"
            "/icbc_chart_list：查看追踪列表和数据采集情况。\n"
            "/icbc_chart [币种]：查看汇率走势折线图。\n"
            "/icbc_chart_auto [on/off]：开启/关闭定时推送走势图。\n\n"
            "※ 其他设置：\n"
            "/icbc_cron [cron表达式]：自定义后台监控刷新频率。\n"
            "/icbc_help：查看此帮助信息。\n\n"
            "※ Cron 表达式简易教程：\n"
            "建议：后台轮询间隔尽量设置在 60 分钟或更长，防止过于频繁请求导致数据获取失败。\n"
            "格式: 分 时 日 月 周\n"
            "举例:\n"
            "0 * * * *     (每小时的第0分执行一次，约每60分钟)\n"
            "*/30 * * * *  (每30分钟执行一次)\n"
            "0 8 * * *     (每天早上8点执行)\n"
            "0 9,13,18 * * * (每天9、13、18点执行)"
        )
        yield event.plain_result(help_text)

    async def monitor_loop(self):
        while True:
            try:
                cron_expr = self.data.get("cron", "*/30 * * * *")
                if not croniter.croniter.is_valid(cron_expr):
                    cron_expr = "*/30 * * * *"

                now = datetime.now()
                cron = croniter.croniter(cron_expr, now)
                next_time = cron.get_next(datetime)
                sleep_seconds = (next_time - now).total_seconds()

                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)

                rates = await self.fetch_exchange_rates()
                if not rates:
                    continue

                # 采集汇率曲线数据
                self._collect_chart_data(rates)

                # 定时推送汇率走势图
                await self._auto_send_charts()

                monitors = self.data.get("monitors", {})
                if not monitors:
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
                                f"汇率提醒: {target_rate['currencyCHName']} {type_name}为 {price}，已{condition}设定的阈值 {threshold}！"
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
                            await self.context.send_message(
                                session_id, MessageChain(chain=[Plain(msg)])
                            )
                        except Exception as e:
                            logger.error(f"消息推送失败: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"汇率监控后台任务报错: {e}")
