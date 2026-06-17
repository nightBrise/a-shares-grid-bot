"""
系统参数默认值 - A 股网格交易系统

所有散落在各模块的硬编码默认值统一收敛于此。
load_config() 加载用户 config.yaml 后与此合并，用户值覆盖默认值。
"""


def get_defaults() -> dict:
    """返回完整默认配置字典"""
    return {
        "version": "2.0.0",

        # === 资金 ===
        "capital": {
            "total": 1000000,
            "cash_reserve_ratio": 0.4,
            "max_position_per_stock": 0.10,
        },

        # === 风控 ===
        "risk": {
            "max_drawdown_threshold": 0.10,
            "single_stock_loss_threshold": 0.15,
            "max_positions": 5,
        },

        # === 网格参数 ===
        "grid": {
            "base_spacing": 0.02,
            "initial_position": 0.45,
            "grid_amount": 3000,
            "max_grids": 5,
            "atr_period": 20,
            "atr_coef": 1.5,
            "min_spacing": 0.003,
            "max_spacing": 0.15,
        },

        # === 动态网格 ===
        "dynamic_grid": {
            "volatility_regime_k": {"low": 2.5, "medium": 2.0, "high": 1.5},
            "volatility_thresholds": {"low": 0.20, "high": 0.35},
            "rail_z_coef": 2.0,
            "t1_min_spacing_coef": 1.5,
            "force_close": {"grids_below": 3, "close_pct": 0.50},
            "position_limit_pct": 0.05,
        },

        # === 优化 ===
        "backtest": {
            "days": 250,
            "n_trials": 300,
            "n_startup_trials": 30,
            "commission_rate": 0.00015,
            "stamp_tax": 0.0005,
            "transfer_fee": 0.00002,
            "slippage_rate": 0.001,
            "buy_spacing_range": [0.01, 0.06],
            "sell_spacing_range": [0.01, 0.06],
            "amount_multiplier_range": [0.3, 3.0],
            "spacing_decay_range": [0.5, 2.0],
            "initial_position_range": [0.15, 0.75],
            "max_grids_range": [3, 15],
        },

        # === 并行优化 ===
        "parallel_optimization": {
            "enabled": True,
            "max_workers": None,
            "prefill_trials": 20,
        },

        # === 路径 ===
        "paths": {
            "data_dir": "./data",
            "output_dir": "./output",
            "signal_file": "signals.csv",
            "log_file": "log.txt",
            "report_file": "report.json",
        },

        # === 网络 ===
        "network": {
            "prefer_baostock": True,
            "use_baostock_fallback": True,
            "aggressive_switch": True,
            "min_delay_per_stock": 10.0,
            "max_delay_per_stock": 20.0,
            "batch_size": 3,
            "long_rest_after_batches": 2,
            "long_rest_duration": 60.0,
            "adaptive_cooldown_base": 30.0,
            "adaptive_cooldown_multiplier": 2.0,
            "max_cooldown": 300.0,
            "recovery_factor": 0.8,
            "max_retries": 2,
            "base_retry_delay": 5.0,
            "enable_jitter": True,
            "stock_list_delay": 10.0,
            "stock_list_per_exchange_rest": 5.0,
            "consecutive_failure_limit": 3,
            "failure_rest_duration": 30.0,
            "rotate_user_agent": True,
            "rotate_referer": True,
            "enable_random_extra_delay": True,
            "extra_delay_probability": 0.15,
            "extra_delay_min": 5.0,
            "extra_delay_max": 20.0,
        },

        # === 数据源 ===
        "data_sources": {
            "akshare": {"enabled": True},
            "baostock": {"enabled": True},
        },

        # === 选股 ===
        "selection": {
            "hurst_threshold": 0.5,
            "min_price": 5.0,
            "max_price": 500.0,
            "min_turnover": 5000,
        },

        # === 高级选股 ===
        "advanced_screening": {
            "weights": {
                "etf": {"F1": 0.35, "F2": 0.15, "F3": 0.30, "F4": 0.20},
                "stock": {"F1": 0.25, "F2": 0.35, "F3": 0.20, "F4": 0.20},
            },
            "quality_threshold": 0.65,
            "threshold_mode": "adaptive_quantile",
            "adaptive_quantile": 0.75,
            "threshold_soft_cap": 0.82,
            "cash_buffer_ratio": 0.50,
            "concentration_limits": {"max_per_industry": 3},
            "path_memory": {"variance_ratio_q": 5, "min_periods": 120},
            "vol_quality": {"optimal_vol": 0.25, "tolerance": 0.15},
            "vol_tail_low": 0.05,
            "vol_tail_high": 0.95,
            "grid_params": {
                "high_score": {"spacing_coef": 1.8, "position_pct": 0.025},
                "medium_score": {"spacing_coef": 2.2, "position_pct": 0.018},
                "low_score": {"spacing_coef": None, "position_pct": 0},
            },
        },

        # === 风控（circuit breaker）===
        "risk_control": {
            "enabled": True,
            "single_stock_loss_threshold": 0.15,
            "max_drawdown_threshold": 0.10,
            "initial_peak": 1000000.0,
            "vol_adjustment_enabled": True,
        },

        # === 市场状态门控 ===
        "regime_filter": {
            "benchmark_index": "000300.SH",
            "adx_normal_max": 25,
            "adx_warning_max": 35,
            "vol_normal_low": 0.30,
            "vol_normal_high": 0.70,
            "vol_extreme_low": 0.15,
            "vol_extreme_high": 0.85,
            "smoothing_days": 3,
            "confirm_days": 2,
            "hard_stop": {
                "index_drop_threshold": 0.05,
                "limit_down_count": 200,
                "volume_shrink_percentile": 0.10,
            },
        },

        # === 日志 ===
        "logging": {
            "level": "INFO",
            "backup_count": 30,
            "max_bytes": 0,
        },

        # === 数据库下载 ===
        "database_download": {
            "default_history_years": 3,
            "batch_size": 100,
            "resume": True,
            "update_threshold_days": 30,
            "skip_min_record_count": 100,
        },

        # === 交易模式 ===
        "trading": {
            "mode": "paper",
            "fee_rate": 0.00015,
            "stamp_tax_rate": 0.0005,
            "min_fee": 5.0,
        },

        # === 模拟盘 ===
        "paper_trading": {
            "enabled": False,
            "initial_cash": 1000000,
        },
    }
