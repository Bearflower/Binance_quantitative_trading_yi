#!/usr/bin/env python3
"""
网格交易策略回测主入口
运行ETHUSDT网格交易策略回测
"""
import sys
import os
import structlog
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from backtest.grid.backtest_engine import BacktestEngine
from backtest.grid.performance_analyzer import PerformanceAnalyzer
from backtest.grid.report_generator import ReportGenerator


logger = structlog.get_logger()


def main():
    """
    主函数
    """
    try:
        # 配置文件路径
        config_path = Path(__file__).parent / 'config.yaml'

        logger.info("=" * 60)
        logger.info("ETHUSDT网格交易策略回测")
        logger.info("=" * 60)

        # 1. 初始化回测引擎
        logger.info("步骤1: 初始化回测引擎")
        engine = BacktestEngine(str(config_path))

        # 2. 运行回测
        logger.info("步骤2: 运行回测")
        result = engine.run()

        # 3. 性能分析
        logger.info("步骤3: 性能分析")
        analyzer = PerformanceAnalyzer(result['config'])
        analysis_result = analyzer.analyze(
            trades=result['trades'],
            equity_curve=result['equity_curve'],
            initial_balance=engine.initial_balance
        )

        # 4. 生成报告
        logger.info("步骤4: 生成报告")
        report_generator = ReportGenerator(result['config'])
        report_path = report_generator.generate(
            statistics=result['statistics'],
            trades=result['trades'],
            equity_curve=result['equity_curve'],
            analysis_result=analysis_result,
            config=result['config']
        )

        # 5. 输出摘要
        logger.info("步骤5: 输出摘要")
        summary = analyzer.generate_summary(analysis_result)
        print(summary)

        logger.info("=" * 60)
        logger.info("回测完成")
        logger.info(f"报告路径: {report_path}")
        logger.info("=" * 60)

        return 0

    except Exception as e:
        logger.error(f"回测执行失败: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
