#!/usr/bin/env python3
"""
持仓监控模块
负责定时巡检持仓状态，检查止盈止损条件，记录监控日志

监控策略:
- 每 15 分钟检查一次持仓
- 检查止盈止损条件
- 评估强平风险
- 记录监控日志
- 发送飞书通知
"""

import logging
import os
import asyncio
from decimal import Decimal
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from utils.binance_trade_api import get_trade_api, BinanceTradeAPI
from models.database import get_db_manager, DatabaseManager
from utils.lark_notifier import LarkNotifier
from services.close_detector import get_close_detector
from services.trade_statistics import get_stats_calculator
from services.weekly_report import get_report_generator

logger = logging.getLogger(__name__)


class PositionMonitor:
    """持仓监控器类"""
    
    def __init__(self):
        """初始化持仓监控器"""
        self.api: BinanceTradeAPI = get_trade_api()
        self.db: DatabaseManager = get_db_manager()
        
        # 飞书通知
        lark_webhook = os.getenv('LARK_WEBHOOK_URL')
        self.notifier = LarkNotifier(lark_webhook) if lark_webhook else None
        
        # 监控配置
        self.monitoring_interval = int(os.getenv('MONITORING_INTERVAL_MINUTES', '15'))
        self.profit_take_threshold = Decimal(os.getenv('PROFIT_TAKE_THRESHOLD', '0.02'))  # 2%
        self.stop_loss_threshold = Decimal(os.getenv('STOP_LOSS_THRESHOLD', '0.01'))  # 1%
        self.liquidation_warning_level = Decimal(os.getenv('LIQUIDATION_WARNING_LEVEL', '0.1'))  # 10%
        
        # 风险等级定义
        self.risk_levels = {
            'HIGH': Decimal('0.05'),      # 距离强平价<5%
            'MEDIUM': Decimal('0.10'),    # 距离强平价<10%
            'LOW': Decimal('0.20'),       # 距离强平价<20%
            'NONE': Decimal('1.0')        # 距离强平价>=20%
        }
        
        self.scheduler: Optional[AsyncIOScheduler] = None
        
        logger.info("持仓监控器初始化完成")
        logger.info(f"监控间隔：{self.monitoring_interval} 分钟")
        logger.info(f"止盈阈值：{self.profit_take_threshold * 100}%")
        logger.info(f"止损阈值：{self.stop_loss_threshold * 100}%")
    
    def start_monitoring(self):
        """启动定时监控"""
        if self.scheduler:
            logger.warning("监控已在运行")
            return
        
        self.scheduler = AsyncIOScheduler(timezone='Asia/Shanghai')
        
        # 添加定时任务
        self.scheduler.add_job(
            self.monitoring_task,
            IntervalTrigger(minutes=self.monitoring_interval),
            id='position_monitoring',
            name='持仓监控',
            replace_existing=True
        )
        
        self.scheduler.start()
        logger.info(f"✅ 持仓监控已启动，每 {self.monitoring_interval} 分钟执行一次")
        
        # 发送启动通知
        if self.notifier:
            self.notifier.send_text_message(
                f"📊 持仓监控已启动\n"
                f"监控间隔：{self.monitoring_interval} 分钟\n"
                f"止盈阈值：{float(self.profit_take_threshold * 100):.1f}%\n"
                f"止损阈值：{float(self.stop_loss_threshold * 100):.1f}%"
            )
    
    def stop_monitoring(self):
        """停止定时监控"""
        if self.scheduler:
            self.scheduler.shutdown()
            self.scheduler = None
            logger.info("❌ 持仓监控已停止")
    
    async def monitoring_task(self):
        """监控任务 (每 15 分钟执行一次)"""
        logger.info("=" * 60)
        logger.info(f"开始持仓监控任务 - {datetime.now()}")
        
        try:
            # 1. 获取所有持仓
            positions = self.api.get_all_positions()
            logger.info(f"当前持仓数量：{len(positions)}")
            
            if not positions:
                logger.info("无持仓")
            else:
                # 2. 逐个检查持仓
                monitoring_results = []
                
                for position in positions:
                    result = await self.check_position(position)
                    monitoring_results.append(result)
                
                # 3. 保存监控日志
                for result in monitoring_results:
                    self.db.save_monitoring_log(**result)
                
                # 4. 发送监控报告
                if self.notifier and monitoring_results:
                    self._send_monitoring_report(monitoring_results)
            
            # 5. 保存账户余额快照
            self._save_balance_snapshots()
            
            # 6. 检测平仓订单（新增）
            logger.info("检测平仓订单...")
            close_detector = get_close_detector()
            closed_positions = close_detector.detect_closed_positions()
            
            if closed_positions:
                logger.info(f"检测到 {len(closed_positions)} 笔新的平仓记录")
                
                # 更新统计数据
                logger.info("更新交易统计...")
                stats_calculator = get_stats_calculator()
                stats_calculator.update_statistics()
            
            # 7. 检查是否需要发送周报（每周日执行）
            if datetime.now().weekday() == 6:  # 周日
                logger.info("生成并发送周报...")
                report_generator = get_report_generator()
                report_generator.send_weekly_report()
            
            logger.info(f"持仓监控任务完成 - {datetime.now()}")
            logger.info("=" * 60)
        
        except Exception as e:
            logger.error(f"监控任务执行失败：{str(e)}", exc_info=True)
            
            if self.notifier:
                self.notifier.send_text_message(f"❌ 持仓监控任务失败：{str(e)}")
    
    async def check_position(self, position: Dict[str, Any]) -> Dict[str, Any]:
        """
        检查单个持仓
        
        Args:
            position: 持仓信息
        
        Returns:
            监控结果
        """
        symbol = position['symbol']
        position_side = position['positionSide']
        position_amt = Decimal(position['positionAmt'])
        entry_price = Decimal(position['entryPrice'])
        liquidation_price = Decimal(position['liquidationPrice'])
        
        # 获取当前价格
        current_price = self.api.get_ticker_price(symbol)
        
        # 计算未实现盈亏率
        if position_amt > 0:  # 多头
            pnl_rate = (current_price - entry_price) / entry_price
            liquidation_distance = (current_price - liquidation_price) / current_price
        else:  # 空头
            pnl_rate = (entry_price - current_price) / entry_price
            liquidation_distance = (liquidation_price - current_price) / current_price
        
        # 判断风险等级
        risk_level = self._assess_risk_level(liquidation_distance)
        
        # 检查止盈止损
        tp_reached = pnl_rate >= self.profit_take_threshold
        sl_reached = pnl_rate <= -self.stop_loss_threshold
        
        # 确定需要采取的行动
        action_taken = 'NONE'
        remark = ''
        
        if sl_reached:
            action_taken = 'ALERT'
            remark = f'⚠️ 触及止损线！盈亏率：{pnl_rate * 100:.2f}%'
            logger.warning(f"{symbol} {position_side} 触及止损线：{pnl_rate * 100:.2f}%")
        
        elif risk_level == 'HIGH':
            action_taken = 'ALERT'
            remark = f'🚨 高风险！距离强平价：{liquidation_distance * 100:.2f}%'
            logger.warning(f"{symbol} {position_side} 高风险：距离强平 {liquidation_distance * 100:.2f}%")
        
        elif tp_reached:
            action_taken = 'NOTIFY'
            remark = f'✅ 触及止盈线！盈亏率：{pnl_rate * 100:.2f}%'
            logger.info(f"{symbol} {position_side} 触及止盈线：{pnl_rate * 100:.2f}%")
        
        # 更新数据库中的持仓记录
        self.db.save_position(position)
        
        # 返回监控结果
        return {
            'check_time': datetime.now(),
            'symbol': symbol,
            'position_data': position,
            'current_price': current_price,
            'pnl_rate': (pnl_rate * 100).quantize(Decimal('0.01')),
            'tp_reached': tp_reached,
            'sl_reached': sl_reached,
            'liquidation_risk': risk_level,
            'action_taken': action_taken,
            'remark': remark
        }
    
    def _assess_risk_level(self, liquidation_distance: Decimal) -> str:
        """
        评估风险等级
        
        Args:
            liquidation_distance: 距离强平价的百分比
        
        Returns:
            风险等级 (HIGH/MEDIUM/LOW/NONE)
        """
        if liquidation_distance < self.risk_levels['HIGH']:
            return 'HIGH'
        elif liquidation_distance < self.risk_levels['MEDIUM']:
            return 'MEDIUM'
        elif liquidation_distance < self.risk_levels['LOW']:
            return 'LOW'
        else:
            return 'NONE'
    
    def _send_monitoring_report(self, results: List[Dict[str, Any]]):
        """发送监控报告"""
        if not self.notifier:
            return
        
        # 检查是否有需要立即通知的情况
        urgent_results = [r for r in results if r['action_taken'] == 'ALERT']
        
        if urgent_results:
            # 发送紧急通知
            message = "🚨 **持仓风险预警**\n\n"
            
            for result in urgent_results:
                message += f"""
{result['symbol']} {result['position_data']['positionSide']}
当前价：{result['current_price']}
开仓价：{result['position_data']['entryPrice']}
盈亏率：{result['pnl_rate']:+.2f}%
风险等级：{result['liquidation_risk']}
{result['remark']}
                """
            
            self.notifier.send_text_message(message)
        
        # 发送定时监控报告 (汇总)
        report = f"📊 **持仓监控报告** ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n"
        
        for result in results:
            pnl_rate = result['pnl_rate']
            risk_indicator = '🔴' if result['liquidation_risk'] == 'HIGH' else (
                '🟡' if result['liquidation_risk'] == 'MEDIUM' else '🟢'
            )
            
            report += f"""
{risk_indicator} {result['symbol']} {result['position_data']['positionSide']}
持仓：{result['position_data']['positionAmt']}
开仓价：{result['position_data']['entryPrice']}
当前价：{result['current_price']}
盈亏：{pnl_rate:+.2f}%
强平价：{result['position_data']['liquidationPrice']}
            """
        
        self.notifier.send_text_message(report)
    
    def _save_balance_snapshots(self):
        """保存账户余额快照"""
        try:
            # U 本位合约账户
            umfut_balance = self.api.get_umfut_balance('USDT')
            account_info = self.api.get_account_info()
            
            total_margin_balance = Decimal(account_info.get('totalWalletBalance', '0'))
            unrealized_profit = Decimal(account_info.get('crossWalletBalance', '0')) - total_margin_balance
            
            self.db.save_balance_snapshot(
                account_type='UMFUTURE',
                asset='USDT',
                wallet_balance=total_margin_balance,
                available_balance=umfut_balance,
                unrealized_profit=unrealized_profit,
                total_margin_balance=total_margin_balance
            )
            
            # 现货账户
            spot_balance = self.api.get_spot_balance('USDT')
            self.db.save_balance_snapshot(
                account_type='SPOT',
                asset='USDT',
                wallet_balance=spot_balance,
                available_balance=spot_balance
            )
            
            logger.info("账户余额快照已保存")
        
        except Exception as e:
            logger.error(f"保存余额快照失败：{str(e)}")
    
    def get_position_summary(self) -> Dict[str, Any]:
        """获取持仓汇总"""
        positions = self.api.get_all_positions()
        
        if not positions:
            return {'total_positions': 0, 'total_pnl': Decimal('0')}
        
        total_unrealized_pnl = sum(
            Decimal(pos.get('unRealizedProfit', '0')) for pos in positions
        )
        
        return {
            'total_positions': len(positions),
            'positions': positions,
            'total_pnl': total_unrealized_pnl,
            'pnl_rate': self._calculate_total_pnl_rate(positions)
        }
    
    def _calculate_total_pnl_rate(self, positions: List[Dict[str, Any]]) -> Decimal:
        """计算总盈亏率"""
        total_margin = sum(
            Decimal(pos.get('positionAmt', '0')) * Decimal(pos.get('entryPrice', '0')) 
            / Decimal(pos.get('leverage', '20'))
            for pos in positions
        )
        
        total_pnl = sum(
            Decimal(pos.get('unRealizedProfit', '0')) for pos in positions
        )
        
        if total_margin == 0:
            return Decimal('0')
        
        return (total_pnl / total_margin * 100).quantize(Decimal('0.01'))
    
    def emergency_close_position(self, symbol: str, position_side: str = None):
        """
        紧急平仓
        
        Args:
            symbol: 交易对
            position_side: 持仓方向，不提供则平所有方向
        """
        try:
            positions = self.api.get_all_positions()
            
            for pos in positions:
                if pos['symbol'] != symbol:
                    continue
                
                if position_side and pos['positionSide'] != position_side:
                    continue
                
                # 市价平仓
                position_amt = Decimal(pos['positionAmt'])
                if position_amt == 0:
                    continue
                
                side = 'SELL' if position_amt > 0 else 'BUY'
                quantity = abs(position_amt)
                
                logger.warning(f"紧急平仓：{symbol} {pos['positionSide']}, 数量：{quantity}")
                
                order = self.api.place_market_order(
                    symbol=symbol,
                    side=side,
                    position_side=pos['positionSide'],
                    quantity=quantity,
                    reduce_only=True
                )
                
                logger.info(f"紧急平仓完成：订单 ID={order['orderId']}")
                
                # 发送通知
                if self.notifier:
                    self.notifier.send_text_message(
                        f"🚨 **紧急平仓**\n\n"
                        f"交易对：{symbol}\n"
                        f"方向：{side} {pos['positionSide']}\n"
                        f"数量：{quantity}\n"
                        f"订单 ID: {order['orderId']}"
                    )
        
        except Exception as e:
            logger.error(f"紧急平仓失败：{str(e)}", exc_info=True)
            if self.notifier:
                self.notifier.send_text_message(f"❌ 紧急平仓失败：{str(e)}")


# 全局监控器实例
_monitor: Optional[PositionMonitor] = None


def get_position_monitor() -> PositionMonitor:
    """获取全局持仓监控器实例"""
    global _monitor
    if _monitor is None:
        _monitor = PositionMonitor()
    return _monitor


async def start_position_monitoring():
    """启动持仓监控"""
    monitor = get_position_monitor()
    monitor.start_monitoring()
    
    # 立即执行一次监控
    await monitor.monitoring_task()


def stop_position_monitoring():
    """停止持仓监控"""
    monitor = get_position_monitor()
    monitor.stop_monitoring()


if __name__ == '__main__':
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 60)
    print("持仓监控器测试")
    print("=" * 60)
    
    monitor = get_position_monitor()
    
    # 测试获取持仓汇总
    print("\n1. 获取持仓汇总...")
    summary = monitor.get_position_summary()
    print(f"持仓汇总：{summary}")
    
    # 测试风险评估
    print("\n2. 测试风险评估...")
    test_distances = [Decimal('0.03'), Decimal('0.08'), Decimal('0.15'), Decimal('0.25')]
    for distance in test_distances:
        risk = monitor._assess_risk_level(distance)
        print(f"  距离强平 {distance * 100:.2f}% -> 风险等级：{risk}")
    
    print("\n" + "=" * 60)
    print("测试完成!")
    print("=" * 60)
