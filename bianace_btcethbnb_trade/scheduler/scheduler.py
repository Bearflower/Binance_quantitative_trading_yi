#!/usr/bin/env python3
"""
调度器核心模块

功能：
1. 配置和管理 APScheduler
2. 协调各模块执行流程
3. 提供统一的调度接口
"""

import os
import sys
import logging
import pytz
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import yaml

# 导入拆分后的模块
from scheduler.analyzer import MarketAnalyzer
from scheduler.trade_executor import TradeExecutor
from scheduler.statistics import StatisticsManager
from scheduler.notifier import NotificationManager

# 导入频率控制器
from services.frequency_controller import get_frequency_controller

# 导入配置
from config.settings import TIMEZONE, LARK_WEBHOOK_URL
from config.strategy_params import get_params

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/scheduler_new.log', encoding='utf-8')
    ]
)

logger = logging.getLogger('scheduler')


class RuleEngineScheduler:
    """规则引擎调度器"""

    def __init__(self, enable_auto_trade: bool = False, data_source: str = 'kline_service'):
        """
        初始化调度器

        Args:
            enable_auto_trade: 是否启用自动交易
            data_source: 数据源类型 ('kline_service' 或 'binance_api')
        """
        self.enable_auto_trade = enable_auto_trade
        self.data_source = data_source
        self.params = get_params()

        # 初始化各模块
        self.analyzer = MarketAnalyzer(data_source=data_source)
        self.statistics = StatisticsManager()
        self.notifier = NotificationManager()

        # 初始化频率控制器
        from models.database import get_db_manager
        db = get_db_manager()
        self.frequency_controller = get_frequency_controller(db)

        # 初始化交易执行器（如果启用自动交易）
        self.trade_executor = None
        if enable_auto_trade:
            self.trade_executor = TradeExecutor(self.frequency_controller)

        logger.info(f"调度器初始化完成 - 自动交易：{'已启用' if enable_auto_trade else '已禁用'}")

    def run_analysis(self) -> Dict[str, Any]:
        """
        执行一次完整的分析和交易流程

        Returns:
            执行结果
        """
        logger.info("=" * 60)
        logger.info("开始执行规则引擎分析")
        logger.info(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"数据源：{self.data_source}")
        logger.info(f"自动交易：{'已启用' if self.enable_auto_trade else '已禁用'}")
        logger.info("=" * 60)

        result = {
            'success': False,
            'timestamp': datetime.now(),
            'signals': [],
            'executed_trades': [],
            'risk_report': None,
            'message': ''
        }

        try:
            # 步骤 1: 检查已平仓订单并更新胜率统计
            logger.info("步骤 1: 检查已平仓订单并更新胜率统计...")
            if self.trade_executor and self.trade_executor.trade_api:
                self.statistics.check_closed_positions_and_update_stats(
                    self.trade_executor.trade_api
                )

            # 步骤 2: 执行市场分析
            logger.info("步骤 2: 执行市场分析...")
            trade_api = self.trade_executor.trade_api if self.trade_executor else None
            analysis_result = self.analyzer.analyze_market(trade_api=trade_api)

            if not analysis_result['success']:
                result['message'] = analysis_result['message']
                result['success'] = False
                return result

            # 检查是否需要停止交易
            if '停止交易' in analysis_result['message']:
                self.notifier.send_trading_halt_notification(
                    analysis_result['message'].replace('停止交易：', '')
                )
                result['success'] = True
                result['message'] = analysis_result['message']
                return result

            signals = analysis_result['signals']
            result['signals'] = signals
            result['risk_report'] = analysis_result['risk_report']

            # 步骤 3: 执行交易（如果启用）
            if self.enable_auto_trade and signals:
                logger.info("步骤 3: 执行自动交易...")
                executed_trades, signal_messages = self.trade_executor.execute_trades(signals)
                result['executed_trades'] = executed_trades
                result['message'] = f"执行 {len(executed_trades)}/{len(signals)} 笔交易"

                # 发送交易执行结果通知
                if signal_messages:
                    self.notifier.send_trade_execution_result(signal_messages)
            else:
                result['message'] = f"检测到 {len(signals)} 个信号（自动交易未启用）"

            # 步骤 4: 记录执行统计
            self.statistics.record_daily_stats(
                signals_count=len(signals),
                executed_count=len(result.get('executed_trades', []))
            )

            # 标记执行成功
            result['success'] = True

            # 步骤 5: 发送分析结果通知（只在有信号时发送）
            if signals:
                self.notifier.send_analysis_result(result)

        except Exception as e:
            logger.error(f"分析执行失败：{str(e)}", exc_info=True)
            result['message'] = f'执行失败：{str(e)}'
            result['success'] = False

        logger.info("=" * 60)
        logger.info(f"分析完成：{result['message']}")
        logger.info("=" * 60)

        return result

    def send_daily_report(self):
        """
        发送每日执行报告（在每天早上 9 点发送前一天的报告）
        """
        try:
            # 获取昨天的日期
            yesterday = datetime.now().date() - timedelta(days=1)

            # 生成报告
            report_data = self.statistics.generate_daily_report(yesterday)

            if not report_data:
                logger.info(f"昨天 ({yesterday}) 无统计数据")
                return

            # 发送报告
            self.notifier.send_daily_report(report_data)

        except Exception as e:
            logger.error(f"发送日报失败：{str(e)}", exc_info=True)


def run_scheduler():
    """启动调度器"""
    logger.info(f"启动规则引擎调度器（时区：{TIMEZONE}）")

    # 从配置文件读取调度器配置
    possible_paths = [
        '/app/config/scheduler_config.yaml',
        os.path.join(os.path.dirname(__file__), '..', 'config', 'scheduler_config.yaml'),
        os.path.join(os.path.dirname(__file__), '..', '..', 'config', 'scheduler_config.yaml'),
    ]

    config_loaded = False
    for config_path in possible_paths:
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            # 单数据源配置（只使用 K 线服务）
            kline_service_minute = config.get('kline_service_analysis', {}).get('minute', 25)
            daily_hour = config.get('daily_report', {}).get('hour', 9)
            daily_minute = config.get('daily_report', {}).get('minute', 5)

            logger.info(f"已从配置文件加载：{config_path}")
            logger.info(f"  K 线服务分析时间：每小时 {kline_service_minute:02d} 分")
            logger.info(f"  每日报告时间：{daily_hour:02d}:{daily_minute:02d}")
            config_loaded = True
            break
        except Exception as e:
            logger.debug(f"尝试加载 {config_path} 失败：{e}")
            continue

    if not config_loaded:
        logger.warning(f"读取配置文件失败，使用默认配置")
        kline_service_minute = 25
        daily_hour = 9
        daily_minute = 5

    # 创建调度器
    tz = pytz.timezone(TIMEZONE)
    scheduler = BlockingScheduler(timezone=tz)

    # 先清理已存在的作业，避免重复添加
    for job_id in ['binance_api_analysis', 'kline_service_analysis', 'daily_report']:
        try:
            scheduler.remove_job(job_id)
            logger.info(f"已清理已存在的 {job_id} 作业")
        except KeyError:
            pass  # 作业不存在，无需清理

    # K 线服务数据源分析 - 每小时 25 分（只保留这一个）
    scheduler.add_job(
        run_kline_service_analysis_wrapper,
        CronTrigger(hour='*', minute=kline_service_minute),
        id='kline_service_analysis',
        name='K 线服务数据源分析',
        kwargs={'enable_auto_trade': True}
    )

    # 每天早上发送前一天的日报
    scheduler.add_job(
        send_daily_report_wrapper,
        CronTrigger(hour=daily_hour, minute=daily_minute),
        id='daily_report',
        name='每日交易报告'
    )

    logger.info("调度器配置完成:")
    logger.info(f"  - K 线服务分析：每小时 {kline_service_minute:02d} 分（自动交易）")
    logger.info(f"  - 每日报告：{daily_hour:02d}:{daily_minute:02d}")
    logger.info("  - 使用 Ctrl+C 停止调度器")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止")


def run_analysis_wrapper(enable_auto_trade: bool = True):
    """调度器任务包装器"""
    scheduler_instance = RuleEngineScheduler(enable_auto_trade=enable_auto_trade)
    return scheduler_instance.run_analysis()


def run_binance_api_analysis_wrapper(enable_auto_trade: bool = True):
    """币安 API 数据源分析包装器"""
    scheduler_instance = RuleEngineScheduler(enable_auto_trade=enable_auto_trade, data_source='binance_api')
    return scheduler_instance.run_analysis()


def run_kline_service_analysis_wrapper(enable_auto_trade: bool = True):
    """K 线服务数据源分析包装器"""
    scheduler_instance = RuleEngineScheduler(enable_auto_trade=enable_auto_trade, data_source='kline_service')
    return scheduler_instance.run_analysis()


def send_daily_report_wrapper():
    """发送日报的包装器"""
    scheduler_instance = RuleEngineScheduler(enable_auto_trade=False)
    return scheduler_instance.send_daily_report()


# 数据库表创建 SQL
DAILY_STATS_TABLE_SQL = """
-- 每日执行统计表
CREATE TABLE IF NOT EXISTS daily_execution_stats (
    id SERIAL PRIMARY KEY,
    stat_date DATE NOT NULL UNIQUE,  -- 统计日期
    signals_count INTEGER DEFAULT 0,  -- 检测到的信号数量
    executed_count INTEGER DEFAULT 0,  -- 实际执行的交易数量
    win_count INTEGER DEFAULT 0,  -- 盈利交易数量
    loss_count INTEGER DEFAULT 0,  -- 亏损交易数量
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_stat_date ON daily_execution_stats(stat_date);

-- 添加注释
COMMENT ON TABLE daily_execution_stats IS '每日交易执行统计表';
COMMENT ON COLUMN daily_execution_stats.stat_date IS '统计日期';
COMMENT ON COLUMN daily_execution_stats.signals_count IS '检测到的信号数量';
COMMENT ON COLUMN daily_execution_stats.executed_count IS '实际执行的交易数量';
COMMENT ON COLUMN daily_execution_stats.win_count IS '盈利交易数量';
COMMENT ON COLUMN daily_execution_stats.loss_count IS '亏损交易数量';
"""

TRADE_RECORDS_TABLE_SQL = """
-- 交易记录表（v6.12 频率控制专用）
CREATE TABLE IF NOT EXISTS trade_records (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,  -- 交易对（如 BTCUSDT）
    direction VARCHAR(4),  -- 方向（多/空）
    open_time TIMESTAMP NOT NULL,  -- 开仓时间
    close_time TIMESTAMP,  -- 平仓时间
    pnl DECIMAL(20, 8),  -- 盈亏金额
    pnl_percent DECIMAL(10, 4),  -- 盈亏比例
    entry_price DECIMAL(20, 8),  -- 开仓价
    close_price DECIMAL(20, 8),  -- 平仓价
    quantity DECIMAL(20, 8),  -- 数量
    leverage INTEGER,  -- 杠杆倍数
    signal_grade VARCHAR(2),  -- 信号等级（S/A/B/C）
    status VARCHAR(20) DEFAULT 'OPEN',  -- 状态（OPEN/CLOSED）
    order_id BIGINT,  -- 订单 ID
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_trade_records_symbol ON trade_records(symbol);
CREATE INDEX IF NOT EXISTS idx_trade_records_open_time ON trade_records(open_time);
CREATE INDEX IF NOT EXISTS idx_trade_records_status ON trade_records(status);
CREATE INDEX IF NOT EXISTS idx_trade_records_symbol_time ON trade_records(symbol, open_time);

-- 添加注释
COMMENT ON TABLE trade_records IS '交易记录表（v6.12 频率控制专用）';
COMMENT ON COLUMN trade_records.symbol IS '交易对';
COMMENT ON COLUMN trade_records.direction IS '开仓方向';
COMMENT ON COLUMN trade_records.open_time IS '开仓时间';
COMMENT ON COLUMN trade_records.close_time IS '平仓时间';
COMMENT ON COLUMN trade_records.pnl IS '盈亏金额';
COMMENT ON COLUMN trade_records.status IS '交易状态';
COMMENT ON COLUMN trade_records.signal_grade IS '信号等级';
"""


def init_database():
    """初始化数据库表"""
    try:
        # 直接执行 SQL 创建表（避免 DSN 问题）
        from models.database import get_db_connection
        from psycopg2.extras import RealDictCursor

        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # 创建每日统计表
                cursor.execute(DAILY_STATS_TABLE_SQL)
                # 创建交易记录表（v6.12 频率控制）
                cursor.execute(TRADE_RECORDS_TABLE_SQL)
                conn.commit()

        logger.info("数据库表初始化完成：daily_execution_stats, trade_records")
    except Exception as e:
        logger.error(f"数据库表初始化失败：{str(e)}")


if __name__ == "__main__":
    # 初始化数据库表
    init_database()

    # 检查命令行参数
    if len(sys.argv) > 1:
        if sys.argv[1] == '--auto-trade':
            # 立即执行一次分析并自动交易
            logger.info("立即执行一次分析（带自动交易）...")
            scheduler = RuleEngineScheduler(enable_auto_trade=True)
            scheduler.run_analysis()
        elif sys.argv[1] == '--dry-run':
            # 立即执行一次分析（不带自动交易）
            logger.info("立即执行一次分析（不带自动交易）...")
            scheduler = RuleEngineScheduler(enable_auto_trade=False)
            scheduler.run_analysis()
        else:
            print("用法:")
            print("  python scheduler.py              # 启动调度器（每小时执行）")
            print("  python scheduler.py --auto-trade # 立即执行一次（带自动交易）")
            print("  python scheduler.py --dry-run    # 立即执行一次（不带自动交易）")
    else:
        # 启动调度器
        run_scheduler()
