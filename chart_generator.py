import os
import tempfile
from datetime import datetime

import matplotlib
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg

from astrbot.api import logger

THEMES = {
    "light": {
        "bg_color": "#ffffff",
        "card_color": "#f8f9fa",
        "text_color": "#1a1a2e",
        "text_secondary": "#6c757d",
        "grid_color": "#e9ecef",
        "border_color": "#dee2e6",
        "buy_color": "#1976D2",
        "sell_color": "#E65100",
        "color_green": "#2e7d32",
        "color_red": "#c62828",
    },
    "dark": {
        "bg_color": "#1e1e1e",
        "card_color": "#252526",
        "text_color": "#e0e0e0",
        "text_secondary": "#9e9e9e",
        "grid_color": "#333333",
        "border_color": "#424242",
        "buy_color": "#64b5f6",
        "sell_color": "#ffb74d",
        "color_green": "#81c784",
        "color_red": "#e57373",
    }
}

class ExchangeRateChartGenerator:
    _cached_font_prop = None

    @classmethod
    def _get_font_prop(cls) -> fm.FontProperties:
        """获取可用的中文字体 FontProperties，跨平台兼容。"""
        if cls._cached_font_prop is not None:
            return cls._cached_font_prop

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
                    cls._cached_font_prop = fm.FontProperties(fname=path)
                    logger.info(f"汇率图表使用中文字体: {path}")
                    return cls._cached_font_prop
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
                    cls._cached_font_prop = fm.FontProperties(fname=f)
                    logger.info(f"汇率图表使用中文字体: {f}")
                    return cls._cached_font_prop
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
                        cls._cached_font_prop = fm.FontProperties(fname=path)
                        logger.info(f"汇率图表使用插件自带字体: {path}")
                        return cls._cached_font_prop
                    except Exception:
                        continue

        logger.warning(
            "未找到可用的中文字体，图表中文可能显示异常。\n"
            "解决方案: 1) 安装系统中文字体 (如 apt install fonts-noto-cjk);\n"
            "         2) 在插件 fonts/ 目录下放置中文字体文件 (.ttf/.otf)。"
        )
        cls._cached_font_prop = fm.FontProperties()
        return cls._cached_font_prop

    @classmethod
    def generate(cls, currency: str, records: list, theme_name: str = "light") -> str | None:
        """根据历史数据生成高质量汇率走势图，返回图片文件路径。"""
        try:
            font_prop = cls._get_font_prop()

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

            # 获取主题
            theme = THEMES.get(theme_name, THEMES["light"])
            bg_color = theme["bg_color"]
            card_color = theme["card_color"]
            text_color = theme["text_color"]
            text_secondary = theme["text_secondary"]
            grid_color = theme["grid_color"]
            border_color = theme["border_color"]
            buy_color = theme["buy_color"]
            sell_color = theme["sell_color"]
            color_green = theme["color_green"]
            color_red = theme["color_red"]

            # 创建图表 (对象化 API)
            fig = Figure(figsize=(12, 6))
            canvas = FigureCanvasAgg(fig)
            fig.set_facecolor(bg_color)
            
            matplotlib.rcParams["axes.unicode_minus"] = False

            ax = fig.add_subplot(111)
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

            # 循环绘制结汇价和购汇价区域
            for prices, color, label, has_data in [
                (buy_prices, buy_color, "结汇价", has_buy),
                (sell_prices, sell_color, "购汇价", has_sell),
            ]:
                if has_data and prices:
                    ax.plot(
                        times,
                        prices,
                        linewidth=2.2,
                        label=label,
                        color=color,
                        zorder=3,
                        solid_capstyle="round",
                    )
                    ax.fill_between(
                        times,
                        prices,
                        fill_bottom,
                        alpha=0.08,
                        color=color,
                        zorder=2,
                    )
                    # 最新值标记点 (发光效果)
                    ax.scatter(
                        [times[-1]],
                        [prices[-1]],
                        color=color,
                        s=80,
                        zorder=5,
                        edgecolors=bg_color,
                        linewidth=1.5,
                    )
                    ax.scatter(
                        [times[-1]],
                        [prices[-1]],
                        color=color,
                        s=200,
                        zorder=4,
                        alpha=0.12,
                    )

            # ===== 最高价 / 最低价标注 =====
            for prices, color, has_data in [
                (buy_prices, buy_color, has_buy),
                (sell_prices, sell_color, has_sell),
            ]:
                if not has_data:
                    continue
                valid = [(t, p) for t, p in zip(times, prices) if p > 0]
                if len(valid) < 2:
                    continue

                valid_prices = [p for _, p in valid]
                max_val = max(valid_prices)
                min_val = min(valid_prices)
                if max_val == min_val:
                    continue

                max_idx = valid_prices.index(max_val)
                min_idx = valid_prices.index(min_val)
                max_time = valid[max_idx][0]
                min_time = valid[min_idx][0]

                # 最高价标注（点上方）
                ax.annotate(
                    f"最高 {max_val:.4f}",
                    xy=(max_time, max_val),
                    xytext=(0, 14),
                    textcoords="offset points",
                    fontproperties=fp_anno,
                    color=color,
                    ha="center", va="bottom",
                    bbox={
                        "boxstyle": "round,pad=0.3",
                        "facecolor": bg_color,
                        "edgecolor": color,
                        "alpha": 0.9,
                        "linewidth": 0.8,
                    },
                    zorder=7,
                )

                # 最低价标注（点下方）
                ax.annotate(
                    f"最低 {min_val:.4f}",
                    xy=(min_time, min_val),
                    xytext=(0, -14),
                    textcoords="offset points",
                    fontproperties=fp_anno,
                    color=color,
                    ha="center", va="top",
                    bbox={
                        "boxstyle": "round,pad=0.3",
                        "facecolor": bg_color,
                        "edgecolor": color,
                        "alpha": 0.9,
                        "linewidth": 0.8,
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

            fig.tight_layout(rect=[0, 0.02, 1, 0.98])

            # 保存到临时文件
            fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="icbc_chart_")
            os.close(fd)
            canvas.print_figure(
                tmp_path,
                dpi=150,
                bbox_inches="tight",
                facecolor=fig.get_facecolor(),
                edgecolor="none",
            )
            return tmp_path

        except Exception as e:
            logger.error(f"生成汇率图表失败: {e}")
            return None
