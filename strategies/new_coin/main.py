"""
新币做空策略主入口
负责加载配置、初始化客户端、执行策略逻辑
"""
import asyncio
import os
from datetime import datetime
import structlog

from shared.binance_api import BinanceClient
from shared.config_loader import load_strategy_config
from shared.database import DatabaseManager
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.trade_logger import TradeLogger
from shared.utils import setup_logging
from shared import condition_orders
from strategies.new_coin.strategy import NewCoinStrategy


logger = structlog.get_logger()


async def main():
    """
    主函数
    执行新币做空策略的完整流程
    """
    # 配置日志
    log_level = os.getenv("LOG_LEVEL", "INFO")
    log_format = os.getenv("LOG_FORMAT", "json")
    setup_logging(level=log_level, format=log_format)

    logger.info("新币做空策略启动", timestamp=datetime.now().isoformat())

    try:
        # 加载配置（合并基础配置 + AI 调优覆盖层）
        config = load_strategy_config(os.path.dirname(__file__))
        logger.info("配置加载成功（含覆盖层合并）", strategy_dir=os.path.dirname(__file__))

        # 验证必要的环境变量
        required_env_vars = [
            "BINANCE_API_KEY",
            "BINANCE_API_SECRET",
            "KLINE_SERVICE_URL",
            "NOTIFICATION_SERVICE_URL",
            "DATABASE_HOST",
            "DATABASE_PORT",
            "DATABASE_NAME",
            "DATABASE_USER",
            "DATABASE_PASSWORD"
        ]

        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"缺少必要的环境变量: {', '.join(missing_vars)}")

        # 初始化客户端
        binance_client = BinanceClient(
            api_key=os.getenv("BINANCE_API_KEY"),
            api_secret=os.getenv("BINANCE_API_SECRET"),
            testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true"
        )

        kline_service = KLineService(
            service_url=os.getenv("KLINE_SERVICE_URL"),
            timeout=int(os.getenv("KLINE_SERVICE_TIMEOUT", "10"))
        )

        notification_client = NotificationClient(
            service_url=os.getenv("NOTIFICATION_SERVICE_URL"),
            timeout=int(os.getenv("NOTIFICATION_SERVICE_TIMEOUT", "10"))
        )

        db_manager = DatabaseManager(
            host=os.getenv("DATABASE_HOST"),
            port=int(os.getenv("DATABASE_PORT", "5432")),
            database=os.getenv("DATABASE_NAME"),
            user=os.getenv("DATABASE_USER"),
            password=os.getenv("DATABASE_PASSWORD"),
            min_pool_size=int(os.getenv("DATABASE_MIN_POOL_SIZE", "5")),
            max_pool_size=int(os.getenv("DATABASE_MAX_POOL_SIZE", "20"))
        )

        logger.info("客户端初始化完成")

        # 创建策略实例
        strategy = NewCoinStrategy(config=config)

        # 设置客户端
        await strategy.set_binance_client(binance_client)
        await strategy.set_kline_service(kline_service)
        await strategy.set_notification_client(notification_client)
        await strategy.set_database(db_manager)

        # 初始化策略
        await strategy.initialize()

        logger.info("策略初始化完成，开始运行")

        # 使用异步上下文管理器管理资源
        async with binance_client, kline_service, notification_client:
            # 连接数据库
            await db_manager.connect()
            
            # 初始化交易记录器（自动记录所有下单到 trading.trade_records）
            trade_logger = TradeLogger(db_manager, "新币做空策略")
            await trade_logger.ensure_table_exists()
            # 初始化条件单记录表（用于孤儿条件单清理和订单追踪）
            await condition_orders.ensure_table(db_manager)
            binance_client.set_trade_logger(trade_logger)
            logger.info("交易记录器初始化完成", strategy="新币做空策略")

            try:
                # 运行策略
                await strategy.run()
            finally:
                # 断开数据库连接
                await db_manager.disconnect()

    except KeyboardInterrupt:
        logger.info("策略被用户中断")

    except Exception as e:
        logger.error(
            "策略执行失败",
            error=str(e),
            exc_info=True
        )

        # 发送错误通知
        try:
            if 'notification_client' in locals() and 'db_manager' in locals():
                await notification_client.send(
                    message=f"【新币做空策略执行失败】\n错误信息: {str(e)}",
                    level="error",
                    project="new_coin"
                )
        except Exception as notify_error:
            logger.error(
                "发送错误通知失败",
                error=str(notify_error)
            )

        raise

    finally:
        logger.info("新币做空策略结束", timestamp=datetime.now().isoformat())


if __name__ == "__main__":
    asyncio.run(main())
