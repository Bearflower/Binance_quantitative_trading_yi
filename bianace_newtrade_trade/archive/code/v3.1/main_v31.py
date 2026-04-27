"""
币安新币精准做空系统 v3.1

基于 1 小时 K 线的技术面分析
- 三次冲顶形态识别
- 成交量分析
- 信号冷却机制
- 每小时第 1 分钟评分

修改内容：
1. 取消每 30 分钟评分，改为每小时第 1 分钟评分
2. 三次冲顶跨 K 线判断
3. 成交量比较基准：前 5 根 K 线平均成交量的 1.5 倍
4. 信号冷却：2 小时内不重复开仓
5. 时间窗口：新币上线 48 小时内
"""

import sys
import argparse
import signal
from datetime import datetime, time
from typing import Optional

from utils.logger import logger
from config.settings import settings
from core.scheduler import TaskScheduler, MonitoringScheduler
from core.listing_detector import NewListingDetector
from core.scoring_engine import ScoringEngine
from core.signal_manager import SignalManager
from core.technical_analyzer_v31 import technical_analyzer_v31


# 全局变量
scheduler: Optional[MonitoringScheduler] = None
running = True


def signal_handler(signum, frame):
    """信号处理函数"""
    global running
    logger.info(f"收到退出信号：{signum}")
    running = False


def init_system():
    """初始化系统组件 v3.1"""
    logger.info("=" * 60)
    logger.info("币安新币精准做空系统 v3.1")
    logger.info("基于 1 小时 K 线 + 三次冲顶形态 + 量价背离")
    logger.info("=" * 60)
    
    # 初始化各组件
    logger.info("正在初始化系统组件...")
    
    # 1. 初始化新币检测器
    detector = NewListingDetector()
    logger.info("✅ 新币检测器初始化完成")
    
    # 2. 初始化评分引擎
    scoring_engine = ScoringEngine()
    logger.info("✅ 评分引擎初始化完成")
    
    # 3. 初始化信号管理器 v3.1（带冷却机制）
    signal_manager = SignalManager()
    logger.info("✅ 信号管理器 v3.1 初始化完成（带冷却机制）")
    
    # 4. 初始化调度器
    scheduler = MonitoringScheduler()
    logger.info("✅ 监控调度器初始化完成")
    
    logger.info("=" * 60)
    logger.info("系统初始化完成，准备启动监控服务")
    logger.info("=" * 60)
    
    return detector, scoring_engine, signal_manager, scheduler


def monitor_new_coins_task_v31(detector, scoring_engine, signal_manager):
    """
    监控新币任务 v3.1
    
    仅在每小时第 1 分钟执行评分
    基于刚刚收盘的 1 小时 K 线
    """
    try:
        logger.debug("开始执行新币监控任务 v3.1...")
        
        # 检查当前时间是否为整点第 1 分钟
        now = datetime.now()
        if now.minute != 1:
            logger.debug(f"⏰ 当前时间为 {now.minute} 分，跳过评分（仅在整点第 1 分钟执行）")
            return
        
        logger.info(f"⏰ 整点第 1 分钟，开始执行评分...")
        
        # 1. 扫描新上线的币种（48 小时内）
        new_listings = detector.detect_new_listings(hours=48)
        
        if not new_listings:
            logger.debug("ℹ️ 没有符合条件的新上线币种")
            return
        
        logger.info(f"发现 {len(new_listings)} 个新上线币种（48 小时内）：{new_listings}")
        
        # 2. 对每个新币进行评分
        for symbol_data in new_listings:
            try:
                symbol = symbol_data if isinstance(symbol_data, str) else symbol_data.get('symbol')
                if not symbol:
                    continue
                
                # 获取上市时间信息
                if isinstance(symbol_data, dict):
                    listing_hours = symbol_data.get('hours_since_listing', 24)
                    listing_time = symbol_data.get('listing_time')
                else:
                    detector_listing = detector.processed_symbols.get(symbol, {})
                    listing_hours = detector_listing.get('hours_since_listing', 24)
                    listing_time_str = detector_listing.get('listing_time')
                    if listing_time_str:
                        try:
                            listing_time = datetime.fromisoformat(listing_time_str)
                        except Exception:
                            listing_time = None
                
                # 检查是否可以生成信号（冷却机制 + 时间窗口）
                can_generate, reason = signal_manager.can_generate_signal(symbol, listing_hours)
                if not can_generate:
                    logger.info(f"⏭️  {symbol} 跳过评分：{reason}")
                    continue
                
                # 获取资金费率
                from core.binance_client import binance_client
                try:
                    funding_rate = binance_client.get_funding_rate(symbol)
                except Exception as e:
                    logger.debug(f"无法获取 {symbol} 的资金费率：{e}")
                    funding_rate = None
                
                # 计算合约数据评分
                from core.contract_scorer import contract_scorer
                contract_score, contract_reason = contract_scorer.calculate_contract_score(symbol)
                logger.info(f"📊 {symbol} 合约数据评分：{contract_score:.2f}/10.0 ({contract_reason})")
                
                # 计算 OI/市值比率
                oi_ratio, oi_valid = contract_scorer.calculate_oi_ratio(symbol)
                if not oi_valid:
                    oi_ratio = 0.5
                
                # 基本面评分
                from core.unlock_manager import UnlockDataManager
                unlock_manager = UnlockDataManager(auto_fetch=True)
                
                if symbol not in unlock_manager.unlock_data:
                    logger.debug(f"🔄 配置文件中没有 {symbol}，尝试自动获取解锁数据...")
                    unlock_manager.auto_add_symbol(symbol)
                
                fundamental_score = unlock_manager.score_fundamental(symbol, days=90)
                
                if fundamental_score >= 7.0:
                    logger.info(f"📅 {symbol} 基本面评分：{fundamental_score:.1f} (存在大额解锁)")
                
                # 技术面评分 v3.1（基于 1 小时 K 线，包含三次冲顶和量价背离）
                technical_score, technical_details = technical_analyzer_v31.calculate_technical_score(
                    symbol, 
                    listing_hours=listing_hours
                )
                logger.info(f"📊 {symbol} 技术面评分 v3.1: {technical_score:.2f}/10.0")
                logger.debug(f"🔍 {symbol} 技术细节：{technical_details.get('reason', '')}")
                
                # 情绪面评分
                sentiment_score = 5.0
                if funding_rate is not None:
                    annual_rate = funding_rate * 3 * 365 * 100
                    is_new_coin = listing_hours < 72
                    
                    if is_new_coin:
                        if annual_rate > 200:
                            sentiment_score = 10.0
                        elif annual_rate > 100:
                            sentiment_score = 8.0
                        elif annual_rate > 50:
                            sentiment_score = 6.0
                        elif annual_rate > -100:
                            sentiment_score = 5.0
                        elif annual_rate > -500:
                            sentiment_score = 4.0
                        else:
                            sentiment_score = 3.5
                    else:
                        if annual_rate > 100:
                            sentiment_score = 10.0
                        elif annual_rate > 50:
                            sentiment_score = 7.0
                        elif annual_rate > 20:
                            sentiment_score = 6.0
                        elif annual_rate > 0:
                            sentiment_score = 5.0
                        elif annual_rate > -50:
                            sentiment_score = 4.0
                        elif annual_rate > -100:
                            sentiment_score = 3.5
                        else:
                            sentiment_score = 3.0
                
                logger.info(f"📊 {symbol} 情绪面评分：{sentiment_score:.1f}/10 (年化费率：{funding_rate * 3 * 365 * 100:.1f}%)")
                
                # 获取已评分次数
                is_rescore = symbol_data.get('is_rescore', False) if isinstance(symbol_data, dict) else False
                scoring_count = symbol_data.get('scoring_count', 0) if isinstance(symbol_data, dict) else 0
                
                # 计算本次评分是第几次
                scoring_attempt = scoring_count + 1 if is_rescore else 1
                
                logger.info(f"🔄 {symbol} 评分次数：第{scoring_attempt}次 (已评分{scoring_count}次)")
                
                # 生成评分报告
                additional_details = {
                    'is_rescore': is_rescore,
                    'scoring_attempt': scoring_attempt,
                    'scoring_count': scoring_count,
                    'funding_rate': funding_rate,
                    'technical_details': technical_details
                }
                
                current_price = 0.0
                try:
                    ticker = binance_client.get_ticker(symbol)
                    if ticker and 'lastPrice' in ticker:
                        current_price = float(ticker['lastPrice'])
                except Exception as e:
                    logger.debug(f"无法获取 {symbol} 的当前价格：{e}")
                
                report = scoring_engine.generate_scoring_report(
                    symbol=symbol,
                    contract_score=contract_score,
                    fundamental_score=fundamental_score,
                    technical_score=technical_score,
                    sentiment_score=sentiment_score,
                    oi_ratio=oi_ratio if oi_ratio else 0.5,
                    listing_hours=listing_hours,
                    listing_time=listing_time,
                    scoring_attempt=scoring_attempt,
                    additional_details=additional_details,
                    current_price=current_price
                )
                
                # 检查是否应该开仓
                if scoring_engine.should_entry(report):
                    logger.info(f"✅ {symbol} 达到开仓条件，综合评分：{report.total_score:.2f}")
                    
                    # 获取K线数据（用于ATR止损止盈计算）
                    klines = None
                    try:
                        klines = binance_client.get_kline_data(symbol, interval='1h', limit=100)
                    except Exception as e:
                        logger.debug(f"无法获取 {symbol} 的K线数据：{e}")
                    
                    # 生成交易信号
                    signal = signal_manager.generate_signal(
                        symbol=symbol,
                        scoring_result=report,
                        current_price=current_price,
                        klines=klines,
                        expire_hours=1
                    )
                    
                    if signal:
                        logger.info(f"🎯 生成信号：{signal.id[:8]}, {symbol}")
                        
                        # 标记为已交易（启动冷却）
                        signal_manager.mark_as_traded(symbol)
                        
                        # 发送信号通知
                        if settings.feishu_webhook:
                            from core.notifier import feishu_notifier
                            feishu_notifier.send_signal_notification(signal)
                            logger.info(f"📤 已发送信号通知到飞书")
                else:
                    logger.info(f"ℹ️ {symbol} 未达到开仓条件，综合评分：{report.total_score:.2f}")
                
                # 更新评分记录
                detector.update_scoring_record(
                    symbol=symbol,
                    score=report.total_score,
                    signal_generated=signal_generated,
                    scoring_attempt=scoring_attempt,
                    veto=report.veto,
                    veto_reason=report.veto_reason if report.veto else ""
                )
                
                # 发送评分完成通知（每次评分都发送，无论分数高低）
                if settings.feishu_webhook:
                    from core.notifier import feishu_notifier
                    
                    # 判断是否生成信号
                    signal_generated = not report.veto and report.total_score >= settings.min_signal_score
                    
                    # 发送评分完成通知
                    feishu_notifier.send_scoring_complete_notification(
                        symbol=symbol,
                        total_score=report.total_score,
                        scoring_attempt=scoring_attempt,
                        signal_generated=signal_generated,
                        order_placed=False,  # 当前系统需要手动确认下单
                        veto=report.veto,
                        veto_reason=report.veto_reason if report.veto else "",
                        current_price=report.current_price
                    )
                    logger.info(f"📤 已发送 {symbol} 评分完成通知")
                
            except Exception as e:
                logger.error(f"❌ 评分 {symbol} 失败：{e}", exc_info=True)
        
        logger.info("✅ 新币监控任务 v3.1 执行完成")
        
    except Exception as e:
        logger.error(f"❌ 监控任务执行失败：{e}", exc_info=True)


def start_monitoring():
    """启动监控服务 v3.1"""
    logger.info("🚀 启动币安新币精准做空系统 v3.1")
    
    # 初始化系统
    detector, scoring_engine, signal_manager, scheduler = init_system()
    
    # 注册定时任务：每小时第 1 分钟执行
    scheduler.add_cron_task(
        task_id='hourly_scoring',
        func=monitor_new_coins_task_v31,
        minute=1,  # 每小时第 1 分钟
        detector=detector,
        scoring_engine=scoring_engine,
        signal_manager=signal_manager
    )
    
    logger.info("✅ 已注册定时任务：每小时第 1 分钟执行评分")
    
    # 注册信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 启动调度器
    logger.info("🚀 启动定时任务调度器...")
    scheduler.start()
    
    logger.info("✅ 系统已启动，按 Ctrl+C 退出")
    
    try:
        # 保持运行
        import time
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("👋 正在退出...")
    finally:
        logger.info("💤 正在关闭调度器...")
        scheduler.shutdown()
        logger.info("✅ 系统已安全退出")


def view_signals():
    """查看待确认信号"""
    signal_manager = SignalManager()
    signals = signal_manager.get_pending_signals()
    
    if not signals:
        print("\nℹ️  没有待确认的信号\n")
        return
    
    print("\n" + "=" * 80)
    print("待确认信号列表")
    print("=" * 80)
    
    for sig in signals:
        print(f"\n币种：{sig.symbol}")
        print(f"信号 ID: {sig.id[:8]}")
        print(f"综合评分：{sig.scoring_result.total_score:.2f}")
        print(f"当前价格：{sig.current_price:.2f}")
        print(f"入场区间：{sig.entry_min:.2f} - {sig.entry_max:.2f}")
        print(f"止损价：{sig.stop_loss:.2f}")
        print(f"止盈价 1: {sig.take_profit_1:.2f} (20%)")
        print(f"止盈价 2: {sig.take_profit_2:.2f} (30%)")
        print(f"创建时间：{sig.created_at}")
        print(f"过期时间：{sig.expire_at}")
        print(f"剩余时间：{sig.time_remaining()}")
    
    print("\n" + "=" * 80 + "\n")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='币安新币精准做空系统 v3.1')
    
    subparsers = parser.add_subparsers(dest='command', help='可用命令')
    
    # start 命令
    start_parser = subparsers.add_parser('start', help='启动监控服务')
    start_parser.set_defaults(func=start_monitoring)
    
    # signals 命令
    signals_parser = subparsers.add_parser('signals', help='查看待确认信号')
    signals_parser.set_defaults(func=view_signals)
    
    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        sys.exit(1)
    
    args.func()


if __name__ == '__main__':
    main()
