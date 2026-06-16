"""
indicators.py - 向量化技术指标计算模块

提供网格交易选股所需的技术指标计算，全部使用 numpy/numba 向量化加速。

指标列表：
- Hurst 指数 (60日窗口)
- OU 半衰期 (Ornstein-Uhlenbeck 过程)
- ADX 趋向指标 (14日 Wilder 平滑)
- 年化波动率 (60日)
- ATR (20日)
"""

from typing import Tuple
import logging

import numpy as np
import pandas as pd
try:
    from numba import jit, prange
except ImportError:
    # Fallback: create a no-op decorator if numba is not available
    def jit(*args, **kwargs):
        def decorator(func):
            return func
        return decorator
    prange = range

logger = logging.getLogger("grid_trading")


# ==================== Hurst Exponent (60-day window) ====================


@jit(nopython=True, cache=True)
def _rs_analysis_vectorized(prices: np.ndarray, max_lag: int = 20) -> float:
    """
    Numba 加速的 R/S 分析计算 Hurst 指数。

    Algorithm:
    1. 对于每个 lag ∈ [2, max_lag]，将序列分割为多个段落
    2. 每个段落: R = max(累积离差) - min(累积离差), S = std(段落)
    3. R/S = mean(R/S over all segments) for each lag
    4. 回归 log(lag) ~ log(R/S), 斜率 = Hurst 指数

    Parameters:
        prices: 1D 价格数组
        max_lag: 最大滞后阶数 (默认 20)

    Returns:
        Hurst 指数 (float, 范围 [0, 1])
    """
    n = len(prices)
    if n < max_lag * 2:
        return 0.5

    rs_stats = np.zeros(max_lag - 1)
    lag_count = 0

    for lag in range(2, max_lag + 1):
        n_segments = n // lag
        if n_segments < 2:
            continue

        rs_values = np.zeros(n_segments)
        for seg_idx in range(n_segments):
            start = seg_idx * lag
            end = start + lag
            segment = prices[start:end]

            mean_val = np.mean(segment)
            cumdev = np.cumsum(segment - mean_val)
            R = np.max(cumdev) - np.min(cumdev)
            # Numba doesn't support ddof in std, compute manually
            variance = np.sum((segment - mean_val) ** 2) / (len(segment) - 1)
            S = np.sqrt(variance)

            if S > 1e-10:
                rs_values[seg_idx] = R / S

        if np.any(rs_values > 0):
            rs_stats[lag_count] = np.mean(rs_values[rs_values > 0])
            lag_count += 1

    if lag_count < 2:
        return 0.5

    # 线性回归: log(R/S) = intercept + Hurst * log(lag)
    # 直接使用 np.arange 创建浮点数组，避免 astype 调用
    valid_lags = np.arange(2.0, 2.0 + lag_count)
    valid_rs = rs_stats[:lag_count]

    log_lags = np.log(valid_lags)
    log_rs = np.log(valid_rs + 1e-10)

    # OLS 斜率
#     n_pts = len(log_lags)
    mean_x = np.mean(log_lags)
    mean_y = np.mean(log_rs)

    numerator = np.sum((log_lags - mean_x) * (log_rs - mean_y))
    denominator = np.sum((log_lags - mean_x) ** 2) + 1e-10

    hurst = numerator / denominator
    hurst = max(0.0, min(1.0, hurst))  # 限制在 [0, 1]

    return hurst


def calculate_hurst_exponent(price_series: pd.Series, max_lag: int = 20) -> float:
    """
    单次 Hurst 指数计算 — 委托给 Numba JIT 加速的 R/S 分析。

    与 calculate_hurst_60d 的区别：只计算最后一次的 Hurst 值，不滚动。
    """
    prices = price_series.dropna().values.astype(np.float64)
    if len(prices) < max_lag * 2:
        return 0.5
    return _rs_analysis_vectorized(prices, max_lag)


def calculate_hurst_60d(
    df: pd.DataFrame,
    price_col: str = "close",
    window: int = 60,
) -> pd.Series:
    """
    计算 60 日滚动 Hurst 指数用于均值回归检测。

    Interpretation:
    - H < 0.5: 均值回归 (适合网格交易)
    - H = 0.5: 随机游走
    - H > 0.5: 趋势追踪

    Parameters:
        df: 包含价格数据的 DataFrame
        price_col: 价格列名 (默认 'close')
        window: 滚动窗口大小 (默认 60 交易日)

    Returns:
        Hurst 指数 Series，与输入 DataFrame 索引对齐
    """
    prices = df[price_col].values
    n = len(prices)
    result = np.full(n, np.nan)

    min_data_required = window
    max_lag = min(20, window // 3)

    for i in range(min_data_required - 1, n):
        price_window = prices[i - window + 1 : i + 1]
        result[i] = _rs_analysis_vectorized(price_window, max_lag=max_lag)

    return pd.Series(result, index=df.index, name=f"hurst_{window}d")


# ==================== OU Half-Life (Ornstein-Uhlenbeck Process) ====================


@jit(nopython=True, cache=True)
def _ou_half_life_vectorized(log_prices: np.ndarray) -> float:
    """
    从对数价格序列计算 OU 过程半衰期。

    Model: log(P_t) = α + β * log(P_{t-1}) + ε
    其中 β 是均值回归系数

    Half-life formula: τ = ln(2) / (-β) 当 |β| < 1
    若 β >= 0 (无均值回归)，返回 np.inf

    Parameters:
        log_prices: 1D 对数价格数组

    Returns:
        半衰期（交易日天数），若无均值回归返回 np.inf
    """
    n = len(log_prices)
    if n < 20:
        return np.inf

    # OLS 回归: log(P_t) = α + β * log(P_{t-1})
    y = log_prices[1:]  # t
    x = log_prices[:-1]  # t-1

#     n_pts = len(y)
    mean_x = np.mean(x)
    mean_y = np.mean(y)

    numerator = np.sum((x - mean_x) * (y - mean_y))
    denominator = np.sum((x - mean_x) ** 2) + 1e-10

    beta = numerator / denominator

    # 检查平稳性: |β| < 1 for OU process
    if beta >= 0 or beta <= -1:
        return np.inf

    # 半衰期 = ln(2) / (-beta)
    half_life = np.log(2) / (-beta)

    # 过滤微观噪声：半衰期 < 7天视为订单簿噪声，网格高频触发将吞噬手续费
    if half_life < 7.0:
        return np.inf

    # 限制合理范围 (1 天到 500 天)
    if half_life < 1 or half_life > 500:
        return np.inf

    return half_life


def calculate_ou_half_life(
    df: pd.DataFrame,
    price_col: str = "close",
    min_periods: int = 60,
) -> pd.Series:
    """
    计算滚动 OU 半衰期用于均值回归分析。

    Interpretation:
    - τ < 15 天: 快速均值回归 (网格交易首选)
    - τ = 15-60 天: 中等均值回归
    - τ > 60 天: 慢速/弱均值回归
    - τ = ∞: 无均值回归 (趋势)

    Parameters:
        df: 包含价格数据的 DataFrame
        price_col: 价格列名 (默认 'close')
        min_periods: 滚动计算最小周期数 (默认 60)

    Returns:
        半衰期 Series (天数)，与输入 DataFrame 索引对齐
    """
    log_prices = np.log(df[price_col].values + 1e-10)
    n = len(log_prices)
    result = np.full(n, np.inf)

    for i in range(min_periods - 1, n):
        log_window = log_prices[i - min_periods + 1 : i + 1]
        result[i] = _ou_half_life_vectorized(log_window)

    return pd.Series(result, index=df.index, name="ou_half_life")


# ==================== ADX (Average Directional Index) ====================


@jit(nopython=True, cache=True)
def _adx_calculation_vectorized(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Numba 加速的 ADX 计算，使用 Wilder 平滑。

    Parameters:
        high: 最高价数组
        low: 最低价数组
        close: 收盘价数组
        period: ADX 周期 (默认 14)

    Returns:
        (adx, plus_di, minus_di) - ADX, +DI, -DI 数组
    """
    n = len(close)
    if n < period * 2 + 1:
        return (
            np.full(n, np.nan),
            np.full(n, np.nan),
            np.full(n, np.nan),
        )

    # True Range 和 Directional Movement
    tr = np.zeros(n - 1)
    plus_dm = np.zeros(n - 1)
    minus_dm = np.zeros(n - 1)

    for i in range(1, n):
        high_diff = high[i] - high[i - 1]
        low_diff = low[i - 1] - low[i]

        tr[i - 1] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

        if high_diff > low_diff and high_diff > 0:
            plus_dm[i - 1] = high_diff
        if low_diff > high_diff and low_diff > 0:
            minus_dm[i - 1] = low_diff

    # Wilder 平滑 (EMA with alpha = 1/period)
#     alpha = 1.0 / period

    # ATR (Wilder 平滑)
    atr = np.zeros(n - 1)
    atr[0] = np.mean(tr[:period])
    for i in range(1, period):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    for i in range(period, n - 1):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    # +DM 和 -DM (Wilder 平滑)
    plus_dm_smooth = np.zeros(n - 1)
    minus_dm_smooth = np.zeros(n - 1)

    plus_dm_smooth[0] = np.mean(plus_dm[:period])
    minus_dm_smooth[0] = np.mean(minus_dm[:period])

    for i in range(1, period):
        plus_dm_smooth[i] = (plus_dm_smooth[i - 1] * (period - 1) + plus_dm[i]) / period
        minus_dm_smooth[i] = (minus_dm_smooth[i - 1] * (period - 1) + minus_dm[i]) / period

    for i in range(period, n - 1):
        plus_dm_smooth[i] = (plus_dm_smooth[i - 1] * (period - 1) + plus_dm[i]) / period
        minus_dm_smooth[i] = (minus_dm_smooth[i - 1] * (period - 1) + minus_dm[i]) / period

    # +DI 和 -DI
    plus_di = np.zeros(n - 1)
    minus_di = np.zeros(n - 1)

    for i in range(n - 1):
        if atr[i] > 1e-10:
            plus_di[i] = 100 * plus_dm_smooth[i] / atr[i]
            minus_di[i] = 100 * minus_dm_smooth[i] / atr[i]

    # DX
    dx = np.zeros(n - 1)
    for i in range(n - 1):
        di_sum = plus_di[i] + minus_di[i]
        if di_sum > 1e-10:
            dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / di_sum

    # ADX (Wilder 平滑 DX)
    adx = np.full(n, np.nan)
    adx[period] = np.mean(dx[:period])

    for i in range(period + 1, n - 1):
        adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    # Pad DI arrays to length n with NaN at the beginning
    plus_di_padded = np.zeros(n)
    plus_di_padded[1:] = plus_di
    plus_di_padded[0] = np.nan

    minus_di_padded = np.zeros(n)
    minus_di_padded[1:] = minus_di
    minus_di_padded[0] = np.nan

    return adx, plus_di_padded, minus_di_padded


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    计算 ADX (平均趋向指数) 及其组成部分。

    Interpretation:
    - ADX < 20: 趋势弱 (趋势追踪不适合)
    - ADX 20-40: 中等趋势
    - ADX > 40: 强趋势
    - 网格交易偏好低 ADX (< 25) 表示区间振荡

    Parameters:
        df: 包含 high, low, close 列的 DataFrame
        period: ADX 周期 (默认 14，Wilder 推荐)

    Returns:
        包含 adx, plus_di, minus_di 列的 DataFrame
    """
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    adx, plus_di, minus_di = _adx_calculation_vectorized(high, low, close, period)

    result = pd.DataFrame(
        {
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
        },
        index=df.index,
    )

    return result


# ==================== Annualized Volatility (60-day) ====================


def calculate_volatility_60d(
    df: pd.DataFrame,
    price_col: str = "close",
    annualization_factor: float = 252.0,
) -> pd.Series:
    """
    计算 60 日年化波动率。

    Formula: σ_annual = std(log_returns) * sqrt(252)

    Interpretation:
    - 低波动 (< 0.15): 稳定，可能缺乏网格交易机会
    - 中等波动 (0.15-0.40): 适合网格交易
    - 高波动 (> 0.40): 风险大，宽幅震荡

    Parameters:
        df: 包含价格数据的 DataFrame
        price_col: 价格列名 (默认 'close')
        annualization_factor: 年化因子 (A 股默认 252 交易日)

    Returns:
        年化波动率 Series
    """
    log_returns = np.log(df[price_col] / df[price_col].shift(1))
    rolling_std = log_returns.rolling(window=60, min_periods=20).std()
    annualized_vol = rolling_std * np.sqrt(annualization_factor)

    return annualized_vol.rename("volatility_60d")


# ==================== ATR (Average True Range) ====================


@jit(nopython=True, cache=True)
def _atr_calculation_vectorized(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 20
) -> np.ndarray:
    """
    Numba 加速的 ATR 计算。

    Formula:
        TR = max(H-L, |H-PCP|, |L-PCP|)
        ATR = Wilder_Smooth(TR, period)

    Parameters:
        high: 最高价数组
        low: 最低价数组
        close: 收盘价数组
        period: ATR 周期 (默认 20)

    Returns:
        ATR 数组
    """
    n = len(close)
    tr = np.zeros(n - 1)

    for i in range(1, n):
        tr[i - 1] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    # Wilder 平滑 ATR
    atr = np.zeros(n)
    if n > period:
        atr[period] = np.mean(tr[:period])
        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i - 1]) / period

    return atr


def calculate_atr(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """
    计算平均真实范围 (ATR)。

    Parameters:
        df: 包含 high, low, close 列的 DataFrame
        period: ATR 周期 (默认 20)

    Returns:
        ATR Series
    """
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    atr = _atr_calculation_vectorized(high, low, close, period)

    return pd.Series(atr, index=df.index, name=f"atr_{period}")


# ==================== Complete Indicator Set ====================


def calculate_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    为单只股票计算完整的多因子指标集。

    Parameters:
        df: 包含 OHLCV 数据 (open, high, low, close, volume, amount) 的 DataFrame

    Returns:
        包含所有指标列的 DataFrame

    指标列:
        - hurst_60d: 60日 Hurst 指数
        - ou_half_life: OU 过程半衰期 (天)
        - adx: 平均趋向指数 (14日)
        - plus_di: +DI 指标
        - minus_di: -DI 指标
        - volatility_60d: 年化波动率 (60日)
        - atr_20: 平均真实范围 (20日)
        - path_memory: 方差比因子 (Variance Ratio)
    """
    result = df.copy()

    # 检查必要列
    required_cols = ["high", "low", "close"]
    missing = [c for c in required_cols if c not in result.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    # Hurst 60日
    if "close" in result.columns and len(result) >= 60:
        result["hurst_60d"] = calculate_hurst_60d(result)

    # OU 半衰期
    if "close" in result.columns and len(result) >= 60:
        result["ou_half_life"] = calculate_ou_half_life(result)

    # ADX 组成部分
    if all(c in result.columns for c in ["high", "low", "close"]) and len(result) >= 30:
        adx_df = calculate_adx(result, period=14)
        result["adx"] = adx_df["adx"]
        result["plus_di"] = adx_df["plus_di"]
        result["minus_di"] = adx_df["minus_di"]

    # 波动率 60日
    if "close" in result.columns and len(result) >= 60:
        result["volatility_60d"] = calculate_volatility_60d(result)

    # ATR 20日
    if all(c in result.columns for c in ["high", "low", "close"]) and len(result) >= 20:
        result["atr_20"] = calculate_atr(result, period=20)

    # Path Memory (Variance Ratio)
    if "close" in result.columns and len(result) >= 120:
        result["path_memory"] = calculate_variance_ratio(result, q=20, min_periods=120)

    return result


def get_latest_indicators(df: pd.DataFrame) -> dict:
    """
    获取最新一只股票的指标值（最后一个有效值）。

    注意：对于有时间延迟的指标（如 ADX），最后一个观测值可能为 NaN，
    因为计算需要 future 数据。本函数回溯找到最后一个有效值。

    Parameters:
        df: 包含所有指标列的 DataFrame

    Returns:
        包含最新有效指标值的字典
    """
    if df.empty:
        return {}

    indicators = {}
    for col in ["hurst_60d", "ou_half_life", "adx", "plus_di", "minus_di", "volatility_60d", "atr_20", "path_memory"]:
        if col not in df.columns:
            continue

        # 回溯找到最后一个非 NaN 值
        series = df[col]
        valid_mask = series.notna()

        if valid_mask.any():
            # 找到最后一个有效值的索引
            last_valid_idx = valid_mask.idxmax()  # 找到最后一个 True 的位置
            # 但这不对 - idxmax 返回第一个最大值的索引
            # 需要用 last_valid_idx = series[valid_mask].index[-1]

            # 正确做法：找到最后一个有效值的索引
            valid_indices = series[valid_mask].index
            if len(valid_indices) > 0:
                last_valid_idx = valid_indices[-1]
                val = series.loc[last_valid_idx]
                indicators[col] = float(val) if pd.notna(val) else None
        else:
            indicators[col] = None

    return indicators


# ==================== Path Memory (Variance Ratio) ====================


def calculate_variance_ratio(
    df: pd.DataFrame,
    price_col: str = "close",
    q: int = 20,
    min_periods: int = 120,
) -> pd.Series:
    """
    计算方差比 (Variance Ratio) 作为 Path_Memory 因子。

    Variance Ratio 检验序列是趋势持续还是反持续：
    - VR > 1: 趋势记忆 (长期持有有利，网格易破网)
    - VR < 1: 反持续记忆 (均值回归，网格有效)
    - VR ≈ 1: 随机游走

    Formula:
        VR(q) = Var(μ_q) * q / Var(μ_1)
    其中 μ_q 是 q 期滚动均值

    A股应用：
    - VR < 1 表示反持续性，均值回归特性强，适合网格交易
    - 转换为得分：VR 越远离 1（越小）得分越高

    Parameters:
        df: 包含价格数据的 DataFrame
        price_col: 价格列名 (默认 'close')
        q: 滚动窗口期数 (默认 20，约1个月)
        min_periods: 最小样本数 (默认 120，约半年)

    Returns:
        VR 得分 Series (0~1)，VR=1 时得 0.5，VR 越远离 1 得分越高
    """
    log_returns = np.log(df[price_col] / df[price_col].shift(1)).dropna()
    n = len(log_returns)

    if n < min_periods:
        return pd.Series(np.nan, index=df.index, name="path_memory")

    # 计算 Var(μ_1) - 单期收益率方差
    var_1 = log_returns.var()

    if var_1 < 1e-10:
        return pd.Series(0.5, index=df.index[-n:], name="path_memory")

    # 计算 Var(μ_q) * q
    rolling_mean = log_returns.rolling(window=q).mean()
    var_q = rolling_mean.var() * q

    # 方差比
    vr = var_q / var_1

    # 限制 VR 在合理范围
    vr = max(0.1, min(10.0, vr))

    # 转换为 0~1 得分：VR=1 时得 0.5，VR 越小（反持续）得分越高
    # 使用 1 - |VR - 1| / (VR + 1) 映射
    # 当 VR=1 → 0.5，当 VR→0 → 1，当 VR→∞ → 0
    score = 1 - abs(vr - 1) / (vr + 1)

    # 扩展到完整索引
    result = pd.Series(np.nan, index=df.index, name="path_memory")
    result.iloc[-n:] = score

    return result


def calculate_path_memory(
    df: pd.DataFrame,
    price_col: str = "close",
    q: int = 20,
    min_periods: int = 120,
) -> pd.Series:
    """
    计算 Path_Memory 因子（Variance Ratio 的逆向得分）。

    此函数是 calculate_variance_ratio 的别名，保持命名一致性。

    Parameters:
        df: 包含价格数据的 DataFrame
        price_col: 价格列名 (默认 'close')
        q: 滚动窗口期数 (默认 20)
        min_periods: 最小样本数 (默认 120)

    Returns:
        Path_Memory 得分 Series (0~1)
    """
    return calculate_variance_ratio(df, price_col, q, min_periods)
