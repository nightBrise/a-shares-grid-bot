"""
screener.py - 多因子横截面打分选股器

替代绝对 Hurst < 0.5 阈值筛选，使用加权多因子模型进行横截面打分排序。

打分因子：
- OU 半衰期 (35%): 优先快速均值回归
- Hurst 指数 (30%): 趋势性指标
- ADX (20%): 趋势强度
- 波动率适配 (15%): 中等波动最好
"""

from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("grid_trading")


# ==================== Default Configuration ====================

DEFAULT_FACTOR_WEIGHTS = {
    "ou_half_life": -0.35,  # 负: 偏好低值 (快速回归)
    "hurst": -0.30,  # 负: 偏好低值 (均值回归)
    "adx": -0.20,  # 负: 偏好低值 (趋势弱)
    "volatility_fit": 0.15,  # 正: 偏好中等值
}

# 因子边界用于归一化 (横截面百分位)
FACTOR_BOUNDS = {
    "ou_half_life": (10, 60),  # 10-60 天范围
    "hurst": (0.35, 0.55),  # 0.35-0.55 范围
    "adx": (15, 30),  # 15-30 范围
    "volatility": (0.15, 0.35),  # 15%-35% 年化波动率
}


# ==================== Data Classes ====================


@dataclass
class FactorScores:
    """单个股票的因子得分容器。"""

    ou_half_life_score: float
    hurst_score: float
    adx_score: float
    volatility_fit_score: float
    total_score: float
    percentile_rank: float


@dataclass
class StockFactors:
    """单只股票原始因子值容器。"""

    code: str
    hurst_60d: float
    ou_half_life: float
    adx: float
    volatility_60d: float
    avg_turnover: float  # 万元
    price: float
    is_st: bool = False


# ==================== Multi-Factor Screener ====================


class MultiFactorScreener:
    """
    多因子横截面选股器，用于网格交易标的筛选。

    Scoring Model:
    1. 初筛: 流动性、价格、ST 检查
    2. 因子计算: 计算原始因子值
    3. 横截面归一化: 在全市场中的百分位排名
    4. 加权打分: 按配置权重组合因子

    Weights (可配置):
        - OU 半衰期: -35% (负 = 偏好低值)
        - Hurst: -30% (负 = 偏好低值)
        - ADX: -20% (负 = 偏好低值)
        - 波动率适配: +15% (正 = 偏好中等值)

    Output: 按排名排序的 Top N 股票列表，含因子分解
    """

    def __init__(self, config: Optional[dict] = None):
        """
        初始化选股器。

        Parameters:
            config: 配置字典，包含 factor_weights 和 initial_filters
        """
        self.config = config or {}

        # 打分权重
        self.weights = self.config.get("factor_weights", DEFAULT_FACTOR_WEIGHTS)

        # 初筛阈值
        self.min_turnover = self.config.get("min_turnover", 10000)  # 万元 (1亿)
        self.min_price = self.config.get("min_price", 5.0)
        self.max_price = self.config.get("max_price", 500.0)
        self.max_stocks = self.config.get("max_stocks", 50)

        # 验证权重和
        total_weight = sum(abs(v) for v in self.weights.values())
        if abs(total_weight - 1.0) > 0.01:
            raise ValueError(f"因子权重必须绝对值和为 1.0，当前为 {total_weight}")

        logger.info(
            f"MultiFactorScreener 初始化: weights={self.weights}, "
            f"min_turnover={self.min_turnover}万"
        )

    def initial_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        应用初筛条件：流动性、价格、ST 过滤。

        Filters:
        1. 成交额 >= 1亿 (avg_turnover 单位: 万元)
        2. 价格 [5, 500]
        3. 非 ST (is_st == False 或 is_st 列不存在)

        Parameters:
            df: 包含 code, price, avg_turnover, is_st 列的 DataFrame

        Returns:
            过滤后的 DataFrame
        """
        if df.empty:
            return df

        n_before = len(df)

        # 成交额过滤: >= 1亿 (10000万)
        if "avg_turnover" in df.columns:
            df = df[df["avg_turnover"] >= self.min_turnover].copy()
        else:
            logger.warning("avg_turnover 列不存在，跳过流动性过滤")

        # 价格过滤
        if "price" in df.columns:
            df = df[
                (df["price"] >= self.min_price) & (df["price"] <= self.max_price)
            ].copy()

        # ST 过滤 (如果 is_st 列存在)
        if "is_st" in df.columns:
            df = df[df["is_st"] == False].copy()

        n_after = len(df)
        logger.info(
            f"初筛: {n_before} -> {n_after} 股票 "
            f"(移除 {n_before - n_after} 只)"
        )

        return df

    def _rescale(
        self, series: pd.Series, low: float, high: float
    ) -> pd.Series:
        """
        将 series 归一化到 [0, 1] 基于 low-high 边界。

        边界外的值被限制到 0 或 1。
        """
        return ((series - low) / (high - low)).clip(0, 1)

    def calculate_factor_scores(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算横截面归一化因子得分。

        Process:
        1. 对每个因子，计算在全市场的百分位排名
        2. 基于配置边界重新缩放到 [0, 1]
        3. 应用权重并求和得到总分

        Parameters:
            df: 包含因子列的 DataFrame: hurst_60d, ou_half_life, adx, volatility_60d

        Returns:
            添加了得分列的 DataFrame
        """
        if df.empty:
            return df

        result = df.copy()

        # 处理 ou_half_life 中的无穷大 (替换为最大值的两倍)
        if "ou_half_life" in result.columns:
            finite_vals = result["ou_half_life"][np.isfinite(result["ou_half_life"])]
            if len(finite_vals) > 0:
                max_hl = finite_vals.max()
                result["ou_half_life"] = result["ou_half_life"].replace(np.inf, max_hl * 2)
            else:
                result["ou_half_life"] = 100.0  # 默认值

        # 百分位排名 (越高对负向因子越有利)
        if "ou_half_life" in result.columns:
            result["ou_hl_pct"] = result["ou_half_life"].rank(pct=True)
        if "hurst_60d" in result.columns:
            result["hurst_pct"] = result["hurst_60d"].rank(pct=True)
        if "adx" in result.columns:
            result["adx_pct"] = result["adx"].rank(pct=True)

        # 波动率: 偏好中等 (离中位数越近越好)
        if "volatility_60d" in result.columns:
            vol_median = result["volatility_60d"].median()
            result["vol_distance"] = abs(result["volatility_60d"] - vol_median)
            result["vol_fit_pct"] = 1 - result["vol_distance"].rank(pct=True)

        # 基于边界的归一化得分
        # 对于负向因子: 低值 = 高分

        # OU 半衰期得分
        if "ou_half_life" in result.columns:
            result["ou_score"] = 1 - self._rescale(
                result["ou_half_life"],
                FACTOR_BOUNDS["ou_half_life"][0],
                FACTOR_BOUNDS["ou_half_life"][1],
            )

        # Hurst 得分
        if "hurst_60d" in result.columns:
            result["hurst_score"] = 1 - self._rescale(
                result["hurst_60d"],
                FACTOR_BOUNDS["hurst"][0],
                FACTOR_BOUNDS["hurst"][1],
            )

        # ADX 得分
        if "adx" in result.columns:
            result["adx_score"] = 1 - self._rescale(
                result["adx"],
                FACTOR_BOUNDS["adx"][0],
                FACTOR_BOUNDS["adx"][1],
            )

        # 波动率适配得分 (中等最好)
        if "volatility_60d" in result.columns:
            result["vol_score"] = self._rescale(
                result["volatility_60d"],
                FACTOR_BOUNDS["volatility"][0],
                FACTOR_BOUNDS["volatility"][1],
            )

        # 加权总分
        score_components = []
        weight_components = []

        if "ou_score" in result.columns and "ou_half_life" in self.weights:
            score_components.append(self.weights["ou_half_life"] * result["ou_score"])
            weight_components.append(abs(self.weights["ou_half_life"]))

        if "hurst_score" in result.columns and "hurst" in self.weights:
            score_components.append(self.weights["hurst"] * result["hurst_score"])
            weight_components.append(abs(self.weights["hurst"]))

        if "adx_score" in result.columns and "adx" in self.weights:
            score_components.append(self.weights["adx"] * result["adx_score"])
            weight_components.append(abs(self.weights["adx"]))

        if "vol_score" in result.columns and "volatility_fit" in self.weights:
            score_components.append(
                self.weights["volatility_fit"] * result["vol_score"]
            )
            weight_components.append(abs(self.weights["volatility_fit"]))

        if score_components:
            total_weight_used = sum(weight_components)
            if total_weight_used > 0:
                result["total_score"] = sum(score_components) / total_weight_used
            else:
                result["total_score"] = 0.5
        else:
            result["total_score"] = 0.5

        # 总体百分位排名
        result["score_percentile"] = result["total_score"].rank(pct=True) * 100

        return result

    def rank_stocks(self, df: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
        """
        按总分排名并返回 Top N。

        Parameters:
            df: 包含 total_score 列的 DataFrame
            top_n: 返回的 Top N 股票数

        Returns:
            按 total_score 降序排列、限制为 top_n 的 DataFrame
        """
        if df.empty:
            return df

        df = df.sort_values("total_score", ascending=False)
        df["rank"] = range(1, len(df) + 1)

        # 添加得分分解列
        score_cols = ["ou_score", "hurst_score", "adx_score", "vol_score"]
        for col in score_cols:
            if col not in df.columns:
                df[col] = np.nan

        # 返回 Top N
        return df.head(top_n)

    def screen(self, stocks_data: List[Dict], top_n: int = 20) -> pd.DataFrame:
        """
        主筛选函数：应用过滤器、打分、排名。

        Parameters:
            stocks_data: 股票因子字典列表，来自 indicators 模块
            top_n: 返回的 Top N 股票数

        Returns:
            包含得分和排名的 Top N DataFrame，按 rank 排序

        Example Input:
            stocks_data = [
                {
                    'code': '600519.SH',
                    'hurst_60d': 0.42,
                    'ou_half_life': 12.5,
                    'adx': 18.3,
                    'volatility_60d': 0.22,
                    'avg_turnover': 50000,
                    'price': 1800.0,
                    'is_st': False
                },
                ...
            ]
        """
        # 转换为 DataFrame
        df = pd.DataFrame(stocks_data)

        if df.empty:
            logger.warning("筛选数据为空")
            return pd.DataFrame()

        # Stage 1: 初筛
        df = self.initial_filter(df)

        if df.empty:
            logger.warning("初筛后无股票")
            return pd.DataFrame()

        # Stage 2: 因子打分
        df = self.calculate_factor_scores(df)

        # Stage 3: 排名并选择 Top N
        df = self.rank_stocks(df, top_n=top_n)

        logger.info(
            f"筛选完成: {len(df)}/{top_n} 只股票被选中"
        )

        return df

    def get_factor_contributions(
        self, df: pd.DataFrame, code: str
    ) -> Optional[Dict]:
        """
        获取单只股票的因子贡献分解。

        Parameters:
            df: 包含得分列的 DataFrame
            code: 股票代码

        Returns:
            因子贡献字典，如果股票不在结果中返回 None
        """
        stock_row = df[df["code"] == code]
        if stock_row.empty:
            return None

        row = stock_row.iloc[0]

        return {
            "code": code,
            "ou_half_life_contrib": row.get("ou_score", np.nan) * self.weights.get("ou_half_life", 0),
            "hurst_contrib": row.get("hurst_score", np.nan) * self.weights.get("hurst", 0),
            "adx_contrib": row.get("adx_score", np.nan) * self.weights.get("adx", 0),
            "volatility_contrib": row.get("vol_score", np.nan) * self.weights.get("volatility_fit", 0),
            "total_score": row.get("total_score", np.nan),
            "rank": row.get("rank", np.nan),
        }


# ==================== Batch Screening for Universe ====================


def screen_universe(
    df_universe: pd.DataFrame,
    config: dict,
    top_n: int = 20,
) -> pd.DataFrame:
    """
    使用多因子模型对全市场股票进行筛选。

    此函数设计为从 strategy.py 的选股逻辑调用。

    Parameters:
        df_universe: 包含所有股票因子数据的 DataFrame
        config: 配置字典
        top_n: 返回的 Top N 股票数

    Returns:
        包含得分和排名的 Top N DataFrame
    """
    logger.info(f"使用多因子模型筛选 {len(df_universe)} 只股票...")

    # 初始化选股器
    screening_config = config.get("screening", {})
    screener = MultiFactorScreener(screening_config)

    # 准备股票数据
    stocks_data = []
    for _, row in df_universe.iterrows():
        stock_dict = {
            "code": row.get("code", ""),
            "hurst_60d": row.get("hurst_60d", 0.5),
            "ou_half_life": row.get("ou_half_life", np.inf),
            "adx": row.get("adx", 50.0),
            "volatility_60d": row.get("volatility_60d", 0.3),
            "avg_turnover": row.get("avg_turnover", 0),
            "price": row.get("price", 0),
            "is_st": row.get("is_st", False),
        }
        stocks_data.append(stock_dict)

    # 筛选
    result = screener.screen(stocks_data, top_n=top_n)

    logger.info(f"筛选完成: {len(result)} 只股票被选中")

    return result


def create_screening_report(df_result: pd.DataFrame) -> str:
    """
    创建筛选报告摘要。

    Parameters:
        df_result: screen() 输出的 DataFrame

    Returns:
        格式化的报告字符串
    """
    if df_result.empty:
        return "无股票符合筛选条件"

    lines = []
    lines.append("=" * 60)
    lines.append("多因子筛选报告")
    lines.append("=" * 60)
    lines.append(f"选中股票数: {len(df_result)}")
    lines.append("")

    for _, row in df_result.head(10).iterrows():
        lines.append(
            f"#{int(row['rank']):2d} {row['code']:<12s} "
            f"总分: {row['total_score']:.4f} "
            f"(Hurst:{row.get('hurst_score', 0):.2f} "
            f"OU:{row.get('ou_score', 0):.2f} "
            f"ADX:{row.get('adx_score', 0):.2f} "
            f"Vol:{row.get('vol_score', 0):.2f})"
        )

    if len(df_result) > 10:
        lines.append(f"... 还有 {len(df_result) - 10} 只股票")

    lines.append("=" * 60)

    return "\n".join(lines)


# ==================== Advanced Multi-Factor Screener (v2) ====================


class AdvancedMultiFactorScreener:
    """
    高级多因子横截面选股器 v2（机构级）

    四因子正交化设计：
    - F1: Reversion_Speed (OU半衰期) - 均值回归速度
    - F2: Trend_Strength (ADX) - 趋势持续性
    - F3: Vol_Quality (波动率倒U型) - 波动质量
    - F4: Path_Memory (Variance Ratio) - 残差分形结构

    特性：
    - 横截面多元正交化（F4 对 F1、F2 回归取残差）
    - 双轨权重（ETF vs 股票）
    - 动态阈值（adaptive_quantile 模式）
    - 现金缓冲机制
    """

    # 默认双轨权重
    DEFAULT_ETF_WEIGHTS = {
        "F1": 0.35,  # Reversion_Speed
        "F2": 0.15,  # Trend_Strength
        "F3": 0.30,  # Vol_Quality
        "F4": 0.20,  # Path_Memory
    }

    DEFAULT_STOCK_WEIGHTS = {
        "F1": 0.25,  # Reversion_Speed
        "F2": 0.35,  # Trend_Strength
        "F3": 0.20,  # Vol_Quality
        "F4": 0.20,  # Path_Memory
    }

    def __init__(self, config: Optional[dict] = None):
        """
        初始化高级选股器。

        Parameters:
            config: 配置字典，包含 advanced_screening 配置
        """
        self.config = config or {}
        adv_cfg = self.config.get("advanced_screening", {})

        # 权重配置
        weights_cfg = adv_cfg.get("weights", {})
        self.etf_weights = weights_cfg.get("etf", self.DEFAULT_ETF_WEIGHTS)
        self.stock_weights = weights_cfg.get("stock", self.DEFAULT_STOCK_WEIGHTS)

        # 阈值配置
        self.quality_threshold = adv_cfg.get("quality_threshold", 0.65)
        self.threshold_mode = adv_cfg.get("threshold_mode", "adaptive_quantile")
        self.adaptive_quantile = adv_cfg.get("adaptive_quantile", 0.75)
        self.cash_buffer_ratio = adv_cfg.get("cash_buffer_ratio", 0.50)

        # 波动质量参数
        vol_cfg = adv_cfg.get("vol_quality", {})
        self.vol_optimal = vol_cfg.get("optimal_vol", 0.25)
        self.vol_tolerance = vol_cfg.get("tolerance", 0.15)

        # Path Memory 参数
        pm_cfg = adv_cfg.get("path_memory", {})
        # q=5 对齐网格触发频率(3-10日)，捕捉短期反转特性
        self.vr_q = pm_cfg.get("variance_ratio_q", 5)
        self.vr_min_periods = pm_cfg.get("min_periods", 120)

        # 网格参数映射
        self.grid_params = adv_cfg.get("grid_params", {
            "high_score": {"spacing_coef": 1.8, "position_pct": 0.025},
            "medium_score": {"spacing_coef": 2.2, "position_pct": 0.018},
            "low_score": {"spacing_coef": None, "position_pct": 0},
        })

        logger.info(
            f"AdvancedMultiFactorScreener 初始化: "
            f"threshold={self.quality_threshold}({self.threshold_mode}), "
            f"ETF_w={self.etf_weights}, Stock_w={self.stock_weights}"
        )

    def _vol_quality(self, volatility: float) -> float:
        """
        计算波动质量得分（倒U型函数）。

        波动率在 optimal_vol 附近得最高分，越远得分越低。
        """
        if pd.isna(volatility):
            return 0.0
        score = 1 - abs(volatility - self.vol_optimal) / self.vol_tolerance
        return max(0.0, min(1.0, score))

    def _percentile_normalize(self, series: pd.Series, inverse: bool = True) -> pd.Series:
        """
        横截面百分位归一化。

        Parameters:
            series: 原始因子值
            inverse: True 表示负向因子（低值高分）

        Returns:
            归一化到 [0, 1] 的得分
        """
        if len(series) < 2:
            return pd.Series(0.5, index=series.index)

        # 保存原始索引
        original_index = series.index

        # 处理 NaN：先填充为中位数（保持排名结构）
        nan_mask = series.isna()
        if nan_mask.any():
            # 用非 NaN 值的中位数填充 NaN
            median_val = series[~nan_mask].median()
            series = series.fillna(median_val)

        pct_rank = series.rank(pct=True)
        if inverse:
            result = 1 - pct_rank
        else:
            result = pct_rank

        # 恢复原始 NaN 位置为 NaN（不做填充，让它们保持 NaN）
        result[nan_mask] = np.nan

        return result

    def _orthogonalize_cross_section(self, df: pd.DataFrame) -> pd.Series:
        """
        横截面多元正交化：F4 ⊥ {F1, F2}

        使用多元线性回归剥离 F1、F2 对 F4 的联合影响，
        取残差作为最终的 F4 得分。
        """
        if len(df) < 10:
            return df['F4_norm']

        X = df[['F1_norm', 'F2_norm']].values
        y = df['F4_norm'].values

        # 添加常数项
        X_b = np.column_stack([np.ones(len(X)), X])

        # 多元线性回归（使用最小二乘法）
        beta, _, _, _ = np.linalg.lstsq(X_b, y, rcond=None)

        # 计算残差
        resid = y - X_b @ beta

        # 安全重缩放至 [0, 1]
        ptp = np.ptp(resid)
        if ptp > 1e-6:
            result = (resid - resid.min()) / ptp
        else:
            result = np.full_like(resid, 0.5)

        return pd.Series(result, index=df.index)

    def _get_weights(self, asset_type: str) -> dict:
        """获取资产类型的权重配置。"""
        if asset_type == "etf":
            return self.etf_weights
        return self.stock_weights

    def _get_threshold(self, scores: pd.Series) -> float:
        """
        计算动态阈值。

        Parameters:
            scores: 评分 Series

        Returns:
            阈值
        """
        if self.threshold_mode == "fixed":
            return self.quality_threshold

        # adaptive_quantile 模式：至少取 Top (1-adaptive_quantile)
        dynamic_threshold = scores.quantile(self.adaptive_quantile)
        cap = self.config.get("threshold_soft_cap", 0.82)  # 从配置读取软上限
        return np.clip(max(self.quality_threshold, dynamic_threshold), self.quality_threshold, cap)

    def get_grid_params(self, score: float) -> dict:
        """
        根据评分返回网格参数。

        Parameters:
            score: 综合评分

        Returns:
            包含 spacing_coef 和 position_pct 的字典
        """
        if score >= 0.80:
            return self.grid_params.get("high_score", {
                "spacing_coef": 1.8, "position_pct": 0.025
            })
        elif score >= 0.65:
            return self.grid_params.get("medium_score", {
                "spacing_coef": 2.2, "position_pct": 0.018
            })
        else:
            return self.grid_params.get("low_score", {
                "spacing_coef": None, "position_pct": 0
            })

    def initial_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        应用初筛条件：流动性、价格、ST 过滤。
        """
        if df.empty:
            return df

        n_before = len(df)

        # 成交额过滤: >= 1亿 (10000万)
        if "avg_turnover" in df.columns:
            df = df[df["avg_turnover"] >= 10000].copy()
        else:
            logger.warning("avg_turnover 列不存在，跳过流动性过滤")

        # 价格过滤
        if "price" in df.columns:
            df = df[
                (df["price"] >= 5.0) & (df["price"] <= 500.0)
            ].copy()

        # ST 过滤
        if "is_st" in df.columns:
            df = df[df["is_st"] == False].copy()

        logger.info(f"初筛: {n_before} -> {len(df)} 股票 (移除 {n_before - len(df)} 只)")

        return df

    def calculate_scores(self, df: pd.DataFrame, asset_type: str = "stock") -> pd.DataFrame:
        """
        计算四因子横截面得分。

        Parameters:
            df: 包含因子列的 DataFrame
            asset_type: 'etf' 或 'stock'

        Returns:
            添加了得分列的 DataFrame
        """
        if df.empty:
            return df

        result = df.copy()

        # Step 1: 百分位归一化（F1, F2 是负向因子）
        if "ou_half_life" in result.columns:
            finite_vals = result["ou_half_life"][np.isfinite(result["ou_half_life"])]
            if len(finite_vals) > 0:
                max_hl = finite_vals.max()
                result["ou_half_life"] = result["ou_half_life"].replace(np.inf, max_hl * 2)
            else:
                result["ou_half_life"] = 100.0
            result["F1_norm"] = self._percentile_normalize(result["ou_half_life"], inverse=True)

        if "adx" in result.columns:
            result["F2_norm"] = self._percentile_normalize(result["adx"], inverse=True)

        # Step 2: F3 波动质量（倒U型）
        if "volatility_60d" in result.columns:
            result["F3_norm"] = result["volatility_60d"].apply(self._vol_quality)

        # Step 3: F4 Path_Memory（负向，越小越好）
        if "path_memory" in result.columns:
            result["F4_norm"] = self._percentile_normalize(result["path_memory"], inverse=True)

        # Step 4: F4 正交化（剥离 F1、F2 影响）
        if "F1_norm" in result.columns and "F2_norm" in result.columns and "F4_norm" in result.columns:
            valid_mask = result[["F1_norm", "F2_norm", "F4_norm"]].notna().all(axis=1)
            if valid_mask.sum() >= 10:
                valid_df = result.loc[valid_mask].copy()
                result.loc[valid_mask, "F4_ortho"] = self._orthogonalize_cross_section(valid_df)
            else:
                result["F4_ortho"] = result.get("F4_norm", 0.5)

        if "F4_ortho" not in result.columns:
            result["F4_ortho"] = result.get("F4_norm", 0.5)

        # Step 5: 加权总分
        weights = self._get_weights(asset_type)

        f1_score = result.get("F1_norm", 0.5)
        f2_score = result.get("F2_norm", 0.5)
        f3_score = result.get("F3_norm", 0.5)
        f4_score = result.get("F4_ortho", 0.5)

        total_score = (
            weights["F1"] * f1_score +
            weights["F2"] * f2_score +
            weights["F3"] * f3_score +
            weights["F4"] * f4_score
        )

        result["total_score"] = total_score
        result["score_percentile"] = result["total_score"].rank(pct=True) * 100

        # Step 6: 质量阈值判断
        threshold = self._get_threshold(result["total_score"])
        result["passes_threshold"] = result["total_score"] >= threshold

        # 现金缓冲标记
        result["cash_buffer"] = ~result["passes_threshold"]

        # 添加网格参数
        result["grid_params"] = result["total_score"].apply(self.get_grid_params)

        return result

    def _apply_concentration_limits(self, df: pd.DataFrame, max_per_industry: int = 3) -> pd.DataFrame:
        """
        行业分散约束（ETF/股票隔离）。

        ETF 无申万行业分类，需隔离处理。仅对股票执行行业限制。

        Parameters:
            df: 包含 asset_type, industry, total_score 列的 DataFrame
            max_per_industry: 单一行业最多 N 只股票

        Returns:
            应用分散约束后的 DataFrame
        """
        if df.empty:
            return df

        # 分离资产类型：ETF 无申万行业分类，需隔离处理
        df_stock = df[df.get('asset_type', 'stock') == 'stock'].copy()
        df_etf = df[df.get('asset_type', 'stock') == 'etf'].copy()

        # 仅对股票执行行业限制
        selected_stocks = []
        if not df_stock.empty and 'industry' in df_stock.columns:
            df_stock['industry'] = df_stock['industry'].fillna('UNKNOWN')
            industry_cnt = pd.Series(dtype=int)

            for _, row in df_stock.sort_values('total_score', ascending=False).iterrows():
                ind = row['industry']
                if industry_cnt.get(ind, 0) < max_per_industry:
                    selected_stocks.append(row)
                    industry_cnt[ind] = industry_cnt.get(ind, 0) + 1

            df_stock = pd.DataFrame(selected_stocks) if selected_stocks else pd.DataFrame()

        return pd.concat([df_stock, df_etf], ignore_index=True)

    def rank_stocks(self, df: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
        """
        按总分排名并返回 Top N。
        """
        if df.empty:
            return df

        df = df.sort_values("total_score", ascending=False)
        df["rank"] = range(1, len(df) + 1)

        # 应用行业分散约束（仅当 industry 列存在时）
        if 'industry' in df.columns:
            max_per_industry = self.config.get("max_per_industry", 3)
            df = self._apply_concentration_limits(df, max_per_industry=max_per_industry)
            df = df.head(top_n)  # 约束后再次截取 top_n
            df["rank"] = range(1, len(df) + 1)

        return df.head(top_n)

    def screen(
        self,
        stocks_data: List[Dict],
        asset_type: str = "stock",
        top_n: int = 20,
    ) -> pd.DataFrame:
        """
        主筛选函数。

        Parameters:
            stocks_data: 股票因子字典列表
            asset_type: 'etf' 或 'stock'
            top_n: 返回的 Top N 股票数

        Returns:
            包含得分和排名的 Top N DataFrame
        """
        # 转换为 DataFrame
        df = pd.DataFrame(stocks_data)

        if df.empty:
            logger.warning("筛选数据为空")
            return pd.DataFrame()

        # Stage 1: 初筛
        df = self.initial_filter(df)

        if df.empty:
            logger.warning("初筛后无股票")
            return pd.DataFrame()

        # Stage 2: 因子打分
        df = self.calculate_scores(df, asset_type=asset_type)

        # Stage 3: 排名并选择 Top N
        df = self.rank_stocks(df, top_n=top_n)

        # 统计
        n_passed = df["passes_threshold"].sum() if "passes_threshold" in df.columns else len(df)
        logger.info(
            f"筛选完成: {len(df)}/{top_n} 只股票被选中, "
            f"{n_passed} 只通过质量阈值"
        )

        return df

    def get_factor_contributions(self, df: pd.DataFrame, code: str) -> Optional[Dict]:
        """
        获取单只股票的因子贡献分解。
        """
        stock_row = df[df["code"] == code]
        if stock_row.empty:
            return None

        row = stock_row.iloc[0]
        weights = self._get_weights(row.get("asset_type", "stock"))

        return {
            "code": code,
            "F1_reversion_contrib": row.get("F1_norm", 0.5) * weights["F1"],
            "F2_trend_contrib": row.get("F2_norm", 0.5) * weights["F2"],
            "F3_vol_contrib": row.get("F3_norm", 0.5) * weights["F3"],
            "F4_memory_contrib": row.get("F4_ortho", 0.5) * weights["F4"],
            "total_score": row.get("total_score", 0),
            "rank": row.get("rank", 0),
            "passes_threshold": row.get("passes_threshold", False),
            "grid_params": row.get("grid_params", {}),
        }


def screen_universe_advanced(
    df_universe: pd.DataFrame,
    config: dict,
    asset_type: str = "stock",
    top_n: int = 20,
) -> pd.DataFrame:
    """
    使用高级多因子模型对全市场进行筛选。

    Parameters:
        df_universe: 包含所有股票因子数据的 DataFrame
        config: 配置字典
        asset_type: 'etf' 或 'stock'
        top_n: 返回的 Top N 股票数

    Returns:
        包含得分和排名的 Top N DataFrame
    """
    logger.info(
        f"使用高级多因子模型筛选 {len(df_universe)} 只股票 "
        f"(asset_type={asset_type})..."
    )

    # 初始化选股器
    screener = AdvancedMultiFactorScreener(config)

    # 准备股票数据
    stocks_data = []
    for _, row in df_universe.iterrows():
        stock_dict = {
            "code": row.get("code", ""),
            "ou_half_life": row.get("ou_half_life", np.inf),
            "adx": row.get("adx", 50.0),
            "volatility_60d": row.get("volatility_60d", 0.3),
            "path_memory": row.get("path_memory", 0.5),
            "avg_turnover": row.get("avg_turnover", 0),
            "price": row.get("price", 0),
            "is_st": row.get("is_st", False),
        }
        stocks_data.append(stock_dict)

    # 筛选
    result = screener.screen(stocks_data, asset_type=asset_type, top_n=top_n)

    logger.info(f"筛选完成: {len(result)} 只股票被选中")

    return result


def create_advanced_screening_report(df_result: pd.DataFrame) -> str:
    """
    创建高级筛选报告摘要。
    """
    if df_result.empty:
        return "无股票符合筛选条件"

    lines = []
    lines.append("=" * 70)
    lines.append("高级多因子筛选报告 (v2)")
    lines.append("=" * 70)
    lines.append(f"选中股票数: {len(df_result)}")

    n_passed = df_result["passes_threshold"].sum() if "passes_threshold" in df_result.columns else len(df_result)
    lines.append(f"通过质量阈值: {n_passed}/{len(df_result)}")

    if "cash_buffer" in df_result.columns:
        n_cash = df_result["cash_buffer"].sum()
        lines.append(f"现金缓冲: {n_cash}/{len(df_result)}")

    lines.append("")

    for _, row in df_result.head(10).iterrows():
        params = row.get("grid_params", {})
        threshold_mark = "✓" if row.get("passes_threshold", True) else "⚠"
        lines.append(
            f"{threshold_mark} #{int(row['rank']):2d} {row['code']:<12s} "
            f"总分: {row['total_score']:.4f} "
            f"(F1:{row.get('F1_norm', 0):.2f} "
            f"F2:{row.get('F2_norm', 0):.2f} "
            f"F3:{row.get('F3_norm', 0):.2f} "
            f"F4:{row.get('F4_ortho', 0):.2f}) "
            f"网格:{params.get('spacing_coef', 'N/A')}×ATR"
        )

    if len(df_result) > 10:
        lines.append(f"... 还有 {len(df_result) - 10} 只股票")

    lines.append("=" * 70)

    return "\n".join(lines)
