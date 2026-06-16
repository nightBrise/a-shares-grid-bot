"""
A 股网格交易系统 - 可视化仪表板
基于 Gradio + Plotly，读取本地 SQLite 数据，展示 K 线、技术指标、网格参数。
"""

import json
import logging
import functools
from datetime import datetime, timedelta
from pathlib import Path

import yaml

import gradio as gr
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

logger = logging.getLogger("grid_trading")

# ==================== 数据加载 ====================

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")

_FALLBACK_NAMES = {
    "000300.SH": "沪深300",
    "159901.SZ": "深100ETF", "159915.SZ": "创业板ETF", "159919.SZ": "沪深300ETF",
    "159922.SZ": "中证500ETF", "510050.SH": "上证50ETF", "510300.SH": "沪深300ETF",
    "510500.SH": "中证500ETF", "588000.SH": "科创50ETF", "588080.SH": "科创板50ETF",
}

_STOCK_NAME_CACHE: dict[str, str] = {}

@functools.lru_cache(maxsize=1)
def _load_stock_names() -> dict[str, str]:
    """从本地 SQLite 数据库加载股票中文名称。"""
    names = dict(_FALLBACK_NAMES)
    try:
        from data_layer.market_db import get_all_stock_names
        names.update(get_all_stock_names())
    except Exception:
        pass
    return names


def get_stock_name(code: str) -> str:
    """获取单只股票的中文名称。"""
    if not _STOCK_NAME_CACHE:
        _STOCK_NAME_CACHE.update(_load_stock_names())
    return _STOCK_NAME_CACHE.get(code, "")


# 全局缓存：避免每次请求都重新加载全部数据
_data_cache = None


def load_all_data() -> pd.DataFrame:
    """从 SQLite 读取历史数据，仅保留配置中的股票（带缓存）。"""
    global _data_cache
    if _data_cache is not None:
        return _data_cache
    configured = tuple(load_config_stocks())
    if not configured:
        return pd.DataFrame()
    try:
        from data_layer.market_db import get_multi_stock_data
        df = get_multi_stock_data(list(configured), data_dir="./data")
        if df is None or df.empty:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["code", "date"]).drop_duplicates(subset=["code", "date"])
        _data_cache = df
        return df
    except Exception as e:
        logger.warning(f"从 SQLite 加载数据失败: {e}")
        return pd.DataFrame()


def load_config_stocks() -> list[str]:
    """从 config.yaml 读取用户配置的股票列表。"""
    config_path = Path("configuration/config.yaml")
    if not config_path.exists():
        return []
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return config.get("stocks", [])


def get_available_codes() -> list[str]:
    """列出配置中有数据的股票代码。"""
    configured = load_config_stocks()
    if not configured:
        return []
    df = load_all_data()
    if df.empty:
        return []
    available = set(df["code"].unique())
    return sorted([c for c in configured if c in available])


def code_label(code: str) -> str:
    """返回 '代码 - 名称' 格式的标签。"""
    name = get_stock_name(code)
    return f"{code} {name}" if name else code


def extract_code(label: str) -> str:
    """从 '代码 名称' 格式中提取代码。"""
    return label.split()[0] if label else ""


def load_stock_data(code: str) -> pd.DataFrame:
    """获取指定股票的 OHLCV 数据。"""
    df = load_all_data()
    stock_df = df[df["code"] == code].copy()
    stock_df = stock_df.set_index("date").sort_index()
    return stock_df


def load_report() -> dict | None:
    """加载优化报告。"""
    report_path = OUTPUT_DIR / "report.json"
    if report_path.exists():
        with open(report_path) as f:
            return json.load(f)
    return None


def get_equity_data(code: str) -> tuple[list[float], list[str], list[dict], list[float]] | None:
    """从 report.json 提取全量净值数据、日期列表、交易明细和基准净值。"""
    report = load_report()
    if not report:
        return None
    for r in report.get("results", []):
        if r.get("code") == code:
            pv = r.get("full_portfolio_values", [])
            if not pv:
                return None
            trades_detail = r.get("full_trades_detail", [])
            bv = r.get("full_benchmark_values", [])
            data_start = r.get("full_data_start", "")
            data_end = r.get("full_data_end", "")
            if data_start and data_end:
                full_period = f"{data_start} 至 {data_end}"
            else:
                phase1 = report.get("phase1_period", "")
                phase2 = report.get("phase2_period", "")
                full_period = _merge_periods(phase1, phase2)
            dates = _generate_backtest_dates(full_period, len(pv) + 1)
            if not dates:
                return None
            return pv, dates[1:], trades_detail, bv
    return None


def _merge_periods(phase1: str, phase2: str) -> str:
    """合并 'YYYY-MM-DD 至 YYYY-MM-DD' 格式的两个时期为一个完整范围。"""
    import re
    m1 = re.search(r'(\d{4}-\d{2}-\d{2}).*?(\d{4}-\d{2}-\d{2})', phase1)
    m2 = re.search(r'(\d{4}-\d{2}-\d{2}).*?(\d{4}-\d{2}-\d{2})', phase2)
    if m1 and m2:
        start = min(m1.group(1), m2.group(1))
        end = max(m1.group(2), m2.group(2))
        return f"{start} 至 {end}"
    return phase2 or phase1


def _generate_backtest_dates(period_str: str, n: int) -> list[str]:
    """从 'YYYY-MM-DD 至 YYYY-MM-DD' 生成 n 个交易日日期字符串。"""
    import re
    m = re.search(r'(\d{4}-\d{2}-\d{2}).*?(\d{4}-\d{2}-\d{2})', period_str)
    if not m:
        return []
    start, end = m.group(1), m.group(2)
    df = load_all_data()
    if df.empty:
        return []
    # 取所有股票的并集日期
    all_dates = sorted(df["date"].unique())
    dates_in_range = [d for d in all_dates if start <= str(d)[:10] <= end]
    # 转为 ISO 日期字符串，Plotly.js 原生支持
    return [d.strftime("%Y-%m-%d") if hasattr(d, 'strftime') else str(d)[:10] for d in dates_in_range[:n]]


# ==================== 技术指标计算 ====================


def add_moving_averages(df: pd.DataFrame) -> pd.DataFrame:
    """添加均线。"""
    for period in [5, 10, 20, 60]:
        df[f"ma{period}"] = df["close"].rolling(period).mean()
    return df


def add_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """添加布林带。"""
    df["bb_mid"] = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    df["bb_upper"] = df["bb_mid"] + std_dev * std
    df["bb_lower"] = df["bb_mid"] - std_dev * std
    return df


def compute_full_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算全部技术指标（复用 indicators.py）。"""
    try:
        from trading_core.indicators import calculate_all_indicators
        return calculate_all_indicators(df)
    except Exception:
        # fallback: 手动计算基础指标
        result = df.copy()
        if "close" in result.columns and len(result) >= 60:
            # 简化版 Hurst（不依赖 numba）
            result["hurst_60d"] = result["close"].rolling(60).apply(
                lambda x: _simple_hurst(x.values), raw=False
            )
            # 简化版波动率
            log_ret = np.log(result["close"] / result["close"].shift(1))
            result["volatility_60d"] = log_ret.rolling(60).std() * np.sqrt(252)
        if all(c in result.columns for c in ["high", "low", "close"]) and len(result) >= 30:
            # 简化版 ATR
            tr = pd.DataFrame({
                "hl": result["high"] - result["low"],
                "hc": (result["high"] - result["close"].shift(1)).abs(),
                "lc": (result["low"] - result["close"].shift(1)).abs(),
            }).max(axis=1)
            result["atr_20"] = tr.rolling(20).mean()
        return result


def _simple_hurst(series: np.ndarray) -> float:
    """简化版 Hurst 指数（R/S 分析）。"""
    n = len(series)
    if n < 20:
        return np.nan
#     max_k = min(n // 2, 30)
    rs_list = []
    ns_list = []
    for k in [int(n // 2 ** i) for i in range(1, 6) if n // 2 ** i >= 10]:
        subseries = [series[i * k:(i + 1) * k] for i in range(n // k)]
        rs_vals = []
        for sub in subseries:
            if len(sub) < 2:
                continue
            mean = np.mean(sub)
            dev = sub - mean
            cumdev = np.cumsum(dev)
            r = np.max(cumdev) - np.min(cumdev)
            s = np.std(sub, ddof=1)
            if s > 0:
                rs_vals.append(r / s)
        if rs_vals:
            rs_list.append(np.mean(rs_vals))
            ns_list.append(k)
    if len(rs_list) < 2:
        return np.nan
    log_ns = np.log(ns_list)
    log_rs = np.log(rs_list)
    slope = np.polyfit(log_ns, log_rs, 1)[0]
    return float(np.clip(slope, 0, 1))


# ==================== 图表构建 ====================


def build_kline_chart(
    code: str,
    show_ma: list[int],
    show_bollinger: bool,
    show_grid_lines: bool,
    date_range: tuple[str, str] | None,
) -> go.Figure:
    """构建 K 线图 + 成交量 + 指标子图。"""
    df = load_stock_data(code)
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="无数据", showarrow=False, font=dict(size=20))
        return fig

    # 日期过滤
    if date_range:
        start, end = date_range
        if start:
            df = df[df.index >= start]
        if end:
            df = df[df.index <= end]

    # 计算指标
    df = add_moving_averages(df)
    if show_bollinger:
        df = add_bollinger_bands(df)

    # 计算技术指标
    df = compute_full_indicators(df)

    # Plotly 日期：转为纯字符串避开 Gradio gr.Plot 的 datetime 序列化问题
    x_dates = df.index.strftime("%Y-%m-%d").tolist()

    # 检查是否有净值数据
    equity_info = get_equity_data(code)
    has_equity = equity_info is not None

    # 确定子图行数
    indicator_rows = []
    for col in ["hurst_60d", "adx", "volatility_60d", "ou_half_life"]:
        if col in df.columns and df[col].notna().any() and np.isfinite(df[col]).any():
            indicator_rows.append(col)

    equity_subrows = 2 if has_equity else 0  # 净值 + 回撤
    n_rows = 2 + equity_subrows + len(indicator_rows)
    heights = [0.35, 0.10]
    if has_equity:
        heights += [0.12, 0.08]
    remaining = 0.45 - (0.20 if has_equity else 0)
    heights += [remaining / max(len(indicator_rows), 1)] * len(indicator_rows)

    stock_name = get_stock_name(code)
    title = f"{code} {stock_name}" if stock_name else code

    subplot_titles = [f"{title}"]
    subplot_titles += ["成交量"]
    if has_equity:
        subplot_titles += ["净值曲线", "回撤"]
    indicator_titles = {
        "hurst_60d": "Hurst指数", "adx": "ADX趋势",
        "volatility_60d": "波动率", "ou_half_life": "OU半衰期",
    }
    subplot_titles += [indicator_titles.get(col, col) for col in indicator_rows]

    fig = make_subplots(
        rows=n_rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=heights,
        subplot_titles=subplot_titles,
    )

    # K 线
    colors_up = "red"
    colors_down = "green"

    fig.add_trace(
        go.Candlestick(
            x=x_dates,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            increasing_line_color=colors_up,
            decreasing_line_color=colors_down,
            increasing_fillcolor=colors_up,
            decreasing_fillcolor=colors_down,
            name="K 线",
            hovertemplate="%{x|%Y年%m月%d日}<br>开盘: %{open:.2f}<br>最高: %{high:.2f}<br>最低: %{low:.2f}<br>收盘: %{close:.2f}<extra></extra>",
        ),
        row=1,
        col=1,
    )

    # 均线
    ma_colors = {"ma5": "#FF6B6B", "ma10": "#4ECDC4", "ma20": "#45B7D1", "ma60": "#96CEB4"}
    for ma in show_ma:
        col_name = f"ma{ma}"
        if col_name in df.columns:
            fig.add_trace(
                go.Scatter(
                    x=x_dates,
                    y=df[col_name],
                    mode="lines",
                    name=f"MA{ma}",
                    line=dict(width=1, color=ma_colors.get(col_name, "white")),
                    hovertemplate=f"%{{x}}<br>MA{ma}: %{{y:.2f}}<extra></extra>",
                ),
                row=1,
                col=1,
            )

    # 布林带
    if show_bollinger and "bb_upper" in df.columns:
        fig.add_trace(
            go.Scatter(
                x=x_dates,
                y=df["bb_upper"],
                mode="lines",
                name="布林上轨",
                line=dict(width=1, color="rgba(255,255,0,0.5)", dash="dot"),
                hovertemplate="%{x|%Y年%m月%d日}<br>布林上轨: %{y:.2f}<extra></extra>",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=x_dates,
                y=df["bb_lower"],
                mode="lines",
                name="布林下轨",
                line=dict(width=1, color="rgba(255,255,0,0.5)", dash="dot"),
                fill="tonexty",
                fillcolor="rgba(255,255,0,0.05)",
                hovertemplate="%{x|%Y年%m月%d日}<br>布林下轨: %{y:.2f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

    # 网格线
    if show_grid_lines:
        _add_grid_lines(fig, code, df)

    # 成交量
    vol_colors = [
        colors_up if c >= o else colors_down
        for c, o in zip(df["close"], df["open"])
    ]
    fig.add_trace(
        go.Bar(
            x=x_dates,
            y=df["volume"],
            marker_color=vol_colors,
            name="成交量",
            showlegend=False,
            hovertemplate="%{x|%Y年%m月%d日}<br>成交量: %{y:.0f}<extra></extra>",
        ),
        row=2,
        col=1,
    )

    # 净值曲线和回撤
    next_row = 3
    if has_equity:
        eq_values, eq_dates_full, trades_detail, benchmark_values = equity_info
        # 按日期范围过滤净值数据
        eq_dates = eq_dates_full
        bv_filtered = benchmark_values
        if date_range:
            start, end = date_range
            filtered_indices = [
                i for i, d in enumerate(eq_dates_full)
                if (not start or d >= start) and (not end or d <= end)
            ]
            if filtered_indices:
                eq_values = [eq_values[i] for i in filtered_indices]
                eq_dates = [eq_dates_full[i] for i in filtered_indices]
                if benchmark_values:
                    bv_filtered = [benchmark_values[i] for i in filtered_indices if i < len(benchmark_values)]
                # 过滤交易：只保留在日期范围内的交易，并重映射 day 索引
                idx_map = {old: new for new, old in enumerate(filtered_indices)}
                filtered_trades = []
                for t in trades_detail:
                    d = t.get("day", 0)
                    if 1 <= d <= len(eq_dates_full) and (d - 1) in idx_map:
                        t_copy = dict(t)
                        t_copy["day"] = idx_map[d - 1] + 1
                        filtered_trades.append(t_copy)
                trades_detail = filtered_trades
        # 归一化净值 → 百分比收益（0为基准，正=赚钱，负=亏损）
        norm_values = [(v / eq_values[0] - 1) * 100 if eq_values[0] > 0 else 0.0 for v in eq_values]
        grid_ret = norm_values[-1] if norm_values else 0
        # 网格净值曲线
        fig.add_trace(
            go.Scatter(
                x=eq_dates,
                y=norm_values,
                mode="lines",
                name=f"网格 ({grid_ret:+.1f}%)",
                line=dict(width=1.5, color="#FFD700"),
                fill="tozeroy",
                fillcolor="rgba(255,215,0,0.08)",
                hovertemplate="%{x|%Y年%m月%d日}<br>网格收益: %{y:+.2f}%<extra></extra>",
            ),
            row=next_row,
            col=1,
        )
        # 买入持有基准线
        if bv_filtered and len(bv_filtered) >= len(eq_values):
            bh_norm = [(v / bv_filtered[0] - 1) * 100 if bv_filtered[0] > 0 else 0.0 for v in bv_filtered[:len(eq_values)]]
            bh_ret = bh_norm[-1] if bh_norm else 0
            alpha_ret = grid_ret - bh_ret
            fig.add_trace(
                go.Scatter(
                    x=eq_dates,
                    y=bh_norm,
                    mode="lines",
                    name=f"持有 ({bh_ret:+.1f}%)",
                    line=dict(width=1.2, color="#888", dash="dash"),
                    hovertemplate="%{x|%Y年%m月%d日}<br>持有收益: %{y:+.2f}%<extra></extra>",
                ),
                row=next_row,
                col=1,
            )
            # 超额收益标注（移到底部避免与子图标题重叠）
            fig.add_annotation(
                text=f"网格 {grid_ret:+.1f}% | 持有 {bh_ret:+.1f}% | 超额 {alpha_ret:+.1f}%",
                xref=f"x{next_row} domain", yref=f"y{next_row} domain",
                x=0.5, y=-0.25, xanchor="center", yanchor="top",
                showarrow=False,
                font=dict(size=11, color="#FFD700" if alpha_ret >= 0 else "#FF6B6B"),
            )
        # 基准线 0%
        fig.add_hline(
            y=0, line_dash="dash", line_color="rgba(255,255,255,0.3)",
            row=next_row, col=1,
        )
        # 回撤子图（从百分比收益计算）
        peak = norm_values[0]
        drawdowns = []
        for v in norm_values:
            if v > peak:
                peak = v
            drawdowns.append(peak - v)  # 直接用百分比差值
        fig.add_trace(
            go.Scatter(
                x=eq_dates,
                y=drawdowns,
                mode="lines",
                name="回撤%",
                line=dict(width=1, color="#FF6B6B"),
                fill="tozeroy",
                fillcolor="rgba(255,107,107,0.2)",
                hovertemplate="%{x|%Y年%m月%d日}<br>回撤: %{y:.2f}%<extra></extra>",
            ),
            row=next_row + 1,
            col=1,
        )
        # 交易标注
        _add_trade_annotations(fig, trades_detail, eq_dates, df)
        next_row += 2

    # 指标子图
    indicator_colors = {
        "hurst_60d": "#FF6B6B",
        "adx": "#4ECDC4",
        "volatility_60d": "#45B7D1",
        "ou_half_life": "#96CEB4",
    }
    for i, col in enumerate(indicator_rows):
        row_idx = next_row + i
        fig.add_trace(
            go.Scatter(
                x=x_dates,
                y=df[col],
                mode="lines",
                name=indicator_titles.get(col, col),
                line=dict(width=1.2, color=indicator_colors.get(col, "#FFFFFF")),
                hovertemplate=f"%{{x}}<br>{indicator_titles.get(col, col)}: %{{y:.4f}}<extra></extra>",
            ),
            row=row_idx,
            col=1,
        )
        # ADX 额外绘制 +DI / -DI
        if col == "adx" and "plus_di" in df.columns:
            fig.add_trace(
                go.Scatter(
                    x=x_dates,
                    y=df["plus_di"],
                    mode="lines",
                    name="+DI",
                    line=dict(width=0.8, color="rgba(255,107,107,0.5)"),
                    hovertemplate="%{x|%Y年%m月%d日}<br>+DI: %{y:.2f}<extra></extra>",
                ),
                row=row_idx,
                col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=x_dates,
                    y=df["minus_di"],
                    mode="lines",
                    name="-DI",
                    line=dict(width=0.8, color="rgba(78,205,196,0.5)"),
                    hovertemplate="%{x|%Y年%m月%d日}<br>-DI: %{y:.2f}<extra></extra>",
                ),
                row=row_idx,
                col=1,
            )
        # Hurst 参考线
        if col == "hurst_60d":
            fig.add_hline(
                y=0.5, line_dash="dash", line_color="rgba(255,255,255,0.3)",
                annotation_text="H=0.5", row=row_idx, col=1,
            )

    # 布局
    fig.update_layout(
        height=300 + 150 * n_rows,
        autosize=True,
        template="plotly_dark",
        xaxis_rangeslider_visible=False,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=60, r=20, t=80, b=30),
        dragmode="zoom",
        hovermode="x unified",
    )
    # x 轴为纯日期字符串，显式声明类型防止 Plotly 误判
    fig.update_xaxes(type="date", tickformat="%Y年%m月")
    # 限制缩放范围不超出数据边界
    fig.update_xaxes(fixedrange=False, rangemode="normal")
    fig.update_yaxes(fixedrange=False, rangemode="normal")
    # 启用滚轮缩放（通过 plotly.js config）
    fig._config = {"scrollZoom": True}

    return fig


def _add_grid_lines(fig: go.Figure, code: str, df: pd.DataFrame):
    """从 report.json 读取网格参数并绘制网格线。"""
    report = load_report()
    if not report:
        return

    result = None
    for r in report.get("results", []):
        if r.get("code") == code:
            result = r
            break
    if not result:
        return

    params = result.get("final_params", {})
    spacing = params.get("grid_spacing", 0.02)
    max_grids = params.get("max_grids", 5)

    # 用最近收盘价作为参考价
    ref_price = df["close"].iloc[-1]
    if pd.isna(ref_price) or ref_price <= 0:
        return

    for level in range(-max_grids, max_grids + 1):
        price = ref_price * (1 + level * spacing)
        color = "rgba(255,165,0,0.3)" if level == 0 else "rgba(100,149,237,0.2)"
        dash = "solid" if level == 0 else "dash"
        fig.add_hline(
            y=price,
            line_dash=dash,
            line_color=color,
            row=1,
            col=1,
        )


def _add_trade_annotations(fig: go.Figure, trades: list[dict], eq_dates: list[str], df: pd.DataFrame):
    """在 K 线图上标注买卖交易点位。"""
    if not trades or not eq_dates:
        return
    buy_dates, buy_prices, buy_labels = [], [], []
    sell_dates, sell_prices, sell_labels = [], [], []
    for t in trades:
        day = t.get("day", 0)
        if day < 1 or day > len(eq_dates):
            continue
        date = eq_dates[day - 1]  # eq_dates 从第2天开始，day=1 对应 eq_dates[0]
        price = t.get("price", 0)
        qty = t.get("qty", 0)
        if t.get("type") == "buy":
            buy_dates.append(date)
            buy_prices.append(price)
            buy_labels.append(f"买 {qty}股 @{price:.3f}")
        else:
            sell_dates.append(date)
            sell_prices.append(price)
            sell_labels.append(f"卖 {qty}股 @{price:.3f}")
    # 限制标注数量避免图表过密（最多各显示 50 个）
    max_marks = 50
    if len(buy_dates) > max_marks:
        step = len(buy_dates) // max_marks
        buy_dates = buy_dates[::step]
        buy_prices = buy_prices[::step]
        buy_labels = buy_labels[::step]
    if len(sell_dates) > max_marks:
        step = len(sell_dates) // max_marks
        sell_dates = sell_dates[::step]
        sell_prices = sell_prices[::step]
        sell_labels = sell_labels[::step]
    if buy_dates:
        fig.add_trace(
            go.Scatter(
                x=buy_dates,
                y=buy_prices,
                mode="markers",
                name="买入",
                marker=dict(symbol="triangle-up", size=8, color="#00FF00"),
                text=buy_labels,
                hoverinfo="text+x",
            ),
            row=1,
            col=1,
        )
    if sell_dates:
        fig.add_trace(
            go.Scatter(
                x=sell_dates,
                y=sell_prices,
                mode="markers",
                name="卖出",
                marker=dict(symbol="triangle-down", size=8, color="#FF4444"),
                text=sell_labels,
                hoverinfo="text+x",
            ),
            row=1,
            col=1,
        )


# ==================== 优化结果表格 ====================


def build_optimization_summary_chart(code: str = "") -> go.Figure:
    """单股票：P1 vs P2 参数对比 + 指标面板。多股票：各股对比柱状图。"""
    report = load_report()
    if not report:
        fig = go.Figure()
        fig.add_annotation(text="无优化报告", showarrow=False, font=dict(size=16))
        return fig

    results = report.get("results", [])
    if not results:
        fig = go.Figure()
        fig.add_annotation(text="无优化结果", showarrow=False, font=dict(size=16))
        return fig

    if code:
        results = [r for r in results if r.get("code") == code]
        if not results:
            fig = go.Figure()
            fig.add_annotation(text=f"无 {code} 的优化结果", showarrow=False, font=dict(size=16))
            return fig

    # 单股票：参数对比 + 指标面板
    if code and len(results) == 1:
        return _build_single_stock_summary(results[0])

    # 多股票：各股对比柱状图
    codes = [r.get("code", "") for r in results]
    labels = [code_label(c) for c in codes]
    returns = [r.get("final_return", 0) * 100 for r in results]
    bh_returns = [r.get("full_benchmark_return", 0) * 100 for r in results]
    drawdowns = [r.get("final_drawdown", 0) * 100 for r in results]
    calmars = [r.get("final_calmar", 0) for r in results]
    trades = [r.get("final_trades", 0) for r in results]

    fig = make_subplots(
        rows=2, cols=2,
        subplot_titles=["收益率对比 (%)", "最大回撤 (%)", "卡玛比率", "交易次数"],
        vertical_spacing=0.12,
        horizontal_spacing=0.08,
    )

    colors_ret = ["#FF4444" if r < 0 else "#00CC66" for r in returns]
    fig.add_trace(
        go.Bar(x=labels, y=returns, marker_color=colors_ret, name="网格收益",
               text=[f"{r:.1f}%" for r in returns], textposition="outside"),
        row=1, col=1,
    )
    fig.add_trace(
        go.Bar(x=labels, y=bh_returns, marker_color="#666", name="持有收益",
               text=[f"{r:.1f}%" for r in bh_returns], textposition="outside"),
        row=1, col=1,
    )

    fig.add_trace(
        go.Bar(x=labels, y=drawdowns, marker_color="#FF6B6B", name="回撤",
               text=[f"{d:.1f}%" for d in drawdowns], textposition="outside"),
        row=1, col=2,
    )

    colors_cal = ["#FF4444" if c < 0 else "#4ECDC4" for c in calmars]
    fig.add_trace(
        go.Bar(x=labels, y=calmars, marker_color=colors_cal, name="卡玛比率",
               text=[f"{c:.2f}" for c in calmars], textposition="outside"),
        row=2, col=1,
    )

    fig.add_trace(
        go.Bar(x=labels, y=trades, marker_color="#45B7D1", name="交易次数",
               text=[str(t) for t in trades], textposition="outside"),
        row=2, col=2,
    )

    fig.update_layout(
        height=500,
        autosize=True,
        template="plotly_dark",
        showlegend=False,
        margin=dict(l=40, r=20, t=50, b=80),
    )
    fig.update_xaxes(tickangle=-30)
    return fig


def _build_single_stock_summary(r: dict) -> go.Figure:
    """单股票优化汇总：参数 + 指标，全部用表格展示。"""
    p1 = r.get("phase1_params", {})
    fp = r.get("final_params", {})
    code = r.get("code", "")
    name = get_stock_name(code)
    title = f"{code} {name}" if name else code

    # 参数行
    param_rows = [
        ("网格间距", _fmt_param(p1.get("grid_spacing", 0), "grid_spacing"),
         _fmt_param(fp.get("grid_spacing", 0), "grid_spacing"),
         "相邻网格价格百分比间距，越大交易越少但单笔利润高"),
        ("网格金额", _fmt_param(p1.get("grid_amount", 0), "grid_amount"),
         _fmt_param(fp.get("grid_amount", 0), "grid_amount"),
         "每格交易金额（元），受流动性约束"),
        ("初始仓位", _fmt_param(p1.get("initial_position", 0), "initial_position"),
         _fmt_param(fp.get("initial_position", 0), "initial_position"),
         "建仓比例，0.5 = 用 50% 资金买入底仓"),
        ("最大层数", _fmt_param(p1.get("max_grids", 0), "max_grids"),
         _fmt_param(fp.get("max_grids", 0), "max_grids"),
         "中心线上下各 N 层网格，层数越多覆盖范围越广"),
    ]

    # 指标行
    ret = r.get("final_return", 0) * 100
    bh_ret = r.get("full_benchmark_return", 0) * 100
    alpha = ret - bh_ret
    dd = r.get("final_drawdown", 0) * 100
    calmar = r.get("final_calmar", 0)
    n_trades = r.get("final_trades", 0)
    score = r.get("final_score", 0)
    score_str = f"{score:.2f}" if score != -999 else "不达标"

    metric_rows = [
        ("网格收益", f"{ret:+.2f}%", "网格策略回测总收益"),
        ("持有收益", f"{bh_ret:+.2f}%", "同期买入持有收益（基准）"),
        ("超额收益", f"{alpha:+.2f}%", "网格相对持有的超额，正值=网格有效"),
        ("最大回撤", f"{dd:.2f}%", "从峰值回落幅度，越小越好"),
        ("卡玛比率", f"{calmar:.3f}", "收益率/回撤，越高越好（负值=亏损）"),
        ("交易次数", f"{n_trades}", "回测期间触发的网格买卖次数"),
        ("综合得分", score_str, "复合优化目标，-999 表示回撤超限未达标"),
    ]

    # 合并
    categories = ["参数"] * len(param_rows) + ["指标"] * len(metric_rows)
    items = [r[0] for r in param_rows + metric_rows]
    p1_col = [r[1] for r in param_rows] + ["—"] * len(metric_rows)
    fp_col = [r[2] for r in param_rows] + ["—"] * len(metric_rows)
    values = ["—"] * len(param_rows) + [r[1] for r in metric_rows]
    descriptions = [r[3] for r in param_rows] + [r[2] for r in metric_rows]

    fig = go.Figure(go.Table(
        columnorder=[1, 2, 3, 4, 5, 6],
        columnwidth=[60, 90, 90, 90, 80, 250],
        header=dict(
            values=["分类", "项目", "贝叶斯", "WF微调", "数值", "说明"],
            fill_color="#2d3748",
            font=dict(color="white", size=12),
            align="left",
            height=30,
        ),
        cells=dict(
            values=[categories, items, p1_col, fp_col, values, descriptions],
            fill_color=[
                ["#1a202c"] * len(categories),  # 分类
                ["#1a202c"] * len(categories),  # 项目
                ["#1a202c"] * len(categories),  # P1
                ["#1a202c"] * len(categories),  # P2
                ["#1a202c"] * len(categories),  # 数值
                ["#1a202c"] * len(categories),  # 说明
            ],
            font=dict(color="white", size=11),
            align="left",
            height=26,
        ),
    ))

    fig.update_layout(
        height=260,
        autosize=True,
        template="plotly_dark",
        margin=dict(l=20, r=20, t=40, b=20),
        title_text=f"{title} 优化结果",
        title_x=0.5,
    )
    return fig


def _fmt_param(val: float, key: str) -> str:
    """格式化参数显示值。"""
    if key == "grid_spacing":
        return f"{val:.4f}"
    if key == "grid_amount":
        return f"{val:,.0f}"
    if key == "initial_position":
        return f"{val:.3f}"
    if key == "max_grids":
        return f"{int(val)}"
    return f"{val}"


def build_report_table(code: str = "") -> pd.DataFrame | None:
    """构建优化结果表格。传入 code 则只显示该股票。"""
    report = load_report()
    if not report:
        return None

    rows = []
    for r in report.get("results", []):
        if code and r.get("code") != code:
            continue
        fp = r.get("final_params", {})
        pp = r.get("phase1_params", {})
        grid_ret = r.get("final_return", 0)
        bh_ret = r.get("full_benchmark_return", 0)
        rows.append({
            "股票代码": r.get("code", ""),
            "P1间距": pp.get("grid_spacing", ""),
            "P1金额": pp.get("grid_amount", ""),
            "P1初始仓位": pp.get("initial_position", ""),
            "P1最大层数": pp.get("max_grids", ""),
            "最终间距": fp.get("grid_spacing", ""),
            "最终金额": fp.get("grid_amount", ""),
            "最终初始仓位": fp.get("initial_position", ""),
            "最终最大层数": fp.get("max_grids", ""),
            "卡玛比率": r.get("final_calmar", ""),
            "最大回撤": r.get("final_drawdown", ""),
            "网格收益": grid_ret,
            "持有收益": bh_ret,
            "超额收益": grid_ret - bh_ret,
            "交易次数": r.get("final_trades", ""),
        })

    df = pd.DataFrame(rows)
    # 格式化数值
    for col in ["P1间距", "最终间距", "最大回撤", "网格收益", "持有收益", "超额收益"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: f"{x:.4f}" if isinstance(x, float) else x)
    for col in ["卡玛比率"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: f"{x:.2f}" if isinstance(x, float) else x)
    return df


def build_stock_summary(code: str) -> dict:
    """获取单只股票的基本摘要。"""
    df = load_stock_data(code)
    if df.empty:
        return {}

    latest = df.iloc[-1]
    first = df.iloc[0]
    return {
        "股票代码": code,
        "股票名称": get_stock_name(code),
        "数据起始": str(first.name),
        "数据截止": str(latest.name),
        "总天数": len(df),
        "最新收盘": f"{latest['close']:.3f}",
        "最高价": f"{df['high'].max():.3f}",
        "最低价": f"{df['low'].min():.3f}",
        "区间涨幅": f"{(latest['close'] / first['close'] - 1) * 100:.2f}%",
    }


# ==================== Gradio UI ====================


def create_dashboard():
    """创建 Gradio 仪表板。"""
    all_codes = get_available_codes()
    all_labels = [code_label(c) for c in all_codes]

    # 已优化股票列表
    report = load_report()
    optimized_codes = set()
    if report:
        for r in report.get("results", []):
            code = r.get("code", "")
            if code and not r.get("error"):
                optimized_codes.add(code)
    optimized_labels = [code_label(c) for c in all_codes if c in optimized_codes]

    with gr.Blocks(
        title="A 股网格交易系统 - 可视化仪表板",
    ) as app:
        gr.Markdown("# A 股网格交易系统 - 可视化仪表板")

        with gr.Accordion("指标与参数说明", open=False):
            gr.Markdown("""
### K 线图叠加指标
| 指标 | 含义 | 网格交易参考 |
|------|------|-------------|
| **MA5/10/20/60** | 5/10/20/60 日简单移动平均线 | MA 向上排列 = 多头趋势，向下 = 空头；价格在 MA 附近适合开网格 |
| **布林带** | 20 日均线 ± 2 倍标准差 | 价格触及上轨可能回落，触及下轨可能反弹；带宽收窄预示变盘 |

### 子图技术指标
| 指标 | 含义 | 网格交易参考 |
|------|------|-------------|
| **Hurst 指数** | 60 日滚动 R/S 分析，衡量趋势持续性 | **H < 0.5** = 均值回归（适合网格）；H = 0.5 = 随机游走；H > 0.5 = 趋势（不适合网格） |
| **ADX** | 14 日平均趋向指数，衡量趋势强度 | **ADX < 20** = 无趋势（适合网格）；20-40 = 中等趋势；> 40 = 强趋势（暂停网格） |
| **+DI / -DI** | 方向指标，与 ADX 配合 | +DI > -DI = 多头主导；-DI > +DI = 空头主导 |
| **波动率** | 60 日年化波动率（对数收益） | 决定网格间距 k 系数：低波动 k=2.5（窄间距），高波动 k=1.5（宽间距） |
| **OU 半衰期** | Ornstein-Uhlenbeck 均值回归半衰期 | **< 15 天** = 快速回归（理想）；15-60 天 = 中等；> 60 天 = 回归慢（不推荐网格） |

### 网格线（橙色=中心线，蓝色虚线=网格层级）
| 参数 | 含义 |
|------|------|
| **间距 (grid_spacing)** | 相邻网格的价格百分比间距，如 0.02 = 2% |
| **金额 (grid_amount)** | 每格交易金额（元） |
| **初始仓位 (initial_position)** | 建仓比例，0.5 = 用 50% 资金建底仓 |
| **最大层数 (max_grids)** | 中心线上下各 N 层网格 |

### 净值曲线与回撤
| 指标 | 含义 |
|------|------|
| **净值曲线** | 归一化资金曲线（起始=1.0），展示回测期间的盈亏走势 |
| **回撤** | 从历史最高净值回落的百分比，越小越好；绿色填充区域 |
| **交易标注** | K 线图上绿色上箭头=买入，红色下箭头=卖出，悬停可查看详情 |

### 优化结果表
| 列 | 含义 |
|----|------|
| **P1 / 最终** | 阶段一(贝叶斯) / 阶段二(WF微调)后的参数 |
| **卡玛比率** | 收益率 / 最大回撤，越高越好（负值表示亏损） |
| **最大回撤** | 期间最大净值回撤比例 |
| **收益率** | 回测期间总收益率 |
| **交易次数** | 回测期间触发的网格交易次数 |
""")

        with gr.Row():
            code_dropdown = gr.Dropdown(
                choices=all_labels,
                value=all_labels[0] if all_labels else None,
                label="选择股票",
                scale=2,
            )
            range_dropdown = gr.Dropdown(
                choices=["全部", "3个月", "6个月", "1年", "2年", "3年"],
                value="全部",
                label="日期范围",
                scale=1,
            )
            opt_only_checkbox = gr.Checkbox(
                label="仅已优化",
                value=False,
                info=f"只显示已完成优化的 {len(optimized_codes)} 只股票",
                scale=1,
            )

        with gr.Row():
            show_ma5 = gr.Checkbox(label="MA5", value=True, info="5 日均线，短线趋势")
            show_ma10 = gr.Checkbox(label="MA10", value=True, info="10 日均线")
            show_ma20 = gr.Checkbox(label="MA20", value=True, info="20 日均线，布林带中轨")
            show_ma60 = gr.Checkbox(label="MA60", value=False, info="60 日均线，中长线趋势")
            show_bollinger = gr.Checkbox(label="布林带", value=False, info="20日±2σ，价格多数时间在此通道内运行")
            show_grid = gr.Checkbox(label="网格线", value=True, info="优化后的网格层级线（橙色=中心，蓝色=买卖层）")

        with gr.Row():
            chart_output = gr.Plot(value=None, label="K 线图")

        with gr.Row():
            summary_output = gr.JSON(label="股票摘要", value={})
            report_table = gr.Dataframe(label="优化结果", interactive=False, value=pd.DataFrame())

        with gr.Row():
            summary_chart_output = gr.Plot(value=None, label="优化汇总")

        # 事件绑定
        def filter_optimized(opt_only):
            """切换仅显示已优化股票。"""
            choices = optimized_labels if opt_only else all_labels
            return gr.Dropdown(choices=choices, value=choices[0] if choices else None)

        opt_only_checkbox.change(
            fn=filter_optimized,
            inputs=[opt_only_checkbox],
            outputs=[code_dropdown],
        )

        def update_chart(label, ma5, ma10, ma20, ma60, bollinger, grid, range_label):
            code = extract_code(label)
            if not code:
                return None, {}, pd.DataFrame(), None
            show_ma = []
            if ma5:
                show_ma.append(5)
            if ma10:
                show_ma.append(10)
            if ma20:
                show_ma.append(20)
            if ma60:
                show_ma.append(60)

            # 日期范围
            date_range_val = None
            if range_label and range_label != "全部":
                days_map = {"3个月": 90, "6个月": 180, "1年": 365, "2年": 730, "3年": 1095}
                days = days_map.get(range_label, 0)
                if days:
                    end = datetime.now().strftime("%Y-%m-%d")
                    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                    date_range_val = (start, end)

            fig = build_kline_chart(code, show_ma, bollinger, grid, date_range_val)
            summary = build_stock_summary(code)
            report_df = build_report_table(code)
            summary_chart = build_optimization_summary_chart(code)
            return fig, summary, report_df, summary_chart

        inputs = [
            code_dropdown, show_ma5, show_ma10, show_ma20, show_ma60,
            show_bollinger, show_grid, range_dropdown,
        ]

        for inp in inputs:
            inp.change(
                fn=update_chart,
                inputs=inputs,
                outputs=[chart_output, summary_output, report_table, summary_chart_output],
            )

    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--share", action="store_true", help="生成公网链接，支持外网访问")
    args = parser.parse_args()

    app = create_dashboard()
    app.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=args.share,
    )
