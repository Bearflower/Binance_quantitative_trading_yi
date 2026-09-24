"""
佣金回填任务

在 main_docker.py lifespan 中由 APScheduler IntervalTrigger 调度，事后把
Binance userTrades 中的真实佣金回填到 trading.trade_records.commission
（下单结果不含 commission 字段，落库恒为 0）。

与 equity_snapshot_job.py 同构：复用 data_service 已有的 BinanceClient / DatabaseManager
（优先使用 data_service 上已初始化的 _binance_client / _db_manager / _trade_logger），
逻辑收敛在 shared/trade_logger.TradeLogger.reconcile_commissions，本文件仅做宿主调度；
失败仅记日志告警，不抛出、不阻断主流程。
"""
import os

import structlog

logger = structlog.get_logger()


async def run_reconcile(data_service) -> dict:
    """执行一次佣金回填

    复用 data_service 上已初始化的 _binance_client / _db_manager / _trade_logger，
    调用 TradeLogger.reconcile_commissions。任一步骤失败仅记日志，不抛出异常。

    Args:
        data_service: DataService 实例（需具备 _ensure_initialized、_binance_client、
                      _db_manager、_trade_logger 属性）

    Returns:
        dict: 回填汇总 {queried_orders, matched_orders, total_commission}；
              失败时返回 {error} 最小信息。
    """
    try:
        await data_service._ensure_initialized()
        # 回填时间窗口（小时），走环境变量注入，禁止硬编码
        lookback_hours = int(os.getenv("COMMISSION_RECONCILE_LOOKBACK_HOURS", "24"))
        summary = await data_service._trade_logger.reconcile_commissions(
            data_service._binance_client,
            lookback_hours=lookback_hours,
        )
        # 注意：不在此处重复打印"佣金回填完成"——reconcile_commissions 内部已打汇总日志，
        # 外层再打会造成同一条结果出现两条日志（首轮双跑排查时发现）。
        return summary
    except Exception as e:
        logger.error("佣金回填任务失败", error=str(e))
        return {"error": str(e)}