"""
工具模块 - A 股网格交易系统 v1.6.0
功能：A 股风控、费用计算、日志、通知预留
"""

import os
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, time
from typing import Optional, Tuple, Dict
import yaml


# ==================== 配置加载 ====================

def load_config(config_path: str = "configuration/config.yaml") -> dict:
    """加载 YAML 静态配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def load_state(state_path: str = "configuration/config_state.json") -> dict:
    """加载 JSON 动态状态文件"""
    if not os.path.exists(state_path):
        # 如果状态文件不存在，返回默认状态
        return {
            "version": "1.6.0",
            "selection_status": {
                "completed": False,
                "last_selection_date": "",
                "last_data_update_date": "",
                "selection_count": 0,
                "strategy_version": "v1.6.0"
            },
            "runtime_state": {
                "last_run_date": "",
                "last_run_mode": "",
                "consecutive_failures": 0
            }
        }
    with open(state_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_state(state: dict, state_path: str = "config_state.json") -> None:
    """保存状态到 JSON 文件"""
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_version() -> str:
    """获取系统版本号"""
    try:
        config = load_config()
        return config.get('version', '1.0.0')
    except Exception:
        return "1.0.0"


def setup_logging(output_dir: str = "./output", level: str = "INFO", 
                  version: str = "1.0.0", backup_count: int = 30,
                  max_bytes: int = 0) -> logging.Logger:
    """
    设置日志系统
    - 同时输出到控制台和文件
    - 使用 RotatingFileHandler 按日期轮转日志
    - 保留最近 30 天日志
    - 日志格式包含 version 字段
    
    参数:
        output_dir: 输出目录
        level: 日志级别
        version: 系统版本号 (会包含在日志中)
        backup_count: 保留的备份文件数量 (天数)
        max_bytes: 单个日志文件最大字节数 (0 表示不限制)
    """
    # 创建输出目录
    os.makedirs(output_dir, exist_ok=True)
    
    # 配置根日志器
    logger = logging.getLogger("grid_trading")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # 清除已有处理器
    logger.handlers.clear()
    
    # 自定义日志格式器 - 包含版本号
    class VersionFormatter(logging.Formatter):
        """自定义格式器，添加 version 字段"""
        def __init__(self, version: str, fmt: str, datefmt: str = None):
            super().__init__(fmt, datefmt)
            self.version = version
        
        def format(self, record):
            record.version = self.version
            return super().format(record)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_fmt = '[v%(version)s] %(asctime)s | %(levelname)-8s | %(message)s'
    console_formatter = VersionFormatter(version, console_fmt, '%Y-%m-%d %H:%M:%S')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # 文件处理器 - 使用 RotatingFileHandler 实现日志轮转
    log_file = os.path.join(output_dir, "log.txt")
    file_handler = RotatingFileHandler(
        filename=log_file,
        encoding='utf-8',
        maxBytes=max_bytes if max_bytes > 0 else 10*1024*1024,  # 默认 10MB
        backupCount=backup_count
    )
    file_fmt = '[v%(version)s] %(asctime)s | %(levelname)-8s | %(funcName)s:%(lineno)d | %(message)s'
    file_formatter = VersionFormatter(version, file_fmt, '%Y-%m-%d %H:%M:%S')
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    
    return logger


# ==================== A 股风控规则 ====================

def check_trading_time() -> bool:
    """
    检查当前时间是否为 A 股交易时段
    - 交易日：周一至周五
    - 交易时段：9:30-11:30, 13:00-15:00
    """
    now = datetime.now()
    
    # 检查周末
    if now.weekday() >= 5:
        return False
    
    # 检查交易时段
    morning_start = time(9, 30)
    morning_end = time(11, 30)
    afternoon_start = time(13, 0)
    afternoon_end = time(15, 0)
    
    current_time = now.time()
    
    is_trading = (morning_start <= current_time <= morning_end or 
                  afternoon_start <= current_time <= afternoon_end)
    
    return is_trading


def check_limit_status(current_price: float, pre_close: float, 
                       threshold: float = 9.8) -> Tuple[bool, bool]:
    """
    检查涨跌停状态
    
    参数:
        current_price: 当前价格
        pre_close: 昨日收盘价
        threshold: 涨跌停阈值 (默认 9.8%)
    
    返回:
        (is_limit_up, is_limit_down): 涨停标志，跌停标志
    """
    if pre_close <= 0:
        return False, False
    
    change_pct = abs(current_price - pre_close) / pre_close * 100
    
    is_limit_up = change_pct >= threshold and current_price > pre_close
    is_limit_down = change_pct >= threshold and current_price < pre_close
    
    return is_limit_up, is_limit_down


def check_t1_rule(sell_quantity: int, available_position: int) -> bool:
    """
    T+1 规则检查：卖出数量不能超过可用持仓
    
    参数:
        sell_quantity: 计划卖出数量
        available_position: 可用持仓 (昨日持仓)
    
    返回:
        是否符合 T+1 规则
    """
    return sell_quantity <= available_position


def validate_buy_quantity(quantity: int) -> int:
    """
    验证买入数量：必须是 100 的整数倍 (A 股最小交易单位)
    
    参数:
        quantity: 计划买入数量
    
    返回:
        调整后的合法数量 (向下取整到 100 的倍数)
    """
    return (quantity // 100) * 100


# ==================== A 股费用计算 ====================

def calculate_transaction_fee(trade_amount: float, trade_type: str,
                              commission_rate: float = 0.00015,
                              stamp_tax: float = 0.0005,
                              transfer_fee: float = 0.00002) -> float:
    """
    计算 A 股交易费用
    
    费用构成:
    1. 佣金：成交金额 × 费率，最低 5 元 (买卖双向收取)
    2. 印花税：成交金额 × 费率 (仅卖出收取)
    3. 过户费：成交金额 × 费率 (买卖双向收取)
    
    参数:
        trade_amount: 成交金额 (元)
        trade_type: 'buy' 或 'sell'
        commission_rate: 佣金费率 (默认万 1.5)
        stamp_tax: 印花税率 (默认万 5)
        transfer_fee: 过户费率 (默认万 2)
    
    返回:
        总费用 (元)
    """
    # 佣金 (最低 5 元)
    commission = max(trade_amount * commission_rate, 5.0)
    
    # 印花税 (仅卖出)
    tax = trade_amount * stamp_tax if trade_type == 'sell' else 0.0
    
    # 过户费
    transfer = trade_amount * transfer_fee
    
    total_fee = commission + tax + transfer
    
    return total_fee


# ==================== 通知预留接口 ====================

def send_notification(message: str, config: Optional[dict] = None) -> bool:
    """
    发送交易通知 (预留接口)
    
    当前实现：仅打印到日志
    未来扩展：可接入钉钉、企业微信、邮件等
    
    参数:
        message: 通知内容
        config: 配置字典 (包含 Webhook URL 等)
    
    返回:
        是否发送成功
    """
    logger = logging.getLogger("grid_trading")
    logger.info(f"[通知预留] {message}")
    
    # TODO: 未来可扩展以下功能
    # 1. 钉钉机器人: requests.post(config['DINGTALK_WEBHOOK'], json={...})
    # 2. 企业微信: requests.post(wechat_work_url, json={...})
    # 3. 邮件通知: smtplib 发送邮件
    # 4. 短信通知: 阿里云 SMS 等
    
    return True


# ==================== 辅助函数 ====================

def format_number(num: float, decimals: int = 2) -> str:
    """格式化数字显示 (千分位分隔符)"""
    return f"{num:,.{decimals}f}"


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """安全除法 (避免除零错误)"""
    if denominator == 0:
        return default
    return numerator / denominator
