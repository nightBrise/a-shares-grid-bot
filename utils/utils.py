"""
工具模块 - A 股网格交易系统 v2.0.0
功能：A 股风控、费用计算、日志、通知预留
"""

import os
import json
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional
import yaml


# ==================== 配置加载 ====================

def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个字典，override 值覆盖 base 值"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str = "configuration/config.yaml") -> dict:
    """
    加载配置：defaults.py（系统默认值） + config.yaml（用户覆盖） → 合并后的完整配置

    用户只需填写想覆盖的参数，其余用系统默认值。
    """
    from trading_core.defaults import get_defaults

    config = get_defaults()

    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            user_config = yaml.safe_load(f) or {}
        config = _deep_merge(config, user_config)

    return config


def load_state(state_path: str = "configuration/config_state.json") -> dict:
    """加载 JSON 动态状态文件"""
    if not os.path.exists(state_path):
        # 如果状态文件不存在，返回默认状态
        return {
            "version": "2.0.0",
            "selection_status": {
                "completed": False,
                "last_selection_date": "",
                "last_data_update_date": "",
                "selection_count": 0,
                "strategy_version": "v2.0.0"
            },
            "runtime_state": {
                "last_run_date": "",
                "last_run_mode": "",
                "consecutive_failures": 0
            }
        }
    with open(state_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_state(state: dict, state_path: str = "configuration/config_state.json") -> None:
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


def validate_buy_quantity(quantity: int) -> int:
    """
    验证买入数量：至少 100 股，向上取整到 100 的整数倍 (A 股最小交易单位)

    参数:
        quantity: 计划买入数量

    返回:
        调整后的合法数量 (至少 1 手，向上取整)
    """
    if quantity <= 0:
        return 0
    return max(100, ((quantity + 99) // 100) * 100)


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


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """安全除法 (避免除零错误)"""
    if denominator == 0:
        return default
    return numerator / denominator


def fmt_date(d: str) -> str:
    """
    转换日期格式：YYYYMMDD -> YYYY-MM-DD

    参数:
        d: 日期字符串

    返回:
        YYYY-MM-DD 格式日期，或原字符串（如果格式不符）
    """
    if d and len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d
