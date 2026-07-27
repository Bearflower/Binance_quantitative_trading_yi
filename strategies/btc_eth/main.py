"""
主流币种趋势回调确认策略（MTPCS）主入口
类型：趋势跟踪 / 回调反弹入场 + 趋势确认
负责加载配置、初始化客户端、执行策略逻辑
支持定时调度（每小时整点执行）
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
import yaml
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from shared.binance_api import BinanceClient
from shared.database import DatabaseManager
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.trade_logger import TradeLogger
from shared.utils import setup_logging
from shared.strategy_state import save_strategy_state
from shared import condition_orders
from strategies.btc_eth.strategy import BTCEthStrategy

# 北京时区（从环境变量读取偏移量，默认 UTC+8）
_timezone_offset_hours = int(os.getenv("TIMEZONE_OFFSET_HOURS", "8"))
BEIJING_TZ = timezone(timedelta(hours=_timezone_offset_hours))


def get_beijing_time() -> datetime:
    """获取当前北京时间"""
    return datetime.now(BEIJING_TZ)


logger = structlog.get_logger()

# 全局变量，保存客户端和策略实例
config = None
binance_client = None
kline_service = None
notification_client = None
strategy = None


async def initialize():
    """
    初始化客户端和策略
    """
    global config, binance_client, kline_service, notification_client, strategy
    
    logger.info("初始化MTPCS策略（主流币种趋势回调确认策略）...")
    
    # 加载配置
    config_path = os.path.join(
        os.path.dirname(__file__),
        "config.yaml"
    )
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    logger.info("配置加载成功", config_path=config_path)
    
    # 验证必要的环境变量
    required_env_vars = [
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "KLINE_SERVICE_URL",
        "NOTIFICATION_SERVICE_URL"
    ]
    
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]
    if missing_vars:
        raise ValueError(f"缺少必要的环境变量: {', '.join(missing_vars)}")
    
    # 初始化客户端
    binance_client = BinanceClient(
        api_key=os.getenv("BINANCE_API_KEY"),
        api_secret=os.getenv("BINANCE_API_SECRET"),
        testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true",
        use_unified_account=os.getenv("BINANCE_USE_PM", "true").lower() == "true"  # 默认使用PM账户
    )
    
    # 初始化数据库和交易记录器（自动记录所有下单到 trading.trade_records）
    db_manager = DatabaseManager(
        host=os.getenv("DATABASE_HOST", "postgres"),
        port=int(os.getenv("DATABASE_PORT", "5432")),
        database=os.getenv("DATABASE_NAME", "trading_platform"),
        user=os.getenv("DATABASE_USER", "trading_user"),
        password=os.getenv("DATABASE_PASSWORD", "trading_password_2024")
    )
    await db_manager.connect()
    
    trade_logger = TradeLogger(db_manager, "MTPCS策略")
    await trade_logger.ensure_table_exists()
    binance_client.set_trade_logger(trade_logger)
    logger.info("交易记录器初始化完成", strategy="MTPCS策略")
    
    kline_service = KLineService(
        service_url=os.getenv("KLINE_SERVICE_URL"),
        timeout=int(os.getenv("KLINE_SERVICE_TIMEOUT", "10"))
    )
    
    notification_client = NotificationClient(
        service_url=os.getenv("NOTIFICATION_SERVICE_URL"),
        timeout=int(os.getenv("NOTIFICATION_SERVICE_TIMEOUT", "10"))
    )
    
    logger.info("客户端初始化完成")
    
    # 创建策略实例
    strategy = BTCEthStrategy(
        config=config,
        binance_client=binance_client,
        kline_service=kline_service,
        notification_client=notification_client,
        db_manager=db_manager
    )
    
    # 初始化条件单记录表（用于孤儿条件单清理和订单追踪）
    await condition_orders.ensure_table(db_manager)

    # 初始化频率控制状态表并加载历史状态
    await strategy.frequency_controller.ensure_table_exists()
    await strategy.frequency_controller.load_state()
    
    # 启动时孤儿条件单检测与清理（v6.23）
    await strategy._startup_orphan_cleanup()
    
    # 确保持仓有止损止盈保护单（v6.20.4：检测缺少条件单的旧持仓并自动补单）
    await strategy._ensure_position_protection()
    
    return config


async def run_strategy():
    """
    执行策略分析（定时任务）
    """
    global binance_client, kline_service, notification_client, strategy
    
    logger.info("MTPCS策略启动", timestamp=get_beijing_time().isoformat())
    
    try:
        # 执行策略
        async with binance_client, kline_service, notification_client:
            # 1. 先更新持仓状态（执行止盈止损）
            logger.info("检查持仓状态并执行止盈止损...")
            await strategy.update_positions()
            
            # 2. 分析市场并生成新信号
            logger.info("开始执行策略分析", symbols=strategy.symbols)
            
            # 同步持仓状态：清除交易所已不存在的僵尸持仓记录
            await strategy._sync_positions_with_exchange()

            # 条件单查询API已废弃，跳过每周期孤儿条件单清理
            # 孤儿条件单由第1层（平仓清理）和第2层（残留扫描）通过本地algoId处理

            signals = []
            analysis_results = []  # 保存所有币种的分析结果
            
            for symbol in strategy.symbols:
                try:
                    # 分析市场
                    result = await strategy.analyze(symbol)
                    
                    # 保存分析结果（analyze()方法现在总是返回结果）
                    analysis_results.append(result)
                    
                    # 如果生成了信号，执行交易
                    if 'direction' in result:  # 有direction表示是信号
                        signals.append(result)
                        
                        # 执行交易信号
                        success = await strategy.execute_signal(result)
                        
                        if success:
                            logger.info(
                                f"{symbol} 交易信号执行成功",
                                direction=result['direction'],
                                grade=result['grade'],
                                score=result['score']
                            )
                            # 开仓成功后立即保存 strategy_states（全部持仓）
                            # 必须保存全部持仓，避免覆盖原有持仓记录导致孤儿单清理任务误判
                            positions = {}
                            for sym, pos in strategy.positions.items():
                                positions[sym] = {
                                    "direction": pos.direction,
                                    "entry_price": float(pos.entry_price) if pos.entry_price else None,
                                    "quantity": float(pos.initial_quantity) if pos.initial_quantity else 0,
                                    "current_quantity": float(pos.current_quantity) if pos.current_quantity else 0,
                                    "entry_time": str(pos.entry_time) if pos.entry_time else "",
                                    "entry_order_id": pos.entry_order_id,
                                    "stop_loss_order_id": pos.stop_loss_order_id,
                                    "tp1_order_id": pos.tp1_order_id,
                                    "tp2_order_id": pos.tp2_order_id,
                                }
                            if positions:
                                await save_strategy_state(strategy.db_manager, "btc_eth", positions)
                        else:
                            logger.error(
                                f"{symbol} 交易信号执行失败",
                                direction=result['direction'],
                                grade=result['grade']
                            )
                    else:
                        logger.info(f"{symbol} 未生成交易信号: {result.get('reason', '未知原因')}")
                
                except Exception as e:
                    logger.error(
                        f"{symbol} 策略执行失败",
                        error=str(e),
                        exc_info=True
                    )
                    # 记录失败的分析结果
                    analysis_results.append({
                        'symbol': symbol,
                        'score': 0,
                        'grade': 'D',
                        'reason': f'执行异常: {str(e)}'
                    })
                    continue
            
            # 汇总结果
            logger.info(
                "策略执行完成",
                total_symbols=len(strategy.symbols),
                signals_generated=len(signals),
                timestamp=get_beijing_time().isoformat()
            )
            
            # 生成详细的分析结果通知
            summary_message = f"【MTPCS策略】{get_beijing_time().strftime('%H:%M')} 分析完成\n"
            summary_message += "\n📊 币种分析报告：\n"
            summary_message += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            
            # 添加每个币种的详细分析信息
            for result in analysis_results:
                symbol = result.get('symbol', 'UNKNOWN')
                score = result.get('score', 0)
                grade = result.get('grade', 'D')
                reason = result.get('reason', '')
                
                # 如果是信号，显示详细信息
                if 'direction' in result:
                    direction = result['direction']
                    entry_price = result.get('entry_price', 0)
                    stop_loss = result.get('initial_stop_loss', 0)
                    tp1_price = result.get('tp1_price', 0)
                    tp2_price = result.get('tp2_price', 0)
                    
                    summary_message += f"{symbol}: 评分 {score} ({grade}级) - {direction} ✅\n"
                    summary_message += f"  入场价: {float(entry_price):.4f}\n"
                    summary_message += f"  止损: {float(stop_loss):.4f}\n"
                    summary_message += f"  止盈: {float(tp1_price):.4f}\n"
                else:
                    # 未生成信号的情况
                    if '数据不完整' in reason or '数据获取失败' in reason:
                        summary_message += f"{symbol}: 数据不足 - 跳过\n"
                        summary_message += f"  原因: {reason}\n"
                    else:
                        summary_message += f"{symbol}: 评分 {score} ({grade}级) - 未达标\n"
                        summary_message += f"  原因: {reason}\n"
            
            summary_message += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            
            # 添加总结信息
            summary_message += "\n📈 总结：\n"
            summary_message += f"- 分析币种: {len(analysis_results)}个\n"
            summary_message += f"- 生成信号: {len(signals)}个\n"
            
            if signals:
                summary_message += "- 交易信号: "
                signal_strs = []
                for sig in signals:
                    signal_strs.append(f"{sig['symbol']} {sig['direction']} ({sig['grade']}级)")
                summary_message += ", ".join(signal_strs) + "\n"
            
            # 重试发送通知（重试次数和间隔从配置读取）
            notify_config = config.get('notification', {})
            max_retries = notify_config.get('max_retries', 3)
            retry_interval = notify_config.get('retry_interval_seconds', 1)
            for retry in range(max_retries):
                try:
                    await notification_client.send(
                        message=summary_message,
                        level="info",
                        project="btc_eth"
                    )
                    break
                except Exception as e:
                    if retry < max_retries - 1:
                        await asyncio.sleep(retry_interval)
                    else:
                        logger.error("通知发送失败", error=str(e))

            # 保存策略状态到 strategy_states（用于 orphan_cleanup 统一检测）
            positions = {}
            for symbol, pos in strategy.positions.items():
                positions[symbol] = {
                    "direction": pos.direction,
                    "entry_price": float(pos.entry_price) if pos.entry_price else None,
                    "quantity": float(pos.initial_quantity) if pos.initial_quantity else 0,
                    "current_quantity": float(pos.current_quantity) if pos.current_quantity else 0,
                    "entry_time": str(pos.entry_time) if pos.entry_time else "",
                    "entry_order_id": pos.entry_order_id,
                    "stop_loss_order_id": pos.stop_loss_order_id,
                    "tp1_order_id": pos.tp1_order_id,
                    "tp2_order_id": pos.tp2_order_id,
                }
            await save_strategy_state(strategy.db_manager, "btc_eth", positions)

    except Exception as e:
        logger.error(
            "策略执行失败",
            error=str(e),
            exc_info=True
        )
        
        # 发送错误通知
        try:
            if notification_client:
                await notification_client.send_alert(
                    title="MTPCS策略执行失败",
                    message=f"错误信息: {str(e)}",
                    level="error"
                )
        except Exception as notify_error:
            logger.error(
                "发送错误通知失败",
                error=str(notify_error)
            )
    
    finally:
        logger.info("MTPCS策略本次执行结束", timestamp=get_beijing_time().isoformat())


async def send_daily_report():
    """
    发送每日交易日报（定时任务）
    """
    global binance_client, notification_client, strategy
    
    logger.info("开始发送MTPCS策略日报")
    
    try:
        async with binance_client, notification_client:
            # 获取昨日交易统计
            yesterday = get_beijing_time().replace(hour=0, minute=0, second=0, microsecond=0)
            
            # 从频率控制器获取统计数据
            stats = strategy.frequency_controller.get_daily_stats(yesterday)
            
            # 构建日报内容
            report_message = f"""
【MTPCS策略日报】
日期: {yesterday.strftime('%Y-%m-%d')}

📊 交易统计:
- 总交易次数: {stats.get('total_trades', 0)}
- 盈利次数: {stats.get('win_count', 0)}
- 亏损次数: {stats.get('loss_count', 0)}
- 胜率: {stats.get('win_rate', 0):.2f}%

💰 盈亏统计:
- 总盈亏: {stats.get('total_pnl', 0):.2f} USDT
- 最大盈利: {stats.get('max_profit', 0):.2f} USDT
- 最大亏损: {stats.get('max_loss', 0):.2f} USDT

📈 风险控制:
- 日亏损限额: {strategy.risk_config['frequency_control']['max_daily_loss_usdt']} USDT
- 连续亏损次数: {stats.get('consecutive_losses', 0)}

💡 策略状态:
- 运行正常 ✅
- 下次分析时间: {(get_beijing_time() + timedelta(hours=1)).replace(minute=25).strftime('%H:%M')}
"""
            
            # 发送日报
            await notification_client.send(
                message=report_message,
                level="info",
                project="btc_eth"
            )
            
            logger.info("MTPCS策略日报发送成功")
    
    except Exception as e:
        logger.error(
            "发送日报失败",
            error=str(e),
            exc_info=True
        )
        
        # 发送错误通知
        try:
            if notification_client:
                await notification_client.send_alert(
                    title="MTPCS策略日报发送失败",
                    message=f"错误信息: {str(e)}",
                    level="error"
                )
        except Exception as notify_error:
            logger.error(
                "发送错误通知失败",
                error=str(notify_error)
            )


async def main():
    """
    主函数
    初始化并启动调度器
    """
    # 配置日志
    log_level = os.getenv("LOG_LEVEL", "INFO")
    log_format = os.getenv("LOG_FORMAT", "json")
    setup_logging(level=log_level, format=log_format)
    
    try:
        # 初始化
        config = await initialize()
        
        # 创建调度器
        scheduler = AsyncIOScheduler()
        
        # 添加定时任务：从配置读取 cron 表达式
        cron_expr = config['strategy']['schedule'].get('cron', '0 * * * *')
        scheduler.add_job(
            run_strategy,
            trigger=CronTrigger.from_crontab(cron_expr),
            id='btc_eth_strategy',
            name='MTPCS策略定时执行（主流币种趋势回调确认）',
            replace_existing=True
        )
        
        # 启动调度器
        scheduler.start()
        logger.info("调度器已启动", cron=cron_expr)
        
        # 立即执行一次（可选）
        logger.info("启动时立即执行一次策略...")
        await run_strategy()
        
        # 保持运行（从配置读取循环间隔，默认 3600 秒）
        loop_interval = config.get('schedule', {}).get('loop_interval_seconds', 3600)
        logger.info("策略服务运行中，等待下次调度...")
        while True:
            await asyncio.sleep(loop_interval)
            
    except KeyboardInterrupt:
        logger.info("收到停止信号，正在关闭...")
        if 'scheduler' in locals():
            scheduler.shutdown()
        logger.info("调度器已关闭")
        
    except Exception as e:
        logger.error(
            "主程序异常退出",
            error=str(e),
            exc_info=True
        )
        raise


if __name__ == "__main__":
    asyncio.run(main())
