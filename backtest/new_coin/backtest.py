"""
新币做空策略回测主程序
运行回测并生成报告
"""
import argparse
import os
import sys

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from backtest.new_coin.backtest_engine import BacktestEngine
import structlog


logger = structlog.get_logger()


def main():
    """主函数"""
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='新币做空策略回测')
    parser.add_argument(
        '--config',
        type=str,
        default='strategies/new_coin/config.yaml',
        help='配置文件路径'
    )
    parser.add_argument(
        '--initial_balance',
        type=float,
        default=500,
        help='初始资金（USDT）'
    )
    parser.add_argument(
        '--start_date',
        type=str,
        default='2025-01-01',
        help='开始日期'
    )
    parser.add_argument(
        '--end_date',
        type=str,
        default='2025-12-31',
        help='结束日期'
    )
    
    args = parser.parse_args()
    
    # 更新配置
    import yaml
    with open(args.config, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 添加回测配置
    config['backtest'] = {
        'initial_balance': args.initial_balance,
        'commission_rate': 0.0004,
        'slippage_rate': 0.0001,
        'leverage': 2,
        'start_date': args.start_date,
        'end_date': args.end_date,
        'data_dir': 'backtest/new_coin/data',
        'mode': 'bar_by_bar'
    }
    
    # 保存临时配置文件
    temp_config_path = 'backtest/new_coin/temp_config.yaml'
    with open(temp_config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True)
    
    logger.info("=" * 60)
    logger.info("新币做空策略回测")
    logger.info("=" * 60)
    logger.info(f"配置文件: {args.config}")
    logger.info(f"初始资金: {args.initial_balance} USDT")
    logger.info(f"时间范围: {args.start_date} ~ {args.end_date}")
    logger.info("=" * 60)
    
    try:
        # 创建回测引擎
        engine = BacktestEngine(temp_config_path)
        
        # 运行回测
        result = engine.run()
        
        # 打印结果摘要
        logger.info("\n" + "=" * 60)
        logger.info("回测结果摘要")
        logger.info("=" * 60)
        logger.info(f"总交易次数: {result['statistics']['total_trades']}")
        logger.info(f"胜率: {result['statistics']['win_rate'] * 100:.2f}%")
        logger.info(f"总盈亏: {result['statistics']['total_pnl']:.2f} USDT")
        logger.info(f"总收益率: {result['statistics']['total_return'] * 100:.2f}%")
        logger.info(f"最大回撤: {result['statistics']['max_drawdown']:.2f} USDT ({result['statistics']['max_drawdown_percent'] * 100:.2f}%)")
        logger.info(f"盈亏比: {result['statistics']['profit_loss_ratio']:.2f}")
        logger.info(f"夏普比率: {result['statistics']['sharpe_ratio']:.2f}")
        logger.info(f"最终资金: {result['statistics']['final_balance']:.2f} USDT")
        logger.info("=" * 60)
        logger.info(f"\n详细报告: {result['report_path']}")
        logger.info("=" * 60)
        
        # 清理临时配置文件
        if os.path.exists(temp_config_path):
            os.remove(temp_config_path)
        
    except Exception as e:
        logger.error(f"回测执行失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
