"""
数据模块 - A 股网格交易系统 v4.0 (Lite 精简版)
功能：统一数据入口，AkShare/Baostock 双数据源，后复权处理、本地缓存
     支持增量数据更新引擎 (Data Incremental ETL)
"""

import threading

import os
import logging
import random
import sqlite3
import time
from datetime import datetime, timedelta, time as dt_time
from typing import Optional, List, Dict, Tuple
import pandas as pd
import numpy as np

logger = logging.getLogger("grid_trading")

# AkShare 数据接口（主数据源）
try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except ImportError:
    AKSHARE_AVAILABLE = False
    logger.warning("akshare 未安装，将仅使用 baostock 数据源")

# Baostock 数据接口（备用数据源）
try:
    import baostock as bs
    BAOSTOCK_AVAILABLE = True
except ImportError:
    BAOSTOCK_AVAILABLE = False
    logger.warning("baostock 未安装，将仅使用 akshare 数据源")

# 腾讯财经数据接口（第3备用数据源）
try:
    from data_layer.tencent_fetcher import fetch_from_tencent
    TENCENT_AVAILABLE = True
except ImportError:
    TENCENT_AVAILABLE = False
    logger.warning("腾讯财经模块未找到，将仅使用 akshare/baostock 数据源")

# 东方财富数据接口（第4备用数据源）
try:
    from data_layer.eastmoney_fetcher import fetch_from_eastmoney
    EASTMONEY_AVAILABLE = True
except ImportError:
    EASTMONEY_AVAILABLE = False
    logger.warning("东方财富模块未找到")


# ==================== 全局状态管理 ====================

# 模块级配置（由 execute_strategy 通过 init_fetcher_config 注入）
_fetcher_config: dict = {}


def init_fetcher_config(config: dict):
    """初始化数据获取模块的配置（在策略执行前调用一次）"""
    global _fetcher_config
    _fetcher_config = config or {}


def get_max_retries() -> int:
    """从配置获取最大重试次数"""
    return _fetcher_config.get('network', {}).get('max_retries', 2)

# 增量更新断点追踪（迁移到 SQLite update_checkpoint 表）
_update_checkpoint: Dict[str, Dict] = {}  # code -> {'last_success': datetime, 'last_error': str, 'last_date': str}


def _load_update_checkpoint() -> Dict[str, Dict]:
    """从 SQLite 加载增量更新断点"""
    global _update_checkpoint
    from data_layer.market_db import get_update_checkpoint
    _update_checkpoint = get_update_checkpoint()
    logger.debug(f"加载断点数据：{len(_update_checkpoint)} 只股票")
    return _update_checkpoint


def _save_update_checkpoint() -> None:
    """保存断点到 SQLite"""
    from data_layer.market_db import save_update_checkpoint as _save_checkpoint
    for code, info in _update_checkpoint.items():
        _save_checkpoint(
            code=code,
            last_success=info.get('last_success', ''),
            last_date=info.get('last_date', ''),
            last_error=info.get('last_error', None),
            consecutive_failures=info.get('consecutive_failures', 0)
        )
    logger.debug(f"保存断点数据：{len(_update_checkpoint)} 只股票")


def update_checkpoint(code: str, success: bool, last_date: str = None,
                     error_msg: str = None, cache_exists: bool = False) -> None:
    """
    更新单只股票的断点信息

    参数:
        code: 股票代码
        success: 是否成功获取数据
        last_date: 最后数据的日期
        error_msg: 错误信息（如失败）
        cache_exists: 是否有本地缓存
    """
    global _update_checkpoint
    if code not in _update_checkpoint:
        _update_checkpoint[code] = {}

    if success:
        _update_checkpoint[code]['last_success'] = datetime.now()
        _update_checkpoint[code]['last_date'] = last_date
        _update_checkpoint[code]['last_error'] = None
        _update_checkpoint[code]['cache_exists'] = cache_exists
        _update_checkpoint[code]['consecutive_failures'] = 0
    else:
        if 'consecutive_failures' not in _update_checkpoint[code]:
            _update_checkpoint[code]['consecutive_failures'] = 0
        _update_checkpoint[code]['consecutive_failures'] += 1
        _update_checkpoint[code]['last_error'] = error_msg

    # 每 10 次更新保存一次
    if len(_update_checkpoint) % 10 == 0:
        _save_update_checkpoint()


def get_cache_status_for_stocks(codes: List[str]) -> Dict[str, Dict]:
    """
    获取股票缓存状态（从 SQLite 读取）

    返回:
        Dict[code, {'has_cache': bool, 'last_date': str, 'record_count': int}]
    """
    _load_update_checkpoint()  # 确保断点已加载
    status = {}
    for code in codes:
        has_cache = False
        record_count = 0
        last_date = None

        try:
            from data_layer.market_db import get_stock_data as get_db_data
            df = get_db_data(code)
            if df is not None and not df.empty:
                has_cache = True
                record_count = len(df)
                last_date = df['date'].max().strftime('%Y-%m-%d')
        except Exception:
            logger.warning("读取缓存状态失败: %s", code, exc_info=True)

        # 优先使用断点数据
        checkpoint_info = _update_checkpoint.get(code, {})

        status[code] = {
            'has_cache': has_cache,
            'record_count': record_count,
            'last_date': last_date or checkpoint_info.get('last_date'),
            'last_success': checkpoint_info.get('last_success'),
            'last_error': checkpoint_info.get('last_error'),
            'consecutive_failures': checkpoint_info.get('consecutive_failures', 0),
        }

    return status

# ==================== A 股交易日历（多数据源交叉校验） ====================

_ta_calendar_cache: Optional[pd.DatetimeIndex] = None
_ta_calendar_cache_time: Optional[datetime] = None
TA_CALENDAR_CACHE_DAYS = 7
_calendar_source: str = ""  # 记录数据来源


def get_trade_calendar(force_refresh: bool = False) -> pd.DatetimeIndex:
    """
    获取 A 股交易日历（多数据源交叉校验）

    数据源优先级:
    1. AKShare (tool_trade_date_hist_sina) - 优先
    2. Baostock (query_trade_dates) - 校验
    3. TuShare (trade_cal) - 仲裁（如果可用）
    4. Fallback (仅排除周末) - 兜底

    参数:
        force_refresh: 是否强制刷新缓存

    返回:
        包含所有交易日的 DatetimeIndex (无时区)
    """
    global _ta_calendar_cache, _ta_calendar_cache_time, _calendar_source

    now = datetime.now()

    # 检查缓存是否有效
    if (not force_refresh
            and _ta_calendar_cache is not None
            and _ta_calendar_cache_time is not None
            and (now - _ta_calendar_cache_time).days < TA_CALENDAR_CACHE_DAYS):
        return _ta_calendar_cache

    # === 数据源 1: AKShare ===
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        cal_ak = pd.DatetimeIndex(pd.to_datetime(df['trade_date']).sort_values())
        _calendar_source = "akshare"
        logger.debug(f"AKShare 返回 {len(cal_ak)} 个交易日")

        # === 交叉校验: Baostock ===
        try:
            import baostock as bs
            import socket
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(10)
            try:
                bs.login()
                # 获取未来30天和过去30天
                start_d = (now - timedelta(days=30)).strftime('%Y-%m-%d')
                end_d = (now + timedelta(days=30)).strftime('%Y-%m-%d')
                rs = bs.query_trade_dates(start_date=start_d, end_date=end_d)
                df_bs = rs.get_data()
                bs.logout()
            finally:
                socket.setdefaulttimeout(old_timeout)

            if not df_bs.empty:
                bs_dates = set(pd.to_datetime(df_bs[df_bs['is_trading_day'] == '1']['calendar_date']))
                ak_dates = set(cal_ak[
                    (cal_ak >= pd.to_datetime(start_d)) &
                    (cal_ak <= pd.to_datetime(end_d))
                ])

                # 比对近30天
                if bs_dates != ak_dates:
                    diff_bs = bs_dates - ak_dates
                    diff_ak = ak_dates - bs_dates
                    logger.warning(
                        f"交易日历不一致！AKShare vs Baostock 差异: "
                        f"Baostock多 {len(diff_bs)} 天, AKShare多 {len(diff_ak)} 天"
                    )
                    # 记录差异但不阻塞，使用 AKShare
                    _calendar_source = "akshare(baostock_mismatch)"
                else:
                    logger.debug("AKShare 与 Baostock 校验一致")
        except Exception as e:
            logger.warning(f"Baostock 校验失败: {e}，跳过校验")

        # 缓存并返回
        _ta_calendar_cache = cal_ak
        _ta_calendar_cache_time = now
        logger.info(f"已获取 A 股交易日历（来源: {_calendar_source}）：{len(cal_ak)} 个交易日")
        return cal_ak

    except Exception as e:
        logger.warning(f"AKShare 获取失败: {e}")

    # === 数据源 2: Baostock 直接获取 ===
    try:
        import baostock as bs
        bs.login()
        rs = bs.query_trade_dates(start_date='2020-01-01', end_date='2030-12-31')
        df_bs = rs.get_data()
        bs.logout()

        if not df_bs.empty:
            cal_bs = pd.DatetimeIndex(pd.to_datetime(
                df_bs[df_bs['is_trading_day'] == '1']['calendar_date']
            ).sort_values())
            _ta_calendar_cache = cal_bs
            _ta_calendar_cache_time = now
            _calendar_source = "baostock"
            logger.info(f"已获取 A 股交易日历（来源: Baostock）：{len(cal_bs)} 个交易日")
            return cal_bs
    except Exception as e:
        logger.warning(f"Baostock 获取失败: {e}")

    # === 数据源 3: TuShare ===
    try:
        import tushare as ts
        # 尝试获取 pro 接口
        pro = ts.pro()
        if pro is not None:
            df_ts = pro.trade_cal(start_date='20200101', end_date='20301231')
            if not df_ts.empty:
                cal_ts = pd.DatetimeIndex(pd.to_datetime(
                    df_ts[df_ts['is_open'] == 1]['cal_date']
                ).sort_values())
                _ta_calendar_cache = cal_ts
                _ta_calendar_cache_time = now
                _calendar_source = "tushare"
                logger.info(f"已获取 A 股交易日历（来源: TuShare）：{len(cal_ts)} 个交易日")
                return cal_ts
    except Exception as e:
        logger.warning(f"TuShare 获取失败: {e}")

    # === 兜底: Fallback 日历 ===
    logger.warning("所有数据源均失败，使用仅排除周末的兜底日历")
    _ta_calendar_cache = _get_fallback_calendar()
    _ta_calendar_cache_time = now
    _calendar_source = "fallback(weekend_only)"
    return _ta_calendar_cache


def _get_fallback_calendar() -> pd.DatetimeIndex:
    """生成仅排除周末的简化日历（兜底用）"""
    start = datetime(2020, 1, 1)
    end = datetime(2030, 12, 31)
    dates = pd.bdate_range(start=start, end=end)
    return dates


def is_trading_day(date) -> bool:
    """判断指定日期是否为交易日（带多数据源校验）"""
    if isinstance(date, str):
        date = pd.to_datetime(date)
    elif isinstance(date, datetime):
        date = pd.Timestamp(date)

    calendar = get_trade_calendar()
    return date in calendar


def get_previous_trading_day(date, n: int = 1):
    """获取指定日期前第 n 个交易日"""
    if isinstance(date, str):
        date = pd.to_datetime(date)
    elif isinstance(date, datetime):
        date = pd.Timestamp(date)

    calendar = get_trade_calendar()
    idx = calendar.searchsorted(date, side='right') - 1
    target_idx = max(0, idx - n + 1)
    return calendar[target_idx]


def get_next_trading_day(date, n: int = 1):
    """获取指定日期后第 n 个交易日"""
    if isinstance(date, str):
        date = pd.to_datetime(date)
    elif isinstance(date, datetime):
        date = pd.Timestamp(date)

    calendar = get_trade_calendar()
    try:
        idx = calendar.searchsorted(date, side='left')
    except ValueError:
        date = pd.Timestamp(date.year, date.month, date.day)
        idx = calendar.searchsorted(date, side='left')
    target_idx = min(len(calendar) - 1, idx + n - 1)
    return calendar[target_idx]


def get_expected_latest_date() -> pd.Timestamp:
    """
    根据当前时间判断股票数据应该最新到哪一天。
    - 当前是交易日且已收盘 (>15:00): 期望数据最新日期 = 今天
    - 当前是交易日且未收盘: 期望数据最新日期 = 前一个交易日
    - 当前不是交易日: 期望数据最新日期 = 前一个交易日
    """
    now = datetime.now()
    today = pd.Timestamp(now.date())

    if not is_trading_day(today):
        return get_previous_trading_day(today, n=1)

    if now.time() >= dt_time(15, 0):
        return today
    else:
        return get_previous_trading_day(today, n=1)


def is_stock_data_fresh(code: str, data_dir: str = "./data") -> bool:
    """检查单只股票缓存数据是否最新（从 SQLite 读取）"""
    expected = get_expected_latest_date()
    meta = get_stock_metadata(code, data_dir)
    if meta and meta.get('last_update_date'):
        return pd.Timestamp(meta['last_update_date']) >= expected
    # 无元数据时尝试读 SQLite
    try:
        from data_layer.market_db import get_stock_data as get_db_data
        df = get_db_data(code, data_dir=data_dir)
        if df is not None and not df.empty:
            last_date = df['date'].max()
            return pd.Timestamp(last_date) >= expected
    except Exception:
        logger.warning("检查数据新鲜度失败", exc_info=True)
    return False


def align_to_trading_day(date, direction: str = 'forward'):
    """
    将日期对齐到最近的交易日

    参数:
        date: 要对齐的日期
        direction: 'forward'=向前找下一个交易日，'backward'=向后找上一个交易日
    """
    calendar = get_trade_calendar()

    if isinstance(date, str):
        date = pd.to_datetime(date)
    elif isinstance(date, datetime):
        date = pd.Timestamp(date)

    # 转换为与日历相同的精度，避免 searchsorted 时单元不匹配
    date = date.as_unit(calendar.unit, round_ok=True)

    if date in calendar:
        return date

    if direction == 'forward':
        idx = calendar.searchsorted(date, side='right')
        if idx >= len(calendar):
            return calendar[-1]
        return calendar[idx]
    else:
        idx = calendar.searchsorted(date, side='left') - 1
        if idx < 0:
            return calendar[0]
        return calendar[idx]


def verify_date_alignment(code: str, df: pd.DataFrame) -> Tuple[bool, List[str], List[str]]:
    """
    严格验证股票数据的交易日对齐（替代 _check_data_integrity）

    使用真实日历检查：
    1. 数据日期是否都是交易日
    2. 是否有遗漏的交易日

    返回: (是否对齐, 缺失日期列表, 警告消息列表)
    """
    if df.empty or len(df) < 2:
        return True, [], []

    warnings = []
    calendar = get_trade_calendar()

    df_sorted = df.sort_values('date').reset_index(drop=True)
    dates = pd.to_datetime(df_sorted['date']).dt.date.tolist()

    # 1. 检查是否有非交易日的数据
    non_trading_dates = []
    for d in dates:
        pd_d = pd.Timestamp(d)
        if pd_d not in calendar and d.weekday() < 5:
            non_trading_dates.append(d.strftime('%Y-%m-%d'))

    if non_trading_dates:
        warnings.append(f"{code} 包含 {len(non_trading_dates)} 个非交易日数据: {non_trading_dates[:5]}...")

    # 2. 检查是否有缺失的交易日
    missing_dates = []
    for i in range(1, len(dates)):
        prev = pd.Timestamp(dates[i-1])
        curr = pd.Timestamp(dates[i])
        delta = (curr - prev).days

        if delta > 1:
            # 检查中间日期是否应该交易日
            current = prev
            while current < curr:
                current += timedelta(days=1)
                if current.weekday() < 5 and current not in calendar:
                    missing_dates.append(current.strftime('%Y-%m-%d'))

    if missing_dates:
        warnings.append(f"{code} 缺失 {len(missing_dates)} 个交易日")

    is_aligned = len(warnings) == 0
    return is_aligned, missing_dates, warnings


# ==================== 增量更新元数据管理 ====================

METADATA_FILE = "metadata.json"

def get_metadata_path(data_dir: str = "./data") -> str:
    """获取元数据文件路径"""
    return os.path.join(data_dir, METADATA_FILE)


def load_metadata(data_dir: str = "./data") -> Dict:
    """
    加载元数据（从 SQLite）

    返回:
        元数据字典，格式：{code: {last_update_date, record_count, ...}}
    """
    try:
        from data_layer.market_db import get_all_metadata
        return get_all_metadata(data_dir)
    except Exception as e:
        logger.debug(f"从 SQLite 加载元数据失败: {e}")
        return {}


def save_metadata(metadata: Dict, data_dir: str = "./data"):
    """
    保存元数据到 SQLite（保留接口兼容性）

    参数:
        metadata: 元数据字典
        data_dir: 数据目录
    """
    try:
        from data_layer.market_db import update_metadata
        for code, info in metadata.items():
            update_metadata(code, data_dir=data_dir, **info)
    except Exception as e:
        logger.debug(f"保存元数据到 SQLite 失败: {e}")


def get_stock_metadata(code: str, data_dir: str = "./data") -> Optional[Dict]:
    """
    获取单只股票的元数据（从 SQLite）

    参数:
        code: 股票代码
        data_dir: 数据目录

    返回:
        股票元数据，包含 last_update_date, record_count 等
    """
    try:
        from data_layer.market_db import get_metadata
        return get_metadata(code, data_dir)
    except Exception as e:
        logger.debug(f"从 SQLite 读取元数据失败: {e}")
        return None


def update_stock_metadata(code: str, last_date: str, record_count: int,
                          data_dir: str = "./data", **kwargs):
    """
    更新单只股票的元数据（写入 SQLite）

    参数:
        code: 股票代码
        last_date: 最后更新日期 (YYYY-MM-DD)
        record_count: 记录总数
        data_dir: 数据目录
        **kwargs: 其他元数据字段
    """
    try:
        from data_layer.market_db import update_metadata
        update_metadata(code, data_dir=data_dir,
                       last_update_date=last_date,
                       record_count=record_count,
                       update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                       **kwargs)
        logger.debug(f"股票 {code} 元数据已更新：最后日期={last_date}, 记录数={record_count}")
    except Exception as e:
        logger.debug(f"更新元数据失败: {e}")


def check_data_integrity(df: pd.DataFrame, code: str) -> Tuple[bool, List[str]]:
    """
    检查数据完整性，识别缺失的交易日（使用真实日历）

    参数:
        df: 股票数据 DataFrame
        code: 股票代码

    返回:
        (是否完整，缺失日期列表)
    """
    is_aligned, missing_dates, warnings = verify_date_alignment(code, df)

    if warnings:
        logger.warning(f"{code} 数据对齐问题: {'; '.join(warnings)}")

    return is_aligned, missing_dates


# ==================== 数据源限流器（每个数据源独立） ====================

class SourceRateLimiter:
    """
    单个数据源的独立限流器

    核心设计：每个数据源有自己的成功/失败计数和延迟状态，
    一个源被限流时不会影响其他源的正常请求。
    """

    def __init__(
        self,
        name: str,
        base_delay: float = 3.0,
        max_delay: float = 600.0,
        multiplier: float = 2.0,
        recovery_factor: float = 0.7,
        min_delay: float = 1.5,
        failure_ceiling: int = 15
    ):
        self.name = name
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.multiplier = multiplier
        self.recovery_factor = recovery_factor
        self.min_delay = min_delay
        self.failure_ceiling = failure_ceiling

        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._current_delay = base_delay
        self._last_request_time = 0
        self._lock = None

    def _get_lock(self):
        if self._lock is None:
            import threading
            self._lock = threading.Lock()
        return self._lock

    def record_success(self):
        with self._get_lock():
            self._consecutive_failures = 0
            self._consecutive_successes += 1
            if self._consecutive_successes >= 2:
                self._current_delay = max(
                    self.base_delay,
                    self._current_delay * self.recovery_factor
                )

    def record_failure(self):
        with self._get_lock():
            self._consecutive_failures += 1
            self._consecutive_successes = 0
            if self._consecutive_failures >= 2:
                capped_failures = min(self._consecutive_failures, self.failure_ceiling)
                self._current_delay = min(
                    self.max_delay,
                    self.base_delay * (self.multiplier ** (capped_failures - 1))
                )

    def wait(self) -> float:
        import time as _time
        import random as _random

        with self._get_lock():
            current_time = _time.time()
            elapsed = current_time - self._last_request_time
            jitter = _random.uniform(-0.3, 0.3) * self._current_delay
            actual_delay = max(0, self._current_delay + jitter)
            if elapsed < actual_delay:
                _time.sleep(actual_delay - elapsed)
            self._last_request_time = _time.time()
        return self._current_delay

    def get_status(self) -> dict:
        with self._get_lock():
            return {
                'name': self.name,
                'current_delay': self._current_delay,
                'consecutive_failures': self._consecutive_failures,
                'consecutive_successes': self._consecutive_successes,
                'mode': 'RECOVERY' if self._consecutive_successes >= 3 else
                        'BACKOFF' if self._consecutive_failures >= 2 else
                        'NORMAL'
            }

    def is_healthy(self) -> bool:
        """返回该数据源当前是否健康（未被严重限流）"""
        with self._get_lock():
            return self._consecutive_failures < 5


class DataSourceManager:
    """
    多数据源管理器

    职责：
    1. 为每个数据源维护独立的 SourceRateLimiter
    2. 按健康度排序选择数据源（健康的优先）
    3. 快速轮询：一个源失败后立即尝试下一个，不等待全局延迟
    4. 记录各源的历史成功率，用于长期优先级调整
    """

    SOURCE_NAMES = ['baostock', 'akshare', 'tencent', 'eastmoney']

    def __init__(self, config: dict = None):
        cfg = config or {}
        network_cfg = cfg.get('network', {})

        base = network_cfg.get('min_delay_per_stock', 3.0)
        max_d = network_cfg.get('max_cooldown', 600.0)

        self._limiters: Dict[str, SourceRateLimiter] = {}
        for name in self.SOURCE_NAMES:
            # 不同源可以配置不同的基础延迟（AkShare反爬虫最严，需要更保守）
            src_base = base * (2.5 if name == 'akshare' else 1.0)
            self._limiters[name] = SourceRateLimiter(
                name=name,
                base_delay=src_base,
                max_delay=max_d,
                multiplier=2.0,
                recovery_factor=0.7,
                min_delay=src_base * 0.5,
                failure_ceiling=15
            )

        # 历史成功率追踪（长期健康度）
        self._history: Dict[str, Dict] = {
            name: {'success': 0, 'failure': 0}
            for name in self.SOURCE_NAMES
        }

        # Baostock 连接状态（线程安全，替代全局变量 _bs_logged_in / _bs_server_reject）
        self._bs_logged_in = False
        self._bs_server_reject = False
        self._bs_last_failed_time = 0.0
        self._bs_lock = threading.Lock()

    def get_limiter(self, name: str) -> SourceRateLimiter:
        return self._limiters.get(name)

    # --- Baostock 连接状态方法 ---

    def is_bs_logged_in(self) -> bool:
        with self._bs_lock:
            return self._bs_logged_in

    def set_bs_logged_in(self, value: bool):
        with self._bs_lock:
            self._bs_logged_in = value

    def is_server_reject(self, source: str) -> bool:
        with self._bs_lock:
            return self._bs_server_reject if source == 'baostock' else False

    def set_server_reject(self, source: str, value: bool):
        if source == 'baostock':
            with self._bs_lock:
                self._bs_server_reject = value

    def ensure_bs_login(self, force_relogin: bool = False):
        """确保 Baostock 已登录（线程安全）"""
        if not BAOSTOCK_AVAILABLE:
            return

        with self._bs_lock:
            if self._bs_logged_in and not force_relogin:
                return

            # 登录失败冷却：1 分钟内不重试
            if time.time() - self._bs_last_failed_time < 60:
                logger.debug("Baostock 登录失败冷却中...")
                return

        try:
            if self._bs_logged_in:
                try:
                    bs.logout()
                except Exception:
                    logger.warning("Baostock 登出失败", exc_info=True)

            bs.login()
            with self._bs_lock:
                self._bs_logged_in = True
            logger.info("Baostock 登录成功")

        except Exception as e:
            logger.warning(f"Baostock 登录失败：{str(e)}")
            with self._bs_lock:
                self._bs_logged_in = False
                self._bs_last_failed_time = time.time()

    # --- 数据源健康管理 ---

    def record_source_result(self, name: str, success: bool):
        """记录某数据源的一次请求结果"""
        limiter = self._limiters.get(name)
        if limiter:
            if success:
                limiter.record_success()
                self._history[name]['success'] += 1
            else:
                limiter.record_failure()
                self._history[name]['failure'] += 1

    def get_source_health_score(self, name: str) -> float:
        """
        计算数据源的健康分数 (0.0 ~ 1.0)
        综合考虑：当前延迟、连续失败次数、历史成功率
        """
        limiter = self._limiters.get(name)
        if not limiter:
            return 0.0

        hist = self._history[name]
        total = hist['success'] + hist['failure']
        if total > 0:
            hist_rate = hist['success'] / total
        else:
            hist_rate = 0.5  # 无历史时给中性分

        status = limiter.get_status()
        # 延迟越低越好，最大600s映射到0~1
        delay_score = max(0, 1 - status['current_delay'] / 600)
        # 连续失败越少越好
        failure_score = max(0, 1 - status['consecutive_failures'] / 10)

        # 加权：历史成功率40% + 当前延迟30% + 连续失败30%
        return hist_rate * 0.4 + delay_score * 0.3 + failure_score * 0.3

    def get_priority_order(self, prefer_baostock: bool = True) -> List[str]:
        """
        按健康分数降序返回数据源优先级列表
        """
        available = []
        for name in self.SOURCE_NAMES:
            if name == 'baostock' and not BAOSTOCK_AVAILABLE:
                continue
            if name == 'akshare' and not AKSHARE_AVAILABLE:
                continue
            if name == 'tencent' and not TENCENT_AVAILABLE:
                continue
            if name == 'eastmoney' and not EASTMONEY_AVAILABLE:
                continue
            available.append(name)

        # 按健康分数排序
        available.sort(key=lambda n: self.get_source_health_score(n), reverse=True)

        # 如果用户明确偏好 baostock 且它在可用列表中，将其提到最前
        if prefer_baostock and 'baostock' in available:
            available.remove('baostock')
            available.insert(0, 'baostock')

        return available

    def get_all_status(self) -> Dict[str, dict]:
        """返回所有数据源的状态摘要"""
        return {
            name: {
                **limiter.get_status(),
                'health_score': round(self.get_source_health_score(name), 2),
                'history': self._history[name]
            }
            for name, limiter in self._limiters.items()
        }

    def print_health_report(self):
        """打印数据源健康报告（用于验证限流效果）"""
        status = self.get_all_status()
        tracker_stats = get_request_tracker().get_stats()

        lines = [
            "",
            "=" * 65,
            "  数据源健康报告",
            "=" * 65,
            f"  全局请求速率: {tracker_stats['requests_last_minute']}/{tracker_stats['max_per_minute']} per min, "
            f"{tracker_stats['requests_last_hour']}/{tracker_stats['max_per_hour']} per hour",
            "-" * 65,
            f"  {'数据源':<12} {'状态':<10} {'健康分':<8} {'延迟':<8} {'成功':<6} {'失败':<6} {'连续失败':<8}",
            "-" * 65,
        ]

        for name, s in status.items():
            hist = s['history']
#             total = hist['success'] + hist['failure']
            mode = s.get('mode', 'N/A')
            lines.append(
                f"  {name:<12} {mode:<10} {s['health_score']:<8.2f} "
                f"{s['current_delay']:<8.1f} {hist['success']:<6} {hist['failure']:<6} "
                f"{s['consecutive_failures']:<8}"
            )

        total_req = sum(s['history']['success'] + s['history']['failure'] for s in status.values())
        total_fail = sum(s['history']['failure'] for s in status.values())
        fail_rate = total_fail / total_req if total_req > 0 else 0

        lines.extend([
            "-" * 65,
            f"  总请求数: {total_req}, 总失败: {total_fail}, 失败率: {fail_rate:.1%}",
            "=" * 65,
        ])

        report = "\n".join(lines)
        logger.info(report)
        return report


# 全局数据源管理器实例
_global_source_manager: Optional[DataSourceManager] = None


def get_source_manager(config: dict = None) -> DataSourceManager:
    """获取全局数据源管理器（延迟初始化）"""
    global _global_source_manager
    if _global_source_manager is None or config is not None:
        _global_source_manager = DataSourceManager(config)
    return _global_source_manager


# ==================== 自适应限流器 ====================

class AdaptiveRateLimiter:
    """
    自适应请求频率限制器

    特性:
    - 跟踪连续失败/成功次数
    - 失败时指数退避 (base * multiplier^failures)
    - 成功后逐步恢复正常（每次成功减少一点延迟）
    - 线程安全的状态管理
    """

    def __init__(
        self,
        base_delay: float = 5.0,
        max_delay: float = 300.0,
        multiplier: float = 2.0,
        recovery_factor: float = 0.8,
        min_delay: float = 2.0,
        failure_ceiling: int = 10
    ):
        """
        初始化自适应限流器

        参数:
            base_delay: 基础延迟（秒）
            max_delay: 最大延迟（秒）
            multiplier: 失败时指数增长倍数
            recovery_factor: 成功后延迟减少因子
            min_delay: 最小延迟（秒）
            failure_ceiling: 最大连续失败计数
        """
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.multiplier = multiplier
        self.recovery_factor = recovery_factor
        self.min_delay = min_delay
        self.failure_ceiling = failure_ceiling

        # 内部状态
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._current_delay = base_delay
        self._last_request_time = 0
        self._lock = None  # 延迟初始化

    def _get_lock(self):
        """延迟初始化锁（避免导入时问题）"""
        if self._lock is None:
            import threading
            self._lock = threading.Lock()
        return self._lock

    def record_success(self):
        """记录一次成功请求"""
        with self._get_lock():
            self._consecutive_failures = 0
            self._consecutive_successes += 1

            # 成功后逐步恢复：delay *= recovery_factor（但不低于 base_delay）
            if self._consecutive_successes >= 2:
                self._current_delay = max(
                    self.base_delay,
                    self._current_delay * self.recovery_factor
                )

    def record_failure(self):
        """记录一次失败请求"""
        with self._get_lock():
            self._consecutive_failures += 1
            self._consecutive_successes = 0

            # 失败时指数退避
            if self._consecutive_failures >= 2:
                self._current_delay = min(
                    self.max_delay,
                    self.base_delay * (self.multiplier ** (self._consecutive_failures - 1))
                )

    def wait(self) -> float:
        """执行延迟等待（基于当前状态动态调整）"""
        import time
        import random as _random

        with self._get_lock():
            current_time = time.time()
            elapsed = current_time - self._last_request_time

            # 添加随机抖动 (±25%)
            jitter = _random.uniform(-0.25, 0.25) * self._current_delay
            actual_delay = max(0, self._current_delay + jitter)

            if elapsed < actual_delay:
                time.sleep(actual_delay - elapsed)

            self._last_request_time = time.time()

        return self._current_delay

    def get_status(self) -> dict:
        """获取当前状态"""
        with self._get_lock():
            return {
                'current_delay': self._current_delay,
                'consecutive_failures': self._consecutive_failures,
                'consecutive_successes': self._consecutive_successes,
                'mode': 'RECOVERY' if self._consecutive_successes >= 3 else
                        'BACKOFF' if self._consecutive_failures >= 2 else
                        'NORMAL'
            }


# ==================== 全局请求速率追踪器 ====================

class GlobalRequestTracker:
    """
    全局请求速率追踪器 - 防止触发平台级封禁

    跟踪所有数据源的总请求量，当超过阈值时强制等待。
    与 SourceRateLimiter（单源限流）互补，这是全局限制。
    """

    def __init__(self, max_per_minute: int = 20, max_per_hour: int = 200):
        self.max_per_minute = max_per_minute
        self.max_per_hour = max_per_hour
        self._timestamps: List[float] = []
        self._lock = None

    def _get_lock(self):
        if self._lock is None:
            import threading
            self._lock = threading.Lock()
        return self._lock

    def check_and_wait(self):
        """检查速率，必要时等待直到可以发送请求"""
        import time as _time

        with self._get_lock():
            now = _time.time()
            # 清理过期时间戳
            self._timestamps = [t for t in self._timestamps if now - t < 3600]

            while True:
                now = _time.time()
                recent_1min = [t for t in self._timestamps if now - t < 60]
                recent_1hour = self._timestamps

                if len(recent_1min) < self.max_per_minute and len(recent_1hour) < self.max_per_hour:
                    self._timestamps.append(now)
                    return

                # 计算需要等待的时间
                if len(recent_1min) >= self.max_per_minute:
                    wait_until = recent_1min[0] + 60
                else:
                    wait_until = recent_1hour[0] + 3600

                wait_sec = max(0.5, wait_until - now)
                logger.warning(
                    f"全局速率限制: 1min={len(recent_1min)}/{self.max_per_minute}, "
                    f"1hour={len(recent_1hour)}/{self.max_per_hour}, "
                    f"等待 {wait_sec:.1f}s"
                )
                _time.sleep(wait_sec)

    def get_stats(self) -> dict:
        with self._get_lock():
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < 3600]
            recent_1min = [t for t in self._timestamps if now - t < 60]
            return {
                'requests_last_minute': len(recent_1min),
                'requests_last_hour': len(self._timestamps),
                'max_per_minute': self.max_per_minute,
                'max_per_hour': self.max_per_hour,
            }


_global_request_tracker = None


def get_request_tracker() -> GlobalRequestTracker:
    global _global_request_tracker
    if _global_request_tracker is None:
        _global_request_tracker = GlobalRequestTracker()
    return _global_request_tracker


# ==================== 批次处理器 ====================

class BatchProcessor:
    """
    改进的批次处理器

    特性:
    - 减小批次大小，更频繁的休息
    - 自适应休息时长（根据失败情况增加）
    """

    def __init__(
        self,
        batch_size: int = 5,
        long_rest_after_batches: int = 3,
        long_rest_duration: float = 30.0,
        short_rest_duration: float = 5.0,
        adaptive_long_rest: bool = True
    ):
        """
        初始化批次处理器

        参数:
            batch_size: 每批处理的股票数量
            long_rest_after_batches: 每处理几批后长时间休息
            long_rest_duration: 长休息时长（秒）
            short_rest_duration: 短休息时长（秒）
            adaptive_long_rest: 是否启用自适应长休息
        """
        self.batch_size = batch_size
        self.long_rest_after_batches = long_rest_after_batches
        self.long_rest_duration = long_rest_duration
        self.short_rest_duration = short_rest_duration
        self.adaptive_long_rest = adaptive_long_rest

        self._batch_count = 0
        self._total_processed = 0
        self._total_failures = 0

    def pre_request_wait(self, rate_limiter: AdaptiveRateLimiter, extra_delay: float = 0):
        """请求前等待（结合自适应限流器）"""
        base_wait = rate_limiter.wait()

        if extra_delay > 0:
            time.sleep(extra_delay)

        return base_wait + extra_delay

    def post_request_handling(self, success: bool, rate_limiter: AdaptiveRateLimiter):
        """请求后处理（批次判断、休息）"""
        self._total_processed += 1

        if success:
            rate_limiter.record_success()
            self._batch_count += 1
        else:
            rate_limiter.record_failure()
            self._total_failures += 1

            # 失败后增加长休息概率
            if self.adaptive_long_rest and self._total_failures >= 3:
                self._batch_count = self.long_rest_after_batches  # 触发长休息

        # 判断是否需要长休息
        if self._batch_count >= self.long_rest_after_batches:
            self._batch_count = 0
            actual_rest = self.long_rest_duration

            if self.adaptive_long_rest:
                # 根据失败率调整休息时长
                failure_rate = self._total_failures / max(self._total_processed, 1)
                if failure_rate > 0.3:
                    actual_rest *= 2  # 失败率高时加倍
                elif failure_rate > 0.5:
                    actual_rest *= 3

            logger.info(f"批次长休息 {actual_rest:.0f} 秒 (失败率: {failure_rate:.1%})")
            time.sleep(actual_rest)
        else:
            # 短休息
            time.sleep(self.short_rest_duration)

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            'processed': self._total_processed,
            'failures': self._total_failures,
            'failure_rate': self._total_failures / max(self._total_processed, 1),
            'batches': self._batch_count
        }


# ==================== 全局限流器实例（延迟初始化） ====================

_global_rate_limiter = None
_global_batch_processor = None


def get_global_rate_limiter(config: dict = None) -> AdaptiveRateLimiter:
    """获取或创建全局自适应限流器实例"""
    global _global_rate_limiter

    if _global_rate_limiter is None or config is not None:
        if config is None:
            config = {}

        network_cfg = config.get('network', {})
        _global_rate_limiter = AdaptiveRateLimiter(
            base_delay=network_cfg.get('min_delay_per_stock', 5.0),
            max_delay=network_cfg.get('max_cooldown', 600.0),
            multiplier=network_cfg.get('adaptive_cooldown_multiplier', 2.0),
            recovery_factor=network_cfg.get('recovery_factor', 0.8),
            min_delay=network_cfg.get('min_delay_per_stock', 2.0)
        )

    return _global_rate_limiter


def get_global_batch_processor(config: dict = None) -> BatchProcessor:
    """获取或创建全局批次处理器实例"""
    global _global_batch_processor

    if _global_batch_processor is None or config is not None:
        if config is None:
            config = {}

        network_cfg = config.get('network', {})
        _global_batch_processor = BatchProcessor(
            batch_size=network_cfg.get('batch_size', 5),
            long_rest_after_batches=network_cfg.get('long_rest_after_batches', 3),
            long_rest_duration=network_cfg.get('long_rest_duration', 30.0),
            short_rest_duration=network_cfg.get('min_delay_per_stock', 3.0)
        )

    return _global_batch_processor


# ==================== 线程本地限流器（并行化支持） ====================

_thread_local = threading.local()


def get_thread_rate_limiter(config: dict = None) -> AdaptiveRateLimiter:
    """
    获取当前线程的 RateLimiter（线程独立）

    避免多线程共享同一个限流器导致的锁竞争，
    同时保持各自独立的状态（成功/失败计数）
    """
    if not hasattr(_thread_local, 'rate_limiter'):
        if config is None:
            config = {}

        network_cfg = config.get('network', {})
        _thread_local.rate_limiter = AdaptiveRateLimiter(
            base_delay=network_cfg.get('min_delay_per_stock', 3.0),
            max_delay=network_cfg.get('max_delay_per_stock', 8.0),
            multiplier=network_cfg.get('adaptive_cooldown_multiplier', 2.0),
            recovery_factor=network_cfg.get('recovery_factor', 0.8),
            min_delay=network_cfg.get('min_delay_per_stock', 2.0)
        )

    return _thread_local.rate_limiter


# ==================== 批量行情预过滤 ====================


def get_stocks_basic_info_batch() -> pd.DataFrame:
    """
    批量获取股票基本信息（AkShare 优先，Baostock 备用）

    优点：一次请求返回所有股票，极其快速

    返回 DataFrame 包含：code, name, price, turnover, is_st
    """
    # === 数据源 1: AkShare ===
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()

        # 列名映射
        df = df.rename(columns={
            '代码': 'code',
            '名称': 'name',
            '最新价': 'price',
            '成交额': 'turnover',  # 万元
            '涨跌额': 'change',
            '涨跌幅': 'pct_change'
        })

        # 判断 ST（名称中包含 ST 或 退市）
        df['is_st'] = df['名称'].str.contains('ST|退市', regex=True, na=False)

        # 格式化代码为统一格式（基于代码前缀判断交易所）
        df['code'] = df['代码'] + '.' + df['代码'].apply(
            lambda x: 'SH' if str(x).startswith(('6', '5')) else 'SZ'
        )

        logger.debug(f"批量获取行情成功（AkShare）：{len(df)} 只股票")
        return df

    except Exception as e:
        logger.warning(f"AkShare 批量行情失败: {e}，尝试 Baostock...")

    # === 数据源 2: Baostock 备用 ===
    # 注意：Baostock 没有真正的批量实时行情接口
    # 只查询少量股票作为补充，返回的数据可能不完整
    try:
        import baostock as bs

        bs.login()

        # 获取最近交易日
        today = datetime.now().strftime('%Y-%m-%d')

        # 只获取主要股票的最新行情（sh.6xxxxxx 和 sz.0/3xxxxxx）
        # 每只股票查询最近3天数据取最新，限制数量以保证速度
        results = []
        lookback_days = 3
        max_stocks = 50  # 最多50只

        # 主板/蓝筹股列表
        main_codes = [
            'sh.600000', 'sh.600016', 'sh.600019', 'sh.600028', 'sh.600036',
            'sh.600050', 'sh.600104', 'sh.600109', 'sh.600111', 'sh.600150',
            'sh.600519', 'sh.600887', 'sh.600999', 'sh.601006', 'sh.601012',
            'sh.601088', 'sh.601166', 'sh.601288', 'sh.601318', 'sh.601328',
            'sh.601398', 'sh.601601', 'sh.601628', 'sh.601668', 'sh.601688',
            'sh.601766', 'sh.601818', 'sh.601857', 'sh.601988', 'sh.602012',
            'sz.000001', 'sz.000002', 'sz.000063', 'sz.000066', 'sz.000100',
            'sz.000333', 'sz.000338', 'sz.000425', 'sz.000538', 'sz.000568',
            'sz.000651', 'sz.000858', 'sz.000876', 'sz.000895', 'sz.000983',
            'sz.002001', 'sz.002024', 'sz.002594', 'sz.002714', 'sz.300015'
        ]

        for code in main_codes[:max_stocks]:
            try:
                rs = bs.query_history_k_data_plus(
                    code,
                    "date,close,amount",
                    start_date=(datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d'),
                    end_date=today,
                    frequency="d"
                )
                if rs.error_code == '0':
                    data = rs.get_data()
                    if not data.empty:
                        latest = data.iloc[-1]
                        # 注意：返回数据只有 date/close/amount，没有 code 列，用原始 code 转换
                        bs_code = code.replace('sh.', '').replace('sz.', '').upper()
                        exchange = 'SH' if code.startswith('sh') else 'SZ'
                        results.append({
                            'code': f"{bs_code}.{exchange}",
                            'name': '',
                            'price': float(latest['close']) if latest['close'] else 0,
                            'turnover': float(latest['amount']) / 10000 if latest['amount'] else 0,
                            'change': 0,
                            'pct_change': 0,
                            'is_st': False
                        })
            except Exception:
                logger.debug("获取单只股票行情失败（Baostock），跳过", exc_info=True)
                continue

        bs.logout()

        if results:
            df_baostock = pd.DataFrame(results)
            logger.info(f"批量获取行情成功（Baostock 备用）：{len(df_baostock)} 只股票")
            return df_baostock

    except Exception as e:
        logger.warning(f"Baostock 批量行情也失败: {e}")

    return pd.DataFrame()


def pre_filter_stocks_fast(candidate_list: List[str],
                           config: dict) -> List[str]:
    """
    使用 stock_metadata 中的 ST 标记进行快速预过滤

    在严格本地模式下，不再获取实时行情，仅从 stock_metadata 读取 is_st。
    成交额过滤已移至历史数据计算阶段（initial_filter）。

    参数:
        candidate_list: 候选股票代码列表
        config: 配置字典

    返回:
        过滤后的股票代码列表（仅过滤 ST 股票）
    """
    data_dir = config.get('paths', {}).get('data_dir', './data')

    try:
        from data_layer.market_db import load_st_flags
        st_flags = load_st_flags(data_dir)
        filtered = [code for code in candidate_list if not st_flags.get(code, False)]
        removed = len(candidate_list) - len(filtered)
        if removed > 0:
            logger.info(f"预过滤 ST 股票: {len(candidate_list)} → {len(filtered)} 只")
        return filtered
    except Exception as e:
        logger.debug(f"预过滤失败: {e}，返回原始列表")
        return candidate_list


# ==================== 请求频率控制（兼容旧接口） ====================

def enforce_rate_limit(min_delay: float = 2.0, max_delay: float = 5.0):
    """
    执行请求频率限制（模拟人类操作间隔）

    参数:
        min_delay: 最小延迟时间（秒）
        max_delay: 最大延迟时间（秒）
    """
    last_time = getattr(enforce_rate_limit, '_last_request_time', 0.0)

    current_time = time.time()
    elapsed = current_time - last_time

    # 计算随机延迟（包含人类行为的不规则性）
    random_delay = random.uniform(min_delay, max_delay)

    if elapsed < random_delay:
        sleep_time = random_delay - elapsed
        logger.debug(f"频率控制：等待 {sleep_time:.2f} 秒")
        time.sleep(sleep_time)

    enforce_rate_limit._last_request_time = time.time()


# ==================== Baostock 数据源（备用） ====================

def _ensure_baostock_login(force_relogin: bool = False):
    """
    确保 Baostock 已登录（委托给 DataSourceManager，线程安全）

    参数:
        force_relogin: 是否强制重新登录
    """
    get_source_manager().ensure_bs_login(force_relogin=force_relogin)


def fetch_from_baostock(code: str, start_date: str = None,
                        end_date: str = None, adjust: str = "qfq") -> pd.DataFrame:
    """
    从 Baostock 获取股票历史行情（备用数据源）

    参数:
        code: 股票代码 (格式：600519.SH)
        start_date: 开始日期 (YYYYMMDD)
        end_date: 结束日期 (YYYYMMDD)
        adjust: 复权类型 ('': 不复权，'qfq': 前复权，'hfq': 后复权)

    返回:
        DataFrame 包含：date, open, high, low, close, volume, amount
    """
    if not BAOSTOCK_AVAILABLE:
        return pd.DataFrame()

    source_mgr = get_source_manager()

    # 确保已登录
    _ensure_baostock_login()

    if not source_mgr.is_bs_logged_in():
        logger.warning("Baostock 未登录，无法获取数据")
        return pd.DataFrame()

    # 转换股票代码格式 (600519.SH -> sh.600519)
    if '.' in code:
        symbol_part = code.split('.')[0]
        exchange = code.split('.')[1].lower()
        bs_code = f"{exchange}.{symbol_part}"
    else:
        logger.error(f"股票代码格式错误：{code}")
        return pd.DataFrame()

    # 日期格式转换 (AkShare 用 YYYYMMDD, Baostock 用 YYYY-MM-DD)
    def convert_date_format(date_str):
        if not date_str:
            return None
        # 如果是 YYYYMMDD 格式，转换为 YYYY-MM-DD
        if len(date_str) == 8 and date_str.isdigit():
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        return date_str

    start_date = convert_date_format(start_date)
    end_date = convert_date_format(end_date)

    # 默认日期范围 (Baostock 需要 YYYY-MM-DD 格式)
    if not start_date:
        start_date = "2020-01-01"
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    try:
        # 日线数据字段（不包括 time，分红送股日会有 time 字段但日线不需要）
        fields = "date,open,high,low,close,volume,amount,turn,pctChg"

        # 查询日线数据
        rs = bs.query_history_k_data_plus(
            bs_code,
            fields,
            start_date=start_date,
            end_date=end_date,
            frequency="d",  # 日线
            adjustflag="3" if adjust == 'qfq' else ("2" if adjust == 'hfq' else "1")
        )

        # 检查查询结果是否有效
        if rs is None:
            logger.warning(f"Baostock 查询返回 None：{code}")
            # 重新尝试登录
            get_source_manager().set_bs_logged_in(False)
            _ensure_baostock_login()
            return pd.DataFrame()

        if not hasattr(rs, 'error_code') or rs.error_code != '0':
            error_msg = getattr(rs, 'error_msg', 'Unknown error') if rs else 'None'
            logger.warning(f"Baostock 查询失败：{error_msg}")
            return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])

        # 转换为 DataFrame
        data_list = []
        while rs and rs.next():
            data_list.append(rs.get_row_data())

        if not data_list:
            logger.warning(f"Baostock 未获取到数据：{code}")
            return pd.DataFrame()

        df = pd.DataFrame(data_list, columns=rs.fields)
        
        # 重命名列名为英文
        column_mapping = {
            'date': 'date',
            'open': 'open',
            'high': 'high',
            'low': 'low',
            'close': 'close',
            'volume': 'volume',
            'amount': 'amount',
            'turn': 'turnover_rate',
            'pctChg': 'pct_change'
        }
        
        df = df.rename(columns=column_mapping)
        
        # 转换日期格式
        df['date'] = pd.to_datetime(df['date'])
        
        # 转换数值类型
        numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # 选择需要的列
        required_cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
        available_cols = [col for col in required_cols if col in df.columns]
        df = df[available_cols]
        
        logger.info(f"✓ {code} Baostock 成功获取 {len(df)} 条记录")
        
        return df
        
    except Exception as e:
        logger.error(f"✗ {code} Baostock 获取失败：{str(e)}")
        raise  # 传播异常，让调用方能识别错误类型


# ==================== 增量数据更新引擎 ====================

def fetch_incremental_data(code: str, start_date: str, end_date: str = None,
                           adjust: str = "qfq", max_retries: int = None) -> Optional[pd.DataFrame]:
    """
    获取增量数据（指定日期范围）
    
    参数:
        code: 股票代码
        start_date: 开始日期 (YYYYMMDD)
        end_date: 结束日期 (YYYYMMDD)，默认今天
        adjust: 复权类型
        max_retries: 最大重试次数
    
    返回:
        增量数据 DataFrame，失败返回 None
    """
    try:
        df = fetch_stock_history(
            code, 
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
            max_retries=max_retries
        )
        
        if df is not None and not df.empty:
            return df
        else:
            return None
            
    except Exception as e:
        logger.error(f"{code} 增量数据获取失败：{str(e)}")
        return None


def append_new_data(df_existing: pd.DataFrame, df_new: pd.DataFrame, 
                    code: str) -> pd.DataFrame:
    """
    将新数据追加到现有数据，去重并排序
    
    参数:
        df_existing: 现有数据
        df_new: 新数据
        code: 股票代码
    
    返回:
        合并后的数据
    """
    if df_existing.empty:
        return df_new
    
    if df_new.empty:
        return df_existing
    
    # 合并数据
    df_combined = pd.concat([df_existing, df_new], ignore_index=True)
    
    # 按日期去重（保留最新记录）
    df_combined = df_combined.drop_duplicates(subset=['date'], keep='last')
    
    # 按日期排序
    df_combined = df_combined.sort_values('date').reset_index(drop=True)
    
    logger.info(f"{code} 数据合并完成：原有{len(df_existing)}条，新增{len(df_new)}条，合并后{len(df_combined)}条")
    
    return df_combined


def backfill_missing_data(code: str, missing_dates: List[str], 
                          data_dir: str = "./data", adjust: str = "qfq") -> bool:
    """
    补全缺失的交易日数据
    
    参数:
        code: 股票代码
        missing_dates: 缺失的日期列表
        data_dir: 数据目录
        adjust: 复权类型
    
    返回:
        是否成功补全
    """
    if not missing_dates:
        return True
    
    logger.info(f"{code} 开始补全 {len(missing_dates)} 个缺失交易日...")
    
    # 将缺失日期分组（连续日期合并为一个区间）
    date_ranges = []
    if missing_dates:
        missing_sorted = sorted(missing_dates)
        current_range = [missing_sorted[0]]
        
        for i in range(1, len(missing_sorted)):
            prev_date = datetime.strptime(missing_sorted[i-1], '%Y-%m-%d')
            curr_date = datetime.strptime(missing_sorted[i], '%Y-%m-%d')
            
            # 如果日期连续，加入当前区间
            if (curr_date - prev_date).days <= 3:  # 允许周末间隔
                current_range.append(missing_sorted[i])
            else:
                # 否则开始新区间
                date_ranges.append((current_range[0], current_range[-1]))
                current_range = [missing_sorted[i]]
        
        # 添加最后一个区间
        if current_range:
            date_ranges.append((current_range[0], current_range[-1]))
    
    # 获取每个区间的数据
    all_backfill_data = []
    for start_dt, end_dt in date_ranges:
        # Baostock 需要 YYYY-MM-DD 格式（带破折号）
        start_str = datetime.strptime(start_dt, '%Y-%m-%d').strftime('%Y-%m-%d')
        end_str = datetime.strptime(end_dt, '%Y-%m-%d').strftime('%Y-%m-%d')
        
        logger.debug(f"补全区间：{start_str} 至 {end_str}")
        
        df_chunk = fetch_stock_history(code, start_date=start_str, end_date=end_str, 
                                       adjust=adjust, max_retries=2)
        
        if df_chunk is not None and not df_chunk.empty:
            all_backfill_data.append(df_chunk)
            time.sleep(random.uniform(1.0, 2.0))  # 请求间隔
    
    if not all_backfill_data:
        logger.warning(f"{code} 无法获取任何补全数据")
        return False
    
    # 合并所有补全数据
    df_backfill = pd.concat(all_backfill_data, ignore_index=True)
    df_backfill = df_backfill.drop_duplicates(subset=['date'], keep='last')
    
    logger.info(f"{code} 成功补全 {len(df_backfill)} 条记录")
    
    # 加载现有数据并合并（从 SQLite 读取）
    from data_layer.market_db import get_stock_data as get_db_data, save_stock_data
    df_existing = get_db_data(code, data_dir=data_dir)
    if df_existing is not None and not df_existing.empty:
        df_combined = append_new_data(df_existing, df_backfill, code)
    else:
        df_combined = df_backfill
    save_stock_data(code, df_combined, data_dir)

    # 更新元数据
    last_date = df_combined['date'].max().strftime('%Y-%m-%d')
    update_stock_metadata(code, last_date, len(df_combined), data_dir)
    
    return True


def incremental_update(code: str, data_dir: str = "./data",
                       adjust: str = "qfq", force_full: bool = False,
                       selected_stocks: Optional[List[str]] = None) -> pd.DataFrame:
    """
    执行增量更新逻辑（SQLite 版本）

    流程:
    1. 从 SQLite 读取现有数据
    2. 计算需要更新的日期范围
    3. 获取增量数据
    4. 写入 SQLite
    5. 更新元数据

    参数:
        code: 股票代码
        data_dir: 数据目录
        adjust: 复权类型
        force_full: 是否强制全量更新
        selected_stocks: 选出的 top_n 股票列表，非空时仅对这些股票执行增量更新

    返回:
        更新后的完整数据
    """
    from data_layer.market_db import get_stock_data as get_db_data, save_stock_data, update_metadata

    # === 步骤 0: 检查是否需要增量更新 ===
    if selected_stocks is not None and code not in selected_stocks:
        logger.info(f"{code} 不在选出的 top_n 股票列表中，跳过增量更新")
        df_existing = get_db_data(code, data_dir=data_dir)
        return df_existing if df_existing is not None and not df_existing.empty else pd.DataFrame()

    # === 步骤 1: 从 SQLite 读取现有数据 ===
    df_existing = get_db_data(code, data_dir=data_dir)
    stock_meta = get_stock_metadata(code, data_dir)

    today = datetime.now()
    today_str = today.strftime('%Y%m%d')

    # === 步骤 2: 判断更新模式 ===
    if force_full or df_existing is None or df_existing.empty or stock_meta is None:
        logger.info(f"{code} 首次获取或强制全量更新，拉取自 2020-01-01 起的全部数据...")
        start_date = '20200101'

        df_full = fetch_stock_history(code, start_date=start_date, end_date=today_str,
                                      adjust=adjust)

        if not df_full.empty:
            save_stock_data(code, df_full, data_dir)
            last_date = df_full['date'].max().strftime('%Y-%m-%d')
            update_metadata(code, data_dir=data_dir,
                           last_update_date=last_date,
                           record_count=len(df_full),
                           update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                           update_mode='full')
            logger.info(f"{code} 全量更新完成：{len(df_full)}条记录")
            update_checkpoint(code, success=True, last_date=last_date)
        return df_full if not df_full.empty else pd.DataFrame()

    # === 增量更新模式 ===
    last_update_str = stock_meta.get('last_update_date', '')

    try:
        last_update_date = datetime.strptime(last_update_str, '%Y-%m-%d')
    except ValueError:
        logger.warning(f"{code} 元数据日期格式错误，切换到全量更新")
        last_update_date = today - timedelta(days=730)

    incremental_start = (last_update_date + timedelta(days=1)).strftime('%Y%m%d')

    if incremental_start > today_str:
        logger.info(f"{code} 数据已是最新 (最后更新：{last_update_str})")
        return df_existing

    logger.info(f"Incremental update: 请求 {incremental_start} 至 {today_str} 的数据...")

    # === 步骤 3: 获取增量数据 ===
    try:
        df_incremental = fetch_incremental_data(
            code,
            start_date=incremental_start,
            end_date=today_str,
            adjust=adjust
        )

        if df_incremental is not None and not df_incremental.empty:
            # === 步骤 4: 追加新数据 ===
            df_updated = append_new_data(df_existing, df_incremental, code)
            save_stock_data(code, df_updated, data_dir)

            # === 步骤 5: 检查完整性 ===
            is_complete, missing = check_data_integrity(df_updated, code)

            # === 步骤 6: 更新元数据 ===
            last_date = df_updated['date'].max().strftime('%Y-%m-%d')
            update_metadata(code, data_dir=data_dir,
                           last_update_date=last_date,
                           record_count=len(df_updated),
                           update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                           update_mode='incremental',
                           incremental_days=(today - last_update_date).days)

            days_fetched = (today - last_update_date).days
            logger.info(f"Incremental update: {days_fetched} days data fetched for {code}")
            update_checkpoint(code, success=True, last_date=last_date)
            return df_updated

        else:
            logger.warning(f"{code} 增量数据为空，可能网络问题或已停牌")
            update_checkpoint(code, success=False, error_msg="增量数据为空")
            return df_existing

    except Exception as e:
        # === 异常处理：降级为全量拉取 ===
        logger.warning(f"{code} 增量更新失败 ({type(e).__name__}: {str(e)[:100]}...)，降级为全量更新...")

        start_date = '20200101'
        df_fallback = fetch_stock_history(code, start_date=start_date, end_date=today_str,
                                          adjust=adjust)

        if not df_fallback.empty:
            save_stock_data(code, df_fallback, data_dir)
            last_date = df_fallback['date'].max().strftime('%Y-%m-%d')
            update_metadata(code, data_dir=data_dir,
                           last_update_date=last_date,
                           record_count=len(df_fallback),
                           update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                           update_mode='fallback')
            update_checkpoint(code, success=True, last_date=last_date)
            logger.warning(f"{code} 降级更新完成：{len(df_fallback)}条记录")
            return df_fallback
        else:
            update_checkpoint(code, success=False, error_msg=f"降级失败: {str(e)[:50]}")
            logger.error(f"{code} 降级更新也失败，返回现有数据")
            return df_existing


# ==================== 数据获取（增强版） ====================

def fetch_stock_history(code: str, start_date: str = None,
                        end_date: str = None, adjust: str = "qfq",
                        max_retries: int = None,
                        base_delay: float = 2.0,
                        timeout: int = 30,
                        use_baostock_fallback: bool = True,
                        prefer_baostock: bool = True,
                        aggressive_switch: bool = True) -> pd.DataFrame:
    """
    获取股票历史行情 (日线) - 多数据源并行轮询版 v2

    特性:
    - 5数据源：Baostock / AkShare / 腾讯 / 东方财富 / 新浪(网易)
    - 每个数据源独立限流器，一个源被限流不影响其他源
    - 按健康分数动态排序，优先使用最稳定的源
    - 单 attempt 内轮询所有源，失败立即切换，不等待全局延迟
    - 重试次数提升至 5 次

    参数:
        code: 股票代码 (格式：600519.SH)
        start_date: 开始日期 (YYYYMMDD)，默认 20200101
        end_date: 结束日期 (YYYYMMDD)，默认今天
        adjust: 复权类型 ('': 不复权，'qfq': 前复权，'hfq': 后复权)
        max_retries: 最大重试次数 (默认从配置读取，fallback 3)
        base_delay: 基础延迟范围 (默认 2.0 秒)
        timeout: 请求超时时间 (秒，默认 30 秒)
        use_baostock_fallback: 是否启用 Baostock 备用数据源
        prefer_baostock: 是否优先使用 Baostock（默认 True）
        aggressive_switch: 是否启用快速切换模式（默认 True）

    返回:
        DataFrame 包含：date, open, high, low, close, volume, amount
    """
    if max_retries is None:
        max_retries = get_max_retries()

    # 分离代码和交易所
    if '.' in code:
        symbol = code.split('.')[0]
#         exchange = code.split('.')[1].lower()
    else:
        logger.error(f"股票代码格式错误：{code}")
        return pd.DataFrame()

    # 默认日期范围
    if not start_date:
        start_date = "20200101"
    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")

    # 获取数据源管理器（带独立限流器）
    source_mgr = get_source_manager()

    # 定义各数据源获取函数
    def fetch_from_akshare_impl():
        if not AKSHARE_AVAILABLE:
            return pd.DataFrame()
        df = ak.stock_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=start_date, end_date=end_date, adjust=adjust
        )
        if df is not None and not df.empty and len(df) > 0:
            col_map = {
                '日期': 'date', '开盘': 'open', '最高': 'high',
                '最低': 'low', '收盘': 'close', '成交量': 'volume',
                '成交额': 'amount', '振幅': 'amplitude',
                '涨跌幅': 'pct_change', '涨跌额': 'change',
                '换手率': 'turnover_rate'
            }
            df = df.rename(columns=col_map)
            df['date'] = pd.to_datetime(df['date'])
            need = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
            avail = [c for c in need if c in df.columns]
            return df[avail]
        return pd.DataFrame()

    def fetch_from_tencent_impl():
        if not TENCENT_AVAILABLE:
            return pd.DataFrame()
        def fmt(d):
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d
        df = fetch_from_tencent(code, start_date=fmt(start_date),
                                end_date=fmt(end_date), adjust=adjust)
        if df is not None and not df.empty:
            if 'amount' not in df.columns and 'close' in df.columns and 'volume' in df.columns:
                df['amount'] = df['close'] * df['volume']
            need = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
            avail = [c for c in need if c in df.columns]
            return df[avail]
        return pd.DataFrame()

    def fetch_from_eastmoney_impl():
        if not EASTMONEY_AVAILABLE:
            return pd.DataFrame()
        df = fetch_from_eastmoney(code, start_date=start_date,
                                  end_date=end_date, adjust=adjust)
        if df is not None and not df.empty:
            need = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
            avail = [c for c in need if c in df.columns]
            return df[avail]
        return pd.DataFrame()

    # 数据源 → 获取函数的映射
    source_fetchers = {
        'baostock': lambda: fetch_from_baostock(code, start_date, end_date, adjust),
        'akshare': fetch_from_akshare_impl,
        'tencent': fetch_from_tencent_impl,
        'eastmoney': fetch_from_eastmoney_impl,
    }

    # 重试逻辑
    any_success_this_run = False
    for attempt in range(max_retries):
        # 每轮重试都按当前健康度重新排序数据源
        source_order = source_mgr.get_priority_order(prefer_baostock=prefer_baostock)

        if attempt > 0:
            logger.info(f"{code} 第 {attempt + 1}/{max_retries} 轮重试，数据源优先级: {source_order}")

        # 当前 attempt 内依次尝试所有数据源
        best_df = None  # 记录当前 attempt 中数据量最多的结果
        for source in source_order:
            limiter = source_mgr.get_limiter(source)
            if limiter is None:
                continue

            # 全局速率检查（防止触发平台级封禁）
            get_request_tracker().check_and_wait()

            # 使用该数据源独立的限流器（一个源被限流不影响其他源）
            limiter.wait()

            # 跳过被标记为服务端拒绝的 Baostock
            if source == 'baostock' and source_mgr.is_server_reject(source) and aggressive_switch:
                logger.debug(f"{code} 跳过 Baostock (服务端拒绝标记)")
                continue

            try:
                fetch_fn = source_fetchers.get(source)
                if fetch_fn is None:
                    continue

                log_level = logging.INFO if source in ('baostock', 'tencent', 'eastmoney') else logging.DEBUG
                logger.log(log_level, f"{code} 尝试从 {source.upper()} 获取...")

                df = fetch_fn()

                if df is not None and not df.empty and len(df) > 0:
                    logger.info(f"✓ {code} {source.upper()} 成功获取 {len(df)} 条记录")
                    source_mgr.record_source_result(source, success=True)
                    source_mgr.set_server_reject(source, False)
                    any_success_this_run = True
                    # 记录最优结果，继续尝试其他源取数据量最多的
                    if best_df is None or len(df) > len(best_df):
                        best_df = df
                        logger.info(f"{code} 当前最优: {source.upper()} ({len(df)}条)")
                    continue

                # 空结果视为失败（但可能是服务端拒绝）
                if source == 'baostock' and BAOSTOCK_AVAILABLE:
                    source_mgr.set_server_reject(source, True)
                    logger.warning(f"{code} Baostock 服务端拒绝 (空返回)")

                source_mgr.record_source_result(source, success=False)

            except Exception as e:
#                 last_error = e
                source_mgr.record_source_result(source, success=False)
                error_msg = str(e)

                # Baostock 网络接收错误特殊处理
                if source == 'baostock' and ('网络接收错误' in error_msg or '查询失败' in error_msg):
                    source_mgr.set_server_reject(source, True)
                    logger.warning(f"{code} Baostock 网络错误，标记跳过")

                logger.debug(f"{code} {source.upper()} 异常: {error_msg[:80]}")
                # 立即继续下一个源，不等待
                continue

        # 当前 attempt 结束，返回数据量最多的结果
        if best_df is not None and not best_df.empty:
            logger.info(f"{code} 本轮最优结果: {len(best_df)} 条记录")
            return best_df

        # 当前 attempt 所有源都失败
        if attempt < max_retries - 1:
            # 两轮重试之间使用全局限流器做间隔（给服务端喘息时间）
            global_limiter = get_global_rate_limiter()
            global_limiter.record_failure()
            wait_time = global_limiter.wait()
            logger.warning(
                f"{code} 本轮 {len(source_order)} 个源均失败，"
                f"全局冷却 {wait_time:.1f}s 后进入第 {attempt + 2} 轮..."
            )

    # 所有重试耗尽
    status_summary = source_mgr.get_all_status()
    healthy_sources = [n for n, s in status_summary.items() if s.get('health_score', 0) > 0.3]
    logger.error(
        f"✗ {code} 最终无法获取数据 ["
        f"重试{max_retries}轮, 健康源:{healthy_sources}, "
        f"任一成功:{any_success_this_run}]"
    )
    update_checkpoint(
        code, success=False,
        error_msg=f"所有{max_retries}轮重试失败",
        cache_exists=False
    )
    return pd.DataFrame()


def merge_and_save_incremental(
    new_df: pd.DataFrame,
    code: str,
    data_dir: str,
    source_name: str,
) -> int:
    """增量数据与现有数据合并，写入 SQLite 和元数据。

    Returns:
        合并后的总记录数
    """
    from data_layer.market_db import get_stock_data as get_db_data, save_stock_data, update_metadata

    existing = get_db_data(code, data_dir=data_dir)
    if existing is not None and not existing.empty:
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=['date'], keep='last')
        combined = combined.sort_values('date').reset_index(drop=True)
    else:
        combined = new_df

    save_stock_data(code, combined, data_dir)

    last_date = combined['date'].max().strftime('%Y-%m-%d')
    update_metadata(code, data_dir=data_dir,
                   last_update_date=last_date,
                   record_count=len(combined),
                   update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                   update_mode=source_name, source=source_name)
    return len(combined)


def get_stock_data(code: str, data_dir: str = "./data",
                   use_cache: bool = True, fallback_to_cache: bool = True,
                   prefer_baostock: bool = True,
                   enable_incremental: bool = True,
                   force_full: bool = False,
                   selected_stocks: Optional[List[str]] = None,
                   **kwargs) -> pd.DataFrame:
    """
    获取股票数据 (优先 SQLite，支持双数据源自动切换，增量更新引擎)

    参数:
        code: 股票代码
        data_dir: 数据目录
        use_cache: 是否使用缓存
        fallback_to_cache: 网络失败时是否降级使用过期缓存
        prefer_baostock: 是否优先使用 Baostock（默认 True，更稳定）
        enable_incremental: 是否启用增量更新（默认启用）
        force_full: 是否强制全量更新（忽略增量逻辑）
        selected_stocks: 选出的 top_n 股票列表，仅对这些股票执行增量更新
        **kwargs: 传递给 fetch_stock_history 的参数

    返回:
        DataFrame 包含历史行情
    """
    # === 优先从 SQLite 读取 ===
    try:
        from data_layer.market_db import get_stock_data as get_db_data
        df_db = get_db_data(code, data_dir=data_dir)
        if df_db is not None and len(df_db) > 0:
            last_date = df_db['date'].max()
            days_diff = (datetime.now() - last_date).days
            if days_diff <= 30:
                logger.info(f"✓ {code} 使用 SQLite 数据 (最后更新：{last_date.strftime('%Y-%m-%d')})")
                return df_db
            elif not enable_incremental:
                logger.info(f"✓ {code} 使用本地缓存 ({days_diff}天前, 增量更新已禁用)")
                return df_db
            else:
                logger.info(f"{code} SQLite 数据已过时 ({days_diff}天前), 尝试更新...")
    except Exception as e:
        logger.debug(f"SQLite 读取失败: {e}")

    # === 增量更新模式（仅针对选出的 top_n 股票）===
    if enable_incremental and not force_full:
        logger.info(f"{code} 使用增量更新引擎...")
        return incremental_update(code, data_dir=data_dir, adjust='qfq', force_full=False,
                                  selected_stocks=selected_stocks)

    # === 网络获取 ===
    logger.info(f"正在获取 {code} 的最新数据 (prefer_baostock={prefer_baostock})...")
    df = fetch_stock_history(
        code,
        prefer_baostock=prefer_baostock,
        aggressive_switch=True,
        use_baostock_fallback=True,
        **kwargs
    )

    # 保存到 SQLite
    if len(df) > 0:
        try:
            from data_layer.market_db import save_stock_data, update_metadata
            save_stock_data(code, df, data_dir)
            last_date = df['date'].max().strftime('%Y-%m-%d')
            update_metadata(code, data_dir=data_dir,
                           last_update_date=last_date,
                           record_count=len(df),
                           update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                           update_mode='full_legacy')
        except Exception as e:
            logger.debug(f"SQLite 写入失败: {e}")

        return df
    else:
        # 网络获取失败，降级使用 SQLite（即使数据较旧）
        try:
            from data_layer.market_db import get_stock_data as get_db_data
            df_db = get_db_data(code, data_dir=data_dir)
            if df_db is not None and len(df_db) > 0:
                last_date = df_db['date'].max()
                logger.warning(
                    f"⚠ {code} 网络获取失败，降级使用 SQLite 数据 "
                    f"(最后更新：{last_date.strftime('%Y-%m-%d')})"
                )
                return df_db
        except Exception:
            logger.warning("从 SQLite 读取 %s 失败，尝试从网络获取", code, exc_info=True)

        logger.error(f"✗ {code} 无法获取任何数据 (SQLite 和网络均失败)")
        return pd.DataFrame()


# ==================== 数据清洗 ====================

def clean_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    清洗数据：去除异常值、停牌日、填充缺失值
    
    参数:
        df: 原始 DataFrame
    
    返回:
        清洗后的 DataFrame
    """
    if df.empty:
        return df
    
    df = df.copy()
    
    # 按日期排序
    df = df.sort_values('date').reset_index(drop=True)
    
    # 去除完全为空的行
    df = df.dropna(how='all')
    
    # 检查价格合理性 (价格不能为负或零)
    price_cols = ['open', 'high', 'low', 'close']
    for col in price_cols:
        if col in df.columns:
            invalid_mask = df[col] <= 0
            if invalid_mask.any():
                logger.warning(f"发现 {invalid_mask.sum()} 条异常价格记录，已移除")
                df = df[~invalid_mask]
    
    # 填充缺失值 (使用前向填充)
    numeric_cols = df.select_dtypes(include=[np.number]).columns
    df[numeric_cols] = df[numeric_cols].ffill()
    
    # 仍有缺失值的行删除
    df = df.dropna()
    
    # 重置索引
    df = df.reset_index(drop=True)
    
    logger.info(f"数据清洗完成，剩余 {len(df)} 条有效记录")
    
    return df



def is_valid_a_stock_code(code: str) -> bool:
    """
    验证是否为有效的 A 股股票代码
    
    A 股代码规则:
    - 沪市 A 股：600xxx, 601xxx, 603xxx, 605xxx (SH)
    - 深市 A 股：000xxx, 001xxx, 002xxx(中小板), 003xxx (SZ)
    - 创业板：300xxx, 301xxx (SZ)
    - 科创板：688xxx, 689xxx (SH)
    
    排除:
    - B 股 (900xxx, 200xxx)
    - 退市股票
    - 其他市场股票
    
    参数:
        code: 股票代码（格式：600519.SH）
    
    返回:
        是否为有效的 A 股代码
    """
    if not code or '.' not in code:
        return False
    
    # 提取数字部分和交易所后缀
    parts = code.split('.')
    if len(parts) != 2:
        return False
    
    symbol, exchange = parts
    
    # 验证交易所后缀
    if exchange not in ['SH', 'SZ']:
        return False
    
    # 验证代码长度和格式
    if not symbol.isdigit() or len(symbol) != 6:
        return False
    
    # 沪市 A 股验证
    if exchange == 'SH':
        # 沪市 A 股：600, 601, 603, 605, 688, 689 开头
        valid_prefixes_sh = ['600', '601', '603', '605', '688', '689']
        return any(symbol.startswith(prefix) for prefix in valid_prefixes_sh)
    
    # 深市 A 股验证
    if exchange == 'SZ':
        # 深市 A 股：000, 001, 002, 003, 300, 301 开头
        valid_prefixes_sz = ['000', '001', '002', '003', '300', '301']
        return any(symbol.startswith(prefix) for prefix in valid_prefixes_sz)
    
    return False


def get_all_a_stocks(force_refresh: bool = False, prefer_baostock: bool = True) -> pd.DataFrame:
    """
    获取所有 A 股股票列表 (沪深两市) - 安全版

    改进:
    1. 优先使用 Baostock（更稳定，对请求频率限制宽松）
    2. 沪深分别获取，交易所间休息
    3. AkShare 失败后不再继续（快速失败）

    参数:
        force_refresh: 是否强制刷新缓存
        prefer_baostock: 是否优先使用 Baostock（默认 True）

    返回:
        DataFrame 包含：code, name, exchange, list_date 等
    """
    logger.info("正在获取全市场 A 股列表（安全模式）...")

    # 预延迟（模拟打开浏览器、输入网址的时间）
    logger.info("预延迟 10 秒...")
    time.sleep(random.uniform(8.0, 12.0))

    results = []
    success = False

    # === 优先使用 Baostock（更稳定） ===
    if prefer_baostock and BAOSTOCK_AVAILABLE:
        logger.info("从 Baostock 获取 A 股列表（优先）...")

        try:
            _ensure_baostock_login()

            if get_source_manager().is_bs_logged_in():
                # 分两次获取（沪市、深市）
                for exchange_code, exchange_name in [('sh', 'SH'), ('sz', 'SZ')]:
                    logger.info(f"获取 {exchange_name} 市股票...")

                    rs = bs.query_all_stock(exchange_code)

                    # 检查 rs 是否有效
                    if rs is None or not hasattr(rs, 'error_code'):
                        logger.warning(f"Baostock {exchange_name} 市查询返回无效结果")
                        continue

                    if rs.error_code == '0':
                        data_list = []
                        while rs.next():
                            data_list.append(rs.get_row_data())

                        if data_list:
                            df = pd.DataFrame(data_list, columns=rs.fields)
                            if 'code' in df.columns:
                                for _, row in df.iterrows():
                                    code = str(row['code']).zfill(6)
                                    full_code = f"{code}.{exchange_name}"

                                    if is_valid_a_stock_code(full_code):
                                        results.append({
                                            'code': full_code,
                                            'name': row.get('code_name', ''),
                                            'exchange': exchange_name
                                        })

                    # 交易所间休息
                    rest_time = random.uniform(3.0, 5.0)
                    logger.info(f"{exchange_name} 市完成，休息 {rest_time:.1f} 秒...")
                    time.sleep(rest_time)

                if results:
                    logger.info(f"Baostock 成功获取 {len(results)} 只 A 股")
                    success = True

        except Exception as e:
            logger.error(f"Baostock 获取失败：{str(e)}")
            # Baostock 失败后不再尝试 AkShare（快速失败）
            logger.warning("Baostock 失败，不再尝试 AkShare（快速失败策略）")
            success = False

    # === 只有在 Baostock 失败时才尝试 AkShare ===
    if not success and AKSHARE_AVAILABLE:
        logger.info("尝试从 AkShare 获取 A 股列表（备用）...")

        try:
            # 添加延迟
            enforce_rate_limit(min_delay=5.0, max_delay=10.0)

            # 获取沪市 A 股列表
            logger.info("获取沪市 A 股列表...")
            df_sh = ak.stock_info_sh_name_code()

            # 处理沪市股票
            if 'code' in df_sh.columns:
                for _, row in df_sh.iterrows():
                    code = str(row['code']).zfill(6)
                    full_code = f"{code}.SH"

                    if is_valid_a_stock_code(full_code):
                        name = row.get('name', '')
                        results.append({'code': full_code, 'name': name, 'exchange': 'SH'})

            # 沪深间休息
            rest_time = random.uniform(5.0, 8.0)
            logger.info(f"沪市完成，休息 {rest_time:.1f} 秒...")
            time.sleep(rest_time)

            # 获取深市 A 股列表
            logger.info("获取深市 A 股列表...")
            df_sz = ak.stock_info_a_code_name()

            # 处理深市股票
            if 'code' in df_sz.columns:
                for _, row in df_sz.iterrows():
                    code = str(row['code']).zfill(6)
                    full_code = f"{code}.SZ"

                    if is_valid_a_stock_code(full_code):
                        name = row.get('name', '')
                        results.append({'code': full_code, 'name': name, 'exchange': 'SZ'})

            if results:
                logger.info(f"AkShare 成功获取 {len(results)} 只 A 股")
                success = True

        except Exception as e:
            logger.error(f"AkShare 最终也失败：{str(e)}")
            # AkShare 失败后直接返回空，不再继续
            success = False

    # === 结果处理 ===
    if not results:
        logger.warning("未从任何数据源获取到有效的 A 股代码")
        # 返回备选方案：使用常见 A 股
        fallback_stocks = [
            "600519.SH", "000858.SZ", "601318.SH", "000333.SZ", "600036.SH",
            "000651.SZ", "002415.SZ", "601166.SH", "600276.SH", "300750.SZ"
        ]
        logger.warning(f"使用备选 A 股列表：{len(fallback_stocks)}只")
        return pd.DataFrame({
            'code': fallback_stocks,
            'name': [''] * len(fallback_stocks),
            'exchange': [code.split('.')[1] for code in fallback_stocks]
        })

    df_all = pd.DataFrame(results)

    # 二次过滤：确保所有代码都符合 A 股规范
    initial_count = len(df_all)
    df_all = df_all[df_all['code'].apply(is_valid_a_stock_code)]
    filtered_count = initial_count - len(df_all)

    if filtered_count > 0:
        logger.info(f"二次过滤移除 {filtered_count} 只非 A 股股票")

    # 按交易所和代码排序
    df_all = df_all.sort_values(['exchange', 'code']).reset_index(drop=True)

    logger.info(f"✓ 成功获取 {len(df_all)} 只 A 股股票 (沪市{len(df_all[df_all['exchange']=='SH'])}只，深市{len(df_all[df_all['exchange']=='SZ'])}只)")

    return df_all


def download_all_stocks(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    data_dir: str = "./data",
    resume: bool = True,
    batch_size: int = 100,
    max_stocks: Optional[int] = None,
    min_delay: float = 10.0,
    max_delay: float = 20.0,
    skip_min_records: int = 100
) -> Dict:
    """
    全市场历史数据下载到 SQLite

    参数:
        start_date: 起始日期 (YYYY-MM-DD 或 YYYYMMDD)，默认 3 年前的 1 月 1 日
        end_date: 结束日期 (YYYY-MM-DD 或 YYYYMMDD)，默认今天
        data_dir: 数据目录
        resume: 是否断点续传（跳过已完整下载的股票）
        batch_size: 每 N 只报告一次进度
        max_stocks: 限制下载数量（调试用）
        min_delay: 单只股票间最小延迟（秒）
        max_delay: 单只股票间最大延迟（秒）
        skip_min_records: 断点续传跳过的最小记录数

    返回:
        {'total', 'success', 'failed', 'skipped', 'elapsed_seconds'}
    """
    from data_layer.market_db import save_stock_data, update_metadata, get_all_metadata, init_db

    init_db(data_dir)

    today = datetime.now()

    # 默认起始日期：3 年前的 1 月 1 日
    if not start_date:
        start_date = f"{today.year - 3}-01-01"
    if not end_date:
        end_date = today.strftime('%Y-%m-%d')

    # 统一转为 YYYYMMDD 格式传给 fetch_stock_history
    def _fmt(d: str) -> str:
        if '-' in d:
            return d.replace('-', '')
        return d

#     start_fmt = _fmt(start_date)
#     end_fmt = _fmt(end_date)

    logger.info("=" * 60)
    logger.info("开始全市场数据下载")
    logger.info(f"日期范围: {start_date} ~ {end_date}")
    logger.info(f"断点续传: {resume}, 批次大小: {batch_size}")
    logger.info("=" * 60)

    # 获取全市场股票列表
    try:
        df_stocks = get_all_a_stocks()
        all_codes = df_stocks['code'].tolist()
    except Exception as e:
        logger.error(f"获取全市场股票列表失败: {e}")
        return {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0, 'elapsed_seconds': 0}

    if max_stocks:
        all_codes = all_codes[:max_stocks]

    total = len(all_codes)
    logger.info(f"全市场股票总数: {total}")

    # 获取已下载的元数据
    existing_meta = get_all_metadata(data_dir) if resume else {}
    logger.info(f"数据库中已有 {len(existing_meta)} 只股票")

    # 过滤已完成的（数据足够新且有足够记录数则跳过）
    codes_to_download = []
    skipped_codes = []
    for code in all_codes:
        meta = existing_meta.get(code)
        if meta and resume:
            last_date = meta.get('last_update_date', '')
            record_count = meta.get('record_count', 0) or 0
            if last_date and record_count >= skip_min_records:
                try:
                    last_dt = datetime.strptime(last_date, '%Y-%m-%d')
                    days_since = (today - last_dt).days
                    # 90 天内更新过且有足够记录，视为已完整
                    if days_since <= 90:
                        skipped_codes.append(code)
                        continue
                except ValueError:
                    pass
        codes_to_download.append(code)

    skipped = len(skipped_codes)
    logger.info(f"已跳过（已完整）: {skipped}, 待下载: {len(codes_to_download)}")

    success_count = 0
    failed_count = 0
    start_time = time.time()

    for i, code in enumerate(codes_to_download, 1):
        try:
            logger.info(f"[{i}/{len(codes_to_download)}] 下载 {code} ...")

            # 直接调用腾讯API（当前唯一稳定数据源），避免多源轮询的超时等待
            from data_layer.tencent_fetcher import fetch_from_tencent
            df = fetch_from_tencent(
                code,
                start_date=start_date,
                end_date=end_date,
                adjust='qfq'
            )

            if df is not None and not df.empty:
                # 补充 amount 列（腾讯不返回成交额）
                if 'amount' not in df.columns and 'close' in df.columns and 'volume' in df.columns:
                    df['amount'] = df['close'] * df['volume']

                save_stock_data(code, df, data_dir)
                last_date = df['date'].max().strftime('%Y-%m-%d')
                update_metadata(
                    code, data_dir=data_dir,
                    last_update_date=last_date,
                    record_count=len(df),
                    update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    update_mode='batch_download'
                )
                success_count += 1
                logger.info(f"  ✓ {code} 成功: {len(df)} 条, 至 {last_date}")
            else:
                failed_count += 1
                logger.warning(f"  ✗ {code} 返回空数据")

        except Exception as e:
            failed_count += 1
            logger.error(f"  ✗ {code} 失败: {e}")

        # 进度报告
        if i % batch_size == 0:
            elapsed = time.time() - start_time
            avg = elapsed / i if i > 0 else 0
            remain = avg * (len(codes_to_download) - i)
            logger.info(f"--- 进度: {i}/{len(codes_to_download)} "
                       f"成功:{success_count} 失败:{failed_count} 跳过:{skipped} "
                       f"已用:{elapsed/60:.1f}min 剩余预估:{remain/60:.1f}min ---")

        # 请求间隔（最后一只不等待）
        if i < len(codes_to_download):
            delay = random.uniform(min_delay, max_delay)
            time.sleep(delay)

    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("全市场数据下载完成")
    logger.info(f"总计: {total}, 成功: {success_count}, 失败: {failed_count}, 跳过: {skipped}")
    logger.info(f"总耗时: {elapsed/60:.1f} 分钟")
    logger.info("=" * 60)

    # 下载完成后同步更新 ST 标记
    st_result = update_st_flags(data_dir)
    logger.info(f"ST 标记更新: {st_result}")

    return {
        'total': total,
        'success': success_count,
        'failed': failed_count,
        'skipped': skipped,
        'elapsed_seconds': round(elapsed, 1)
    }


def update_all_stocks(
    data_dir: str = "./data",
    days_threshold: int = 30,
    batch_size: int = 100
) -> Dict:
    """
    全市场增量更新

    参数:
        data_dir: 数据目录
        days_threshold: 超过 N 天未更新才拉取
        batch_size: 每 N 只报告一次进度

    返回:
        {'total', 'updated', 'skipped', 'failed', 'elapsed_seconds'}
    """
    from data_layer.market_db import get_all_metadata, init_db

    init_db(data_dir)

    today = datetime.now()
#     today_str = today.strftime('%Y-%m-%d')

    logger.info("=" * 60)
    logger.info("开始全市场增量更新")
    logger.info(f"更新阈值: 超过 {days_threshold} 天未更新的股票")
    logger.info("=" * 60)

    # 获取所有已下载的元数据
    all_meta = get_all_metadata(data_dir)
    if not all_meta:
        logger.warning("数据库为空，没有可更新的股票")
        return {'total': 0, 'updated': 0, 'skipped': 0, 'failed': 0, 'elapsed_seconds': 0}

    codes_to_update = []
    skipped_codes = []

    for code, meta in all_meta.items():
        last_update = meta.get('last_update_date', '')
        if not last_update:
            codes_to_update.append(code)
            continue

        try:
            last_dt = datetime.strptime(last_update, '%Y-%m-%d')
            days_diff = (today - last_dt).days
            if days_diff > days_threshold:
                codes_to_update.append(code)
            else:
                skipped_codes.append(code)
        except ValueError:
            codes_to_update.append(code)

    total = len(all_meta)
    logger.info(f"数据库中共有 {total} 只股票")
    logger.info(f"需要更新: {len(codes_to_update)}, 已最新: {len(skipped_codes)}")

    updated_count = 0
    failed_count = 0
    start_time = time.time()

    for i, code in enumerate(codes_to_update, 1):
        try:
            logger.info(f"[{i}/{len(codes_to_update)}] 增量更新 {code} ...")
            df = incremental_update(code, data_dir=data_dir)
            if df is not None and not df.empty:
                updated_count += 1
                logger.info(f"  ✓ {code} 更新完成: {len(df)} 条")
            else:
                failed_count += 1
                logger.warning(f"  ✗ {code} 更新返回空数据")
        except Exception as e:
            failed_count += 1
            logger.error(f"  ✗ {code} 更新失败: {e}")

        if i % batch_size == 0:
            elapsed = time.time() - start_time
            logger.info(f"--- 进度: {i}/{len(codes_to_update)} "
                       f"更新:{updated_count} 失败:{failed_count} 已用:{elapsed/60:.1f}min ---")

        if i < len(codes_to_update):
            time.sleep(random.uniform(10.0, 20.0))

    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("全市场增量更新完成")
    logger.info(f"总计: {total}, 更新: {updated_count}, 失败: {failed_count}, 已最新: {len(skipped_codes)}")
    logger.info(f"总耗时: {elapsed/60:.1f} 分钟")
    logger.info("=" * 60)

    return {
        'total': total,
        'updated': updated_count,
        'failed': failed_count,
        'skipped': len(skipped_codes),
        'elapsed_seconds': round(elapsed, 1)
    }


# ==================== 智能下载 + ST 标记更新 ====================


def update_st_flags(data_dir: str = "./data") -> Dict:
    """
    从网络获取全市场 ST 标记并更新到 stock_metadata.is_st

    参数:
        data_dir: 数据目录

    返回:
        {'updated', 'failed'}
    """
    from data_layer.market_db import _get_db_path, init_db

    init_db(data_dir)
    db_path = _get_db_path(data_dir)

    try:
        df = get_stocks_basic_info_batch()
        if df.empty:
            logger.warning("获取 ST 标记失败：网络返回空数据")
            return {'updated': 0, 'failed': 1}

        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            updated = 0
            for _, row in df.iterrows():
                code = row.get('code')
                is_st = 1 if row.get('is_st', False) else 0
                if code:
                    cursor.execute(
                        "UPDATE stock_metadata SET is_st = ? WHERE code = ?",
                        (is_st, code)
                    )
                    if cursor.rowcount > 0:
                        updated += 1
            conn.commit()

        logger.info(f"ST 标记更新完成: {updated} 只股票")
        return {'updated': updated, 'failed': 0}

    except Exception as e:
        logger.error(f"ST 标记更新失败: {e}")
        return {'updated': 0, 'failed': 1}


def smart_download_or_update(
    data_dir: str = "./data",
    resume: bool = True,
    batch_size: int = 100,
    min_delay: float = 10.0,
    max_delay: float = 20.0,
    skip_min_records: int = 100,
    days_threshold: int = 30,
    max_stocks: Optional[int] = None,
    start_date: Optional[str] = None
) -> Dict:
    """
    智能判断：数据库有足够数据且大部分新鲜 → 增量更新，否则全量下载

    参数:
        data_dir: 数据目录
        resume: 断点续传（全量下载时）
        batch_size: 批次大小
        min_delay/max_delay: 请求间隔
        skip_min_records: 断点续传最小记录数
        days_threshold: 增量更新阈值天数
        max_stocks: 调试用最大股票数
        start_date: 全量下载起始日期

    返回:
        {'mode': 'full'|'incremental', 'result': dict}
    """
    from data_layer.market_db import get_all_codes_from_kline, get_all_metadata

    codes = get_all_codes_from_kline(data_dir)
    total_codes = len(codes)

    # 判断标准：
    # 1. 至少有 500 只股票
    # 2. 80% 以上的股票在 30 天内更新过
    if total_codes >= 500:
        all_meta = get_all_metadata(data_dir)
        today = datetime.now()
        fresh_count = 0
        for code, meta in all_meta.items():
            last_update = meta.get('last_update_date', '')
            if last_update:
                try:
                    last_dt = datetime.strptime(last_update, '%Y-%m-%d')
                    if (today - last_dt).days <= days_threshold:
                        fresh_count += 1
                except ValueError:
                    pass

        fresh_ratio = fresh_count / total_codes if total_codes > 0 else 0
        if fresh_ratio >= 0.8:
            logger.info(f"数据库状态良好 ({total_codes} 只，{fresh_ratio*100:.0f}% 新鲜)，执行增量更新")
            result = update_all_stocks(
                data_dir=data_dir,
                days_threshold=days_threshold,
                batch_size=batch_size
            )
            mode = 'incremental'
        else:
            logger.info(f"数据库陈旧 ({fresh_ratio*100:.0f}% 新鲜)，执行全量下载")
            result = download_all_stocks(
                start_date=start_date,
                data_dir=data_dir,
                resume=resume,
                batch_size=batch_size,
                max_stocks=max_stocks,
                min_delay=min_delay,
                max_delay=max_delay,
                skip_min_records=skip_min_records
            )
            mode = 'full'
    else:
        logger.info(f"数据库股票不足 ({total_codes} < 500)，执行全量下载")
        result = download_all_stocks(
            start_date=start_date,
            data_dir=data_dir,
            resume=resume,
            batch_size=batch_size,
            max_stocks=max_stocks,
            min_delay=min_delay,
            max_delay=max_delay,
            skip_min_records=skip_min_records
        )
        mode = 'full'

    # 无论哪种方式，最后都更新 ST 标记
    st_result = update_st_flags(data_dir)
    logger.info(f"ST 标记更新: {st_result}")

    return {
        'mode': mode,
        'result': result,
        'st_update': st_result
    }


# ==================== 选股数据准备（增强版） ====================

