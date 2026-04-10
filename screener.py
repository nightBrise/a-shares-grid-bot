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
