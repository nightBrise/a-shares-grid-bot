#!/usr/bin/env python3
"""
A 股网格交易系统 v1.0.0 - 主程序入口
功能：统一调度选股、优化、信号生成三大模块

使用方式:
    python main.py              # 使用 config.yaml 中的 mode
    python main.py --mode select   # 强制指定模式
    python main.py --version       # 显示版本号
"""

import sys
import os
import logging
import argparse
from datetime import datetime

# 定义版本号
__version__ = "0.2.0"

# 导入项目模块
from utils import load_config, load_state, save_state, setup_logging, send_notification, get_version
from strategy import execute_strategy


def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description=f'A 股网格交易系统 v{__version__}',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                      # 使用配置文件中的默认模式
  python main.py --mode select        # 执行选股模式
  python main.py --mode optimize      # 执行参数优化
  python main.py --mode signal        # 生成交易信号
  python main.py --mode wf            # 执行 Walk-Forward 分析
  python main.py --config my_config.yaml  # 使用自定义配置文件
  python main.py --force-select       # 强制重新选股 (忽略已有股票池)
  python main.py --rolling 1m         # 每月滚动一次 Walk-Forward 分析
  python main.py --wf-date 2024-12-31 # 指定 Walk-Forward 分析的当前日期
  python main.py --version            # 显示版本号
        """
    )
    
    parser.add_argument(
        '--mode', '-m',
        type=str,
        choices=['select', 'optimize', 'signal', 'wf'],
        help='运行模式 (覆盖配置文件设置)'
    )
    
    parser.add_argument(
        '--config', '-c',
        type=str,
        default='config.yaml',
        help='配置文件路径 (默认：config.yaml)'
    )
    
    parser.add_argument(
        '--state', '-s',
        type=str,
        default='config_state.json',
        help='状态文件路径 (默认：config_state.json)'
    )
    
    parser.add_argument(
        '--log-level', '-l',
        type=str,
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='日志级别 (默认：INFO)'
    )
    
    parser.add_argument(
        '--force-select', '-f',
        action='store_true',
        help='强制重新选股 (忽略配置文件中已有的股票列表)'
    )

    parser.add_argument(
        '--rolling',
        type=str,
        metavar='PERIOD',
        help='Walk-Forward 滚动周期 (如：1m=每月，1q=每季，1w=每周). 仅用于 wf 模式'
    )
    
    parser.add_argument(
        '--wf-date',
        type=str,
        metavar='DATE',
        help='Walk-Forward 分析的当前日期 T (格式：YYYY-MM-DD). 默认为今天'
    )
    
    parser.add_argument(
        '--version', '-v',
        action='version',
        version=f'%(prog)s {__version__}'
    )
    
    return parser.parse_args()


def main():
    """主函数"""
    # 解析命令行参数
    args = parse_arguments()
    
    # 加载配置
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"错误：配置文件不存在 - {args.config}")
        print("请确保 config.yaml 文件存在")
        sys.exit(1)
    except Exception as e:
        print(f"错误：加载配置文件失败 - {str(e)}")
        sys.exit(1)
    
    # 加载状态
    try:
        state = load_state(args.state)
    except Exception as e:
        print(f"警告：加载状态文件失败 - {str(e)}, 使用默认状态")
        state = load_state()
    
    # 获取版本号 (优先从配置文件读取，否则使用代码中的版本)
    version = config.get('version', __version__)
    
    # 确定运行模式
    mode = args.mode or config.get('mode', 'select')
    
    # 获取输出目录
    output_dir = config.get('paths', {}).get('output_dir', './output')
    
    # 获取日志配置
    log_config = config.get('logging', {})
    log_level = args.log_level or log_config.get('level', 'INFO')
    backup_count = log_config.get('backup_count', 30)
    max_bytes = log_config.get('max_bytes', 0)
    
    # 设置日志
    logger = setup_logging(
        output_dir=output_dir, 
        level=log_level,
        version=version,
        backup_count=backup_count,
        max_bytes=max_bytes
    )
    
    # 打印欢迎信息
    logger.info("=" * 70)
    logger.info(f"       A 股网格交易系统 v{version}")
    logger.info("=" * 70)
    logger.info(f"启动时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"运行模式：{mode.upper()}")
    logger.info(f"配置文件：{args.config}")
    logger.info(f"状态文件：{args.state}")
    
    # Walk-Forward 相关参数
    if args.rolling:
        logger.info(f"滚动周期：{args.rolling}")
    if args.wf_date:
        logger.info(f"Walk-Forward 当前日期：{args.wf_date}")
    
    logger.info("=" * 70)
    
    # 检查交易时间 (可选)
    risk_cfg = config.get('risk', {})
    if risk_cfg.get('check_trading_time', False):
        from utils import check_trading_time
        if not check_trading_time():
            logger.warning("当前不在 A 股交易时段，是否继续运行？")
            # 不强制退出，仅提示
    
    # 更新运行时状态
    state['runtime_state']['last_run_date'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    state['runtime_state']['last_run_mode'] = mode
    save_state(state, args.state)
    
    # 发送启动通知
    send_notification(f"系统启动 - 模式:{mode.upper()}, 版本:{version}", config)
    
    # 执行策略
    success = False
    result = None
    
    try:
        # Walk-Forward 模式特殊处理
        if mode == 'wf':
            from datetime import datetime as dt
            from strategy import run_walk_forward_analysis
            
            # 解析当前日期
            wf_current_date = None
            if args.wf_date:
                try:
                    wf_current_date = dt.strptime(args.wf_date, '%Y-%m-%d')
                except ValueError:
                    logger.error(f"无效的日期格式：{args.wf_date}. 请使用 YYYY-MM-DD 格式")
                    sys.exit(1)
            
            # 执行 Walk-Forward 分析
            logger.info("\n开始执行 Walk-Forward 分析...")
            result = run_walk_forward_analysis(
                config, 
                current_date=wf_current_date,
                rolling_period=args.rolling
            )
            success = result is not None
            
        else:
            # 传统模式
            result = execute_strategy(mode, args.config)
            
            if result is not None:
                if isinstance(result, int):
                    logger.info(f"\n执行完成，返回结果：{result}")
                elif hasattr(result, '__len__'):
                    logger.info(f"\n执行完成，生成 {len(result)} 条记录")
                else:
                    logger.info(f"\n执行完成")
                
                success = True
            else:
                logger.warning("执行返回空结果")
        
    except KeyboardInterrupt:
        logger.error("\n用户中断执行")
        sys.exit(1)
        
    except Exception as e:
        logger.exception(f"程序执行异常：{str(e)}")
        send_notification(f"系统错误 - {str(e)}", config)
        sys.exit(1)
    
    # 打印结束信息
    logger.info("=" * 70)
    if success:
        logger.info("✓ 任务执行成功")
        logger.info(f"输出目录：{os.path.abspath(output_dir)}")
        
        if mode == 'wf':
            logger.info("Walk-Forward 分析输出文件:")
            logger.info("  - wf_stock_selection_*.csv : 选股池构建结果")
            logger.info("  - wf_optimization_report_*.json : 参数优化报告")
            logger.info("  - wf_summary_report_*.json : 汇总报告")
        else:
            logger.info("请查看 output/ 目录下的文件:")
            logger.info("  - signals.csv  : 交易信号 (信号模式)")
            logger.info("  - report.json  : 优化报告 (优化模式)")
            logger.info("  - stock_selection.csv : 选股结果 (选股模式)")
            logger.info("  - log.txt      : 运行日志")
    else:
        logger.warning("⚠ 任务执行失败，请检查日志")
    logger.info("=" * 70)
    
    # 发送完成通知
    if success:
        send_notification(f"任务完成 - 模式:{mode.upper()}, 版本:{version}", config)
    
    # 返回状态码
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
