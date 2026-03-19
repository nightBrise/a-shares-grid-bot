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
from typing import Optional, List, Dict, Tuple
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
    检查数据完整性，识别缺失的交易日
    
    A 股交易日历规则：
    - 周一至周五为交易日（法定节假日除外）
    - 不检查具体节假日（简化处理），仅检查工作日缺失
    
    参数:
        df: 股票数据 DataFrame
        code: 股票代码
    
    返回:
        (是否完整，缺失日期列表)
    """
    if df.empty or len(df) < 2:
        return True, []
    
    missing_dates = []
    
    # 按日期排序
    df_sorted = df.sort_values('date').reset_index(drop=True)
    dates = pd.to_datetime(df_sorted['date']).dt.date.tolist()
    
    # 检查相邻日期间隔
    for i in range(1, len(dates)):
        prev_date = dates[i-1]
        curr_date = dates[i]
        
        # 计算间隔天数
        delta_days = (curr_date - prev_date).days
        
        # 如果间隔大于 1 天，检查是否有工作日缺失
        if delta_days > 1:
            current = prev_date
            while current != curr_date:
                current += timedelta(days=1)
                # 检查是否为工作日（周一到周五）
                if current.weekday() < 5:  # 0-4 为周一到周五
                    # 简化的节假日判断：排除常见长假月份的部分日期
                    month_day = (current.month, current.day)
                    # 排除常见假期（春节、国庆等，简化处理）
                    if not is_likely_holiday(current):
                        missing_dates.append(current.strftime('%Y-%m-%d'))
    
    is_complete = len(missing_dates) == 0
    
    if not is_complete and missing_dates:
        logger.warning(f"{code} 发现 {len(missing_dates)} 个可能的缺失交易日")
    
    return is_complete, missing_dates


def is_likely_holiday(date: datetime.date) -> bool:
    """
    判断日期是否可能是法定节假日（简化版本）
    
    参数:
        date: 要判断的日期
    
    返回:
        是否可能是节假日
    """
    # 常见中国节假日（固定日期）
    holidays = [
        # 元旦
        (1, 1),
        # 劳动节
        (5, 1), (5, 2), (5, 3),
        # 国庆节
        (10, 1), (10, 2), (10, 3), (10, 4), (10, 5), (10, 6), (10, 7),
        # 中秋节（近似，农历八月十五前后）
        (9, 15), (9, 16), (9, 17),
        # 清明节（近似，公历 4 月 4-6 日）
        (4, 4), (4, 5), (4, 6),
        # 端午节（近似，农历五月初五前后）
        (6, 8), (6, 9), (6, 10),
    ]
    
    # 春节（近似，农历正月初一前后，公历 1 月下旬到 2 月中旬）
    spring_festival_period = [
        (1, 21), (1, 22), (1, 23), (1, 24), (1, 25), (1, 26), (1, 27), (1, 28), (1, 29), (1, 30), (1, 31),
        (2, 1), (2, 2), (2, 3), (2, 4), (2, 5), (2, 6), (2, 7), (2, 8), (2, 9), (2, 10),
        (2, 11), (2, 12), (2, 13), (2, 14), (2, 15), (2, 16), (2, 17), (2, 18), (2, 19), (2, 20),
    ]
    
    month_day = (date.month, date.day)
    
    return month_day in holidays or month_day in spring_festival_period


# ==================== HTTP 会话管理 ====================

# 全局 Session 对象，复用连接
_http_session = None

def get_http_session():
    """获取或创建 HTTP Session 实例"""
    global _http_session
    
    if _http_session is None:
        try:
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            
            _http_session = requests.Session()
            
            # 配置重试策略
            retry_strategy = Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["HEAD", "GET", "OPTIONS"]
            )
            
            adapter = HTTPAdapter(max_retries=retry_strategy)
            _http_session.mount("http://", adapter)
            _http_session.mount("https://", adapter)
            
            logger.debug("HTTP Session 已创建，启用连接池")
            
        except ImportError:
            logger.warning("requests 库未安装，无法使用 Session 优化")
            return None
    
    return _http_session


# ==================== User-Agent 轮换（增强版） ====================

# 随机 User-Agent 列表（更多样化，模拟真实用户）
USER_AGENTS = [
    # Chrome Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    # Chrome macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    # Chrome Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Firefox Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    # Firefox macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    # Firefox Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    # Edge Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
]

# 常用中文网站 Referer 列表（模拟真实访问来源）
REFERERS = [
    "https://www.baidu.com/s?wd=股票行情",
    "https://www.sogou.com/web?query=股票数据",
    "https://cn.bing.com/search?q=A 股行情",
    "https://finance.sina.com.cn/",
    "https://www.eastmoney.com/",
    "",  # 有时也使用空 Referer
]

def get_random_user_agent() -> str:
    """获取随机 User-Agent"""
    return random.choice(USER_AGENTS)

def get_random_referer() -> str:
    """获取随机 Referer"""
    return random.choice(REFERERS)


# ==================== 请求频率控制 ====================

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


def add_human_behavior_delay(base_delay: float = 1.5):
    """
    添加人类行为延迟（随机性更强，模拟真实用户）
    
    参数:
        base_delay: 基础延迟时间（秒）
    
    返回:
        实际延迟时间
    """
    # 加入高斯分布的随机性（更接近人类行为）
    delay = max(0.5, random.gauss(base_delay, base_delay * 0.3))
    time.sleep(delay)
    return delay


# ==================== Baostock 数据源（备用） ====================

def _ensure_baostock_login():
    """确保 Baostock 已登录"""
    global _bs_logged_in
    
    if not _bs_logged_in and BAOSTOCK_AVAILABLE:
        try:
            bs.login()
            _bs_logged_in = True
            logger.debug("Baostock 登录成功")
        except Exception as e:
            logger.warning(f"Baostock 登录失败：{str(e)}")
            _bs_logged_in = False


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
    
    # 默认日期范围
    if not start_date:
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")
    
    try:
        # 根据复权类型选择字段
        if adjust == 'qfq':
            fields = "date,time,open,high,low,close,volume,amount,turn,pctChg"
        elif adjust == 'hfq':
            fields = "date,time,open,high,low,close,volume,amount,turn,pctChg"
        else:
            fields = "date,time,open,high,low,close,volume,amount,turn,pctChg"
        
        # 查询日线数据
        rs = bs.query_history_k_data_plus(
            bs_code,
            fields,
            start_date=start_date,
            end_date=end_date,
            frequency="d",  # 日线
            adjustflag="3" if adjust == 'qfq' else ("2" if adjust == 'hfq' else "1")
        )
        
        # 检查查询结果
        if rs.error_code != '0':
            logger.error(f"Baostock 查询失败：{rs.error_msg}")
            return pd.DataFrame()
        
        # 转换为 DataFrame
        data_list = []
        while (rs.error_code == '0') and rs.next():
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
        return pd.DataFrame()


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
        start_str = datetime.strptime(start_dt, '%Y-%m-%d').strftime('%Y%m%d')
        end_str = datetime.strptime(end_dt, '%Y-%m-%d').strftime('%Y%m%d')
        
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
        df_existing = pd.read_csv(cache_path, parse_dates=['date'])
        df_combined = append_new_data(df_existing, df_backfill, code)
        save_to_cache(df_combined, cache_path)
        
        # 更新元数据
        last_date = df_combined['date'].max().strftime('%Y-%m-%d')
        update_stock_metadata(code, last_date, len(df_combined), data_dir)
    
    return True


def incremental_update(code: str, data_dir: str = "./data", 
                       adjust: str = "qfq", force_full: bool = False) -> pd.DataFrame:
    """
    执行增量更新逻辑
    
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
    
    返回:
        更新后的完整数据
    """
    cache_path = get_cache_path(code, data_dir)
    
    # === 步骤 1: 读取元数据和本地数据 ===
    stock_meta = get_stock_metadata(code, data_dir)
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
            save_to_cache(df_full, cache_path)
            last_date = df_full['date'].max().strftime('%Y-%m-%d')
            update_stock_metadata(code, last_date, len(df_full), data_dir, 
                                 update_mode='full', update_reason='initial_or_force')
            logger.info(f"{code} 全量更新完成：{len(df_full)}条记录")
            
            # 检查完整性
            is_complete, missing = check_data_integrity(df_full, code)
            if not is_complete and missing:
                logger.info(f"{code} 检测到 {len(missing)} 个缺失日期，尝试补全...")
                backfill_missing_data(code, missing, data_dir, adjust)
        
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
            save_to_cache(df_updated, cache_path)
            
            # === 步骤 5: 检查完整性 ===
            is_complete, missing = check_data_integrity(df_updated, code)
            
            if not is_complete and missing:
                logger.info(f"{code} 检测到 {len(missing)} 个缺失日期，自动补全...")
                backfill_success = backfill_missing_data(code, missing, data_dir, adjust)
                
                if backfill_success:
                    # 重新加载完整数据
                    df_updated = load_from_cache(cache_path)
                    is_complete, missing = check_data_integrity(df_updated, code)
                    
                    if not is_complete:
                        logger.warning(f"{code} 补全后仍有 {len(missing)} 个缺失日期")
            
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
            
            return df_updated
        
        else:
            # 增量为空，可能数据源问题
            logger.warning(f"{code} 增量数据为空，可能网络问题或已停牌")
            return df_existing
            
    except Exception as e:
        # === 异常处理：降级为全量拉取近 1 年数据 ===
        logger.warning(f"{code} 增量更新失败 ({type(e).__name__}: {str(e)[:100]}...)，降级为全量更新...")
        
        start_date = (today - timedelta(days=365)).strftime('%Y%m%d')
        df_fallback = fetch_stock_history(code, start_date=start_date, end_date=today_str,
                                          adjust=adjust, max_retries=5)
        
        if not df_fallback.empty:
            save_to_cache(df_fallback, cache_path)
            last_date = df_fallback['date'].max().strftime('%Y-%m-%d')
            update_stock_metadata(
                code, 
                last_date, 
                len(df_fallback), 
                data_dir,
                update_mode='fallback',
                fallback_reason=str(type(e).__name__)
            )
            logger.warning(f"{code} 降级更新完成：{len(df_fallback)}条记录")
            return df_fallback
        else:
            logger.error(f"{code} 降级更新也失败，返回缓存数据（如有）")
            return df_existing


# ==================== 数据获取（增强版） ====================

def fetch_stock_history(code: str, start_date: str = None, 
                        end_date: str = None, adjust: str = "qfq",
                        max_retries: int = 5, 
                        base_delay: float = 2.0,
                        timeout: int = 30,
                        use_baostock_fallback: bool = True) -> pd.DataFrame:
    """
    获取股票历史行情 (日线) - 增强反反爬虫版
    
    特性:
    - 人类行为模拟（随机延迟、User-Agent 轮换、Referer 轮换）
    - 智能重试机制（指数退避 + 抖动）
    - 请求频率控制
    - AkShare/Baostock 双数据源自动切换
    - 连续失败保护
    
    参数:
        code: 股票代码 (格式：600519.SH)
        start_date: 开始日期 (YYYYMMDD)，默认 20200101
        end_date: 结束日期 (YYYYMMDD)，默认今天
        adjust: 复权类型 ('': 不复权，'qfq': 前复权，'hfq': 后复权)
        max_retries: 最大重试次数 (默认 5 次)
        base_delay: 基础延迟范围 (默认 2.0 秒)
        timeout: 请求超时时间 (秒，默认 30 秒)
        use_baostock_fallback: 是否启用 Baostock 备用数据源
    
    返回:
        DataFrame 包含：date, open, high, low, close, volume, amount
    """
    global _consecutive_failures
    
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
    
    # 重试逻辑 - 增强版（模拟人类行为）
    for attempt in range(max_retries):
        try:
            # === 请求前延迟（模拟人类操作） ===
            if attempt > 0:
                # 指数退避 + 随机抖动
                random_base = random.uniform(2.0, 4.0)  # 更长的基础延迟
                delay = random_base * (2 ** (attempt - 1))
                # 添加随机抖动（±20%）
                jitter = random.uniform(-0.2, 0.2) * delay
                delay += jitter
                # 限制最大延迟不超过 60 秒
                delay = min(delay, 60.0)
                
                logger.warning(
                    f"{code} 第 {attempt} 次重试，等待 {delay:.1f}秒 "
                    f"(基础={random_base:.1f}s, 倍率=2^{attempt-1}, 抖动={jitter:.1f}s)"
                )
                time.sleep(delay)
            
            # === 设置请求头（环境变量） ===
            os.environ['USER_AGENT'] = get_random_user_agent()
            os.environ['HTTP_REFERER'] = get_random_referer()
            
            # === 尝试 AkShare（主数据源） ===
            if AKSHARE_AVAILABLE:
                logger.debug(f"{code} 使用 AkShare 获取数据 (User-Agent: {get_random_user_agent()[:50]}...)")
                
                df = ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust
                )
                
                # 验证数据有效性
                if df is not None and not df.empty and len(df) > 0:
                    # 重命名列名为英文
                    column_mapping = {
                        '日期': 'date',
                        '开盘': 'open',
                        '最高': 'high',
                        '最低': 'low',
                        '收盘': 'close',
                        '成交量': 'volume',
                        '成交额': 'amount',
                        '振幅': 'amplitude',
                        '涨跌幅': 'pct_change',
                        '涨跌额': 'change',
                        '换手率': 'turnover_rate'
                    }
                    
                    df = df.rename(columns=column_mapping)
                    
                    # 转换日期格式
                    df['date'] = pd.to_datetime(df['date'])
                    
                    # 选择需要的列
                    required_cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
                    available_cols = [col for col in required_cols if col in df.columns]
                    df = df[available_cols]
                    
                    logger.info(f"✓ {code} AkShare 成功获取 {len(df)} 条记录 (尝试 {attempt+1}/{max_retries})")
                    _consecutive_failures = 0  # 重置失败计数
                    return df
            
            # === AkShare 失败，尝试 Baostock（备用数据源） ===
            if use_baostock_fallback and BAOSTOCK_AVAILABLE:
                logger.info(f"{code} AkShare 不可用，切换到 Baostock...")
                df_bs = fetch_from_baostock(code, start_date, end_date, adjust)
                
                if df_bs is not None and not df_bs.empty and len(df_bs) > 0:
                    logger.info(f"✓ {code} Baostock 成功获取 {len(df_bs)} 条记录")
                    _consecutive_failures = 0  # 重置失败计数
                    return df_bs
            
            # 两个数据源都失败
            raise Exception("AkShare 和 Baostock 均未返回有效数据")
            
        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__
            _consecutive_failures += 1
            
            # 判断是否为反爬虫相关错误
            anti_bot_keywords = [
                '403', '429', 'Too Many Requests', 'Forbidden',
                '访问频繁', 'IP 被限制', '请求过于频繁',
                'Robot detection', 'Anti-bot'
            ]
            
            is_anti_bot_error = any(kw in error_msg for kw in anti_bot_keywords)
            
            # 判断是否为网络相关错误
            network_keywords = [
                'Connection', 'timeout', 'Timeout', 'Remote end', 
                'network', 'read timed out', 'Max retries',
                'ConnectionError', 'ConnectionResetError'
            ]
            
            is_network_error = any(kw in error_msg for kw in network_keywords)
            
            if is_anti_bot_error:
                logger.warning(
                    f"{code} 可能触发反爬虫机制 ({error_type}): {error_msg[:100]}..."
                )
                # 遇到反爬虫错误，增加额外延迟
                if attempt < max_retries - 1:
                    extra_delay = random.uniform(5.0, 10.0)
                    logger.warning(f"{code} 触发反爬保护，额外等待 {extra_delay:.1f}秒...")
                    time.sleep(extra_delay)
            elif is_network_error and attempt < max_retries - 1:
                logger.warning(f"{code} 网络错误 ({error_type}): {error_msg[:100]}...")
                continue
            elif is_network_error and attempt >= max_retries - 1:
                logger.error(
                    f"✗ {code} 网络错误，重试{max_retries}次后放弃 "
                    f"(总耗时约{sum([base_delay * (2**i) for i in range(max_retries)])}秒)"
                )
            else:
                logger.error(f"✗ {code} 获取失败 ({error_type}): {error_msg}")
                
                # 如果是数据源问题，尝试切换数据源
                if "ak" in error_msg.lower() and use_baostock_fallback:
                    logger.info(f"{code} 尝试使用 Baostock 作为备用数据源...")
                    df_bs = fetch_from_baostock(code, start_date, end_date, adjust)
                    if df_bs is not None and not df_bs.empty and len(df_bs) > 0:
                        return df_bs
                break
    
    # 所有重试都失败
    logger.error(f"✗ {code} 最终无法获取数据 (连续失败{_consecutive_failures}次)")
    return pd.DataFrame()


# ==================== 数据缓存 ====================

def get_cache_path(code: str, data_dir: str = "./data") -> str:
    """获取缓存文件路径"""
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"{code.replace('.', '_')}.csv")


def load_from_cache(cache_path: str) -> Optional[pd.DataFrame]:
    """从本地缓存加载数据"""
    if os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path, parse_dates=['date'])
            logger.debug(f"从缓存加载：{cache_path}")
            return df
        except Exception as e:
            logger.warning(f"读取缓存失败：{str(e)}")
            return None
    return None


def save_to_cache(df: pd.DataFrame, cache_path: str) -> bool:
    """保存数据到本地缓存"""
    try:
        df.to_csv(cache_path, index=False)
        logger.debug(f"数据已缓存：{cache_path}")
        return True
    except Exception as e:
        logger.error(f"保存缓存失败：{str(e)}")
        return False


def get_stock_data(code: str, data_dir: str = "./data", 
                   use_cache: bool = True, fallback_to_cache: bool = True,
                   prefer_baostock: bool = False, 
                   enable_incremental: bool = True,
                   force_full: bool = False,
                   **kwargs) -> pd.DataFrame:
    """
    获取股票数据 (优先读取缓存，支持双数据源自动切换，增量更新引擎)
    
    参数:
        code: 股票代码
        data_dir: 数据目录
        use_cache: 是否使用缓存
        fallback_to_cache: 网络失败时是否降级使用过期缓存
        prefer_baostock: 是否优先使用 Baostock（用于 AkShare 被限制时）
        enable_incremental: 是否启用增量更新（默认启用）
        force_full: 是否强制全量更新（忽略增量逻辑）
        **kwargs: 传递给 fetch_stock_history 的参数
    
    返回:
        DataFrame 包含历史行情
    """
    cache_path = get_cache_path(code, data_dir)
    
    # === 增量更新模式（新特性） ===
    if enable_incremental and not force_full:
        logger.info(f"{code} 使用增量更新引擎...")
        return incremental_update(code, data_dir=data_dir, adjust='qfq', force_full=False)
    
    # === 传统全量模式（向后兼容） ===
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
    
    # 重新获取数据
    if prefer_baostock and BAOSTOCK_AVAILABLE:
        logger.info(f"正在从 Baostock 获取 {code} 的最新数据...")
        df = fetch_from_baostock(code, **kwargs)
        
        # 如果 Baostock 失败，回退到 AkShare
        if df.empty:
            logger.info(f"{code} Baostock 获取失败，切换到 AkShare...")
            df = fetch_stock_history(code, use_baostock_fallback=False, **kwargs)
    else:
        logger.info(f"正在获取 {code} 的最新数据...")
        df = fetch_stock_history(code, use_baostock_fallback=True, **kwargs)
    
    # 保存到缓存
    if len(df) > 0:
        save_to_cache(df, cache_path)
        
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
    """
    计算 ATR (Average True Range, 平均真实波幅)
    
    公式:
    TR = max(high - low, |high - prev_close|, |low - prev_close|)
    ATR = MA(TR, period)
    
    参数:
        df: 包含 high, low, close 列的 DataFrame
        period: ATR 周期
    
    返回:
        ATR 序列
    """
    high = df['high']
    low = df['low']
    close = df['close']
    
    # 计算前一日的收盘价
    prev_close = close.shift(1)
    
    # 计算三种 TR
    tr1 = high - low
    tr2 = abs(high - prev_close)
    tr3 = abs(low - prev_close)
    
    # 取最大值作为 TR
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # 计算 ATR (简单移动平均)
    atr = tr.rolling(window=period).mean()
    
    return atr


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


def get_all_a_stocks(force_refresh: bool = False) -> pd.DataFrame:
    """
    获取所有 A 股股票列表 (沪深两市) - 增强版
    严格过滤，仅返回中国大陆 A 股市场股票
    
    参数:
        force_refresh: 是否强制刷新缓存
    
    返回:
        DataFrame 包含：code, name, exchange, list_date 等
    """
    logger.info("正在获取全市场 A 股列表（严格过滤非 A 股）...")
    
    results = []
    ak_success = False
    
    # === 尝试从 AkShare 获取 ===
    if AKSHARE_AVAILABLE:
        try:
            # 添加延迟模拟人类行为
            enforce_rate_limit(min_delay=3.0, max_delay=6.0)
            
            # 设置随机 User-Agent 和 Referer
            os.environ['USER_AGENT'] = get_random_user_agent()
            os.environ['HTTP_REFERER'] = get_random_referer()
            
            # 获取沪深 A 股列表
            logger.info("获取沪市 A 股列表...")
            df_sh = ak.stock_info_sh_name_code()
            
            # 处理沪市股票
            if 'code' in df_sh.columns:
                for _, row in df_sh.iterrows():
                    code = str(row['code']).zfill(6)
                    full_code = f"{code}.SH"
                    
                    # 严格验证是否为 A 股
                    if is_valid_a_stock_code(full_code):
                        name = row.get('name', '')
                        results.append({'code': full_code, 'name': name, 'exchange': 'SH'})
            
            logger.info(f"获取深市 A 股列表...")
            df_sz = ak.stock_info_a_code_name()
            
            # 处理深市股票
            if 'code' in df_sz.columns:
                for _, row in df_sz.iterrows():
                    code = str(row['code']).zfill(6)
                    full_code = f"{code}.SZ"
                    
                    # 严格验证是否为 A 股
                    if is_valid_a_stock_code(full_code):
                        name = row.get('name', '')
                        results.append({'code': full_code, 'name': name, 'exchange': 'SZ'})
            
            ak_success = len(results) > 0
            
        except Exception as e:
            logger.error(f"AkShare 获取 A 股列表失败：{str(e)}")
            ak_success = False
    
    # === 如果 AkShare 失败，尝试 Baostock ===
    if not ak_success and BAOSTOCK_AVAILABLE:
        logger.info("AkShare 获取失败，尝试从 Baostock 获取 A 股列表...")
        
        try:
            _ensure_baostock_login()
            
            if _bs_logged_in:
                # 查询上交所股票
                rs_sh = bs.query_all_stock('sh')
                if rs_sh.error_code == '0':
                    data_list = []
                    while rs_sh.next():
                        data_list.append(rs_sh.get_row_data())
                    
                    if data_list:
                        df_sh = pd.DataFrame(data_list, columns=rs_sh.fields)
                        if 'code' in df_sh.columns:
                            for _, row in df_sh.iterrows():
                                code = str(row['code']).zfill(6)
                                if code.startswith('6'):  # 沪市
                                    full_code = f"{code}.SH"
                                    if is_valid_a_stock_code(full_code):
                                        results.append({
                                            'code': full_code,
                                            'name': row.get('code_name', ''),
                                            'exchange': 'SH'
                                        })
                
                # 查询深交所股票
                rs_sz = bs.query_all_stock('sz')
                if rs_sz.error_code == '0':
                    data_list = []
                    while rs_sz.next():
                        data_list.append(rs_sz.get_row_data())
                    
                    if data_list:
                        df_sz = pd.DataFrame(data_list, columns=rs_sz.fields)
                        if 'code' in df_sz.columns:
                            for _, row in df_sz.iterrows():
                                code = str(row['code']).zfill(6)
                                if code.startswith('0') or code.startswith('3'):  # 深市
                                    full_code = f"{code}.SZ"
                                    if is_valid_a_stock_code(full_code):
                                        results.append({
                                            'code': full_code,
                                            'name': row.get('code_name', ''),
                                            'exchange': 'SZ'
                                        })
                
                ak_success = len(results) > 0
                
        except Exception as e:
            logger.error(f"Baostock 获取 A 股列表失败：{str(e)}")
    
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

def prepare_selection_data(stocks: List[str], config: dict) -> pd.DataFrame:
    """
    为选股准备数据 (批量获取股票数据并计算指标) - 增强反反爬虫版
    
    策略:
    - 随机延迟（模拟人类操作间隔）
    - 每批处理后长时间休息
    - 连续失败保护
    - 双数据源自动切换
    
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
    
    # 从配置读取网络优化参数
    network_cfg = config.get('network', {})
    min_delay_per_stock = network_cfg.get('min_delay_per_stock', 2.0)
    max_delay_per_stock = network_cfg.get('max_delay_per_stock', 5.0)
    batch_size = network_cfg.get('batch_size', 10)
    long_rest_after_batches = network_cfg.get('long_rest_after_batches', 5)
    long_rest_duration = network_cfg.get('long_rest_duration', 10.0)
    
    # 统计成功和失败数量
    success_count = 0
    fail_count = 0
    consecutive_failures = 0
    
    logger.info(f"\n开始处理 {len(stocks)} 只股票的数据...")
    logger.info(f"延迟设置：{min_delay_per_stock}-{max_delay_per_stock}秒/股票，每{batch_size}只休息{long_rest_duration}秒")
    
    for i, code in enumerate(stocks):
        try:
            # === 批次处理 + 长休息 ===
            if i > 0 and i % (batch_size * long_rest_after_batches) == 0:
                logger.info(f"\n已处理 {i} 只股票，进行长时间休息 ({long_rest_duration}秒)...")
                time.sleep(long_rest_duration)
            
            # === 随机延迟（每只股票之间） ===
            if i > 0:
                delay = random.uniform(min_delay_per_stock, max_delay_per_stock)
                # 偶尔（10% 概率）添加额外延迟，模拟人类休息
                if random.random() < 0.1:
                    extra_delay = random.uniform(5.0, 15.0)
                    logger.info(f"股票 {code} 前添加额外延迟 {extra_delay:.1f}秒...")
                    delay += extra_delay
                
                logger.debug(f"等待 {delay:.1f}秒后处理 {code}...")
                time.sleep(delay)
            
            logger.info(f"[{i+1}/{len(stocks)}] 处理股票：{code}")
            
            # === 获取历史数据 ===
            df = get_stock_data(code, data_dir=data_dir)
            
            if df.empty or len(df) < 60:
                logger.warning(f"{code} 数据不足 (<60 条)，跳过")
                fail_count += 1
                consecutive_failures += 1
                
                # 连续失败过多时，主动休息
                if consecutive_failures >= 5:
                    logger.warning(f"连续失败{consecutive_failures}次，主动休息 30 秒...")
                    time.sleep(30.0)
                    consecutive_failures = 0
                continue
            
            # 重置连续失败计数
            consecutive_failures = 0
            
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
            
            # 进度报告
            if (i + 1) % 20 == 0:
                logger.info(f"进度：{i+1}/{len(stocks)}, 成功:{success_count}, 失败:{fail_count}")
        
        except Exception as e:
            logger.error(f"处理 {code} 时发生异常：{str(e)}")
            fail_count += 1
            consecutive_failures += 1
    
    # === 输出统计信息 ===
    logger.info(f"\n{'='*60}")
    logger.info(f"选股数据处理完成:")
    logger.info(f"  总计：{len(stocks)} 只")
    logger.info(f"  成功：{success_count} 只")
    logger.info(f"  失败：{fail_count} 只")
    logger.info(f"  成功率：{success_count/max(len(stocks),1)*100:.1f}%")
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
