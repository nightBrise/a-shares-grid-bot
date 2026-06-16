"""
screener.py - 多因子横截面打分选股器

替代绝对 Hurst < 0.5 阈值筛选，使用加权多因子模型进行横截面打分排序。

打分因子：
- OU 半衰期 (35%): 优先快速均值回归
- Hurst 指数 (30%): 趋势性指标
- ADX (20%): 趋势强度
- 波动率适配 (15%): 中等波动最好
"""

from typing import Dict, List, Optional
import logging

import numpy as np
import pandas as pd

from trading_core.defaults import get_defaults

logger = logging.getLogger("grid_trading")



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

        defaults = get_defaults()
        adv_cfg = {**defaults.get("advanced_screening", {}), **self.config.get("advanced_screening", {})}

        # 权重配置
        weights_cfg = adv_cfg.get("weights", {})
        self.etf_weights = weights_cfg.get("etf", {"F1": 0.35, "F2": 0.15, "F3": 0.30, "F4": 0.20})
        self.stock_weights = weights_cfg.get("stock", {"F1": 0.25, "F2": 0.35, "F3": 0.20, "F4": 0.20})

        # 阈值配置
        self.quality_threshold = adv_cfg.get("quality_threshold", 0.65)
        self.threshold_mode = adv_cfg.get("threshold_mode", "adaptive_quantile")
        self.adaptive_quantile = adv_cfg.get("adaptive_quantile", 0.75)
        self.cash_buffer_ratio = adv_cfg.get("cash_buffer_ratio", 0.50)

        # 波动质量参数
        vol_cfg = adv_cfg.get("vol_quality", {"optimal_vol": 0.25, "tolerance": 0.15})
        self.vol_optimal = vol_cfg.get("optimal_vol", 0.25)
        self.vol_tolerance = vol_cfg.get("tolerance", 0.15)

        # Path Memory 参数
        pm_cfg = adv_cfg.get("path_memory", {"variance_ratio_q": 5, "min_periods": 120})
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
        计算波动质量得分（高斯核倒U型函数）。

        波动率在 optimal_vol 附近得最高分，越远得分越低。
        使用高斯核：exp(-(σ-σ_opt)² / 2σ₀²)
        """
        if pd.isna(volatility):
            return 0.0
        sigma_0 = self.vol_tolerance  # 0.15
        score = np.exp(-((volatility - self.vol_optimal) ** 2) / (2 * (sigma_0 ** 2)))
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
#         original_index = series.index

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

        # 成交额过滤 (从 config 读取阈值)
        min_turnover = self.config.get('screening', {}).get(
            'initial_filters', {}).get('min_turnover', 5000)
        if "avg_turnover" in df.columns:
            df = df[df["avg_turnover"] >= min_turnover].copy()
        else:
            logger.warning("avg_turnover 列不存在，跳过流动性过滤")

        # 价格过滤
        if "price" in df.columns:
            df = df[
                (df["price"] >= 5.0) & (df["price"] <= 500.0)
            ].copy()

        # ST 过滤
        if "is_st" in df.columns:
            df = df[~df["is_st"]].copy()

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

        # Step 2: F3 波动质量（高斯核倒U型）+ 横截面辅过滤
        if "volatility_60d" in result.columns:
            result["F3_norm"] = result["volatility_60d"].apply(self._vol_quality)

            # 辅过滤：剔除横截面波动率排名尾部的标的（阈值从 config 读取）
            adv_cfg = self.config.get('advanced_screening', {})
            vol_tail_low = adv_cfg.get('vol_tail_low', 0.05)
            vol_tail_high = adv_cfg.get('vol_tail_high', 0.95)
            vol_rank = result["volatility_60d"].rank(pct=True)
            result = result[(vol_rank >= vol_tail_low) & (vol_rank <= vol_tail_high)]

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

    def apply_capital_fitness(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        应用资金适配度评分。

        根据当前资金量、每格金额和股价，计算每只股票的资金适配度。
        采用效率因子平滑处理高价股，避免阶梯式跳变。
        """
        if df.empty:
            return df

        result = df.copy()

        capital_cfg = self.config.get("capital", {})
        grid_cfg = self.config.get("grid", {})

        total = capital_cfg.get("total", 100000)
        max_position = capital_cfg.get("max_position_per_stock", 0.3)
        initial_position = grid_cfg.get("initial_position", 0.45)
        grid_amount = grid_cfg.get("grid_amount", 3000)
        max_grids = grid_cfg.get("max_grids", 5)

        per_stock = total * max_position
        initial_needed = per_stock * initial_position

        def _fitness(row):
            price = row.get("price", 0)
            if price <= 0:
                return 0.0, 0.0

            lot_value = price * 100
            actual_grid = max(grid_amount, lot_value)
            grid_needed = actual_grid * max_grids

            # 基础资金充足度
            if initial_needed > per_stock:
                base = 0.0
            elif initial_needed + grid_needed <= per_stock:
                base = 1.0
            else:
                base = (per_stock - initial_needed) / grid_needed if grid_needed > 0 else 0.0

            # 效率因子：股价越接近 grid_amount/100 效率越高
            if lot_value <= grid_amount:
                efficiency = 1.0
            else:
                ratio = grid_amount / lot_value
                efficiency = ratio ** 0.5

            return base * (0.5 + 0.5 * efficiency), efficiency

        fitness_vals = result.apply(_fitness, axis=1, result_type="expand")
        result["capital_fitness"] = fitness_vals[0]
        result["efficiency"] = fitness_vals[1]

        # 综合评分：因子评分占 70%，资金适配占 30%
        result["final_score"] = result["total_score"] * (
            0.7 + 0.3 * result["capital_fitness"]
        )

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

    def rank_stocks(self, df: pd.DataFrame, top_n: Optional[int] = None,
                    sort_by: str = "total_score") -> pd.DataFrame:
        """按指定得分排名，可选截取 Top N（None = 返回全部）。"""
        if df.empty:
            return df

        if sort_by not in df.columns:
            sort_by = "total_score"

        df = df.sort_values(sort_by, ascending=False)
        df["rank"] = range(1, len(df) + 1)

        # 应用行业分散约束（仅当 industry 列存在时）
        if 'industry' in df.columns:
            max_per_industry = self.config.get("max_per_industry", 3)
            df = self._apply_concentration_limits(df, max_per_industry=max_per_industry)
            if top_n is not None:
                df = df.head(top_n)
            df["rank"] = range(1, len(df) + 1)

        if top_n is not None:
            df = df.head(top_n)
        return df

    def screen(
        self,
        stocks_data: List[Dict],
        asset_type: str = "stock",
        top_n: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        主筛选函数。

        Parameters:
            stocks_data: 股票因子字典列表
            asset_type: 'etf' 或 'stock'
            top_n: 返回的 Top N 股票数（None = 返回全部）

        Returns:
            包含得分和排名的 DataFrame
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

        # Stage 2.5: 资金适配度评分
        df = self.apply_capital_fitness(df)

        # 保存完整评分结果（供外部写入 SQLite）
        self.last_full_results = df.copy()

        # Stage 3: 排名并选择 Top N（按 final_score）
        df = self.rank_stocks(df, top_n=top_n, sort_by="final_score")

        # 统计
        n_passed = df["passes_threshold"].sum() if "passes_threshold" in df.columns else len(df)
        logger.info(
            f"筛选完成: {len(df)} 只股票被选中, "
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

