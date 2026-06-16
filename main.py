#!/usr/bin/env python3
"""
A 股网格交易系统 v2.0.0 - 主程序入口
功能：统一调度选股、两阶段优化、信号生成三大模块

使用方式:
    python main.py --download-db    # 智能下载/更新全市场数据
    python main.py --select         # 执行选股（严格本地模式）
    python main.py --optimize       # 两阶段优化（贝叶斯 + WF微调）
    python main.py --backtest       # 历史回测
    python main.py --version        # 显示版本号
"""

import sys
import os
import argparse
from datetime import datetime

# 定义版本号
__version__ = "2.0.0"

# 导入项目模块
from utils.utils import load_config, load_state, save_state, setup_logging, send_notification
from trading_core.strategy import execute_strategy


def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description=f'A 股网格交易系统 v{__version__}',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py --download-db        # 智能下载/更新全市场数据
  python main.py --select             # 执行选股（严格本地模式）
  python main.py --optimize           # 两阶段优化（贝叶斯 + WF微调）
  python main.py --backtest           # 历史回测
  python main.py --select --force-select  # 强制重新选股
  python main.py --config my_config.yaml  # 使用自定义配置文件
  python main.py --version            # 显示版本号
        """
    )
    
    parser.add_argument(
        '--select',
        action='store_true',
        help='执行选股模式'
    )

    parser.add_argument(
        '--optimize',
        action='store_true',
        help='执行两阶段优化（贝叶斯 + WF微调）'
    )

    parser.add_argument(
        '--backtest',
        action='store_true',
        help='执行历史回测'
    )
    
    parser.add_argument(
        '--config', '-c',
        type=str,
        default='configuration/config.yaml',
        help='配置文件路径 (默认：configuration/config.yaml)'
    )
    
    parser.add_argument(
        '--state', '-s',
        type=str,
        default='configuration/config_state.json',
        help='状态文件路径 (默认：configuration/config_state.json)'
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
        '--download-db',
        action='store_true',
        help='智能下载/更新全市场历史数据到 SQLite'
    )

    parser.add_argument(
        '--download-start-date',
        type=str,
        help='下载起始日期 (YYYY-MM-DD)，默认 3 年前 1 月 1 日'
    )

    parser.add_argument(
        '--download-max-stocks',
        type=int,
        help='限制下载股票数量（调试用）'
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
        print("请确保 configuration/config.yaml 文件存在")
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
    if args.select:
        mode = 'select'
    elif args.optimize:
        mode = 'optimize'
    elif args.backtest:
        mode = 'backtest'
    else:
        mode = config.get('mode', 'select')

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

    logger.info("=" * 70)

    # 处理数据库下载/更新命令
    if args.download_db:
        from data_layer.fetcher import smart_download_or_update
        data_dir = config.get('paths', {}).get('data_dir', './data')
        dl_cfg = config.get('database_download', {})

        logger.info("=" * 70)
        logger.info("           智能数据下载/更新")
        logger.info("=" * 70)
        result = smart_download_or_update(
            data_dir=data_dir,
            resume=dl_cfg.get('resume', True),
            batch_size=dl_cfg.get('batch_size', 100),
            min_delay=config.get('network', {}).get('min_delay_per_stock', 10.0),
            max_delay=config.get('network', {}).get('max_delay_per_stock', 20.0),
            skip_min_records=dl_cfg.get('skip_min_record_count', 100),
            days_threshold=dl_cfg.get('update_threshold_days', 30),
            max_stocks=args.download_max_stocks,
            start_date=args.download_start_date
        )
        logger.info(f"执行结果: {result}")

        logger.info("=" * 70)
        logger.info("任务完成")
        logger.info("=" * 70)
        sys.exit(0)

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
        result = execute_strategy(mode, args.config, force_select=args.force_select)

        if result is not None:
            if isinstance(result, int):
                logger.info(f"\n执行完成，返回结果：{result}")
            elif hasattr(result, '__len__'):
                logger.info(f"\n执行完成，生成 {len(result)} 条记录")
            else:
                logger.info("\n执行完成")

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
        logger.info("任务执行成功")
        logger.info(f"输出目录：{os.path.abspath(output_dir)}")
        logger.info("请查看 output/ 目录下的文件:")
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
