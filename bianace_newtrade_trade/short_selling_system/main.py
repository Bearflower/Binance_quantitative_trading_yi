"""
币安新币精准做空系统 - 主入口

使用方法:
    python main.py start          # 启动监控服务
    python main.py signals        # 查看待确认信号
    python main.py confirm <SYMBOL>  # 确认执行交易
    python main.py cancel <SYMBOL>   # 取消信号
    python main.py --help         # 查看帮助
"""

import sys
import argparse
import signal
from datetime import datetime
from typing import Optional

from utils.logger import logger
from config.settings import settings
from core.scheduler import TaskScheduler
from core.listing_detector import NewListingDetector
from core.scoring_engine import ScoringEngine
from core.signal_manager import SignalManager


# 全局变量
scheduler: Optional[TaskScheduler] = None
running = True


def signal_handler(signum, frame):
    """信号处理函数"""
    global running
    logger.info(f"收到退出信号：{signum}")
    running = False


def init_system():
    """初始化系统组件"""
    logger.info("=" * 60)
    logger.info("币安新币精准做空系统 v1.0.0")
    logger.info("=" * 60)
    
    # 初始化各组件
    logger.info("正在初始化系统组件...")
    
    # 1. 初始化新币检测器
    detector = NewListingDetector()
    logger.info("✅ 新币检测器初始化完成")
    
    # 2. 初始化评分引擎
    scoring_engine = ScoringEngine()
    logger.info("✅ 评分引擎初始化完成")
    
    # 3. 初始化信号管理器
    signal_manager = SignalManager()
    logger.info("✅ 信号管理器初始化完成")
    
    logger.info("=" * 60)
    logger.info("系统初始化完成，准备启动监控服务")
    logger.info("=" * 60)
    
    return detector, scoring_engine, signal_manager


def monitor_new_coins_task(detector, scoring_engine, signal_manager):
    """监控新币任务"""
    try:
        logger.debug("开始执行新币监控任务...")
        
        # 1. 扫描新上线的币种
        new_listings = detector.detect_new_listings(hours=72)
        
        if new_listings:
            logger.info(f"发现 {len(new_listings)} 个新上线币种：{new_listings}")
            
            # 2. 对每个新币进行评分
            for symbol_data in new_listings:
                try:
                    symbol = symbol_data if isinstance(symbol_data, str) else symbol_data.get('symbol')
                    if not symbol:
                        continue
                    
                    # 初始化变量（避免未定义错误）
                    listing_hours = 24  # 默认值
                    listing_time = None
                    is_rescore = False
                    scoring_count = 0
                    
                    # 获取上市时间信息
                    if isinstance(symbol_data, dict):
                        listing_hours = symbol_data.get('hours_since_listing', 24)
                        listing_time = symbol_data.get('listing_time')
                        is_rescore = symbol_data.get('is_rescore', False)
                        scoring_count = symbol_data.get('scoring_count', 0)
                    else:
                        # 如果是字符串，尝试从状态中获取
                        detector_listing = detector.processed_symbols.get(symbol, {})
                        listing_hours = detector_listing.get('hours_since_listing', 24)
                        listing_time_str = detector_listing.get('listing_time')
                        if listing_time_str:
                            try:
                                listing_time = datetime.fromisoformat(listing_time_str)
                            except Exception:
                                listing_time = None
                        is_rescore = detector_listing.get('scoring_count', 0) > 0
                        scoring_count = detector_listing.get('scoring_count', 0)
                    
                    # 如果是新币种（非二次评分），发送上线通知
                    if not is_rescore and settings.feishu_webhook:
                        from core.notifier import feishu_notifier
                        from core.binance_client import binance_client
                        
                        # 获取合约类型
                        symbol_info = binance_client.get_symbol_info(symbol)
                        contract_type = symbol_info.get('contractType', 'PERPETUAL') if symbol_info else 'PERPETUAL'
                        
                        # 发送新币上线通知
                        feishu_notifier.send_new_listing_notification(
                            symbol=symbol,
                            listing_time=listing_time or datetime.now(),
                            hours_since_listing=listing_hours,
                            contract_type=contract_type
                        )
                        logger.info(f"📤 已发送 {symbol} 上线通知")
                    
                    # 获取资金费率
                    from core.binance_client import binance_client
                    try:
                        funding_rate = binance_client.get_funding_rate(symbol)
                    except Exception as e:
                        logger.debug(f"无法获取 {symbol} 的资金费率：{e}")
                        funding_rate = None
                    
                    # 计算合约数据评分（使用新模块，获取真实市值数据）
                    from core.contract_scorer import contract_scorer
                    contract_score, contract_reason = contract_scorer.calculate_contract_score(symbol)
                    logger.info(f"📊 {symbol} 合约数据评分：{contract_score:.2f}/10.0 ({contract_reason})")
                    
                    # 计算 OI/市值比率（用于一票否决检查）
                    oi_ratio, oi_valid = contract_scorer.calculate_oi_ratio(symbol)
                    if not oi_valid:
                        oi_ratio = 0.5  # 默认值
                    
                    # 基本面评分（基于代币解锁，自动获取）
                    from core.unlock_manager import UnlockDataManager
                    unlock_manager = UnlockDataManager(auto_fetch=True)
                    
                    # 如果配置文件中没有该币种，自动添加
                    if symbol not in unlock_manager.unlock_data:
                        logger.debug(f"🔄 配置文件中没有 {symbol}，尝试自动获取解锁数据...")
                        unlock_manager.auto_add_symbol(symbol)
                    
                    fundamental_score = unlock_manager.score_fundamental(symbol, days=90)
                    
                    if fundamental_score >= 7.0:
                        logger.info(f"📅 {symbol} 基本面评分：{fundamental_score:.1f} (存在大额解锁)")
                    
                    # 技术面评分（使用新模块，获取真实 K 线数据和技术指标）
                    from core.technical_analyzer import technical_analyzer
                    technical_score = technical_analyzer.calculate_technical_score(symbol)
                    logger.info(f"📊 {symbol} 技术面评分：{technical_score:.2f}/10.0")
                    
                    # 情绪面评分（基于资金费率）- 宽松模式（适合新币）
                    # 新币（<72 小时）处于价格发现期，资金费率波动大，使用更宽松标准
                    sentiment_score = 5.0
                    if funding_rate is not None:
                        annual_rate = funding_rate * 3 * 365 * 100  # 年化百分比
                        
                        # 判断是否为新币（上线<72 小时）
                        is_new_coin = listing_hours < 72
                        
                        if is_new_coin:
                            # 新币宽松评分标准
                            if annual_rate > 200:
                                sentiment_score = 10.0
                            elif annual_rate > 100:
                                sentiment_score = 8.0
                            elif annual_rate > 50:
                                sentiment_score = 6.0
                            elif annual_rate > -100:  # -100% ~ 50%
                                sentiment_score = 5.0  # 给中间分
                            elif annual_rate > -500:  # -500% ~ -100%
                                sentiment_score = 4.0  # 适度扣分
                            else:  # < -500%
                                sentiment_score = 3.5  # 极度负值才给低分
                            logger.debug(f"🆕 {symbol} 使用新币宽松情绪评分标准")
                        else:
                            # 老币种标准评分
                            if annual_rate > 100:
                                sentiment_score = 10.0
                            elif annual_rate > 50:
                                sentiment_score = 7.0
                            elif annual_rate > 20:
                                sentiment_score = 6.0
                            elif annual_rate > 0:
                                sentiment_score = 5.0
                            elif annual_rate > -50:  # -50% ~ 0%
                                sentiment_score = 4.0
                            elif annual_rate > -100:  # -100% ~ -50%
                                sentiment_score = 3.5
                            else:  # < -100%
                                sentiment_score = 3.0
                            logger.debug(f"📊 {symbol} 使用标准情绪评分标准")
                    
                    logger.info(f"📊 {symbol} 情绪面评分：{sentiment_score:.1f}/10 (年化费率：{funding_rate * 3 * 365 * 100:.1f}%)")
                    
                    # 确定评分次数
                    scoring_attempt = scoring_count + 1 if is_rescore else 1
                    
                    # 根据币种上线时间动态调整权重配置
                    if listing_hours < 72:
                        # 新币配置（更关注技术和合约数据，降低情绪面影响）
                        weights = {
                            'contract': 0.40,    # 40%
                            'fundamental': 0.20, # 20%
                            'technical': 0.35,   # 35%
                            'sentiment': 0.05    # 5%（降低情绪面影响）
                        }
                        logger.info(f"🆕 {symbol} 使用新币权重配置（情绪面仅 5%）")
                    else:
                        # 老币配置（标准配置）
                        weights = {
                            'contract': 0.35,
                            'fundamental': 0.20,
                            'technical': 0.35,
                            'sentiment': 0.10
                        }
                        logger.debug(f"📊 {symbol} 使用标准权重配置")
                    
                    # 准备额外信息
                    additional_details = {
                        'is_rescore': is_rescore,
                        'scoring_attempt': scoring_attempt,
                        'funding_rate': funding_rate
                    }
                    
                    # 获取当前价格
                    current_price = 0.0
                    try:
                        from core.binance_client import binance_client
                        ticker = binance_client.get_ticker(symbol)
                        if ticker and 'lastPrice' in ticker:
                            current_price = float(ticker['lastPrice'])
                    except Exception as e:
                        logger.debug(f"无法获取 {symbol} 的当前价格：{e}")
                    
                    # 生成评分报告
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
                    
                    # 更新评分记录（用于二次评分追踪）
                    detector.update_scoring_record(
                        symbol=symbol,
                        score=report.total_score,
                        signal_generated=not report.veto and report.total_score >= settings.min_signal_score,
                        scoring_details=additional_details
                    )
                    
                    # 发送评分完成通知（每次评分都发送）
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
                    
                    if report and not report.veto and report.total_score >= settings.min_signal_score:
                        # 3. 生成交易信号
                        signal = signal_manager.generate_signal(
                            symbol=symbol,
                            scoring_result=report,
                            current_price=report.current_price,
                            expire_hours=settings.signal_expire_hours
                        )
                        
                        if signal:
                            logger.info(f"✅ 生成新信号：{symbol}, 评分：{report.total_score:.2f}")
                            
                            # 4. 推送通知（如果配置了飞书 webhook）
                            if settings.feishu_webhook:
                                from core.notifier import feishu_notifier
                                feishu_notifier.send_signal_notification(signal)
                                logger.info(f"📤 已推送信号通知：{symbol}")
                        else:
                            logger.warning(f"⚠️ 信号生成失败：{symbol}")
                    else:
                        reason = report.veto_reason if report.veto else f"评分不足 ({report.total_score:.2f} < {settings.min_signal_score})"
                        logger.debug(f"⏭️ 跳过 {symbol}: {reason}")
                    
                    # 检查是否需要发送汇总通知
                    if (settings.send_summary_notification and 
                        settings.feishu_webhook):
                        # 检查是否达到最大评分次数
                        if is_rescore and scoring_attempt >= settings.max_rescore_attempts:
                            # 完成所有评分，发送汇总报告
                            from core.notifier import feishu_notifier
                            from core.listing_detector import listing_detector
                            
                            scoring_history = listing_detector.get_scoring_history(symbol)
                            
                            if scoring_history:
                                feishu_notifier.send_coin_summary_report(
                                    symbol=symbol,
                                    listing_time=listing_time or datetime.now(),
                                    scoring_history=scoring_history,
                                    final_score=report.total_score,
                                    signal_generated=not report.veto and report.total_score >= settings.min_signal_score
                                )
                                logger.info(f"📤 已发送 {symbol} 评分汇总报告")
                        # 检查是否评分终止（分数过低，不再需要继续评分）
                        elif is_rescore and report.total_score < 4.0:
                            # 分数过低，评分终止，发送汇总报告
                            from core.notifier import feishu_notifier
                            from core.listing_detector import listing_detector
                            
                            scoring_history = listing_detector.get_scoring_history(symbol)
                            
                            if scoring_history:
                                feishu_notifier.send_coin_summary_report(
                                    symbol=symbol,
                                    listing_time=listing_time or datetime.now(),
                                    scoring_history=scoring_history,
                                    final_score=report.total_score,
                                    signal_generated=not report.veto and report.total_score >= settings.min_signal_score
                                )
                                logger.info(f"📤 评分终止：已发送 {symbol} 评分汇总报告")
                
                except Exception as e:
                    logger.error(f"❌ 处理 {symbol} 时出错：{e}", exc_info=True)
                    continue
        else:
            logger.debug("未发现新上线币种")
        
        logger.debug("新币监控任务执行完成")
        
    except Exception as e:
        logger.error(f"❌ 监控任务执行失败：{e}", exc_info=True)


def start_monitoring():
    """启动监控服务"""
    global scheduler
    
    logger.info("🚀 启动监控服务...")
    
    # 1. 初始化系统
    detector, scoring_engine, signal_manager = init_system()
    
    # 2. 创建调度器
    scheduler = TaskScheduler()
    
    # 3. 注册监控任务
    # 高频监控：每 60 秒一次（针对 0-24 小时新币）
    scheduler.add_interval_task(
        task_id="monitor_new_coins_high_freq",
        func=monitor_new_coins_task,
        seconds=settings.new_coin_high_freq_interval,
        detector=detector,
        scoring_engine=scoring_engine,
        signal_manager=signal_manager
    )
    
    # 普通监控：每 300 秒一次（针对 1-7 天新币）
    scheduler.add_interval_task(
        task_id="monitor_new_coins_normal_freq",
        func=monitor_new_coins_task,
        seconds=settings.new_coin_normal_freq_interval,
        detector=detector,
        scoring_engine=scoring_engine,
        signal_manager=signal_manager
    )
    
    # 4. 启动调度器
    logger.info("✅ 所有监控任务已注册")
    logger.info(f"📊 监控频率：{settings.new_coin_high_freq_interval}秒 (高频), {settings.new_coin_normal_freq_interval}秒 (普通)")
    logger.info("🎯 开始执行监控任务...")
    
    # 5. 立即执行一次
    logger.info("⚡ 执行首次监控...")
    monitor_new_coins_task(detector, scoring_engine, signal_manager)
    
    # 6. 启动调度器（阻塞）
    try:
        scheduler.scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("👋 收到退出信号，正在关闭...")
        if scheduler:
            scheduler.scheduler.shutdown()
        logger.info("✅ 系统已安全关闭")


def show_signals():
    """查看待确认信号"""
    logger.info("📊 查看待确认信号...")
    
    try:
        from core.signal_manager import signal_manager
        signals = signal_manager.get_pending_signals()
        
        if not signals:
            print("✅ 当前没有待确认的信号")
            return
        
        print(f"\n共有 {len(signals)} 个待确认信号:\n")
        print("-" * 80)
        
        for signal in signals:
            print(f"币种：{signal.symbol}")
            print(f"评分：{signal.scoring_result.total_score:.2f}/10")
            print(f"当前价格：{signal.current_price:.2f} USDT")
            print(f"建议入场：{signal.suggested_entry_min:.2f} - {signal.suggested_entry_max:.2f} USDT")
            print(f"止损位：{signal.stop_loss:.2f} USDT")
            print(f"止盈位：{signal.take_profit_1:.2f} / {signal.take_profit_2:.2f} USDT")
            print(f"创建时间：{signal.created_at}")
            print(f"过期时间：{signal.expire_at}")
            print("-" * 80)
        
        print("\n使用命令确认交易：python main.py confirm <SYMBOL>")
        
    except Exception as e:
        logger.error(f"❌ 获取信号失败：{e}")
        print(f"❌ 错误：{e}")


def confirm_trade(symbol: str, stop_loss: Optional[float] = None, 
                  take_profit: Optional[float] = None, position_size: float = 4.0):
    """确认执行交易"""
    logger.info(f"✅ 确认执行 {symbol} 做空交易...")
    
    try:
        from core.signal_manager import signal_manager
        from core.trading_executor import trading_executor
        
        # 1. 获取信号
        signal = signal_manager.get_signal_by_symbol(symbol)
        if not signal:
            print(f"❌ 错误：未找到 {symbol} 的信号")
            return
        
        # 2. 检查信号是否过期
        if signal.is_expired():
            print(f"⚠️ 警告：信号已过期 ({signal.expire_at})")
            confirm = input("是否继续执行？(y/n): ")
            if confirm.lower() != 'y':
                print("❌ 已取消")
                return
        
        # 3. 获取当前价格
        from core.binance_client import binance_client
        ticker = binance_client.get_futures_ticker(symbol)
        current_price = float(ticker.get('lastPrice', 0))
        
        if not current_price:
            print("❌ 错误：无法获取当前价格")
            return
        
        # 4. 计算止损止盈
        if stop_loss is None:
            stop_loss = signal.stop_loss
        
        if take_profit is None:
            take_profit = signal.take_profit_1
        
        # 5. 计算开仓数量
        quantity = (position_size * signal.leverage) / current_price
        
        # 6. 执行交易
        print(f"\n📊 交易详情:")
        print(f"币种：{symbol}")
        print(f"方向：做空 (SELL)")
        print(f"当前价格：{current_price:.2f} USDT")
        print(f"仓位大小：{position_size} USDT ({signal.leverage}x 杠杆)")
        print(f"开仓数量：{quantity:.4f} {symbol.replace('USDT', '')}")
        print(f"止损价格：{stop_loss:.2f} USDT")
        print(f"止盈价格：{take_profit:.2f} USDT")
        print()
        
        confirm = input("确认执行？(y/n): ")
        if confirm.lower() != 'y':
            print("❌ 已取消")
            return
        
        # 7. 下单
        order_id = trading_executor.execute_short_trade(
            symbol=symbol,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit_1=take_profit,
            take_profit_2=signal.take_profit_2,
            quantity=quantity,
            leverage=signal.leverage,
            reason=f"信号触发：{signal.id[:8]}"
        )
        
        if order_id:
            print(f"✅ 交易执行成功！")
            print(f"订单 ID: {order_id}")
            
            # 8. 更新信号状态
            signal_manager.confirm_signal(signal.id)
            
            logger.info(f"✅ 交易执行成功：{symbol}, 订单 ID: {order_id}")
        else:
            print("❌ 交易执行失败")
    
    except Exception as e:
        logger.error(f"❌ 交易执行失败：{e}", exc_info=True)
        print(f"❌ 错误：{e}")


def cancel_signal(symbol: str):
    """取消信号"""
    logger.info(f"❌ 取消 {symbol} 信号...")
    
    try:
        from core.signal_manager import signal_manager
        
        signal = signal_manager.get_signal_by_symbol(symbol)
        if not signal:
            print(f"❌ 错误：未找到 {symbol} 的信号")
            return
        
        signal_manager.reject_signal(signal.id)
        print(f"✅ 信号已取消：{symbol}")
        
    except Exception as e:
        logger.error(f"❌ 取消信号失败：{e}")
        print(f"❌ 错误：{e}")


def show_status():
    """显示系统状态"""
    logger.info("📈 系统状态检查...")
    
    try:
        print("\n" + "=" * 60)
        print("币安新币精准做空系统 - 系统状态")
        print("=" * 60)
        
        # 1. 检查 API 连接
        from core.binance_client import binance_client
        try:
            ticker = binance_client.get_futures_ticker("BTCUSDT")
            print("✅ 币安 API 连接正常")
        except Exception:
            print("❌ 币安 API 连接失败")
        
        # 2. 显示待确认信号数
        from core.signal_manager import signal_manager
        pending_signals = signal_manager.get_pending_signals()
        print(f"📊 待确认信号：{len(pending_signals)} 个")
        
        # 3. 显示持仓信息
        from core.trading_executor import trading_executor
        positions = trading_executor.get_all_positions()
        print(f"💼 当前持仓：{len(positions)} 个")
        
        print("=" * 60)
        
    except Exception as e:
        logger.error(f"❌ 状态检查失败：{e}")
        print(f"❌ 错误：{e}")


def show_history():
    """显示交易历史"""
    logger.info("📜 查看交易历史...")
    
    try:
        from core.trading_executor import trading_executor
        
        print("\n" + "=" * 60)
        print("交易历史")
        print("=" * 60)
        
        # TODO: 从数据库查询交易历史
        print("(功能开发中)")
        
    except Exception as e:
        logger.error(f"❌ 查询历史失败：{e}")
        print(f"❌ 错误：{e}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='币安新币精准做空系统',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py start          启动监控服务
  python main.py signals        查看待确认信号
  python main.py confirm BTC    确认执行 BTC 做空
  python main.py cancel BTC     取消 BTC 信号
        """
    )
    
    parser.add_argument(
        'command',
        nargs='?',
        choices=['start', 'signals', 'confirm', 'cancel', 'status', 'history'],
        help='命令类型'
    )
    
    parser.add_argument(
        'symbol',
        nargs='?',
        help='交易对符号 (用于 confirm/cancel 命令)'
    )
    
    parser.add_argument(
        '--stop-loss',
        type=float,
        help='止损价格 (可选)'
    )
    
    parser.add_argument(
        '--take-profit',
        type=float,
        help='止盈价格 (可选)'
    )
    
    parser.add_argument(
        '--position-size',
        type=float,
        default=4.0,
        help='仓位大小 (USDT), 默认 4.0'
    )
    
    parser.add_argument(
        '--version',
        action='version',
        version='%(prog)s 1.0.0'
    )
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(0)
    
    logger.info(f"命令：{args.command}, 参数：{args}")
    
    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 执行对应命令
    if args.command == 'start':
        start_monitoring()
    elif args.command == 'signals':
        show_signals()
    elif args.command == 'confirm':
        if not args.symbol:
            print("❌ 错误：confirm 命令需要指定交易对符号")
            sys.exit(1)
        confirm_trade(
            symbol=args.symbol,
            stop_loss=args.stop_loss,
            take_profit=args.take_profit,
            position_size=args.position_size
        )
    elif args.command == 'cancel':
        if not args.symbol:
            print("❌ 错误：cancel 命令需要指定交易对符号")
            sys.exit(1)
        cancel_signal(args.symbol)
    elif args.command == 'status':
        show_status()
    elif args.command == 'history':
        show_history()
    else:
        print(f"❌ 未知命令：{args.command}")
        sys.exit(1)


if __name__ == '__main__':
    main()
