"""
网格管理系统主程序
整合市场监控、网格计算、资金管理和订单执行
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional
from decimal import Decimal

from src.data.binance_client import BinanceClient
from src.data.kline_manager import KlineManager
from src.strategy.market_state import MarketStateDetector
from src.strategy.grid_calculator import GridParameterCalculator, GridParameters
from src.execution.grid_executor import GridExecutor
from src.execution.fund_manager import FundManager
from src.execution.scheduler import TaskScheduler
from src.utils.binance_trade_api import BinanceTradeAPI
from src.utils.config_loader import ConfigLoader
from src.monitoring.notifier import AlertNotifier

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s'
)
logger = logging.getLogger(__name__)


class GridTradingSystem:
    """网格交易系统"""
    
    def __init__(self):
        # 加载配置
        self.config = ConfigLoader()
        exchange_config = self.config.get_exchange_config()
        
        # 初始化 API
        api_key = exchange_config.get('api_key', '')
        api_secret = exchange_config.get('api_secret', '')
        
        self.binance_client = BinanceClient(api_key, api_secret, testnet=False)
        self.trade_api = BinanceTradeAPI(api_key, api_secret)
        
        # 初始化模块
        self.kline_manager = KlineManager(self.binance_client, 'BTCUSDT')
        self.market_detector = MarketStateDetector()
        self.grid_calculator = GridParameterCalculator(
            base_grid_count=self.config.get('strategy.grid.base_grid_count', 30),
            min_grid_count=self.config.get('strategy.grid.min_grid_count', 20),
            max_grid_count=self.config.get('strategy.grid.max_grid_count', 50),
            conservative_mode=True,
            default_investment=self.config.get('strategy.grid.total_investment', 300),
            leverage=self.config.get('strategy.grid.leverage', 10)
        )
        self.grid_executor = GridExecutor(self.trade_api)
        self.fund_manager = FundManager(self.trade_api)
        self.scheduler = TaskScheduler()
        
        # 初始化通知器
        alert_config = self.config.get('monitoring.alert', {})
        self.notifier = AlertNotifier(
            feishu_webhook=alert_config.get('feishu_webhook'),
            dingding_webhook=alert_config.get('dingding_webhook'),
            telegram_bot_token=alert_config.get('telegram_bot_token'),
            telegram_chat_id=alert_config.get('telegram_chat_id'),
            enabled=alert_config.get('enabled', True)
        )
        
        # 状态
        self.current_grid_id: Optional[str] = None
        self.current_grid_params: Optional[GridParameters] = None
        self.is_running = False
    
    async def initialize(self):
        """初始化系统"""
        logger.info("=" * 60)
        logger.info("🚀 网格交易系统初始化")
        logger.info("=" * 60)
        
        # 测试 API 连接
        try:
            account_info = self.trade_api.get_account_info()
            logger.info("✅ 币安 API 连接成功")
        except Exception as e:
            logger.error(f"❌ API 连接失败：{e}")
            return False
        
        # 初始化 K 线管理器
        await self.kline_manager.initialize()
        logger.info("✅ K 线管理器初始化完成")
        
        # 注册定时任务
        self.scheduler.register_event_callback('hourly_inspection', self.hourly_inspection)
        self.scheduler.register_event_callback('parameter_adjustment', self.check_and_adjust)
        
        logger.info("✅ 系统初始化完成")
        return True
    
    async def cleanup_old_orders(self):
        """清理旧的未触发订单"""
        try:
            # 获取所有未成交订单
            try:
                pending_orders = self.trade_api.get_all_open_orders(symbol='BTCUSDT')
            except Exception as e:
                logger.info(f"ℹ️  获取未成交订单失败（可能是账户类型不支持）：{e}")
                logger.info("  跳过订单清理")
                return
            
            if not pending_orders:
                logger.info("✅ 没有未触发的条件单")
                return
            
            logger.info(f"📋 检测到 {len(pending_orders)} 个未触发的条件单")
            
            # 取消所有条件单
            for order in pending_orders:
                order_id = order.get('orderId')
                if order_id:
                    try:
                        self.trade_api.cancel_order(symbol='BTCUSDT', order_id=order_id)
                        logger.info(f"  已取消订单：{order_id}")
                    except Exception as e:
                        logger.error(f"  取消订单失败 {order_id}: {e}")
            
            logger.info("✅ 所有旧条件单已取消")
            
        except Exception as e:
            logger.error(f"❌ 清理订单失败：{e}")
    
    async def hourly_inspection(self):
        """每小时巡检"""
        logger.info("\n" + "=" * 60)
        logger.info("🔍 开始每小时巡检")
        logger.info("=" * 60)
        
        try:
            # 0. 清理旧的未触发订单（每次巡检都执行）
            logger.info("\n📋 清理旧的未触发订单...")
            await self.cleanup_old_orders()
            await asyncio.sleep(2)  # 等待订单取消确认
            
            # 1. 获取市场数据
            current_price = float(self.trade_api.get_ticker_price('BTCUSDT'))
            logger.info(f"当前 BTC 价格：${current_price:,.2f}")
            
            # 2. 获取 K 线数据
            klines_1h = await self.kline_manager.get_klines('1h', limit=100)
            klines_4h = await self.kline_manager.get_klines('4h', limit=100)
            
            # 3. 分析市场状态
            market_result = self.market_detector.detect(
                df_1h=klines_1h,
                df_4h=klines_4h
            )
            
            logger.info(f"市场状态：{market_result.state.value if market_result.state else 'N/A'}")
            logger.info(f"ADX: {market_result.adx:.2f}")
            logger.info(f"置信度：{market_result.confidence*100:.1f}%")
            
            # 4. 计算 ATR
            atr_smooth = klines_1h['atr_14'].iloc[-1] if 'atr_14' in klines_1h.columns else current_price * 0.01
            logger.info(f"ATR: {atr_smooth:.2f}")
            
            # 5. 检查并补充订单，或创建新网格
            if self.current_grid_id is None:
                logger.info("\n📝 检测到没有运行中的网格，创建新网格...")
                await self.create_new_grid(
                    current_price=current_price,
                    atr_smooth=atr_smooth,
                    state=market_result.state,
                    market_result=market_result
                )
            else:
                logger.info(f"\n✅ 当前网格运行中：{self.current_grid_id}")
                # 检查并补充已触发的订单
                logger.info("\n🔄 检查并补充订单...")
                await self.grid_executor.check_and_replenish_orders(
                    grid_id=self.current_grid_id,
                    params=self.current_grid_params,
                    symbol='BTCUSDT'
                )
            
            logger.info("\n" + "=" * 60)
            logger.info("✅ 巡检完成")
            logger.info("=" * 60)
            
            # 发送飞书巡检通知（仅在有网格运行时发送）
            if self.current_grid_id is not None:
                # 构建详细的巡检报告
                inspection_content = f"""📊 网格巡检报告

【市场信息】
网格 ID: {self.current_grid_id}
当前价格：${current_price:,.2f}
市场状态：{market_result.state.value if market_result.state else 'N/A'}
ADX: {f'{market_result.adx:.2f}' if market_result and hasattr(market_result, 'adx') else 'N/A'}
置信度：{f'{market_result.confidence*100:.1f}%' if market_result and hasattr(market_result, 'confidence') else 'N/A'}
趋势强度：{f'{market_result.trend_strength:.2f}' if market_result and hasattr(market_result, 'trend_strength') else 'N/A'}

【网格策略】
网格方向：{self.current_grid_params.grid_direction if self.current_grid_params else 'N/A'}
价格区间：[${self.current_grid_params.lower_price:,.2f}, ${self.current_grid_params.upper_price:,.2f}]
网格数量：{self.current_grid_params.grid_count if self.current_grid_params else 'N/A'}
投资金额：${self.current_grid_params.total_investment if self.current_grid_params else 'N/A'} USDT
杠杆倍数：{self.current_grid_params.leverage if self.current_grid_params else 'N/A'}x

【账户信息】
合约余额：待更新 USDT
可用资金：充足

【下次巡检】
60 分钟后"""
                
                self.notifier.send_feishu(
                    title="📊 网格巡检报告",
                    content=inspection_content,
                    alert_type="info"
                )
            
        except Exception as e:
            logger.error(f"❌ 巡检失败：{e}")
    
    async def create_new_grid(
        self,
        current_price: float,
        atr_smooth: float,
        state,
        market_result=None
    ):
        """创建新网格"""
        try:
            # 1. 计算网格参数
            params = self.grid_calculator.calculate(
                current_price=current_price,
                atr_smooth=atr_smooth,
                state=state
            )
            
            logger.info("\n📊 网格参数:")
            logger.info(f"  价格区间：[${params.lower_price:.2f}, ${params.upper_price:.2f}]")
            logger.info(f"  网格数量：{params.grid_count}")
            logger.info(f"  方向：{params.grid_direction}")
            logger.info(f"  投资金额：${params.total_investment} USDT")
            logger.info(f"  杠杆：{params.leverage}x")
            
            # 2. 准备资金
            required_amount = Decimal(str(params.total_investment))
            funding_success = await self.fund_manager.prepare_grid_funding(
                required_amount=required_amount
            )
            
            if not funding_success:
                logger.error("❌ 资金准备失败，跳过网格创建")
                return
            
            logger.info("✅ 资金准备完成")
            
            # 3. 创建网格订单
            grid_id = f"grid_{int(datetime.now().timestamp())}"
            success = await self.grid_executor.create_grid_orders(
                grid_id=grid_id,
                params=params,
                symbol='BTCUSDT'
            )
            
            if success:
                self.current_grid_id = grid_id
                self.current_grid_params = params
                logger.info(f"✅ 网格创建成功：{grid_id}")
                
                # 发送飞书通知 - 详细版本
                grid_content = f"""🎉 网格创建成功

【策略配置】
网格 ID: {grid_id}
交易对：BTCUSDT
网格方向：{params.grid_direction.value if hasattr(params.grid_direction, 'value') else params.grid_direction}
价格区间：[${params.lower_price:,.2f}, ${params.upper_price:,.2f}]
网格数量：{params.grid_count}

【资金配置】
投资金额：${params.total_investment} USDT
杠杆倍数：{params.leverage}x
需要资金：${params.total_investment} USDT

【市场状态】
当前价格：${current_price:,.2f}
市场趋势：{state.value if state else 'N/A'}
ADX 指标：{f'{market_result.adx:.2f}' if market_result and hasattr(market_result, 'adx') else 'N/A'}
置信度：{f'{market_result.confidence*100:.1f}%' if market_result and hasattr(market_result, 'confidence') else 'N/A'}
趋势强度：{f'{market_result.trend_strength:.2f}' if market_result and hasattr(market_result, 'trend_strength') else 'N/A'}

【订单信息】
买单价格：${params.lower_price:,.2f}
卖单价格：${params.upper_price:,.2f}
订单类型：限价单
仓位模式：单向持仓（BOTH）"""
                
                self.notifier.send_feishu(
                    title="🎉 网格创建成功",
                    content=grid_content,
                    alert_type="info"
                )
            else:
                logger.error("❌ 网格创建失败")
                
        except Exception as e:
            logger.error(f"❌ 创建网格失败：{e}")
    
    async def check_and_adjust(self):
        """检查并调整网格参数"""
        logger.info("\n" + "=" * 60)
        logger.info("🔍 检查网格参数调整")
        logger.info("=" * 60)
        
        try:
            if self.current_grid_id is None:
                logger.info("无运行中的网格，跳过检查")
                return
            
            # 1. 获取当前市场数据
            current_price = float(self.trade_api.get_ticker_price('BTCUSDT'))
            klines_1h = await self.kline_manager.get_klines('1h', limit=100)
            atr_smooth = klines_1h['atr_14'].iloc[-1] if 'atr_14' in klines_1h.columns else current_price * 0.01
            
            # 2. 分析市场状态
            klines_4h = await self.kline_manager.get_klines('4h', limit=100)
            market_result = self.market_detector.detect(
                df_1h=klines_1h,
                df_4h=klines_4h
            )
            
            # 3. 检查触发条件
            triggers = self.grid_calculator.check_adjustment_triggers(
                current_price=current_price,
                atr_smooth=atr_smooth,
                state=market_result.state,
                current_params=self.current_grid_params
            )
            
            logger.info(f"检测到 {len(triggers)} 个触发条件:")
            for trigger in triggers:
                logger.info(f"  - {trigger.description} (严重性：{trigger.severity:.2f})")
            
            # 4. 判断是否需要调整
            should_adjust = self.grid_calculator.should_adjust(
                triggers=triggers,
                current_price=current_price,
                current_params=self.current_grid_params,
                atr_smooth=atr_smooth
            )
            
            if should_adjust:
                logger.info("\n⚠️  满足调整条件，开始调整网格...")
                
                # 5. 计算新参数
                new_params = self.grid_calculator.calculate_grid_parameters(
                    current_price=current_price,
                    atr_smooth=atr_smooth,
                    market_state=market_result.state
                )
                
                # 6. 执行调整
                success = await self.grid_executor.adjust_grid(
                    grid_id=self.current_grid_id,
                    new_params=new_params
                )
                
                if success:
                    self.current_grid_params = new_params
                    logger.info("✅ 网格调整完成")
                else:
                    logger.error("❌ 网格调整失败")
            else:
                logger.info("\n✅ 无需调整")
            
        except Exception as e:
            logger.error(f"❌ 调整检查失败：{e}")
    
    async def start(self):
        """启动系统"""
        logger.info("\n" + "=" * 60)
        logger.info("🚀 启动网格交易系统")
        logger.info("=" * 60)
        
        if not await self.initialize():
            logger.error("系统初始化失败，退出")
            return
        
        self.is_running = True
        
        # 启动调度器
        await self.scheduler.start()
        
        # 执行首次巡检
        await self.hourly_inspection()
        
        logger.info("\n" + "=" * 60)
        logger.info("✅ 系统已启动，开始自动运行")
        logger.info("=" * 60)
        
        # 保持运行
        while self.is_running:
            await asyncio.sleep(60)
    
    async def stop(self):
        """停止系统"""
        logger.info("\n🛑 停止系统...")
        self.is_running = False
        await self.scheduler.stop()
        logger.info("✅ 系统已停止")


async def main():
    """主函数"""
    system = GridTradingSystem()
    
    try:
        await system.start()
    except KeyboardInterrupt:
        logger.info("\n收到中断信号，停止系统")
        await system.stop()
    except Exception as e:
        logger.error(f"系统异常：{e}")
        await system.stop()


if __name__ == '__main__':
    asyncio.run(main())
