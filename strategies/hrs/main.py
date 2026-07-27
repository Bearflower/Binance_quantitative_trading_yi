"""
混合反转策略 HRS 主入口
负责加载配置、初始化客户端、执行策略逻辑
"""
import asyncio
import os
from datetime import datetime
import yaml
import structlog

from shared.binance_api import BinanceClient
from shared.database import DatabaseManager
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.trade_logger import TradeLogger
from shared.utils import setup_logging
from shared import condition_orders
from strategies.hrs.strategy import HRSStrategy


logger = structlog.get_logger()


async def main():
    """主函数，执行 HRS 策略的完整流程"""
    log_level = os.getenv("LOG_LEVEL", "INFO")
    log_format = os.getenv("LOG_FORMAT", "json")
    setup_logging(level=log_level, format=log_format)

    logger.info("HRS策略启动", timestamp=datetime.now().isoformat())

    try:
        # 加载配置
        config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        logger.info("配置加载成功", config_path=config_path)

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
            "DATABASE_PASSWORD",
        ]

        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"缺少必要的环境变量: {', '.join(missing_vars)}")

        # 初始化客户端
        binance_client = BinanceClient(
            api_key=os.getenv("BINANCE_API_KEY"),
            api_secret=os.getenv("BINANCE_API_SECRET"),
            testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true",
        )

        kline_service = KLineService(
            service_url=os.getenv("KLINE_SERVICE_URL"),
            timeout=int(os.getenv("KLINE_SERVICE_TIMEOUT", "10")),
        )

        notification_client = NotificationClient(
            service_url=os.getenv("NOTIFICATION_SERVICE_URL"),
            timeout=int(os.getenv("NOTIFICATION_SERVICE_TIMEOUT", "10")),
        )

        db_manager = DatabaseManager(
            host=os.getenv("DATABASE_HOST"),
            port=int(os.getenv("DATABASE_PORT", "5432")),
            database=os.getenv("DATABASE_NAME"),
            user=os.getenv("DATABASE_USER"),
            password=os.getenv("DATABASE_PASSWORD"),
            min_pool_size=int(os.getenv("DATABASE_MIN_POOL_SIZE", "5")),
            max_pool_size=int(os.getenv("DATABASE_MAX_POOL_SIZE", "20")),
        )

        logger.info("客户端初始化完成")

        # 创建策略实例
        strategy = HRSStrategy(config=config)

        await strategy.set_binance_client(binance_client)
        await strategy.set_kline_service(kline_service)
        await strategy.set_notification_client(notification_client)
        await strategy.set_database(db_manager)

        await strategy.initialize()
        logger.info("策略初始化完成，开始运行")

        async with binance_client, kline_service, notification_client:
            await db_manager.connect()

            trade_logger = TradeLogger(db_manager, "HRS策略")
            await trade_logger.ensure_table_exists()
            # 初始化条件单记录表（用于孤儿条件单清理和订单追踪）
            await condition_orders.ensure_table(db_manager)
            binance_client.set_trade_logger(trade_logger)
            logger.info("交易记录器初始化完成", strategy="HRS策略")

            try:
                await strategy.run()
            finally:
                await db_manager.disconnect()

    except KeyboardInterrupt:
        logger.info("策略被用户中断")

    except Exception as e:
        logger.error("策略执行失败", error=str(e), exc_info=True)

        try:
            if "notification_client" in locals() and "db_manager" in locals():
                # 检查异常告警通知开关
                notif_config = config.get("notification", {})
                notif_events = notif_config.get("events", {})
                if notif_config.get("enabled", True) and notif_events.get("anomaly_alert", True):
                    await notification_client.send(
                        message=f"【HRS策略执行失败】\n错误信息: {str(e)}",
                        level="error",
                        project="hrs",
                    )
        except Exception:
            pass

        raise

    finally:
        logger.info("HRS策略结束", timestamp=datetime.now().isoformat())


if __name__ == "__main__":
    asyncio.run(main())