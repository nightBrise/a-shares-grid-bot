"""
数据模块 - A 股网格交易系统 v4.0 (Lite 精简版)
功能：统一数据入口，AkShare/Baostock 双数据源，后复权处理、本地缓存
     支持增量数据更新引擎 (Data Incremental ETL)
"""

import os
import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple, Any
import pandas as pd
import numpy as np

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


logger = logging.getLogger("grid_trading")

# ==================== 全局状态管理 ====================

# 上次请求时间（用于控制请求频率）
_last_request_time = 0
# 连续失败次数
_consecutive_failures = 0
# 是否已登录 baostock
_bs_logged_in = False
# Baostock 服务端拒绝错误标记（用于区分网络错误和服务端限流）
_bs_server_reject = False

# 增量更新断点追踪
_update_checkpoint: Dict[str, Dict] = {}  # code -> {'last_success': datetime, 'last_error': str, 'last_date': str}
_CHECKPOINT_FILE = "configuration/update_checkpoint.json"

# 候选股票列表缓存
_CANDIDATE_FILE = "configuration/candidate_stocks.json"


def load_candidate_stocks() -> Optional[Dict]:
    """
    加载候选股票列表缓存

    返回:
        {'date': str, 'stocks': List[str]} 或 None（不存在或已过期）
    """
    if not os.path.exists(_CANDIDATE_FILE):
        return None

    try:
        with open(_CANDIDATE_FILE, 'r') as f:
            data = json.load(f)

        # 检查日期是否是今天
        cache_date = data.get('date', '')
        today = datetime.now().strftime('%Y-%m-%d')

        if cache_date != today:
            logger.debug(f"候选股票缓存已过期 ({cache_date} != {today})")
            return None

        return data
    except Exception as e:
        logger.warning(f"加载候选股票缓存失败: {e}")
        return None


def save_candidate_stocks(stocks: List[str]) -> bool:
    """
    保存候选股票列表到缓存

    参数:
        stocks: 候选股票代码列表

    返回:
        是否成功
    """
    try:
        data = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'stocks': stocks,
            'count': len(stocks)
        }
        with open(_CANDIDATE_FILE, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
        logger.debug(f"候选股票列表已缓存: {len(stocks)} 只")
        return True
    except Exception as e:
        logger.error(f"保存候选股票缓存失败: {e}")
        return False


def load_update_checkpoint() -> Dict[str, Dict]:
    """从文件加载增量更新断点"""
    global _update_checkpoint
    if os.path.exists(_CHECKPOINT_FILE):
        try:
            with open(_CHECKPOINT_FILE, 'r') as f:
                data = json.load(f)
                # 转换日期字符串回 datetime
                for code, info in data.items():
                    if 'last_success' in info and isinstance(info['last_success'], str):
                        info['last_success'] = datetime.fromisoformat(info['last_success'])
                _update_checkpoint = data
                logger.debug(f"加载断点数据：{len(_update_checkpoint)} 只股票")
        except Exception as e:
            logger.warning(f"加载断点文件失败：{str(e)}")
    return _update_checkpoint


def save_update_checkpoint() -> None:
    """保存断点到文件"""
    try:
        os.makedirs(os.path.dirname(_CHECKPOINT_FILE), exist_ok=True)
        # 转换 datetime 为字符串以便 JSON 序列化
        data_to_save = {}
        for code, info in _update_checkpoint.items():
            data_to_save[code] = {}
            for k, v in info.items():
                if isinstance(v, datetime):
                    data_to_save[code][k] = v.isoformat()
                else:
                    data_to_save[code][k] = v
        with open(_CHECKPOINT_FILE, 'w') as f:
            json.dump(data_to_save, f, indent=2)
        logger.debug(f"保存断点数据：{len(_update_checkpoint)} 只股票")
    except Exception as e:
        logger.error(f"保存断点文件失败：{str(e)}")


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
        save_update_checkpoint()


def get_cache_status_for_stocks(codes: List[str]) -> Dict[str, Dict]:
    """
    获取股票缓存状态（哪些有缓存、哪些没有）

    返回:
        Dict[code, {'has_cache': bool, 'last_date': str, 'record_count': int}]
    """
    load_update_checkpoint()  # 确保断点已加载
    status = {}
    for code in codes:
        cache_path = get_cache_path(code)
        has_cache = os.path.exists(cache_path)
        record_count = 0
        last_date = None

        if has_cache:
            try:
                df = pd.read_parquet(cache_path)
                record_count = len(df)
                last_date = df['date'].max().strftime('%Y-%m-%d') if not df.empty else None
            except:
                has_cache = False

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
            bs.login()
            # 获取未来30天和过去30天
            start_d = (now - timedelta(days=30)).strftime('%Y-%m-%d')
            end_d = (now + timedelta(days=30)).strftime('%Y-%m-%d')
            rs = bs.query_trade_dates(start_date=start_d, end_date=end_d)
            df_bs = rs.get_data()
            bs.logout()

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
    idx = calendar.searchsorted(date, side='left')
    target_idx = min(len(calendar) - 1, idx + n - 1)
    return calendar[target_idx]


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
    加载元数据文件
    
    返回:
        元数据字典，格式：{code: {last_update_date, record_count, ...}}
    """
    metadata_path = get_metadata_path(data_dir)
    
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            logger.debug(f"已加载元数据：{metadata_path}")
            return metadata
        except Exception as e:
            logger.warning(f"读取元数据失败：{str(e)}")
            return {}
    
    return {}


def save_metadata(metadata: Dict, data_dir: str = "./data"):
    """
    保存元数据到文件
    
    参数:
        metadata: 元数据字典
        data_dir: 数据目录
    """
    metadata_path = get_metadata_path(data_dir)
    
    try:
        # 确保目录存在
        os.makedirs(data_dir, exist_ok=True)
        
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        
        logger.debug(f"元数据已保存：{metadata_path}")
    except Exception as e:
        logger.error(f"保存元数据失败：{str(e)}")


def get_stock_metadata(code: str, data_dir: str = "./data") -> Optional[Dict]:
    """
    获取单只股票的元数据
    
    参数:
        code: 股票代码
        data_dir: 数据目录
    
    返回:
        股票元数据，包含 last_update_date, record_count 等
    """
    metadata = load_metadata(data_dir)
    return metadata.get(code)


def update_stock_metadata(code: str, last_date: str, record_count: int, 
                          data_dir: str = "./data", **kwargs):
    """
    更新单只股票的元数据
    
    参数:
        code: 股票代码
        last_date: 最后更新日期 (YYYY-MM-DD)
        record_count: 记录总数
        data_dir: 数据目录
        **kwargs: 其他元数据字段
    """
    metadata = load_metadata(data_dir)
    
    metadata[code] = {
        'last_update_date': last_date,
        'record_count': record_count,
        'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        **kwargs
    }
    
    save_metadata(metadata, data_dir)
    logger.debug(f"股票 {code} 元数据已更新：最后日期={last_date}, 记录数={record_count}")


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
            max_delay=network_cfg.get('max_cooldown', 300.0),
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

import threading

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

        # 格式化代码为统一格式
        df['code'] = df['代码'] + '.' + df['名称'].apply(
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
    使用缓存的批量行情数据快速预过滤

    策略：
    1. 尝试读取本地缓存的当日行情（data/today_spot.parquet）
    2. 如果缓存过期或不存在，使用 akshare 批量接口获取
    3. 在内存中按条件过滤

    参数:
        candidate_list: 候选股票代码列表
        config: 配置字典

    返回:
        过滤后的股票代码列表
    """
    data_dir = config.get('paths', {}).get('data_dir', './data')
    os.makedirs(data_dir, exist_ok=True)
    cache_file = os.path.join(data_dir, 'today_spot.parquet')

    # 尝试加载缓存
    today_str = datetime.now().strftime('%Y-%m-%d')
    if os.path.exists(cache_file):
        try:
            df_spot = pd.read_parquet(cache_file)
            if len(df_spot) > 0 and str(df_spot.iloc[0].get('date', '')) == today_str:
                logger.info("使用当日行情缓存进行预过滤")
                return _apply_pre_filter(df_spot, candidate_list, config)
        except Exception:
            pass

    # 获取全市场实时行情
    logger.info("获取全市场实时行情（用于预过滤，首次可能需 10-30 秒）...")
    df_spot = get_stocks_basic_info_batch()
    if df_spot.empty:
        logger.warning("无法获取行情数据，跳过预过滤")
        return candidate_list

    # 保存缓存
    df_spot['date'] = today_str
    df_spot.to_parquet(cache_file, index=False)
    logger.info(f"行情数据已缓存：{len(df_spot)} 只")

    return _apply_pre_filter(df_spot, candidate_list, config)


def _apply_pre_filter(df_spot: pd.DataFrame,
                      candidate_list: List[str],
                      config: dict) -> List[str]:
    """
    应用预过滤条件到行情数据

    参数:
        df_spot: 批量获取的行情 DataFrame
        candidate_list: 候选股票代码列表
        config: 配置字典

    返回:
        过滤后的股票代码列表
    """
    pre_cfg = config.get('pre_filter', {})
    min_turnover = pre_cfg.get('min_turnover', 50000)  # 万元
    max_price = pre_cfg.get('max_price', 100.0)

    # 确保 code 列存在且格式正确
    if 'code' not in df_spot.columns:
        logger.warning("行情数据缺少 code 列，跳过预过滤")
        return candidate_list

    # 过滤条件：
    # 1. 在候选列表中
    # 2. 成交额 >= min_turnover 万元
    # 3. 股价 <= max_price
    # 4. 非 ST
    df_filtered = df_spot[
        (df_spot['code'].isin(candidate_list)) &
        (df_spot['turnover'] >= min_turnover) &
        (df_spot['price'] <= max_price) &
        (df_spot['is_st'] == False)
    ]

    filtered_list = df_filtered['code'].tolist()
    logger.info(f"预过滤完成：{len(candidate_list)} → {len(filtered_list)} 只 "
                 f"(成交额≥{min_turnover}万，股价≤{max_price}元)")

    if len(filtered_list) < len(candidate_list):
        removed = len(candidate_list) - len(filtered_list)
        logger.debug(f"预过滤移除 {removed} 只：成交额不足或价格过高")

    return filtered_list


# ==================== 请求频率控制（兼容旧接口） ====================

def enforce_rate_limit(min_delay: float = 2.0, max_delay: float = 5.0):
    """
    执行请求频率限制（模拟人类操作间隔）
    
    参数:
        min_delay: 最小延迟时间（秒）
        max_delay: 最大延迟时间（秒）
    """
    global _last_request_time
    
    current_time = time.time()
    elapsed = current_time - _last_request_time
    
    # 计算随机延迟（包含人类行为的不规则性）
    random_delay = random.uniform(min_delay, max_delay)
    
    if elapsed < random_delay:
        sleep_time = random_delay - elapsed
        logger.debug(f"频率控制：等待 {sleep_time:.2f} 秒")
        time.sleep(sleep_time)
    
    _last_request_time = time.time()


# ==================== Baostock 数据源（备用） ====================

def _ensure_baostock_login(force_relogin: bool = False):
    """
    确保 Baostock 已登录

    参数:
        force_relogin: 是否强制重新登录
    """
    global _bs_logged_in

    if not BAOSTOCK_AVAILABLE:
        return

    # 如果已登录且不强制重新登录，则跳过
    if _bs_logged_in and not force_relogin:
        return

    # 如果之前登录失败，等待一段时间后再试
    if hasattr(_ensure_baostock_login, '_last_failed_time'):
        import time
        last_failed = _ensure_baostock_login._last_failed_time
        if time.time() - last_failed < 60:  # 1分钟内不重试
            logger.debug("Baostock 登录失败冷却中...")
            return

    try:
        # 如果之前已登录，先尝试登出
        if _bs_logged_in:
            try:
                bs.logout()
            except:
                pass

        bs.login()
        _bs_logged_in = True
        logger.info("Baostock 登录成功")

    except Exception as e:
        logger.warning(f"Baostock 登录失败：{str(e)}")
        _bs_logged_in = False
        _ensure_baostock_login._last_failed_time = time.time()


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
    global _bs_logged_in

    if not BAOSTOCK_AVAILABLE:
        return pd.DataFrame()

    # 确保已登录
    _ensure_baostock_login()

    if not _bs_logged_in:
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
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
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
            _bs_logged_in = False
            _ensure_baostock_login()
            return pd.DataFrame()

        if not hasattr(rs, 'error_code') or rs.error_code != '0':
            error_msg = getattr(rs, 'error_msg', 'Unknown error') if rs else 'None'
            logger.warning(f"Baostock 查询失败：{error_msg}")
            # 返回特殊标记，让调用方知道是服务端拒绝而非网络错误
            return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
        # 重置连接状态标志
        _bs_connection_error = False

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
                           adjust: str = "qfq", max_retries: int = 3) -> Optional[pd.DataFrame]:
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
    
    # 加载现有数据并合并
    cache_path = get_cache_path(code, data_dir)
    if os.path.exists(cache_path):
        df_existing = pd.read_parquet(cache_path)
        df_combined = append_new_data(df_existing, df_backfill, code)
        save_to_cache(df_combined, cache_path)
        
        # 更新元数据
        last_date = df_combined['date'].max().strftime('%Y-%m-%d')
        update_stock_metadata(code, last_date, len(df_combined), data_dir)
    
    return True


def incremental_update(code: str, data_dir: str = "./data",
                       adjust: str = "qfq", force_full: bool = False,
                       selected_stocks: Optional[List[str]] = None) -> pd.DataFrame:
    """
    执行增量更新逻辑（仅针对选出的 top_n 股票）

    流程:
    1. 读取本地元数据，获取最后更新日期
    2. 计算需要更新的日期范围
    3. 获取增量数据
    4. 追加到本地文件
    5. 检查数据完整性，必要时补全
    6. 更新元数据

    参数:
        code: 股票代码
        data_dir: 数据目录
        adjust: 复权类型
        force_full: 是否强制全量更新
        selected_stocks: 选出的 top_n 股票列表，非空时仅对这些股票执行增量更新

    返回:
        更新后的完整数据
    """
    cache_path = get_cache_path(code, data_dir)

    # === 步骤 0: 检查是否需要增量更新 ===
    # 如果指定了 selected_stocks 且当前股票不在其中，跳过增量更新
    if selected_stocks is not None and code not in selected_stocks:
        logger.info(f"{code} 不在选出的 top_n 股票列表中，跳过增量更新，仅使用缓存")
        df_existing = load_from_cache(cache_path) if os.path.exists(cache_path) else pd.DataFrame()
        return df_existing if not df_existing.empty else pd.DataFrame()

    # === 步骤 1: 读取元数据和本地数据 ===
    stock_meta = get_stock_metadata(code, data_dir)
    # 优先从季度文件读取历史数据
    df_existing = load_quarter_history(codes=[code], data_dir=data_dir)
    if df_existing.empty:
        # 如果季度文件也没有，尝试旧缓存文件（兼容模式）
        df_existing = load_from_cache(cache_path) if os.path.exists(cache_path) else pd.DataFrame()
    
    # === 步骤 2: 判断更新模式 ===
    today = datetime.now()
    today_str = today.strftime('%Y%m%d')
    
    # 强制全量更新或无本地数据
    if force_full or df_existing.empty or stock_meta is None:
        logger.info(f"{code} 首次获取或强制全量更新，拉取近 1 年数据...")
        start_date = (today - timedelta(days=365)).strftime('%Y%m%d')
        
        df_full = fetch_stock_history(code, start_date=start_date, end_date=today_str, 
                                      adjust=adjust, max_retries=5)
        
        if df_full.empty:
            # 降级：尝试更长时间范围
            logger.warning(f"{code} 近 1 年数据获取失败，尝试近 2 年数据...")
            start_date = (today - timedelta(days=730)).strftime('%Y%m%d')
            df_full = fetch_stock_history(code, start_date=start_date, end_date=today_str,
                                          adjust=adjust, max_retries=5)
        
        if not df_full.empty:
            # 添加 code 列并写入季度文件
            df_full_with_code = df_full.copy()
            df_full_with_code['code'] = code
            save_quarter_history(df_full_with_code, data_dir)
            last_date = df_full['date'].max().strftime('%Y-%m-%d')
            update_stock_metadata(code, last_date, len(df_full), data_dir, 
                                 update_mode='full', update_reason='initial_or_force')
            logger.info(f"{code} 全量更新完成：{len(df_full)}条记录")

            # 更新断点：成功
            update_checkpoint(code, success=True, last_date=last_date,
                            cache_exists=os.path.exists(cache_path))

            # 检查完整性（仅当日志用，不触发耗时补全）
            is_complete, missing = check_data_integrity(df_full, code)
            if not is_complete and missing:
                logger.warning(f"{code} 检测到 {len(missing)} 个缺失日期，跳过补全")
        
        return df_full if not df_full.empty else pd.DataFrame()
    
    # === 增量更新模式 ===
    last_update_str = stock_meta.get('last_update_date', '')
    
    try:
        last_update_date = datetime.strptime(last_update_str, '%Y-%m-%d')
    except ValueError:
        logger.warning(f"{code} 元数据日期格式错误，切换到全量更新")
        last_update_date = today - timedelta(days=365)
    
    # 计算增量日期范围（从最后更新日期的次日开始）
    incremental_start = (last_update_date + timedelta(days=1)).strftime('%Y%m%d')
    
    # 如果增量起始日期晚于今天，无需更新
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
            adjust=adjust,
            max_retries=3
        )
        
        if df_incremental is not None and not df_incremental.empty:
            # === 步骤 4: 追加新数据 ===
            df_updated = append_new_data(df_existing, df_incremental, code)
            # 添加 code 列并写入季度文件
            df_updated['code'] = code
            save_quarter_history(df_updated, data_dir)

            # === 步骤 5: 检查完整性（仅当日志用，不触发耗时补全）===
            is_complete, missing = check_data_integrity(df_updated, code)
            if not is_complete and missing:
                logger.warning(f"{code} 检测到 {len(missing)} 个缺失日期，已缓存数据覆盖优化周期，跳过补全")

            # === 步骤 6: 更新元数据 ===
            last_date = df_updated['date'].max().strftime('%Y-%m-%d')
            update_stock_metadata(
                code,
                last_date,
                len(df_updated),
                data_dir,
                update_mode='incremental',
                incremental_days=(today - last_update_date).days,
                missing_filled=len(missing) if not is_complete else 0
            )
            
            days_fetched = (today - last_update_date).days
            logger.info(f"Incremental update: {days_fetched} days data fetched for {code}")

            # 更新断点：成功
            update_checkpoint(code, success=True, last_date=last_date,
                            cache_exists=os.path.exists(cache_path))

            return df_updated

        else:
            # 增量为空，可能数据源问题
            logger.warning(f"{code} 增量数据为空，可能网络问题或已停牌")
            # 更新断点：失败（数据为空）
            update_checkpoint(code, success=False, error_msg="增量数据为空",
                            cache_exists=os.path.exists(cache_path))
            return df_existing

    except Exception as e:
        # === 异常处理：降级为全量拉取近 1 年数据 ===
        logger.warning(f"{code} 增量更新失败 ({type(e).__name__}: {str(e)[:100]}...)，降级为全量更新...")

        start_date = (today - timedelta(days=365)).strftime('%Y%m%d')
        df_fallback = fetch_stock_history(code, start_date=start_date, end_date=today_str,
                                          adjust=adjust, max_retries=5)

        if not df_fallback.empty:
            # 添加 code 列并写入季度文件
            df_fallback_with_code = df_fallback.copy()
            df_fallback_with_code['code'] = code
            save_quarter_history(df_fallback_with_code, data_dir)
            last_date = df_fallback['date'].max().strftime('%Y-%m-%d')
            update_stock_metadata(
                code,
                last_date,
                len(df_fallback),
                data_dir,
                update_mode='fallback',
                fallback_reason=str(type(e).__name__)
            )
            # 更新断点：降级成功
            update_checkpoint(code, success=True, last_date=last_date,
                            cache_exists=os.path.exists(cache_path))
            logger.warning(f"{code} 降级更新完成：{len(df_fallback)}条记录")
            return df_fallback
        else:
            # 更新断点：降级也失败
            update_checkpoint(code, success=False, error_msg=f"降级失败: {str(e)[:50]}",
                            cache_exists=os.path.exists(cache_path))
            logger.error(f"{code} 降级更新也失败，返回缓存数据（如有）")
            return df_existing


# ==================== 数据获取（增强版） ====================

def fetch_stock_history(code: str, start_date: str = None,
                        end_date: str = None, adjust: str = "qfq",
                        max_retries: int = 3,
                        base_delay: float = 2.0,
                        timeout: int = 30,
                        use_baostock_fallback: bool = True,
                        prefer_baostock: bool = True,
                        aggressive_switch: bool = True) -> pd.DataFrame:
    """
    获取股票历史行情 (日线) - 自适应反反爬虫版

    特性:
    - 自适应限流器（指数退避 + 成功后恢复）
    - 快速失败切换（aggressive_switch）
    - 优先 Baostock（更稳定）
    - AkShare/Baostock 双数据源

    参数:
        code: 股票代码 (格式：600519.SH)
        start_date: 开始日期 (YYYYMMDD)，默认 20200101
        end_date: 结束日期 (YYYYMMDD)，默认今天
        adjust: 复权类型 ('': 不复权，'qfq': 前复权，'hfq': 后复权)
        max_retries: 最大重试次数 (默认 3 次，快速失败)
        base_delay: 基础延迟范围 (默认 2.0 秒)
        timeout: 请求超时时间 (秒，默认 30 秒)
        use_baostock_fallback: 是否启用 Baostock 备用数据源
        prefer_baostock: 是否优先使用 Baostock（默认 True）
        aggressive_switch: 是否启用快速切换模式（默认 True，遇到错误立即切换）

    返回:
        DataFrame 包含：date, open, high, low, close, volume, amount
    """
    global _consecutive_failures
    global _bs_server_reject

    # 分离代码和交易所
    if '.' in code:
        symbol = code.split('.')[0]
        exchange = code.split('.')[1].lower()
    else:
        logger.error(f"股票代码格式错误：{code}")
        return pd.DataFrame()

    # 默认日期范围
    if not start_date:
        start_date = "20200101"
    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")

    # 获取全局限流器
    rate_limiter = get_global_rate_limiter()

    # 定义数据源获取函数
    def fetch_from_akshare_impl():
        """从 AkShare 获取数据"""
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust
        )

        if df is not None and not df.empty and len(df) > 0:
            column_mapping = {
                '日期': 'date', '开盘': 'open', '最高': 'high',
                '最低': 'low', '收盘': 'close', '成交量': 'volume',
                '成交额': 'amount', '振幅': 'amplitude',
                '涨跌幅': 'pct_change', '涨跌额': 'change',
                '换手率': 'turnover_rate'
            }
            df = df.rename(columns=column_mapping)
            df['date'] = pd.to_datetime(df['date'])
            required_cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
            available_cols = [col for col in required_cols if col in df.columns]
            return df[available_cols]
        return pd.DataFrame()

    # 确定数据源顺序
    if prefer_baostock:
        source_order = ['baostock', 'akshare']
    else:
        source_order = ['akshare', 'baostock']

    # 重试逻辑
    for attempt in range(max_retries):
        try:
            # 每次重试都重新确定数据源顺序（基于 prefer_baostock，非修改后的 source_order）
            # 这样确保反爬虫切换后，下次重试仍按原始偏好选择数据源
            if prefer_baostock:
                current_source_order = ['baostock', 'akshare']
            else:
                current_source_order = ['akshare', 'baostock']

            # 请求前等待（使用自适应限流器）
            rate_limiter.wait()

            # 尝试各个数据源
            baostock_server_reject = False  # 初始化，避免未定义错误
            for source in current_source_order:
                # 每次迭代都重新检查 Baostock 跳过条件（避免跨 source 传递）
                skip_baostock = _bs_server_reject and aggressive_switch

                # 如果之前 Baostock 被服务端拒绝，跳过它直接尝试 AkShare
                if source == 'baostock' and skip_baostock:
                    logger.debug(f"{code} 跳过 Baostock (服务端拒绝标记)")
                    continue

                if source == 'baostock' and BAOSTOCK_AVAILABLE and use_baostock_fallback:
                    logger.info(f"{code} 尝试从 Baostock 获取...")
                    df_bs = fetch_from_baostock(code, start_date, end_date, adjust)
                    if df_bs is not None and not df_bs.empty and len(df_bs) > 0:
                        logger.info(f"✓ {code} Baostock 成功获取 {len(df_bs)} 条记录")
                        rate_limiter.record_success()
                        _bs_server_reject = False  # 重置拒绝标记
                        return df_bs

                    # 检测到 Baostock 服务端拒绝（error_code != '0'）
                    if len(df_bs) == 0:
                        baostock_server_reject = True
                        logger.warning(f"{code} Baostock 服务端拒绝 (错误码非0)")
                        # 标记仅在同一 attempt 内生效，避免跳过来自同一 attempt 的 AkShare
                        _bs_server_reject = True

                    # Baostock 失败，快速切换到 AkShare
                    if aggressive_switch:
                        logger.warning(f"{code} Baostock 失败，启用快速切换...")
                        baostock_server_reject = False  # 清除标记，避免异常处理误判
                        continue

                elif source == 'akshare' and AKSHARE_AVAILABLE:
                    logger.debug(f"{code} 尝试从 AkShare 获取...")
                    df = fetch_from_akshare_impl()
                    if df is not None and not df.empty and len(df) > 0:
                        logger.info(f"✓ {code} AkShare 成功获取 {len(df)} 条记录")
                        rate_limiter.record_success()
                        return df

            # 所有源都失败，抛出异常
            raise Exception("所有数据源均未返回有效数据")

        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__
            rate_limiter.record_failure()

            # 判断错误类型
            anti_bot_keywords = [
                '403', '429', 'Too Many Requests', 'Forbidden',
                '访问频繁', 'IP 被限制', '请求过于频繁',
                'Robot detection', 'Anti-bot'
            ]
            is_anti_bot_error = any(kw in error_msg for kw in anti_bot_keywords)

            network_keywords = [
                'Connection', 'timeout', 'Timeout', 'Remote end',
                'network', 'read timed out', 'Max retries',
                'ConnectionError', 'ConnectionResetError',
                # 新增：远程连接断开相关
                'RemoteDisconnected', 'Connection aborted',
                'ConnectionRefusedError', 'SSLError',
                'Could not fetch', 'ECONNRESET', 'EOF',
            ]
            is_network_error = any(kw in error_msg for kw in network_keywords)

            # 检测 Baostock 服务端拒绝（error_code != '0' 返回空 DataFrame 导致）
            is_server_reject = (baostock_server_reject or
                              ('网络接收错误' in error_msg) or
                              ('Baostock 查询失败' in error_msg))

            status = rate_limiter.get_status()

            if is_server_reject:
                # 服务端拒绝：大幅增加等待时间，跳过 Baostock 切换到 AkShare
                logger.warning(f"{code} Baostock 服务端限流，等待 {status['current_delay']:.1f}s 后尝试 AkShare...")
                _bs_server_reject = True  # 设置全局标记
                # 立即切换到 AkShare，不再重试 Baostock
                rate_limiter.wait()  # 等待后再试
                continue
            elif is_anti_bot_error:
                # 反爬虫错误，快速切换到 Baostock
                if aggressive_switch and prefer_baostock:
                    logger.warning(f"{code} 反爬虫错误，快速切换数据源...")
                    source_order = ['baostock']  # 只用 Baostock
                elif attempt < max_retries - 1:
                    extra_delay = random.uniform(10.0, 20.0)
                    logger.warning(f"{code} 额外冷却 {extra_delay:.1f}秒...")
                    time.sleep(extra_delay)
            elif is_network_error and attempt < max_retries - 1:
                continue
            elif attempt >= max_retries - 1:
                logger.error(f"✗ {code} 重试{max_retries}次后放弃")
            else:
                break

    # 所有重试都失败
    status = rate_limiter.get_status()
    logger.error(f"✗ {code} 最终无法获取数据 [限流器: {status['mode']}, delay={status['current_delay']:.1f}s]")
    # 更新断点：失败
    update_checkpoint(code, success=False, error_msg=f"所有重试失败: {status['mode']}",
                    cache_exists=False)
    return pd.DataFrame()


# ==================== 历史数据季度分区存储 ====================

def get_history_dir(data_dir: str = "./data") -> str:
    """获取历史数据目录路径"""
    history_dir = os.path.join(data_dir, "history")
    os.makedirs(history_dir, exist_ok=True)
    return history_dir


def get_quarter_file(date: datetime, data_dir: str = "./data") -> str:
    """
    根据日期获取对应的季度文件路径

    参数:
        date: 日期（datetime 或类似类型）
        data_dir: 数据目录

    返回:
        季度文件路径，如 'data/history/2025_Q2.parquet'
    """
    history_dir = get_history_dir(data_dir)
    year = date.year
    month = date.month

    # 确定季度
    if 1 <= month <= 3:
        quarter = 1
    elif 4 <= month <= 6:
        quarter = 2
    elif 7 <= month <= 9:
        quarter = 3
    else:
        quarter = 4

    return os.path.join(history_dir, f"{year}_Q{quarter}.parquet")


def get_quarter_from_date(date) -> Tuple[int, int]:
    """根据日期获取 (year, quarter)"""
    if isinstance(date, str):
        date = pd.to_datetime(date)
    year = date.year
    month = date.month
    if 1 <= month <= 3:
        quarter = 1
    elif 4 <= month <= 6:
        quarter = 2
    elif 7 <= month <= 9:
        quarter = 3
    else:
        quarter = 4
    return year, quarter


def save_quarter_history(df: pd.DataFrame, data_dir: str = "./data",
                         compression: str = 'snappy') -> bool:
    """
    将数据写入对应季度文件（追加模式，自动去重）

    参数:
        df: 包含 date, code, open, high, low, close, volume, amount 列的 DataFrame
        data_dir: 数据目录
        compression: 压缩算法，默认 snappy

    返回:
        是否成功
    """
    if df is None or df.empty:
        return False

    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])

    # 添加年份和季度列用于分组
    df['year'] = df['date'].dt.year
    df['month'] = df['date'].dt.month
    df['quarter'] = ((df['month'] - 1) // 3) + 1

    # 按日期分组写入对应季度文件
    for (year, quarter), group in df.groupby(['year', 'quarter']):
        quarter_file = os.path.join(get_history_dir(data_dir), f"{year}_Q{quarter}.parquet")

        # 读取现有数据（如果存在）
        existing_df = pd.DataFrame()
        if os.path.exists(quarter_file):
            try:
                existing_df = pd.read_parquet(quarter_file)
            except Exception as e:
                logger.warning(f"读取季度文件失败 {quarter_file}: {e}")

        # 合并并去重
        combined = pd.concat([existing_df, group], ignore_index=True)
        combined = combined.drop_duplicates(subset=['date', 'code'], keep='last')

        # 确保列顺序一致
        required_cols = ['date', 'code', 'open', 'high', 'low', 'close', 'volume', 'amount']
        combined = combined[[c for c in required_cols if c in combined.columns]]

        # 写入文件
        try:
            combined.to_parquet(
                quarter_file,
                index=False,
                engine='pyarrow',
                compression=compression,
            )
            logger.debug(f"写入 {quarter_file}，共 {len(combined)} 条记录")
        except Exception as e:
            logger.error(f"写入季度文件失败 {quarter_file}: {e}")
            return False

    return True


def load_quarter_history(codes: List[str] = None,
                        start_date: str = None,
                        end_date: str = None,
                        data_dir: str = "./data") -> pd.DataFrame:
    """
    从季度文件加载历史数据

    参数:
        codes: 股票代码列表，None 表示所有股票
        start_date: 开始日期（YYYYMMDD 或 YYYY-MM-DD）
        end_date: 结束日期（YYYYMMDD 或 YYYY-MM-DD）
        data_dir: 数据目录

    返回:
        合并后的 DataFrame
    """
    history_dir = get_history_dir(data_dir)

    # 转换日期格式
    if start_date and len(start_date) == 8:
        start_date = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    if end_date and len(end_date) == 8:
        end_date = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

    start_dt = pd.to_datetime(start_date) if start_date else None
    end_dt = pd.to_datetime(end_date) if end_date else None

    # 读取所有季度文件
    all_dfs = []
    if os.path.exists(history_dir):
        for f in os.listdir(history_dir):
            if f.endswith('.parquet'):
                try:
                    df = pd.read_parquet(os.path.join(history_dir, f))
                    all_dfs.append(df)
                except Exception as e:
                    logger.warning(f"读取文件失败 {f}: {e}")

    if not all_dfs:
        return pd.DataFrame()

    # 合并所有数据
    df = pd.concat(all_dfs, ignore_index=True)
    df['date'] = pd.to_datetime(df['date'])

    # 按代码过滤
    if codes:
        df = df[df['code'].isin(codes)]

    # 按日期过滤
    if start_dt:
        df = df[df['date'] >= start_dt]
    if end_dt:
        df = df[df['date'] <= end_dt]

    # 去重
    df = df.drop_duplicates(subset=['date', 'code'], keep='last')

    return df.sort_values('date').reset_index(drop=True)


# ==================== 实时行情内存缓存 ====================

class RealtimeSpotCache:
    """实时行情内存缓存（内存安全版，支持主备切换）"""

    def __init__(self, max_stocks: int = 100):
        """
        参数:
            max_stocks: 最多缓存的股票数量（只关心候选股票）
        """
        self._cache: Dict[str, Dict] = {}
        self._max_stocks = max_stocks
        self._last_update: datetime = datetime.now()
        self._primary = 'akshare'
        self._sources = ['akshare', 'baostock']
        self._update_count = 0

    def update_batch(self, df: pd.DataFrame):
        """批量更新行情"""
        if df is None or df.empty:
            return

        # 只保留需要的列，减少内存占用
        cols = ['code', 'price', 'turnover', 'volume', 'pct_change']
        df = df[[c for c in cols if c in df.columns]].copy()

        # 按成交额排序，只保留 top N 只股票
        if len(df) > self._max_stocks:
            df = df.nlargest(self._max_stocks, 'turnover')

        # 更新缓存（原地更新，不创建新对象）
        for _, row in df.iterrows():
            self._cache[row['code']] = row.to_dict()

        self._last_update = datetime.now()
        self._update_count += 1

    def get_spot(self, code: str) -> Optional[Dict]:
        """获取单只股票实时行情"""
        return self._cache.get(code)

    def save_snapshot(self, path: str):
        """收盘后保存快照到文件"""
        if not self._cache:
            return
        pd.DataFrame.from_dict(self._cache, orient='index').to_parquet(path)

    def get_last_update(self) -> datetime:
        """获取最后更新时间"""
        return self._last_update

    def get_update_count(self) -> int:
        """获取更新次数"""
        return self._update_count


# 全局实时行情缓存实例
_realtime_spot_cache: Optional[RealtimeSpotCache] = None


def get_realtime_spot_cache(max_stocks: int = 100) -> RealtimeSpotCache:
    """获取全局实时行情缓存实例"""
    global _realtime_spot_cache
    if _realtime_spot_cache is None:
        _realtime_spot_cache = RealtimeSpotCache(max_stocks=max_stocks)
    return _realtime_spot_cache


def fetch_spot_data_akshare(codes: List[str] = None) -> pd.DataFrame:
    """从 AkShare 获取实时行情"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()

        # 列名映射
        df = df.rename(columns={
            '代码': 'code',
            '名称': 'name',
            '最新价': 'price',
            '成交额': 'turnover',
            '涨跌幅': 'pct_change',
            '成交量': 'volume',
        })

        # 格式化代码
        df['code'] = df['代码'] + '.' + df['名称'].apply(
            lambda x: 'SH' if str(x).startswith(('6', '5')) else 'SZ'
        )

        # 只保留需要的列
        cols = ['code', 'price', 'turnover', 'volume', 'pct_change']
        df = df[[c for c in cols if c in df.columns]]

        # 按代码过滤
        if codes:
            df = df[df['code'].isin(codes)]

        return df
    except Exception as e:
        logger.warning(f"AkShare 实时行情获取失败: {e}")
        return pd.DataFrame()


def fetch_spot_data_baostock(codes: List[str] = None) -> pd.DataFrame:
    """从 Baostock 获取实时行情（有限支持）"""
    global _bs_logged_in
    if not BAOSTOCK_AVAILABLE:
        return pd.DataFrame()

    _ensure_baostock_login()

    try:
        # Baostock 批量查询有限制，这里简化处理
        import baostock as bs
        rs = bs.query_history_k_data_plus(
            'sh.600000',
            'date,code,open,high,low,close,volume,amount',
            start_date=datetime.now().strftime('%Y-%m-%d'),
            end_date=datetime.now().strftime('%Y-%m-%d'),
            frequency='d'
        )

        if rs is None or rs.error_code != '0':
            return pd.DataFrame()

        data = []
        while rs.next():
            data.append(rs.get_row_data())

        return pd.DataFrame(data, columns=rs.fields)
    except Exception as e:
        logger.warning(f"Baostock 实时行情获取失败: {e}")
        return pd.DataFrame()


def update_realtime_spot(codes: List[str] = None) -> bool:
    """
    更新实时行情（主备切换模式）

    返回:
        是否成功更新
    """
    cache = get_realtime_spot_cache()

    # 尝试主数据源
    if cache._primary == 'akshare':
        df = fetch_spot_data_akshare(codes)
        if df is not None and not df.empty:
            cache.update_batch(df)
            return True

        # 主源失败，切换到备用
        cache._primary = 'baostock'
        df = fetch_spot_data_baostock(codes)
        if df is not None and not df.empty:
            cache.update_batch(df)
            cache._primary = 'akshare'  # 恢复
            return True
    else:
        df = fetch_spot_data_baostock(codes)
        if df is not None and not df.empty:
            cache.update_batch(df)
            return True

        cache._primary = 'akshare'

    return False


# ==================== 数据缓存 ====================

def get_cache_path(code: str, data_dir: str = "./data") -> str:
    """获取缓存文件路径"""
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"{code.replace('.', '_')}.parquet")


def load_from_cache(cache_path: str) -> Optional[pd.DataFrame]:
    """从本地缓存加载数据"""
    if os.path.exists(cache_path):
        try:
            df = pd.read_parquet(cache_path)
            logger.debug(f"从缓存加载：{cache_path}")
            return df
        except Exception as e:
            logger.warning(f"读取缓存失败：{str(e)}")
            return None
    return None


def save_to_cache(df: pd.DataFrame, cache_path: str) -> bool:
    """保存数据到本地缓存"""
    try:
        df.to_parquet(cache_path, index=False, engine='pyarrow')
        logger.debug(f"数据已缓存：{cache_path}")
        return True
    except Exception as e:
        logger.error(f"保存缓存失败：{str(e)}")
        return False


def get_stock_data(code: str, data_dir: str = "./data",
                   use_cache: bool = True, fallback_to_cache: bool = True,
                   prefer_baostock: bool = True,
                   enable_incremental: bool = True,
                   force_full: bool = False,
                   selected_stocks: Optional[List[str]] = None,
                   **kwargs) -> pd.DataFrame:
    """
    获取股票数据 (优先读取缓存，支持双数据源自动切换，增量更新引擎)

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
    cache_path = get_cache_path(code, data_dir)

    # === 增量更新模式（仅针对选出的 top_n 股票）===
    if enable_incremental and not force_full:
        logger.info(f"{code} 使用增量更新引擎...")
        return incremental_update(code, data_dir=data_dir, adjust='qfq', force_full=False,
                                  selected_stocks=selected_stocks)

    # === 传统全量模式 ===
    # 尝试从缓存加载
    if use_cache:
        cached_df = load_from_cache(cache_path)
        if cached_df is not None and len(cached_df) > 0:
            # 检查缓存是否为最新 (简单检查最后一条记录的日期)
            last_date = cached_df['date'].max()
            days_diff = (datetime.now() - last_date).days

            # 如果缓存是 7 天内的，直接使用
            if days_diff <= 7:
                logger.info(f"✓ {code} 使用缓存数据 (最后更新：{last_date.strftime('%Y-%m-%d')})")
                return cached_df
            else:
                logger.info(f"{code} 缓存已过时 ({days_diff}天前), 尝试获取最新数据...")

    # 重新获取数据 - 使用统一的 fetch_stock_history（包含自适应限流和快速切换）
    logger.info(f"正在获取 {code} 的最新数据 (prefer_baostock={prefer_baostock})...")
    df = fetch_stock_history(
        code,
        prefer_baostock=prefer_baostock,
        aggressive_switch=True,
        use_baostock_fallback=True,
        **kwargs
    )

    # 保存到缓存
    if len(df) > 0:
        save_to_cache(df, cache_path)

        # 同时写入季度文件
        df_with_code = df.copy()
        df_with_code['code'] = code
        save_quarter_history(df_with_code, data_dir)

        # 更新元数据（全量模式）
        last_date = df['date'].max().strftime('%Y-%m-%d')
        update_stock_metadata(code, last_date, len(df), data_dir,
                             update_mode='full_legacy')

        return df
    else:
        # 网络获取失败，如果有缓存则降级使用
        if fallback_to_cache and use_cache:
            cached_df = load_from_cache(cache_path)
            if cached_df is not None and len(cached_df) > 0:
                last_date = cached_df['date'].max()
                logger.warning(
                    f"⚠ {code} 网络获取失败，降级使用过期缓存 "
                    f"(最后更新：{last_date.strftime('%Y-%m-%d')})"
                )
                return cached_df

        logger.error(f"✗ {code} 无法获取任何数据 (缓存和网络均失败)")
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


# ==================== 技术指标计算 ====================

def calculate_atr(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """计算 ATR (委托给 indicators.py 实现)"""
    from indicators import calculate_atr as _calc_atr
    return _calc_atr(df, period)


def calculate_volatility(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """
    计算历史波动率 (年化)
    
    公式:
    volatility = std(log_returns) * sqrt(252)
    
    参数:
        df: 包含 close 列的 DataFrame
        period: 计算周期
    
    返回:
        波动率序列
    """
    # 计算对数收益率
    log_returns = np.log(df['close'] / df['close'].shift(1))
    
    # 滚动标准差
    rolling_std = log_returns.rolling(window=period).std()
    
    # 年化 (A 股每年约 252 个交易日)
    volatility = rolling_std * np.sqrt(252)
    
    return volatility


def calculate_hurst_exponent(price_series: pd.Series, max_lag: int = 20) -> float:
    """
    计算 Hurst 指数 (用于判断时间序列的均值回归特性)
    
    原理:
    - H < 0.5: 均值回归序列 (适合网格交易)
    - H = 0.5: 随机游走
    - H > 0.5: 趋势性序列
    
    参数:
        price_series: 价格序列
        max_lag: 最大滞后阶数
    
    返回:
        Hurst 指数
    """
    from scipy.stats import linregress
    
    prices = price_series.values
    
    # 去除 NaN
    prices = prices[~np.isnan(prices)]
    
    if len(prices) < max_lag * 2:
        logger.warning("数据长度不足，Hurst 指数可能不准确")
        return 0.5
    
    # 计算不同 lag 下的 R/S 统计量
    lags = range(2, max_lag + 1)
    
    # 计算每个 lag 的标准差和 R/S
    rs_stats = []
    for lag in lags:
        # 将序列分成长度为 lag 的段
        n_segments = len(prices) // lag
        
        if n_segments < 2:
            continue
        
        segment_stats = []
        for i in range(n_segments):
            segment = prices[i * lag:(i + 1) * lag]
            
            # 计算累积离差
            mean = np.mean(segment)
            cum_dev = np.cumsum(segment - mean)
            
            # 计算极差 R
            R = np.max(cum_dev) - np.min(cum_dev)
            
            # 计算标准差 S
            S = np.std(segment, ddof=1)
            
            if S > 0:
                segment_stats.append(R / S)
        
        if segment_stats:
            rs_stats.append(np.mean(segment_stats))
    
    if len(rs_stats) < 2:
        return 0.5
    
    # 对 log(lag) 和 log(R/S) 进行线性回归
    lags_array = list(lags)[:len(rs_stats)]
    slope, intercept, r_value, p_value, std_err = linregress(
        np.log(lags_array), 
        np.log(rs_stats)
    )
    
    # Hurst 指数即为斜率
    hurst = slope
    
    logger.debug(f"Hurst 指数计算完成：H={hurst:.4f}, R²={r_value:.4f}")
    
    return hurst


# ==================== 获取全市场股票列表（增强 A 股过滤） ====================

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

            if _bs_logged_in:
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


def filter_stocks_by_criteria(df_stocks: pd.DataFrame, 
                               min_turnover: float = 5000,
                               min_price: float = 5.0,
                               max_price: float = 500.0) -> List[str]:
    """
    初步过滤股票池 (基于流动性、价格等基本条件)
    
    参数:
        df_stocks: 股票列表 DataFrame
        min_turnover: 最小成交额 (万元)
        min_price: 最低股价
        max_price: 最高股价
    
    返回:
        符合条件的股票代码列表
    """
    # 这里只做简单过滤，详细过滤在选股策略中进行
    logger.info(f"初步过滤股票池...")
    
    # 暂时返回所有股票，详细过滤在 prepare_selection_data 中进行
    return df_stocks['code'].tolist()


# ==================== 选股数据准备（增强版） ====================

def get_update_strategy(code: str, data_dir: str, hurst_threshold: float = 0.5) -> Tuple[str, Dict]:
    """
    根据股票的缓存数据和历史 Hurst 值决定更新策略

    参数:
        code: 股票代码
        data_dir: 数据目录
        hurst_threshold: Hurst 阈值

    返回:
        (strategy, kwargs) - 策略名称和传递给 get_stock_data 的参数
        strategy 可选:
        - 'full': 全量获取（无缓存或 Hurst 不合格）
        - 'incremental': 增量更新（Hurst 合格）
        - 'cache_only': 仅使用缓存（Hurst 不合格但有缓存）
    """
    cache_path = get_cache_path(code, data_dir)
    metadata = get_stock_metadata(code, data_dir)

    if metadata is None:
        # 无元数据，需要全量获取
        return 'full', {'force_full': True, 'enable_incremental': False}

    hurst = metadata.get('hurst')
    if hurst is None:
        # 有缓存但无 Hurst 值，需要重新计算（全量获取）
        return 'full', {'force_full': True, 'enable_incremental': False}

    if hurst < hurst_threshold:
        # Hurst 合格，继续增量更新
        return 'incremental', {'force_full': False, 'enable_incremental': True}
    else:
        # Hurst 不合格，仅使用缓存不更新
        return 'cache_only', {'force_full': False, 'enable_incremental': False}


def prepare_selection_data(stocks: List[str], config: dict) -> pd.DataFrame:
    """
    为选股准备数据 (批量获取股票数据并计算指标) - 智能增量更新版

    策略:
    - 自适应限流器（指数退避 + 成功后恢复）
    - 批次处理器（更频繁的休息）
    - 连续失败保护
    - 优先使用 Baostock
    - 智能增量更新:
        - Hurst < 0.5 的股票：增量更新（保持合格股票数据新鲜）
        - Hurst >= 0.5 的股票：仅使用缓存（跳过更新，减少请求）
        - 无数据的股票：全量获取

    参数:
        stocks: 股票代码列表
        config: 配置字典

    返回:
        包含各股票指标的 DataFrame
    """
    import time

    results = []
    data_dir = config['paths']['data_dir']
    selection_cfg = config.get('selection', {})
    network_cfg = config.get('network', {})
    hurst_threshold = selection_cfg.get('hurst_threshold', 0.5)

    # 初始化自适应限流器和批次处理器
    rate_limiter = get_global_rate_limiter(config)
    batch_processor = get_global_batch_processor(config)

    # 统计成功和失败数量
    success_count = 0
    fail_count = 0
    skip_update_count = 0  # 因 Hurst 不合格而跳过更新的数量

    logger.info(f"\n开始处理 {len(stocks)} 只股票的数据...")
    status = rate_limiter.get_status()
    logger.info(f"自适应限流器状态：{status['mode']}, 当前延迟={status['current_delay']:.1f}秒")
    logger.info(f"智能更新策略：Hurst < {hurst_threshold} 的股票增量更新，>= {hurst_threshold} 的股票跳过更新")

    for i, code in enumerate(stocks):
        try:
            # === 决定更新策略 ===
            strategy, update_kwargs = get_update_strategy(code, data_dir, hurst_threshold)

            if strategy == 'cache_only':
                logger.info(f"[{i+1}/{len(stocks)}] {code} (Hurst 不合格，使用缓存)")
                # 仅从缓存加载
                cache_path = get_cache_path(code, data_dir)
                df = load_from_cache(cache_path)
                if df is None or len(df) < 60:
                    # 缓存无效，降级为全量获取
                    logger.warning(f"{code} 缓存无效，改为全量获取")
                    df = get_stock_data(code, data_dir=data_dir,
                                        prefer_baostock=network_cfg.get('prefer_baostock', True),
                                        force_full=True, enable_incremental=False)
                    if df.empty or len(df) < 60:
                        fail_count += 1
                        continue
                else:
                    skip_update_count += 1
            else:
                # === 请求前等待（使用自适应限流器） ===
                extra_delay = 0
                # 偶尔（15% 概率）添加额外延迟，模拟人类休息
                if random.random() < network_cfg.get('extra_delay_probability', 0.15):
                    extra_delay = random.uniform(
                        network_cfg.get('extra_delay_min', 5.0),
                        network_cfg.get('extra_delay_max', 20.0)
                    )

                batch_processor.pre_request_wait(rate_limiter, extra_delay)

                if extra_delay > 0:
                    logger.info(f"添加额外人类行为延迟 {extra_delay:.1f}秒")

                logger.info(f"[{i+1}/{len(stocks)}] {code} (策略: {strategy})")

                # === 获取历史数据 ===
                df = get_stock_data(code, data_dir=data_dir,
                                    prefer_baostock=network_cfg.get('prefer_baostock', True),
                                    **update_kwargs)

                if df.empty or len(df) < 60:
                    logger.warning(f"{code} 数据不足 (<60 条)，跳过")
                    fail_count += 1
                    batch_processor.post_request_handling(False, rate_limiter)
                    continue

            # === 清洗数据 ===
            df = clean_data(df)

            # === 计算最新指标 ===
            latest = df.iloc[-1]

            # 计算 ATR
            df['atr'] = calculate_atr(df, selection_cfg.get('atr_period', 20))
            latest_atr = df['atr'].iloc[-1]

            # 计算波动率
            df['volatility'] = calculate_volatility(df, 20)
            latest_vol = df['volatility'].iloc[-1]

            # 计算 Hurst 指数 (使用最近 250 天的收盘价)
            price_series = df['close'].tail(250)
            hurst = calculate_hurst_exponent(price_series)

            # 计算日均成交额 (用于流动性过滤)
            avg_turnover = df['amount'].tail(20).mean() / 10000  # 转换为万元

            # === 收集结果 ===
            results.append({
                'code': code,
                'price': latest['close'],
                'atr': latest_atr if not np.isnan(latest_atr) else 0,
                'volatility': latest_vol if not np.isnan(latest_vol) else 0,
                'hurst': hurst,
                'avg_turnover': avg_turnover,
                'valid': True
            })

            success_count += 1

            # === 保存 Hurst 到元数据 ===
            last_date = df['date'].max().strftime('%Y-%m-%d') if 'date' in df.columns else metadata.get('last_update_date', '')
            update_stock_metadata(code, last_date, len(df), data_dir, hurst=hurst)

            # 请求后处理（批次判断、休息）
            if strategy != 'cache_only':
                batch_processor.post_request_handling(True, rate_limiter)

            # 进度报告
            if (i + 1) % 10 == 0:
                status = rate_limiter.get_status()
                batch_stats = batch_processor.get_stats()
                logger.info(f"进度：{i+1}/{len(stocks)}, 成功:{success_count}, 失败:{fail_count}, 跳过更新:{skip_update_count}, 限流器:{status['mode']}")

        except Exception as e:
            logger.error(f"处理 {code} 时发生异常：{str(e)}")
            fail_count += 1
            if strategy != 'cache_only':
                batch_processor.post_request_handling(False, rate_limiter)

    # === 输出统计信息 ===
    logger.info(f"\n{'='*60}")
    logger.info(f"选股数据处理完成:")
    logger.info(f"  总计：{len(stocks)} 只")
    logger.info(f"  成功：{success_count} 只")
    logger.info(f"  失败：{fail_count} 只")
    logger.info(f"  跳过更新：{skip_update_count} 只 (Hurst >= {hurst_threshold})")
    logger.info(f"  成功率：{success_count/max(len(stocks),1)*100:.1f}%")
    final_status = rate_limiter.get_status()
    final_batch = batch_processor.get_stats()
    logger.info(f"  最终限流器状态：{final_status['mode']}, 延迟={final_status['current_delay']:.1f}秒")
    logger.info(f"  批次统计：处理={final_batch['processed']}, 失败={final_batch['failures']}, 失败率={final_batch['failure_rate']:.1%}")
    logger.info(f"{'='*60}")

    return pd.DataFrame(results)


# ==================== 主函数测试 ====================

if __name__ == "__main__":
    # 简单测试
    logging.basicConfig(level=logging.INFO)
    
    test_code = "600519.SH"
    print(f"\n测试获取 {test_code} 的数据...")
    
    # === 测试增量更新引擎 ===
    print("\n" + "="*60)
    print("第一次运行：全量更新")
    print("="*60)
    
    df = get_stock_data(test_code, force_full=True)
    
    if not df.empty:
        print(f"\n数据形状：{df.shape}")
        print("\n前 5 行:")
        print(df.head())
        print("\n后 5 行:")
        print(df.tail())
        
        # 显示元数据
        metadata = load_metadata()
        if test_code in metadata:
            print(f"\n元数据信息:")
            for key, value in metadata[test_code].items():
                print(f"  {key}: {value}")
        
        # === 第二次运行：增量更新 ===
        print("\n" + "="*60)
        print("第二次运行：增量更新 (应显示 'Incremental update: X days data fetched')")
        print("="*60)
        
        df2 = get_stock_data(test_code, enable_incremental=True)
        
        if not df2.empty:
            print(f"\n增量更新后数据形状：{df2.shape}")
            
            # 验证数据完整性
            is_complete, missing = check_data_integrity(df2, test_code)
            print(f"\n数据完整性检查：{'通过' if is_complete else f'未通过 (缺失{len(missing)}天)'}")
        
        # 测试 ATR 计算
        df2['atr'] = calculate_atr(df2, 20)
        print(f"\n最新 ATR: {df2['atr'].iloc[-1]:.2f}")
        
        # 测试 Hurst 指数
        hurst = calculate_hurst_exponent(df2['close'].tail(250))
        print(f"Hurst 指数：{hurst:.4f}")
    
    print("\n" + "="*60)
    print("增量更新引擎测试完成")
    print("="*60)
