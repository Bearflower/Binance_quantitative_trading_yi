"""
混合反转策略 HRS 主入口
负责加载配置、初始化客户端、执行策略逻辑
"""
import asyncio
import os
from datetime import datetime
import structlog

from shared.binance_api import BinanceClient
from shared.config_loader import load_strategy_config_with_metadata, record_loaded_version
from shared.database import DatabaseManager
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.trade_logger import TradeLogger
from shared.utils import setup_logging
from shared import condition_orders
from strategies.hrs.strategy import HRSStrategy
from strategies.hrs.decision_context import capture_configuration_identity
from strategies.hrs.equity_service import HrsEquityService
from strategies.hrs.funding_inventory import FundingInventoryCollector
from ai_tuner.snapshots.repository import SnapshotRepository


logger = structlog.get_logger()


def validate_equity_gate_configuration():
    value=os.getenv('HRS_EQUITY_GATE_ENABLED','').strip().lower()
    if value not in ('','0','false','no','off','1','true','yes','on'):
        raise ValueError('invalid HRS_EQUITY_GATE_ENABLED')
    enabled=value in ('1','true','yes','on')
    guarded={item.strip() for item in os.getenv('ENTRY_EQUITY_GATE_STRATEGIES','').split(',') if item.strip()}
    if enabled and 'hrs' not in guarded:
        raise ValueError('HRS_EQUITY_GATE_ENABLED requires hrs in ENTRY_EQUITY_GATE_STRATEGIES')
    return enabled


async def run_with_equity_service(strategy,db_manager,binance_client):
    """Refresh before entries; a denied projection keeps normal exit monitoring.

    Fatal background termination is supervised. Shutdown joins supervised tasks before
    the caller releases database/API resources, including cancellation paths.
    """
    if not validate_equity_gate_configuration():
        return await strategy.run()
    service=HrsEquityService(db_manager,binance_client,strategy.capital_mgr,
                             strategy.position_manager,recorder=getattr(strategy,'decision_recorder',None))
    strategy.equity_service=service
    recorder=getattr(strategy,'decision_recorder',None)
    funding=None
    tasks=[]
    try:
        if recorder is not None:
            funding=FundingInventoryCollector(binance_client,recorder,account_id=os.getenv('TRADE_ACCOUNT_ID','default'))
            await funding.collect_once()
        result=await service.refresh()
        logger.info('HRS equity projection initialized',ready=result['ready'],allowed=result['allowed'],reasons=result['reasons'])
        collector=asyncio.create_task(service.run(),name='hrs-equity-collector')
        trading=asyncio.create_task(strategy.run(),name='hrs-strategy')
        tasks=[collector,trading]
        supervised={collector:'equity collector'}
        if funding is not None:
            funding_task=asyncio.create_task(funding.run(),name='hrs-funding-inventory-collector')
            tasks.append(funding_task)
            supervised[funding_task]='funding inventory collector'
        done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for background,label in supervised.items():
            if background in done:
                if background.cancelled():
                    raise RuntimeError('HRS '+label+' stopped unexpectedly')
                raise RuntimeError('HRS '+label+' stopped unexpectedly') from background.exception()
        return await trading
    finally:
        async def shutdown():
            try:
                await service.close()
            finally:
                try:
                    if funding is not None:
                        await funding.close()
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                    strategy.equity_service=None
        cleanup=asyncio.create_task(shutdown())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


async def main():
    """主函数，执行 HRS 策略的完整流程"""
    log_level = os.getenv("LOG_LEVEL", "INFO")
    log_format = os.getenv("LOG_FORMAT", "json")
    setup_logging(level=log_level, format=log_format)

    logger.info("HRS策略启动", timestamp=datetime.now().isoformat())

    try:
        validate_equity_gate_configuration()
        # 加载配置（合并基础配置 + AI 调优覆盖层）
        config, tuner_metadata = load_strategy_config_with_metadata(os.path.dirname(__file__))
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

        snapshot_db_manager = None
        snapshot_db_user = os.getenv("AI_TUNER_SNAPSHOT_DB_USER")
        snapshot_db_password = os.getenv("AI_TUNER_SNAPSHOT_DB_PASSWORD")
        if snapshot_db_user and snapshot_db_password:
            snapshot_db_manager = DatabaseManager(
                host=os.getenv("DATABASE_HOST"),
                port=int(os.getenv("DATABASE_PORT", "5432")),
                database=os.getenv("DATABASE_NAME"),
                user=snapshot_db_user,
                password=snapshot_db_password,
                min_pool_size=1,
                max_pool_size=2,
            )
        else:
            logger.warning("HRS universe 快照停采：独立最小权限数据库凭据未配置")

        logger.info("客户端初始化完成")

        # 创建策略实例
        strategy = HRSStrategy(config=config)
        strategy.loaded_config_metadata = tuner_metadata
        strategy.release_sha = os.getenv('HRS_RELEASE_SHA')
        if os.getenv('HRS_EVIDENCE_CAPTURE_ENABLED') == '1':
            identity = capture_configuration_identity(os.path.dirname(__file__), config, tuner_metadata)
            strategy.configuration_evidence = identity
            strategy.config_hashes = identity['config_hashes']
        record_loaded_version("hrs", tuner_metadata)

        await strategy.set_binance_client(binance_client)
        await strategy.set_kline_service(kline_service)
        await strategy.set_notification_client(notification_client)
        await strategy.set_database(db_manager)
        if snapshot_db_manager is not None:
            strategy.snapshot_repository = SnapshotRepository(snapshot_db_manager)

        await strategy.initialize()
        logger.info("策略初始化完成，开始运行")

        async with binance_client, kline_service, notification_client:
            await db_manager.connect()

            trade_logger = TradeLogger(db_manager, "hrs")
            await trade_logger.ensure_table_exists()
            # 初始化条件单记录表（用于孤儿条件单清理和订单追踪）
            await condition_orders.ensure_table(db_manager)
            binance_client.set_trade_logger(trade_logger)
            logger.info("交易记录器初始化完成", strategy="HRS策略")

            try:
                await run_with_equity_service(strategy,db_manager,binance_client)
            finally:
                if snapshot_db_manager is not None:
                    await snapshot_db_manager.disconnect()
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
