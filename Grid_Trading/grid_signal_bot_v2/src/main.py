"""
网格交易信号灯系统 V2.0 主程序
半自动信号灯模式：分析市场 → 生成信号 → 推送通知
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.market_analyzer import MarketStateAnalyzer, MarketState
from src.core.grid_calculator import GridParameterCalculator
from src.core.position_validator import PositionValidator
from src.core.parameter_comparator import ParameterComparator
from src.data.kline_client import KlineServiceClient
from src.data.database import DatabaseManager
from src.notification.notification_client import NotificationClient
from src.utils.config import ConfigManager
from src.utils.indicators import TechnicalIndicators

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s'
)
logger = logging.getLogger(__name__)


class GridSignalBot:
    """网格信号灯机器人"""
    
    def __init__(self):
        """初始化信号灯机器人"""
        logger.info("=" * 60)
        logger.info("🚀 网格交易信号灯系统 V2.0 初始化")
        logger.info("=" * 60)
        
        # 加载配置
        self.config = ConfigManager()
        
        # 初始化服务客户端
        services_config = self.config.get_services_config()
        self.kline_client = KlineServiceClient(
            base_url=services_config.get('kline', {}).get('url'),
            timeout=services_config.get('kline', {}).get('timeout', 10)
        )
        self.notification_client = NotificationClient(
            base_url=services_config.get('notification', {}).get('url'),
            project=services_config.get('notification', {}).get('project', 'grid'),
            timeout=services_config.get('notification', {}).get('timeout', 10)
        )
        
        # 初始化数据库
        self.db = DatabaseManager()
        
        # 初始化核心模块
        strategy_config = self.config.get_strategy_config()
        indicators_config = strategy_config.get('indicators', {})
        
        self.market_analyzer = MarketStateAnalyzer(
            adx_period=indicators_config.get('adx_period', 14),
            adx_weak_threshold=indicators_config.get('adx_weak_threshold', 20),
            adx_trend_threshold=indicators_config.get('adx_trend_threshold', 25),
            adx_strong_threshold=indicators_config.get('adx_strong_threshold', 40),
            ema_fast_period=indicators_config.get('ema_fast_period', 20),
            ema_slow_period=indicators_config.get('ema_slow_period', 50)
        )
        
        trading_config = self.config.get_trading_config()
        grid_config = strategy_config.get('grid', {})
        
        self.grid_calculator = GridParameterCalculator(
            base_grid_count=grid_config.get('base_grid_count', 30),
            min_grid_count=grid_config.get('min_grid_count', 5),
            max_grid_count=grid_config.get('max_grid_count', 50),
            min_profit_rate=grid_config.get('min_profit_rate', 0.01),
            leverage=trading_config.get('leverage', 10),
            total_investment=trading_config.get('total_investment', 500)
        )
        
        self.position_validator = PositionValidator()
        
        triggers_config = self.config.get('triggers', {})
        self.parameter_comparator = ParameterComparator(
            grid_width_change_threshold=triggers_config.get('grid_width_change', 0.05),
            grid_count_change_threshold=triggers_config.get('grid_count_change', 0.10),
            atr_change_threshold=triggers_config.get('atr_change', 0.20),
            profit_rate_warning_threshold=triggers_config.get('profit_rate_warning', 0.012)
        )
        
        # 状态
        self.current_params = None
        self.current_atr = None
        self.current_market_state = None
        
        logger.info("✅ 系统初始化完成")
    
    async def run_once(self):
        """执行一次巡检"""
        logger.info("\n" + "=" * 60)
        logger.info("🔍 开始巡检")
        logger.info("=" * 60)
        
        try:
            # 1. 获取交易对
            symbol = self.config.get('trading.symbol', 'BTCUSDT')
            
            # 2. 获取 K 线数据
            logger.info(f"📊 获取 {symbol} K 线数据...")
            klines_1h = self.kline_client.get_latest_klines(
                symbol=symbol,
                interval="1h",
                limit=100
            )
            klines_4h = self.kline_client.get_latest_klines(
                symbol=symbol,
                interval="4h",
                limit=100
            )
            
            if not klines_1h:
                logger.error("❌ 获取 K 线数据失败")
                return
            
            # 使用本地计算技术指标
            logger.info("📈 计算技术指标...")
            indicators_1h = TechnicalIndicators.calculate_all_indicators(klines_1h)
            indicators_4h = TechnicalIndicators.calculate_all_indicators(klines_4h) if klines_4h else {}
            
            # 将技术指标添加到最后一条 K 线
            if klines_1h and indicators_1h:
                klines_1h[-1]['adx'] = indicators_1h.get('adx', 0)
                klines_1h[-1]['ema_fast'] = indicators_1h.get('ema_fast', 0)
                klines_1h[-1]['ema_slow'] = indicators_1h.get('ema_slow', 0)
                klines_1h[-1]['atr'] = indicators_1h.get('atr', 0)
            
            if klines_4h and indicators_4h:
                klines_4h[-1]['adx'] = indicators_4h.get('adx', 0)
                klines_4h[-1]['ema_fast'] = indicators_4h.get('ema_fast', 0)
                klines_4h[-1]['ema_slow'] = indicators_4h.get('ema_slow', 0)
                klines_4h[-1]['atr'] = indicators_4h.get('atr', 0)
            
            # 3. 分析市场状态
            logger.info("📈 分析市场状态...")
            market_result = self.market_analyzer.analyze(klines_1h, klines_4h)
            
            logger.info(f"市场状态：{market_result.state.value}")
            logger.info(f"ADX: {market_result.adx:.2f}")
            logger.info(f"置信度：{market_result.confidence*100:.1f}%")
            logger.info(f"趋势强度：{market_result.trend_strength:.2f}")
            
            # 4. 获取当前价格和 ATR
            last_kline = klines_1h[-1]
            current_price = last_kline.get('close_price', 0)
            atr = last_kline.get('atr', current_price * 0.01)
            
            logger.info(f"当前价格：${current_price:,.2f}")
            logger.info(f"ATR: {atr:.2f}")
            
            # 5. 计算网格参数
            logger.info("📐 计算网格参数...")
            grid_params = self.grid_calculator.calculate(
                current_price=current_price,
                atr_smooth=atr,
                market_state=market_result.state,
                trend_strength=market_result.trend_strength
            )
            
            logger.info(f"价格区间：[${grid_params.lower_price:,.2f}, ${grid_params.upper_price:,.2f}]")
            logger.info(f"网格数量：{grid_params.grid_count}")
            logger.info(f"每格利润率：{grid_params.profit_rate*100:.2f}%")
            
            # 6. 验证仓位
            logger.info("💰 验证仓位可行性...")
            position_result = self.position_validator.validate(
                current_price=current_price,
                grid_count=grid_params.grid_count,
                leverage=grid_params.leverage,
                total_investment=grid_params.total_investment
            )
            
            logger.info(position_result.message)
            
            # 7. 对比参数变化
            logger.info("🔄 对比参数变化...")
            market_state_changed = (
                self.current_market_state is not None and
                self.current_market_state != market_result.state
            )
            
            changes = self.parameter_comparator.compare(
                old_params=self.current_params,
                new_params=grid_params,
                old_atr=self.current_atr,
                new_atr=atr
            )
            
            # 8. 判断是否需要推送
            should_notify = self.parameter_comparator.should_notify(
                changes=changes,
                market_state_changed=market_state_changed
            )
            
            # 9. 推送通知
            if should_notify or self.current_params is None:
                logger.info("📤 推送信号通知...")
                success = self.notification_client.send_grid_signal(
                    market_state=market_result.state.value,
                    current_price=current_price,
                    atr=atr,
                    adx=market_result.adx,
                    grid_params=grid_params.to_dict(),
                    position_validation={
                        'is_valid': position_result.is_valid,
                        'qty_per_grid': position_result.qty_per_grid,
                        'suggested_margin': position_result.suggested_margin
                    }
                )
                
                if success:
                    logger.info("✅ 信号推送成功")
                    
                    # 保存到数据库
                    self.db.save_signal(
                        signal_time=datetime.now(),
                        market_state=market_result.state.value,
                        symbol=symbol,
                        grid_params=grid_params.to_dict(),
                        is_pushed=True
                    )
                else:
                    logger.error("❌ 信号推送失败")
            else:
                logger.info("ℹ️  无显著变化，不推送")
            
            # 10. 更新状态
            self.current_params = grid_params
            self.current_atr = atr
            self.current_market_state = market_result.state
            
            # 保存市场状态到数据库
            self.db.save_market_state(
                check_time=datetime.now(),
                symbol=symbol,
                state=market_result.state.value,
                adx=market_result.adx,
                adx_4h=market_result.adx_4h,
                trend_strength=market_result.trend_strength,
                confidence=market_result.confidence
            )
            
            logger.info("\n" + "=" * 60)
            logger.info("✅ 巡检完成")
            logger.info("=" * 60)
            
        except Exception as e:
            logger.error(f"❌ 巡检失败：{e}", exc_info=True)
            # 发送错误通知
            self.notification_client.send_error_alert(
                error_type="巡检失败",
                error_message=str(e)
            )
    
    async def run_loop(self, run_minute: int = 35, rest_hours: list = None):
        """
        定时运行（每小时的指定分钟运行）
        
        Args:
            run_minute: 运行的分钟数（0-59）
            rest_hours: 休息时间段（小时列表，默认 [0, 1, 2, 3, 4, 5]）
        """
        if rest_hours is None:
            rest_hours = [0, 1, 2, 3, 4, 5]
        
        logger.info(f"🔄 启动定时运行模式，每小时的 {run_minute} 分运行")
        if rest_hours:
            logger.info(f"😴 休息时间段：{rest_hours[0]:02d}:00 - {rest_hours[-1]+1:02d}:00")
        
        while True:
            try:
                # 计算到下一个运行时间的等待秒数
                now = datetime.now()
                next_run = now.replace(minute=run_minute, second=0, microsecond=0)
                
                # 如果当前时间已过本小时的运行时间，则设置为下一小时
                if now.minute >= run_minute:
                    next_run = next_run.replace(hour=now.hour + 1)
                
                # 如果小时超过23，则设置为第二天0点
                if next_run.hour >= 24:
                    next_run = next_run.replace(hour=0, day=now.day + 1)
                
                # 检查是否在休息时间段内，如果是则跳到下一个非休息时间
                while next_run.hour in rest_hours:
                    next_run = next_run.replace(hour=next_run.hour + 1)
                    # 如果小时超过23，则设置为第二天0点
                    if next_run.hour >= 24:
                        next_run = next_run.replace(hour=0, day=next_run.day + 1)
                
                wait_seconds = (next_run - now).total_seconds()
                
                if wait_seconds > 0:
                    logger.info(f"\n⏰ 下次运行时间：{next_run.strftime('%Y-%m-%d %H:%M:%S')}")
                    logger.info(f"⏳ 等待 {int(wait_seconds // 60)} 分 {int(wait_seconds % 60)} 秒...")
                    await asyncio.sleep(wait_seconds)
                
                # 执行巡检
                await self.run_once()
                
            except KeyboardInterrupt:
                logger.info("\n🛑 收到中断信号，停止运行")
                break
            except Exception as e:
                logger.error(f"❌ 循环运行异常：{e}", exc_info=True)
                # 等待 5 分钟后重试
                logger.info("⏳ 等待 5 分钟后重试...")
                await asyncio.sleep(300)
    
    def close(self):
        """关闭资源"""
        self.kline_client.close()
        self.notification_client.close()
        self.db.close()
        logger.info("✅ 资源已释放")


async def main():
    """主函数"""
    bot = GridSignalBot()
    
    try:
        # 单次运行模式
        if len(sys.argv) > 1 and sys.argv[1] == "--once":
            await bot.run_once()
        else:
            # 定时运行模式（每小时的指定分钟运行）
            run_minute = int(os.getenv("RUN_MINUTE", "35"))
            
            # 休息时间段配置（逗号分隔的小时列表）
            rest_hours_str = os.getenv("REST_HOURS", "0,1,2,3,4,5")
            rest_hours = [int(h.strip()) for h in rest_hours_str.split(",") if h.strip()]
            
            await bot.run_loop(run_minute=run_minute, rest_hours=rest_hours)
    except KeyboardInterrupt:
        logger.info("\n🛑 收到中断信号")
    finally:
        bot.close()


if __name__ == "__main__":
    asyncio.run(main())
