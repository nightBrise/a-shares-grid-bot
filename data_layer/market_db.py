"""
SQLite 本地数据库模块
功能：统一存储股票历史数据，替代分散的 parquet 文件
"""

import os
import sqlite3
import logging
import json
from datetime import datetime
from typing import Optional, List, Dict
import pandas as pd
# import numpy as np

logger = logging.getLogger("grid_trading")

DEFAULT_DB_PATH = "data/market_data.db"


def _get_db_path(data_dir: str = "./data") -> str:
    """获取数据库文件路径"""
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "market_data.db")


def init_db(data_dir: str = "./data") -> str:
    """
    初始化数据库，创建表和索引

    返回:
        数据库文件路径
    """
    db_path = _get_db_path(data_dir)
    logger.info(f"初始化 SQLite 数据库: {db_path}")

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        # 核心 K 线数据表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_kline (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                amount REAL,
                PRIMARY KEY (code, date)
            )
        """)

        # 索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kline_code ON daily_kline(code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kline_date ON daily_kline(date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kline_code_date ON daily_kline(code, date)")

        # 股票元数据表（替代 metadata.json）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stock_metadata (
                code TEXT PRIMARY KEY,
                last_update_date TEXT,
                record_count INTEGER,
                update_time TEXT,
                update_mode TEXT,
                source TEXT,
                hurst REAL,
                is_st INTEGER DEFAULT 0
            )
        """)

        # 更新日志表（替代 update_checkpoint.json）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS update_log (
                code TEXT NOT NULL,
                update_date TEXT NOT NULL,
                status TEXT,
                source TEXT,
                records_added INTEGER,
                error_msg TEXT,
                PRIMARY KEY (code, update_date)
            )
        """)

        # 选股结果表：集中存储所有评分数据
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stock_screening (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                screening_date TEXT NOT NULL,
                price REAL,
                avg_turnover REAL,
                F1 REAL,
                F2 REAL,
                F3 REAL,
                F4 REAL,
                total_score REAL,
                capital_fitness REAL,
                efficiency REAL,
                final_score REAL,
                passes_threshold INTEGER,
                grid_params TEXT,
                selected INTEGER DEFAULT 0,
                rank INTEGER,
                reason TEXT,
                UNIQUE(code, screening_date)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_screening_date
            ON stock_screening(screening_date, selected)
        """)

        # 回测结果表（只保留最新一次，每次回测覆盖旧数据）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS backtest_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                backtest_date TEXT NOT NULL,
                total_return REAL,
                annual_return REAL,
                max_drawdown REAL,
                calmar_ratio REAL,
                sharpe_ratio REAL,
                n_trades INTEGER,
                win_rate REAL,
                final_value REAL,
                initial_cash REAL,
                params TEXT,
                trades_summary TEXT,
                equity_curve TEXT,
                UNIQUE(code, backtest_date)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_backtest_code
            ON backtest_results(code, backtest_date)
        """)

        # 优化结果表（替代 output/report.json）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS optimization_results (
                code TEXT NOT NULL,
                optimize_date TEXT NOT NULL,
                final_params TEXT,
                phase1_params TEXT,
                final_calmar REAL,
                final_return REAL,
                final_drawdown REAL,
                final_trades INTEGER,
                PRIMARY KEY (code, optimize_date)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_optimize_date
            ON optimization_results(optimize_date)
        """)

        # 增量更新检查点表（替代 update_checkpoint.json）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS update_checkpoint (
                code TEXT PRIMARY KEY,
                last_success TEXT,
                last_date TEXT,
                last_error TEXT,
                consecutive_failures INTEGER DEFAULT 0
            )
        """)

        # === 数据库迁移：兼容已有数据库 ===
        # 1. 为已有 stock_metadata 添加 is_st 列
        cursor.execute("PRAGMA table_info(stock_metadata)")
        existing_cols = [row[1] for row in cursor.fetchall()]
        if 'is_st' not in existing_cols:
            cursor.execute("ALTER TABLE stock_metadata ADD COLUMN is_st INTEGER DEFAULT 0")
            logger.info("迁移：为 stock_metadata 添加 is_st 列")

        # 2. 删除已废弃的 today_spot 表
        cursor.execute("DROP TABLE IF EXISTS today_spot")

        conn.commit()

    logger.info("数据库初始化完成")
    return db_path


def save_stock_data(code: str, df: pd.DataFrame, data_dir: str = "./data") -> int:
    """
    将 DataFrame 写入 daily_kline 表

    参数:
        code: 股票代码
        df: DataFrame，必须包含 date, open, high, low, close, volume, amount
        data_dir: 数据目录

    返回:
        写入的记录数
    """
    if df.empty:
        return 0

    db_path = _get_db_path(data_dir)

    # 确保必要列存在
    required = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
    for col in required:
        if col not in df.columns:
            logger.warning(f"{code} 缺少列 {col}，跳过写入")
            return 0

    # 标准化日期格式
    df = df.copy()
    if pd.api.types.is_datetime64_any_dtype(df['date']):
        df['date'] = df['date'].dt.strftime('%Y-%m-%d')
    df['code'] = code

    # 只保留需要的列，按固定顺序
    cols = ['code', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount']
    df_out = df[cols].copy()

    # 去除无效数据
    df_out = df_out.dropna(subset=['date', 'close'])
    df_out = df_out[df_out['close'] > 0]

    if df_out.empty:
        return 0

    with sqlite3.connect(db_path) as conn:
        # 使用 INSERT OR REPLACE 实现去重更新
        records = df_out.values.tolist()
        cursor = conn.cursor()
        cursor.executemany(
            """INSERT OR REPLACE INTO daily_kline
               (code, date, open, high, low, close, volume, amount)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            records
        )
        conn.commit()

    count = len(df_out)
    logger.debug(f"{code} 写入 {count} 条记录到 SQLite")
    return count


def get_stock_data(code: str, start_date: Optional[str] = None,
                   end_date: Optional[str] = None,
                   data_dir: str = "./data") -> Optional[pd.DataFrame]:
    """
    从 SQLite 读取单只股票的历史数据

    参数:
        code: 股票代码
        start_date: 开始日期 (YYYY-MM-DD)，None 表示不限制
        end_date: 结束日期 (YYYY-MM-DD)，None 表示不限制
        data_dir: 数据目录

    返回:
        DataFrame 或 None（数据库不存在或无数据）
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    query = "SELECT date, open, high, low, close, volume, amount FROM daily_kline WHERE code = ?"
    params = [code]

    if start_date:
        query += " AND date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND date <= ?"
        params.append(end_date)

    query += " ORDER BY date"

    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return None

    df['date'] = pd.to_datetime(df['date'])
    return df


def get_multi_stock_data(codes: List[str], start_date: Optional[str] = None,
                         end_date: Optional[str] = None,
                         data_dir: str = "./data") -> Optional[pd.DataFrame]:
    """
    批量读取多只股票的历史数据（一条 SQL）

    参数:
        codes: 股票代码列表
        start_date: 开始日期 (YYYY-MM-DD)
        end_date: 结束日期 (YYYY-MM-DD)
        data_dir: 数据目录

    返回:
        DataFrame，包含 code, date, open, high, low, close, volume, amount
    """
    if not codes:
        return None

    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    placeholders = ','.join('?' * len(codes))
    query = f"""
        SELECT code, date, open, high, low, close, volume, amount
        FROM daily_kline
        WHERE code IN ({placeholders})
    """
    params = list(codes)

    if start_date:
        query += " AND date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND date <= ?"
        params.append(end_date)

    query += " ORDER BY code, date"

    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return None

    df['date'] = pd.to_datetime(df['date'])
    return df


def update_metadata(code: str, data_dir: str = "./data", **kwargs) -> None:
    """
    更新或插入股票元数据

    参数:
        code: 股票代码
        data_dir: 数据目录
        **kwargs: 元数据字段（last_update_date, record_count, update_time, update_mode, source, hurst）
    """
    db_path = _get_db_path(data_dir)

    # 先查询现有数据
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM stock_metadata WHERE code = ?", (code,))
        row = cursor.fetchone()

        if row:
            # 更新现有记录
            allowed_cols = ['last_update_date', 'record_count', 'update_time', 'update_mode', 'source', 'hurst', 'name', 'is_st']
            updates = []
            values = []
            for col in allowed_cols:
                if col in kwargs:
                    updates.append(f"{col} = ?")
                    values.append(kwargs[col])
            if updates:
                values.append(code)
                cursor.execute(
                    f"UPDATE stock_metadata SET {', '.join(updates)} WHERE code = ?",
                    values
                )
        else:
            # 插入新记录
            cursor.execute(
                """INSERT INTO stock_metadata
                   (code, last_update_date, record_count, update_time, update_mode, source, hurst, name, is_st)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (code,
                 kwargs.get('last_update_date'),
                 kwargs.get('record_count'),
                 kwargs.get('update_time'),
                 kwargs.get('update_mode'),
                 kwargs.get('source'),
                 kwargs.get('hurst'),
                 kwargs.get('name'),
                 kwargs.get('is_st', 0))
            )
        conn.commit()


def save_stock_names(names: dict, data_dir: str = "./data") -> None:
    """批量保存股票中文名称到 stock_metadata。"""
    db_path = _get_db_path(data_dir)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        for code, name in names.items():
            cursor.execute(
                "UPDATE stock_metadata SET name = ? WHERE code = ?",
                (name, code)
            )
        conn.commit()


def get_all_stock_names(data_dir: str = "./data") -> dict:
    """从 stock_metadata 读取所有股票的中文名称。"""
    db_path = _get_db_path(data_dir)
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT code, name FROM stock_metadata WHERE name IS NOT NULL AND name != ''")
            return {row[0]: row[1] for row in cursor.fetchall()}
    except Exception:
        logger.warning("获取股票名称列表失败", exc_info=True)
        return {}


def get_metadata(code: str, data_dir: str = "./data") -> Optional[Dict]:
    """
    读取单只股票的元数据

    返回:
        dict 或 None
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM stock_metadata WHERE code = ?", (code,))
        row = cursor.fetchone()

    if row:
        return dict(row)
    return None


def get_all_metadata(data_dir: str = "./data") -> Dict[str, Dict]:
    """
    读取所有股票元数据

    返回:
        {code: {metadata_dict}}
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return {}

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM stock_metadata")
        rows = cursor.fetchall()

    return {row['code']: dict(row) for row in rows}


def get_all_codes(data_dir: str = "./data") -> List[str]:
    """
    获取数据库中所有已存在的股票代码（从 stock_metadata 表）

    返回:
        股票代码列表
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return []

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT code FROM stock_metadata")
        rows = cursor.fetchall()

    return [row[0] for row in rows]


def get_all_codes_from_kline(data_dir: str = "./data") -> List[str]:
    """
    从 daily_kline 表读取所有股票代码（不依赖 stock_metadata）

    依据：daily_kline 是真实数据来源，stock_metadata 可能不完整。

    返回:
        股票代码列表
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return []

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT code FROM daily_kline")
        rows = cursor.fetchall()

    return [row[0] for row in rows]


def get_filtered_codes_from_metadata(
    min_record_count: int = 200,
    exclude_st: bool = True,
    data_dir: str = "./data"
) -> List[str]:
    """
    从 stock_metadata 预过滤股票代码（方案B优化）

    在读取全量数据前，先按元数据过滤，减少无效数据读取。

    过滤条件：
    1. record_count >= min_record_count（数据量足够计算指标）
    2. is_st = 0（排除ST股票）

    参数:
        min_record_count: 最小记录数（默认200，指标计算需要）
        exclude_st: 是否排除ST股票（默认True）
        data_dir: 数据目录

    返回:
        过滤后的股票代码列表
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return []

    conditions = ["record_count >= ?"]
    params = [min_record_count]

    if exclude_st:
        conditions.append("(is_st = 0 OR is_st IS NULL)")

    where_clause = " AND ".join(conditions)
    query = f"SELECT code FROM stock_metadata WHERE {where_clause}"

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()

    codes = [row[0] for row in rows]
    logger.info(f"元数据预过滤: {len(codes)} 只股票通过 (record_count >= {min_record_count}, 排除ST)")
    return codes


def load_st_flags(data_dir: str = "./data") -> Dict[str, bool]:
    """
    从 stock_metadata 表加载 ST 标记

    返回:
        {code: is_st} 字典，无数据返回空字典
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return {}

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT code, is_st FROM stock_metadata WHERE is_st IS NOT NULL")
            rows = cursor.fetchall()
        return {row[0]: bool(row[1]) for row in rows}
    except Exception:
        logger.warning("加载 ST 标记失败", exc_info=True)
        return {}


def get_db_stats(data_dir: str = "./data") -> Dict:
    """
    获取数据库统计信息

    返回:
        dict 包含总记录数、股票数、日期范围等
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return {'exists': False}

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM daily_kline")
        total_records = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(DISTINCT code) FROM daily_kline")
        total_codes = cursor.fetchone()[0]

        cursor.execute("SELECT MIN(date), MAX(date) FROM daily_kline")
        min_date, max_date = cursor.fetchone()

        cursor.execute("SELECT COUNT(*) FROM stock_metadata")
        metadata_count = cursor.fetchone()[0]

    return {
        'exists': True,
        'db_path': db_path,
        'total_records': total_records,
        'total_codes': total_codes,
        'date_range': (min_date, max_date),
        'metadata_count': metadata_count,
        'db_size_mb': round(os.path.getsize(db_path) / (1024 * 1024), 2)
    }


def delete_stock_data(code: str, data_dir: str = "./data") -> int:
    """
    删除单只股票的所有数据

    返回:
        删除的记录数
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return 0

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM daily_kline WHERE code = ?", (code,))
        deleted = cursor.rowcount
        cursor.execute("DELETE FROM stock_metadata WHERE code = ?", (code,))
        conn.commit()

    logger.info(f"删除 {code}: {deleted} 条记录")
    return deleted


def save_screening_results(df: pd.DataFrame, data_dir: str = "./data",
                           selected_codes: Optional[List[str]] = None) -> int:
    """
    保存选股结果到 stock_screening 表

    参数:
        df: 选股结果 DataFrame，必须包含 code、price、因子评分等列
        data_dir: 数据目录
        selected_codes: 最终选中的股票代码列表（标记 selected=1）

    返回:
        写入的记录数
    """
    if df.empty:
        return 0

    # 确保数据库和表已初始化
    init_db(data_dir)

    db_path = _get_db_path(data_dir)
    today_str = datetime.now().strftime('%Y-%m-%d')

    # 列映射：从 DataFrame 到数据库表
    col_map = {
        'code': 'code',
        'price': 'price',
        'avg_turnover': 'avg_turnover',
        'F1_norm': 'F1',
        'F2_norm': 'F2',
        'F3_norm': 'F3',
        'F4_ortho': 'F4',
        'total_score': 'total_score',
        'capital_fitness': 'capital_fitness',
        'efficiency': 'efficiency',
        'final_score': 'final_score',
        'passes_threshold': 'passes_threshold',
        'grid_params': 'grid_params',
        'rank': 'rank',
        'reason': 'reason',
    }

    selected_set = set(selected_codes or [])

    records = []
    for _, row in df.iterrows():
        record = {'screening_date': today_str}
        for df_col, db_col in col_map.items():
            val = row.get(df_col)
            if pd.isna(val):
                record[db_col] = None
            elif db_col == 'passes_threshold':
                record[db_col] = 1 if val else 0
            elif db_col == 'grid_params' and isinstance(val, dict):
                record[db_col] = json.dumps(val, ensure_ascii=False)
            else:
                record[db_col] = val

        record['selected'] = 1 if row.get('code') in selected_set else 0
        records.append(record)

    if not records:
        return 0

    # 先删除同日期的旧数据（避免重复）
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM stock_screening WHERE screening_date = ?",
            (today_str,)
        )

        columns = list(records[0].keys())
        placeholders = ', '.join(['?' for _ in columns])
        col_names = ', '.join(columns)

        cursor.executemany(
            f"INSERT INTO stock_screening ({col_names}) VALUES ({placeholders})",
            [tuple(r[c] for c in columns) for r in records]
        )
        conn.commit()

    logger.info(f"选股结果已保存到 SQLite: {len(records)} 条记录")
    return len(records)


def get_screening_results(screening_date: Optional[str] = None,
                          data_dir: str = "./data") -> Optional[pd.DataFrame]:
    """
    读取选股结果

    参数:
        screening_date: 选股日期 (YYYY-MM-DD)，None 表示最新日期
        data_dir: 数据目录

    返回:
        DataFrame 或 None
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    if screening_date is None:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT MAX(screening_date) FROM stock_screening"
            )
            row = cursor.fetchone()
            screening_date = row[0] if row else None

    if not screening_date:
        return None

    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(
            """
            SELECT * FROM stock_screening
            WHERE screening_date = ?
            ORDER BY final_score DESC, total_score DESC
            """,
            conn,
            params=(screening_date,)
        )

    if df.empty:
        return None

    df['passes_threshold'] = df['passes_threshold'].astype(bool)
    df['selected'] = df['selected'].astype(bool)
    return df


def get_selected_stocks(screening_date: Optional[str] = None,
                        data_dir: str = "./data") -> List[str]:
    """
    获取最终选中的股票代码列表

    参数:
        screening_date: 选股日期 (YYYY-MM-DD)，None 表示最新日期
        data_dir: 数据目录

    返回:
        股票代码列表
    """
    df = get_screening_results(screening_date, data_dir)
    if df is None or df.empty:
        return []

    selected = df[df['selected']].sort_values('rank')
    return selected['code'].tolist()


def get_latest_screening_date(data_dir: str = "./data") -> Optional[str]:
    """
    获取最新选股日期

    返回:
        日期字符串 (YYYY-MM-DD) 或 None
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(screening_date) FROM stock_screening")
        row = cursor.fetchone()

    return row[0] if row and row[0] else None


# ==================== 回测结果存储 ====================


def save_backtest_result(
    code: str,
    result: dict,
    data_dir: str = "./data"
) -> bool:
    """
    保存单只股票的回测结果到 backtest_results 表（覆盖旧数据）

    参数:
        code: 股票代码
        result: backtest_grid_strategy 返回的字典
        data_dir: 数据目录

    返回:
        是否成功
    """
    db_path = _get_db_path(data_dir)
    today_str = datetime.now().strftime('%Y-%m-%d')

    params = result.get('params', {})
    trades = result.get('trades', [])
    portfolio_values = result.get('portfolio_values', [])

    # 计算胜率
    sell_trades = [t for t in trades if t.get('type') == 'sell']
    profitable_sells = [t for t in sell_trades if t.get('revenue', 0) > t.get('cost', 0)]
    win_rate = len(profitable_sells) / len(sell_trades) if sell_trades else 0

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        # 删除该股票旧回测数据（只保留最新）
        cursor.execute("DELETE FROM backtest_results WHERE code = ?", (code,))

        cursor.execute(
            """INSERT INTO backtest_results
               (code, backtest_date, total_return, annual_return, max_drawdown,
                calmar_ratio, sharpe_ratio, n_trades, win_rate, final_value,
                initial_cash, params, trades_summary, equity_curve)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                code, today_str,
                result.get('total_return'),
                result.get('annual_return'),
                result.get('max_drawdown'),
                result.get('calmar_ratio'),
                result.get('sharpe_ratio'),
                result.get('n_trades'),
                win_rate,
                result.get('final_value'),
                result.get('initial_value'),
                json.dumps(params, ensure_ascii=False) if params else None,
                json.dumps(trades[:50], ensure_ascii=False) if trades else None,
                json.dumps(portfolio_values, ensure_ascii=False) if portfolio_values else None,
            )
        )
        conn.commit()

    return True


def get_backtest_results(code: Optional[str] = None,
                         data_dir: str = "./data") -> Optional[pd.DataFrame]:
    """
    读取回测结果

    参数:
        code: 股票代码，None 表示读取所有
        data_dir: 数据目录

    返回:
        DataFrame 或 None
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    query = "SELECT * FROM backtest_results"
    params = ()
    if code:
        query += " WHERE code = ?"
        params = (code,)
    query += " ORDER BY backtest_date DESC"

    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return None
    return df


def get_latest_backtest_date(data_dir: str = "./data") -> Optional[str]:
    """
    获取最新回测日期

    返回:
        日期字符串 (YYYY-MM-DD) 或 None
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(backtest_date) FROM backtest_results")
        row = cursor.fetchone()

    return row[0] if row and row[0] else None


# ==================== 优化结果存储 ====================

def save_optimization_results(
    results: List[dict],
    data_dir: str = "./data"
) -> bool:
    """
    保存优化结果到 optimization_results 表

    参数:
        results: 优化结果列表（run_two_phase_optimization 返回）
        data_dir: 数据目录

    返回:
        是否成功
    """
    db_path = _get_db_path(data_dir)
    today_str = datetime.now().strftime('%Y-%m-%d')

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        for r in results:
            if r.get('error'):
                continue

            code = r.get('code')
            if not code:
                continue

            cursor.execute(
                """INSERT OR REPLACE INTO optimization_results
                   (code, optimize_date, final_params, phase1_params,
                    final_calmar, final_return, final_drawdown, final_trades)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    code,
                    today_str,
                    json.dumps(r.get('final_params', {})),
                    json.dumps(r.get('phase1_params', {})),
                    r.get('final_calmar', 0),
                    r.get('final_return', 0),
                    r.get('final_drawdown', 0),
                    r.get('final_trades', 0),
                )
            )

    return True


def get_optimization_results(
    code: Optional[str] = None,
    optimize_date: Optional[str] = None,
    data_dir: str = "./data"
) -> Optional[pd.DataFrame]:
    """
    获取优化结果

    参数:
        code: 股票代码（None 返回全部）
        optimize_date: 优化日期（None 返回最新）
        data_dir: 数据目录

    返回:
        DataFrame 或 None
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    conditions = []
    params = []
    if code:
        conditions.append("code = ?")
        params.append(code)
    if optimize_date:
        conditions.append("optimize_date = ?")
        params.append(optimize_date)

    query = "SELECT * FROM optimization_results"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY optimize_date DESC"

    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return None
    return df


def get_latest_optimization_results(data_dir: str = "./data") -> Optional[pd.DataFrame]:
    """
    获取最新日期的优化结果

    返回:
        DataFrame 或 None
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(optimize_date) FROM optimization_results")
        row = cursor.fetchone()
        latest_date = row[0] if row and row[0] else None

    if not latest_date:
        return None

    return get_optimization_results(optimize_date=latest_date, data_dir=data_dir)


# ==================== 增量更新检查点 ====================

def save_update_checkpoint(
    code: str,
    last_success: str,
    last_date: str,
    last_error: Optional[str] = None,
    consecutive_failures: int = 0,
    data_dir: str = "./data"
) -> None:
    """
    保存增量更新检查点到 SQLite（替代 update_checkpoint.json）

    参数:
        code: 股票代码
        last_success: 最后成功时间
        last_date: 最后数据日期
        last_error: 最后错误信息
        consecutive_failures: 连续失败次数
        data_dir: 数据目录
    """
    db_path = _get_db_path(data_dir)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT OR REPLACE INTO update_checkpoint
               (code, last_success, last_date, last_error, consecutive_failures)
               VALUES (?, ?, ?, ?, ?)""",
            (code, last_success, last_date, last_error, consecutive_failures)
        )


def get_latest_data_date(code: str, data_dir: str = "./data") -> Optional[str]:
    """
    获取股票最新数据日期

    参数:
        code: 股票代码
        data_dir: 数据目录

    返回:
        最新日期字符串 (YYYY-MM-DD) 或 None
    """
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    query = "SELECT MAX(date) FROM daily_kline WHERE code = ?"

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, (code,))
        row = cursor.fetchone()

    return row[0] if row and row[0] else None


# ==================== 模拟盘数据表 ====================

def init_paper_tables(data_dir: str = "./data") -> None:
    """
    初始化模拟盘相关表
    """
    db_path = _get_db_path(data_dir)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        # 虚拟持仓表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS paper_positions (
                code TEXT PRIMARY KEY,
                total_quantity INTEGER DEFAULT 0,
                available_quantity INTEGER DEFAULT 0,
                frozen_quantity INTEGER DEFAULT 0,
                avg_cost_price REAL DEFAULT 0,
                market_value REAL DEFAULT 0,
                last_update TEXT
            )
        """)

        # 虚拟交易记录表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT,
                direction TEXT,
                price REAL,
                quantity INTEGER,
                amount REAL,
                fee REAL,
                stamp_tax REAL,
                trade_date TEXT,
                trade_time TEXT,
                pnl REAL,
                status TEXT
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_paper_trades_date
            ON paper_trades(trade_date)
        """)

        # 虚拟账户表（单条记录，id=1）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY DEFAULT 1,
                cash REAL DEFAULT 0,
                total_value REAL DEFAULT 0,
                peak_value REAL DEFAULT 0,
                max_drawdown REAL DEFAULT 0,
                daily_pnl REAL DEFAULT 0,
                total_trades INTEGER DEFAULT 0,
                last_update TEXT
            )
        """)

        # 每日结算快照
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS paper_daily_snapshots (
                date TEXT PRIMARY KEY,
                cash REAL,
                total_value REAL,
                market_value REAL,
                max_drawdown REAL,
                daily_pnl REAL,
                trade_count INTEGER,
                positions TEXT
            )
        """)

        conn.commit()

    logger.info("模拟盘表初始化完成")


def save_paper_position(
    code: str,
    total_quantity: int,
    available_quantity: int,
    frozen_quantity: int,
    avg_cost_price: float,
    market_value: float,
    data_dir: str = "./data"
) -> None:
    """保存虚拟持仓"""
    db_path = _get_db_path(data_dir)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT OR REPLACE INTO paper_positions
               (code, total_quantity, available_quantity, frozen_quantity,
                avg_cost_price, market_value, last_update)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (code, total_quantity, available_quantity, frozen_quantity,
             avg_cost_price, market_value, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()


def get_paper_position(code: str, data_dir: str = "./data") -> Optional[Dict]:
    """获取单只股票虚拟持仓"""
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM paper_positions WHERE code = ?",
            (code,)
        )
        row = cursor.fetchone()

    if not row:
        return None

    return {
        'code': row[0],
        'total_quantity': row[1],
        'available_quantity': row[2],
        'frozen_quantity': row[3],
        'avg_cost_price': row[4],
        'market_value': row[5],
        'last_update': row[6]
    }


def get_all_paper_positions(data_dir: str = "./data") -> List[Dict]:
    """获取所有虚拟持仓"""
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return []

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM paper_positions")
        rows = cursor.fetchall()

    return [
        {
            'code': row[0],
            'total_quantity': row[1],
            'available_quantity': row[2],
            'frozen_quantity': row[3],
            'avg_cost_price': row[4],
            'market_value': row[5],
            'last_update': row[6]
        }
        for row in rows
    ]


def delete_paper_position(code: str, data_dir: str = "./data") -> None:
    """删除虚拟持仓"""
    db_path = _get_db_path(data_dir)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM paper_positions WHERE code = ?", (code,))
        conn.commit()


def save_paper_trade(
    code: str,
    direction: str,
    price: float,
    quantity: int,
    amount: float,
    fee: float,
    stamp_tax: float,
    trade_date: str,
    trade_time: str,
    status: str,
    pnl: float = 0,
    data_dir: str = "./data"
) -> str:
    """保存虚拟交易记录，返回交易ID"""
    db_path = _get_db_path(data_dir)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO paper_trades
               (code, direction, price, quantity, amount, fee, stamp_tax,
                trade_date, trade_time, pnl, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, direction, price, quantity, amount, fee, stamp_tax,
             trade_date, trade_time, pnl, status)
        )
        conn.commit()
        return str(cursor.lastrowid)


def save_paper_account(
    cash: float,
    total_value: float,
    peak_value: float,
    max_drawdown: float,
    data_dir: str = "./data"
) -> None:
    """保存虚拟账户状态"""
    db_path = _get_db_path(data_dir)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT OR REPLACE INTO paper_account
               (id, cash, total_value, peak_value, max_drawdown, last_update)
               VALUES (1, ?, ?, ?, ?, ?)""",
            (cash, total_value, peak_value, max_drawdown,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()


def get_paper_account(data_dir: str = "./data") -> Optional[Dict]:
    """获取虚拟账户状态"""
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM paper_account WHERE id = 1")
        row = cursor.fetchone()

    if not row:
        return None

    return {
        'id': row[0],
        'cash': row[1],
        'total_value': row[2],
        'peak_value': row[3],
        'max_drawdown': row[4],
        'daily_pnl': row[5],
        'total_trades': row[6],
        'last_update': row[7]
    }


def save_paper_daily_snapshot(
    date: str,
    cash: float,
    total_value: float,
    market_value: float,
    max_drawdown: float,
    daily_pnl: float,
    trade_count: int,
    positions: List[Dict],
    data_dir: str = "./data"
) -> None:
    """保存每日结算快照"""
    db_path = _get_db_path(data_dir)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT OR REPLACE INTO paper_daily_snapshots
               (date, cash, total_value, market_value, max_drawdown,
                daily_pnl, trade_count, positions)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, cash, total_value, market_value, max_drawdown,
             daily_pnl, trade_count, json.dumps(positions))
        )
        conn.commit()


def get_paper_daily_snapshots(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    data_dir: str = "./data"
) -> Optional[pd.DataFrame]:
    """获取每日结算快照"""
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    query = "SELECT * FROM paper_daily_snapshots"
    params = []
    conditions = []

    if start_date:
        conditions.append("date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("date <= ?")
        params.append(end_date)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY date"

    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return None
    return df

