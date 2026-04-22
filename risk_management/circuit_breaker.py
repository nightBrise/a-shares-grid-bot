"""
实盘熔断风控模块 - A 股网格交易系统 v1.6.0
功能：
  - 实时监控持仓浮动盈亏
  - 监控总账户净值回撤
  - 触发熔断机制（暂停买入/全局停止）
  - 可配置开关控制

作者：量化专家助手
警告：风控参数需谨慎设置，过度严格可能导致错失机会
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

logger = logging.getLogger("grid_trading")


# ==================== 数据结构定义 ====================

@dataclass
class PositionInfo:
    """持仓信息数据类"""
    code: str                      # 股票代码
    cost_price: float              # 成本价
    current_price: float           # 当前市价
    quantity: int                  # 持仓数量
    unrealized_pnl: float          # 未实现盈亏（元）
    unrealized_pnl_pct: float      # 未实现盈亏比例（%）
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AccountStatus:
    """账户状态数据类"""
    total_value: float             # 账户总市值
    peak_value: float              # 历史最高市值
    drawdown: float                # 当前回撤（%）
    positions: List[PositionInfo]  # 持仓列表
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d['positions'] = [p.to_dict() for p in self.positions]
        return d


@dataclass
class CircuitBreakerState:
    """熔断器状态数据类"""
    is_global_breaker: bool        # 全局熔断标志
    single_stock_breakers: Dict[str, bool]  # 单只股票熔断标志 {code: True/False}
    trigger_reason: str            # 触发原因
    trigger_time: str              # 触发时间
    
    def to_dict(self) -> dict:
        return asdict(self)


# ==================== 熔断风控核心逻辑 ====================

class RiskControlManager:
    """
    实盘熔断风控管理器
    
    触发条件:
    1. 单只股票未实现亏损 ≥ 15% → 暂停该股买入，仅允许卖出
    2. 总账户最大回撤 ≥ 10% → 全局停止所有买入，仅保留卖出
    
    使用方法:
        rc = RiskControlManager(config)
        
        # 每次生成信号前检查
        state = rc.check_circuit_breaker(account_status)
        
        if state.is_global_breaker:
            logger.warning("全局熔断触发，停止所有买入")
            # 过滤所有买入信号
        
        if state.single_stock_breakers.get(code):
            logger.warning(f"{code} 个股熔断，停止买入")
            # 过滤该股买入信号
    """
    
    def __init__(self, config: dict):
        """
        初始化风控管理器
        
        参数:
            config: 配置字典（包含 risk_control 配置项）
        """
        self.config = config
        self.enabled = config.get('risk_control', {}).get('enabled', True)
        
        # 风控阈值
        self.single_stock_loss_threshold = config.get(
            'risk_control', {}
        ).get('single_stock_loss_threshold', 0.15)  # 15%
        
        self.max_drawdown_threshold = config.get(
            'risk_control', {}
        ).get('max_drawdown_threshold', 0.10)  # 10%
        
        # 熔断状态持久化文件
        self.state_file = config.get('paths', {}).get(
            'risk_state_file', 
            './output/risk_state.json'
        )
        
        # 当前熔断状态
        self.current_state = CircuitBreakerState(
            is_global_breaker=False,
            single_stock_breakers={},
            trigger_reason="",
            trigger_time=""
        )
        
        # 历史峰值（用于计算回撤）
        self.peak_value = config.get('risk_control', {}).get('initial_peak', 1000000.0)
        
        # 加载历史状态
        self._load_state()
        
        logger.info("=" * 70)
        logger.info("实盘熔断风控模块已初始化")
        logger.info(f"启用状态：{'是' if self.enabled else '否'}")
        logger.info(f"单股亏损阈值：{self.single_stock_loss_threshold*100:.1f}%")
        logger.info(f"最大回撤阈值：{self.max_drawdown_threshold*100:.1f}%")
        logger.info(f"历史峰值：{self.peak_value:,.2f}")
        logger.info("=" * 70)
    
    def _load_state(self):
        """从文件加载熔断状态"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state_data = json.load(f)
                
                self.current_state = CircuitBreakerState(
                    is_global_breaker=state_data.get('is_global_breaker', False),
                    single_stock_breakers=state_data.get('single_stock_breakers', {}),
                    trigger_reason=state_data.get('trigger_reason', ''),
                    trigger_time=state_data.get('trigger_time', '')
                )
                
                self.peak_value = state_data.get('peak_value', self.peak_value)
                
                logger.info(f"已加载历史熔断状态：{self.state_file}")
                
            except Exception as e:
                logger.warning(f"加载熔断状态失败：{str(e)}, 使用默认状态")
        else:
            logger.info("未找到历史熔断状态文件，使用默认状态")
    
    def _save_state(self):
        """保存熔断状态到文件"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            
            state_data = {
                'is_global_breaker': self.current_state.is_global_breaker,
                'single_stock_breakers': self.current_state.single_stock_breakers,
                'trigger_reason': self.current_state.trigger_reason,
                'trigger_time': self.current_state.trigger_time,
                'peak_value': self.peak_value,
                'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(state_data, f, ensure_ascii=False, indent=2)
            
            logger.debug(f"熔断状态已保存：{self.state_file}")
            
        except Exception as e:
            logger.error(f"保存熔断状态失败：{str(e)}")
    
    def calculate_unrealized_pnl(self, cost_price: float, 
                                  current_price: float, 
                                  quantity: int) -> Tuple[float, float]:
        """
        计算未实现盈亏
        
        参数:
            cost_price: 成本价
            current_price: 当前市价
            quantity: 持仓数量
        
        返回:
            (unrealized_pnl, unrealized_pnl_pct): 未实现盈亏（元），未实现盈亏比例（%）
        """
        if cost_price <= 0 or quantity <= 0:
            return 0.0, 0.0
        
        # 未实现盈亏 = (当前价 - 成本价) × 数量
        unrealized_pnl = (current_price - cost_price) * quantity
        
        # 未实现盈亏比例 = (当前价 - 成本价) / 成本价
        unrealized_pnl_pct = (current_price - cost_price) / cost_price
        
        return unrealized_pnl, unrealized_pnl_pct
    
    def check_single_stock_breaker(self, position: PositionInfo) -> bool:
        """
        检查单只股票是否触发熔断
        
        触发条件：未实现亏损 ≥ 15%
        
        参数:
            position: 持仓信息
        
        返回:
            是否触发熔断
        """
        if not self.enabled:
            return False
        
        # 检查是否已触发
        if position.unrealized_pnl_pct <= -self.single_stock_loss_threshold:
            logger.warning(
                f"⚠️  个股熔断触发：{position.code} "
                f"未实现亏损 {position.unrealized_pnl_pct*100:.2f}% "
                f"(阈值：{self.single_stock_loss_threshold*100:.1f}%)"
            )
            return True
        
        return False
    
    def check_global_breaker(self, account_status: AccountStatus) -> bool:
        """
        检查是否触发全局熔断
        
        触发条件：总账户最大回撤 ≥ 10%
        
        参数:
            account_status: 账户状态
        
        返回:
            是否触发全局熔断
        """
        if not self.enabled:
            return False
        
        # 更新历史峰值
        if account_status.total_value > self.peak_value:
            self.peak_value = account_status.total_value
            logger.info(f"账户市值创新高：{self.peak_value:,.2f}")
        
        # 计算当前回撤
        if self.peak_value > 0:
            current_drawdown = (self.peak_value - account_status.total_value) / self.peak_value
        else:
            current_drawdown = 0.0
        
        # 检查是否触发
        if current_drawdown >= self.max_drawdown_threshold:
            logger.error(
                f"🚨 全局熔断触发！"
                f"当前回撤 {current_drawdown*100:.2f}% "
                f"(阈值：{self.max_drawdown_threshold*100:.1f}%) "
                f"峰值：{self.peak_value:,.2f}, 当前：{account_status.total_value:,.2f}"
            )
            return True
        
        return False
    
    def check_circuit_breaker(self, account_status: AccountStatus) -> CircuitBreakerState:
        """
        执行完整的熔断检查
        
        参数:
            account_status: 账户状态
        
        返回:
            CircuitBreakerState: 熔断器状态
        """
        if not self.enabled:
            logger.debug("风控模块已禁用，跳过熔断检查")
            return CircuitBreakerState(
                is_global_breaker=False,
                single_stock_breakers={},
                trigger_reason="风控模块已禁用",
                trigger_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
        
        logger.info("-" * 70)
        logger.info("开始执行熔断检查...")
        
        # 重置状态
        self.current_state.single_stock_breakers = {}
        self.current_state.is_global_breaker = False
        self.current_state.trigger_reason = ""
        
        # 检查全局熔断
        global_triggered = self.check_global_breaker(account_status)
        
        if global_triggered:
            self.current_state.is_global_breaker = True
            self.current_state.trigger_reason = f"总账户回撤超过阈值 {self.max_drawdown_threshold*100:.1f}%"
            self.current_state.trigger_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            logger.error("全局熔断已激活，暂停所有买入信号")
        
        # 检查单只股票熔断
        for position in account_status.positions:
            if self.check_single_stock_breaker(position):
                self.current_state.single_stock_breakers[position.code] = True
                logger.warning(f"{position.code} 个股熔断已激活，暂停该股买入")
        
        # 保存状态
        self._save_state()
        
        # 输出检查结果
        logger.info(f"熔断检查结果:")
        logger.info(f"  全局熔断：{'是' if self.current_state.is_global_breaker else '否'}")
        logger.info(f"  个股熔断：{len(self.current_state.single_stock_breakers)}只")
        
        if self.current_state.single_stock_breakers:
            for code in self.current_state.single_stock_breakers.keys():
                logger.info(f"    - {code}")
        
        logger.info("-" * 70)
        
        return self.current_state
    
    def should_allow_buy(self, code: str) -> bool:
        """
        检查是否允许买入某只股票
        
        参数:
            code: 股票代码
        
        返回:
            是否允许买入
        """
        if not self.enabled:
            return True
        
        # 全局熔断：禁止所有买入
        if self.current_state.is_global_breaker:
            return False
        
        # 个股熔断：禁止该股买入
        if self.current_state.single_stock_breakers.get(code):
            return False
        
        return True
    
    def should_allow_sell(self, code: str) -> bool:
        """
        检查是否允许卖出某只股票
        
        参数:
            code: 股票代码
        
        返回:
            是否允许卖出（熔断机制下始终允许卖出）
        """
        # 熔断机制下始终允许卖出（止损）
        return True
    
    def reset_global_breaker(self) -> bool:
        """
        手动重置全局熔断
        
        使用场景：市场恢复后，人工介入解除熔断
        
        返回:
            是否成功重置
        """
        if not self.enabled:
            return False
        
        if self.current_state.is_global_breaker:
            logger.info("手动重置全局熔断...")
            self.current_state.is_global_breaker = False
            self.current_state.trigger_reason = "手动重置"
            self.current_state.trigger_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self._save_state()
            return True
        
        return False
    
    def get_account_status(self, positions_data: List[Dict], 
                           cash: float = 0.0) -> AccountStatus:
        """
        从原始数据构建账户状态
        
        参数:
            positions_data: 持仓数据列表
                [{
                    'code': '600519.SH',
                    'cost_price': 1800.0,
                    'current_price': 1750.0,
                    'quantity': 100
                }, ...]
            cash: 现金余额
        
        返回:
            AccountStatus: 账户状态
        """
        positions = []
        total_value = cash
        
        for pos_data in positions_data:
            code = pos_data['code']
            cost_price = pos_data['cost_price']
            current_price = pos_data['current_price']
            quantity = pos_data['quantity']
            
            # 计算未实现盈亏
            pnl, pnl_pct = self.calculate_unrealized_pnl(
                cost_price, current_price, quantity
            )
            
            # 持仓市值
            market_value = current_price * quantity
            
            total_value += market_value
            
            positions.append(PositionInfo(
                code=code,
                cost_price=cost_price,
                current_price=current_price,
                quantity=quantity,
                unrealized_pnl=pnl,
                unrealized_pnl_pct=pnl_pct
            ))
        
        return AccountStatus(
            total_value=total_value,
            peak_value=self.peak_value,
            drawdown=(self.peak_value - total_value) / self.peak_value if self.peak_value > 0 else 0,
            positions=positions
        )


# ==================== 辅助函数 ====================

def create_risk_control_manager(config: dict) -> RiskControlManager:
    """
    创建风控管理器实例
    
    参数:
        config: 配置字典
    
    返回:
        RiskControlManager: 风控管理器
    """
    return RiskControlManager(config)


def filter_signals_by_risk(signals_df, risk_state: CircuitBreakerState, 
                           risk_manager: RiskControlManager) -> any:
    """
    根据风控状态过滤交易信号
    
    参数:
        signals_df: 信号 DataFrame
        risk_state: 熔断器状态
        risk_manager: 风控管理器
    
    返回:
        过滤后的信号 DataFrame
    """
    import pandas as pd
    
    if signals_df.empty:
        return signals_df
    
    df = signals_df.copy()
    
    # 过滤买入信号
    buy_mask = df['direction'] == 'buy'
    
    for idx in df[buy_mask].index:
        code = df.loc[idx, 'code']
        
        # 检查是否允许买入
        if not risk_manager.should_allow_buy(code):
            logger.warning(f"风控过滤：移除 {code} 的买入信号")
            df.loc[idx, 'filtered'] = True
            df.loc[idx, 'filter_reason'] = 'risk_control'
    
    # 标记未被过滤的信号
    if 'filtered' not in df.columns:
        df['filtered'] = False
        df['filter_reason'] = ''
    
    # 返回未被过滤的信号
    return df[~df['filtered']]


# ==================== 测试代码 ====================

if __name__ == "__main__":
    # 简单测试
    logging.basicConfig(level=logging.INFO)
    
    # 模拟配置
    test_config = {
        'risk_control': {
            'enabled': True,
            'single_stock_loss_threshold': 0.15,
            'max_drawdown_threshold': 0.10,
            'initial_peak': 1000000.0
        },
        'paths': {
            'risk_state_file': './test_risk_state.json'
        }
    }
    
    # 创建风控管理器
    rc = RiskControlManager(test_config)
    
    # 模拟持仓数据
    positions_data = [
        {
            'code': '600519.SH',
            'cost_price': 1800.0,
            'current_price': 1750.0,
            'quantity': 100
        },
        {
            'code': '000858.SZ',
            'cost_price': 15.0,
            'current_price': 12.0,
            'quantity': 1000
        }
    ]
    
    # 构建账户状态
    account = rc.get_account_status(positions_data, cash=500000)
    
    print(f"\n账户状态:")
    print(f"  总市值：{account.total_value:,.2f}")
    print(f"  历史峰值：{account.peak_value:,.2f}")
    print(f"  当前回撤：{account.drawdown*100:.2f}%")
    
    print(f"\n持仓明细:")
    for pos in account.positions:
        print(f"  {pos.code}: 成本={pos.cost_price:.2f}, 当前={pos.current_price:.2f}, "
              f"盈亏={pos.unrealized_pnl:.2f} ({pos.unrealized_pnl_pct*100:.2f}%)")
    
    # 执行熔断检查
    state = rc.check_circuit_breaker(account)
    
    print(f"\n熔断状态:")
    print(f"  全局熔断：{state.is_global_breaker}")
    print(f"  个股熔断：{state.single_stock_breakers}")
    
    # 测试买入许可
    for code in ['600519.SH', '000858.SZ', '601318.SH']:
        allow_buy = rc.should_allow_buy(code)
        print(f"  {code} 允许买入：{allow_buy}")
    
    # 清理测试文件
    if os.path.exists('./test_risk_state.json'):
        os.remove('./test_risk_state.json')
