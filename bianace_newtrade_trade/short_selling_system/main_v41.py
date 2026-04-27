"""
币安新币精准做空系统 v4.1

基于 V4.1 评分引擎：
- 使用 OI/上线以来总交易量 替代 OI/市值比
- 合约数据评分：OI/总交易量比率(30%) + OI排名(15%)
- 技术面：纯形态评分（三次冲顶、长上影线、放量滞涨）
- 情绪面：资金费率评分

核心变更：
1. 不再依赖市值数据
2. 使用真实资金费率
3. 评分阈值基于实际数据分布
"""

import sys
import argparse
import signal
from datetime import datetime, time
from typing import Optional, List

from utils.logger import logger
from config.settings import settings
from core.scheduler import TaskScheduler, MonitoringScheduler
from core.listing_detector import NewListingDetector
from core.scoring_engine_v41 import ScoringEngineV41, scoring_engine_v41
from core.pattern_recognition_v4 import PatternRecognitionV4
from core.signal_manager import SignalManager

scheduler: Optional[MonitoringScheduler] = None
running = True


def signal_handler(signum, frame):
    """信号处理函数"""
    global running
    logger.info(f"收到退出信号：{signum}")
    running = False


def init_system_v41():
    """初始化系统组件 v4.1"""
    logger.info("=" * 60)
    logger.info("币安新币精准做空系统 v4.1")
    logger.info("OI/总交易量 + 纯形态技术分析 + 真实资金费率")
    logger.info("=" * 60)
    
    logger.info("正在初始化系统组件...")
    
    detector = NewListingDetector()
    logger.info("✅ 新币检测器初始化完成")
    
    scoring_engine = ScoringEngineV41()
    logger.info("✅ V4.1 评分引擎初始化完成")
    
    pattern_recognition = PatternRecognitionV4()
    logger.info("✅ V4.0 形态识别器初始化完成")
    
    signal_manager = SignalManager()
    logger.info("✅ 信号管理器初始化完成")
    
    scheduler = MonitoringScheduler()
    logger.info("✅ 监控调度器初始化完成")
    
    logger.info("=" * 60)
    logger.info("系统初始化完成，准备启动监控服务")
    logger.info("=" * 60)
    
    return detector, scoring_engine, pattern_recognition, signal_manager, scheduler


def load_real_oi_data() -> dict:
    """加载真实OI和资金费率数据"""
    import json
    import os
    
    data_file = os.path.join(os.path.dirname(__file__), 'data', 'real_oi_funding_data.json')
    
    if not os.path.exists(data_file):
        logger.warning(f"真实数据文件不存在：{data_file}")
        return {}
    
    try:
        with open(data_file, 'r') as f:
            data = json.load(f)
        
        result = {}
        for item in data.get('data', []):
            symbol = item.get('symbol')
            if symbol:
                result[symbol] = {
                    'price': item.get('price'),
                    'oi': item.get('oi'),
                    'oi_usd': item.get('oi_usd'),
                    'funding_rate': item.get('funding_rate'),
                    'volume_24h': item.get('volume_24h'),
                    'market_cap': item.get('market_cap')
                }
        
        logger.info(f"✅ 加载了 {len(result)} 个币种的真实数据")
        return result
    except Exception as e:
        logger.error(f"加载真实数据失败：{e}")
        return {}


def calculate_total_volume(symbol: str, klines: List[dict]) -> float:
    """计算上线以来总交易量"""
    total_volume = 0.0
    for kline in klines:
        quote_volume = kline.get('quote_volume')
        if quote_volume:
            total_volume += float(quote_volume)
        else:
            volume = float(kline.get('volume', 0))
            close = float(kline.get('close', 0))
            total_volume += volume * close
    return total_volume


def get_all_oi_usd(real_data: dict) -> List[float]:
    """获取所有币种的OI(USD)列表，用于排名"""
    oi_list = []
    for symbol, data in real_data.items():
        oi_usd = data.get('oi_usd')
        if oi_usd and oi_usd > 0:
            oi_list.append(oi_usd)
    return oi_list


def monitor_new_coins_task_v41(detector, scoring_engine, pattern_recognition, signal_manager, real_data):
    """
    监控新币任务 v4.1
    
    使用 V4.1 评分引擎
    """
    try:
        logger.debug("开始执行新币监控任务 v4.1...")
        
        now = datetime.now()
        if now.minute != 1:
            logger.debug(f"⏰ 当前时间为 {now.minute} 分，跳过评分")
            return
        
        logger.info(f"⏰ 整点第 1 分钟，开始执行 V4.1 评分...")
        
        new_listings = detector.detect_new_listings(hours=48)
        
        if not new_listings:
            logger.debug("ℹ️ 没有符合条件的新上线币种")
            return
        
        logger.info(f"发现 {len(new_listings)} 个新上线币种（48 小时内）")
        
        all_oi_usd = get_all_oi_usd(real_data)
        recent_coins_oi = all_oi_usd[-10:] if len(all_oi_usd) >= 10 else all_oi_usd
        
        for symbol_data in new_listings:
            try:
                symbol = symbol_data if isinstance(symbol_data, str) else symbol_data.get('symbol')
                if not symbol:
                    continue
                
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
                
                can_generate, reason = signal_manager.can_generate_signal(symbol, listing_hours)
                if not can_generate:
                    logger.info(f"⏭️  {symbol} 跳过评分：{reason}")
                    continue
                
                symbol_real_data = real_data.get(symbol, {})
                oi_usd = symbol_real_data.get('oi_usd', 0)
                funding_rate = symbol_real_data.get('funding_rate', 0.00005)
                
                if not oi_usd:
                    from core.binance_client import binance_client
                    try:
                        oi_usd = binance_client.get_current_open_interest(symbol)
                        
                        if not funding_rate:
                            funding_rate = binance_client.get_funding_rate(symbol) or 0.00005
                    except Exception as e:
                        logger.debug(f"无法获取 {symbol} 的实时数据：{e}")
                        oi_usd = 0
                        funding_rate = 0.00005
                
                from core.binance_client import binance_client
                klines = binance_client.get_kline_data(symbol, interval='1h', limit=100)
                
                if not klines:
                    logger.warning(f"无法获取 {symbol} 的K线数据")
                    continue
                
                total_volume_usd = calculate_total_volume(symbol, klines)
                
                if total_volume_usd <= 0:
                    logger.warning(f"{symbol} 总交易量为0，跳过")
                    continue
                
                current_price = float(klines[-1].get('close', 0)) if klines else 0
                
                pattern_result = pattern_recognition.analyze_patterns(klines)
                
                three_tops_score = pattern_result.get('three_tops', {}).get('score', 0)
                technical_score = pattern_result.get('total_score', 0)
                
                logger.info(f"📊 {symbol} 形态分析：三次冲顶={three_tops_score}, 技术总分={technical_score}")
                
                result = scoring_engine.score(
                    symbol=symbol,
                    oi_usd=oi_usd,
                    total_volume_usd=total_volume_usd,
                    funding_rate=funding_rate,
                    three_tops_detected=pattern_result.get('three_tops', {}).get('detected', False),
                    three_tops_score=three_tops_score,
                    long_upper_shadow=pattern_result.get('long_upper_shadow', {}).get('detected', False),
                    long_upper_shadow_score=pattern_result.get('long_upper_shadow', {}).get('score', 0),
                    volume_divergence=pattern_result.get('volume_divergence', {}).get('detected', False),
                    volume_divergence_score=pattern_result.get('volume_divergence', {}).get('score', 0),
                    listing_hours=listing_hours,
                    current_price=current_price,
                    recent_coins_oi=recent_coins_oi
                )
                
                logger.info(f"📊 {symbol} V4.1 评分结果：")
                logger.info(f"   - 合约分：{result.contract_score:.2f}")
                logger.info(f"   - 技术分：{result.technical_score:.2f}")
                logger.info(f"   - 情绪分：{result.sentiment_score:.2f}")
                logger.info(f"   - 总分：{result.total_score:.2f}")
                logger.info(f"   - OI/总交易量：{result.oi_volume_ratio:.4f}")
                logger.info(f"   - 资金费率：{result.funding_rate:.6f}")
                
                if result.veto:
                    logger.info(f"🚫 {symbol} 一票否决：{result.veto_reason}")
                elif result.total_score >= 6.5:
                    logger.info(f"✅ {symbol} 达到开仓条件！")
                    
                    signal = signal_manager.generate_signal(
                        symbol=symbol,
                        scoring_result=result,
                        current_price=current_price,
                        klines=klines,
                        expire_hours=1
                    )
                    
                    if signal:
                        logger.info(f"🎯 生成信号：{signal.id[:8]}, {symbol}")
                        signal_manager.mark_as_traded(symbol)
                        
                        if settings.feishu_webhook:
                            from core.notifier import feishu_notifier
                            feishu_notifier.send_signal_notification(signal)
                            logger.info(f"📤 已发送信号通知到飞书")
                else:
                    logger.info(f"ℹ️ {symbol} 未达到开仓条件，总分：{result.total_score:.2f}")
                
                is_rescore = symbol_data.get('is_rescore', False) if isinstance(symbol_data, dict) else False
                scoring_count = symbol_data.get('scoring_count', 0) if isinstance(symbol_data, dict) else 0
                scoring_attempt = scoring_count + 1 if is_rescore else 1
                
                signal_generated = not result.veto and result.total_score >= 6.5
                
                scoring_details = {
                    'scoring_attempt': scoring_attempt,
                    'veto': result.veto,
                    'veto_reason': result.veto_reason if result.veto else "",
                    'contract_score': result.contract_score,
                    'technical_score': result.technical_score,
                    'sentiment_score': result.sentiment_score
                }
                
                detector.update_scoring_record(
                    symbol=symbol,
                    score=result.total_score,
                    signal_generated=signal_generated,
                    scoring_details=scoring_details
                )
                
                if settings.feishu_webhook:
                    from core.notifier import feishu_notifier
                    feishu_notifier.send_scoring_complete_notification(
                        symbol=symbol,
                        total_score=result.total_score,
                        scoring_attempt=scoring_attempt,
                        signal_generated=signal_generated,
                        order_placed=False,
                        veto=result.veto,
                        veto_reason=result.veto_reason if result.veto else "",
                        current_price=current_price
                    )
                    logger.info(f"📤 已发送 {symbol} 评分完成通知")
                
            except Exception as e:
                logger.error(f"❌ 评分 {symbol} 失败：{e}", exc_info=True)
        
        logger.info("✅ 新币监控任务 v4.1 执行完成")
        
    except Exception as e:
        logger.error(f"❌ 监控任务执行失败：{e}", exc_info=True)


def start_monitoring():
    """启动监控服务 v4.1"""
    logger.info("🚀 启动币安新币精准做空系统 v4.1")
    
    detector, scoring_engine, pattern_recognition, signal_manager, scheduler = init_system_v41()
    
    real_data = load_real_oi_data()
    
    scheduler.add_cron_task(
        task_id='hourly_scoring_v41',
        func=monitor_new_coins_task_v41,
        minute=1,
        detector=detector,
        scoring_engine=scoring_engine,
        pattern_recognition=pattern_recognition,
        signal_manager=signal_manager,
        real_data=real_data
    )
    
    logger.info("✅ 已注册定时任务：每小时第 1 分钟执行 V4.1 评分")
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("🚀 启动定时任务调度器...")
    scheduler.start()
    
    logger.info("✅ 系统已启动，按 Ctrl+C 退出")
    
    try:
        import time
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("👋 正在退出...")
    finally:
        scheduler.stop()
        logger.info("👋 系统已退出")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='币安新币精准做空系统 v4.1')
    parser.add_argument('--version', action='version', version='v4.1')
    parser.add_argument('--debug', action='store_true', help='启用调试模式')
    
    args = parser.parse_args()
    
    if args.debug:
        import logging
        logging.getLogger().setLevel(logging.DEBUG)
    
    start_monitoring()


if __name__ == '__main__':
    main()
