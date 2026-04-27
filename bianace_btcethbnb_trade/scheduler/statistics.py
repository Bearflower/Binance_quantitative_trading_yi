#!/usr/bin/env python3
"""
统计管理模块

功能：
1. 记录每日执行统计
2. 更新交易统计（盈亏）
3. 检查已平仓订单并更新胜率统计
4. 生成每日交易报告
"""

import logging
from datetime import datetime, timedelta, date
from decimal import Decimal
from typing import Dict, Any, Optional
from models.database import get_db_manager

logger = logging.getLogger(__name__)


class StatisticsManager:
    """统计管理类"""

    def __init__(self):
        """初始化统计管理器"""
        self.db = get_db_manager()
        logger.info("统计管理器已初始化")

    def record_daily_stats(self, signals_count: int, executed_count: int):
        """
        记录每日执行统计

        Args:
            signals_count: 检测到的信号数量
            executed_count: 实际执行的交易数量
        """
        try:
            # 获取今日日期
            today = datetime.now().date()

            # 查询今日统计是否已存在
            query = """
                SELECT id, signals_count, executed_count, win_count, loss_count
                FROM daily_execution_stats
                WHERE stat_date = %s
            """
            result = self.db._execute_one(query, (today,))

            if result:
                # 更新现有记录
                update_query = """
                    UPDATE daily_execution_stats
                    SET signals_count = signals_count + %s,
                        executed_count = executed_count + %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE stat_date = %s
                """
                self.db._execute_query(update_query, (signals_count, executed_count, today))
                logger.debug(f"今日统计已更新：信号 +{signals_count}, 执行 +{executed_count}")
            else:
                # 插入新记录
                insert_query = """
                    INSERT INTO daily_execution_stats
                    (stat_date, signals_count, executed_count, win_count, loss_count)
                    VALUES (%s, %s, %s, 0, 0)
                """
                self.db._execute_query(insert_query, (today, signals_count, executed_count))
                logger.debug(f"今日统计已创建：信号 {signals_count}, 执行 {executed_count}")

        except Exception as e:
            logger.error(f"记录每日统计失败：{str(e)}")

    def update_trade_statistics(self, symbol: str, pnl: Decimal, trade_date: date):
        """
        更新交易统计（盈亏）

        Args:
            symbol: 交易对
            pnl: 盈亏金额（正数=盈利，负数=亏损）
            trade_date: 交易日期
        """
        try:
            # 判断盈亏
            is_win = pnl > 0

            # 更新 daily_execution_stats
            if is_win:
                update_query = """
                    UPDATE daily_execution_stats
                    SET win_count = win_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE stat_date = %s
                """
            else:
                update_query = """
                    UPDATE daily_execution_stats
                    SET loss_count = loss_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE stat_date = %s
                """

            self.db._execute_query(update_query, (trade_date,))

            result = '盈利' if is_win else '亏损'
            logger.info(f"📊 交易统计已更新：{symbol} {result} {abs(pnl):.2f} USDT")

        except Exception as e:
            logger.error(f"更新交易统计失败：{str(e)}")

    def check_closed_positions_and_update_stats(self, trade_api):
        """
        检查已平仓订单并更新胜率统计

        通过查询持仓风险来检测是否有仓位已被平掉，
        如果有则计算盈亏并更新统计

        Args:
            trade_api: 交易 API 实例
        """
        try:
            logger.info("检查已平仓订单...")

            # 查询数据库中所有未平仓的交易记录
            query = """
                SELECT id, symbol, direction, entry_price, quantity, order_id
                FROM trade_records
                WHERE status = 'OPEN'
                ORDER BY created_at DESC
            """
            open_positions = self.db._execute_query(query)

            if not open_positions:
                logger.info("无未平仓记录")
                return

            # 检查每个持仓
            for position in open_positions:
                symbol = position['symbol']
                try:
                    # 获取当前持仓风险
                    positions = trade_api.get_position_risk(symbol)

                    # 如果持仓为 0 或空仓，说明已平仓
                    current_qty = abs(Decimal(positions[0]['positionAmt'])) if positions else Decimal('0')
                    entry_qty = position['quantity']

                    if current_qty < entry_qty or not positions:
                        logger.info(f"检测到 {symbol} 已平仓 (持仓：{current_qty}, 开仓：{entry_qty})")

                        # 获取平仓价格（通过账户余额变化或订单历史）
                        # 简化处理：使用当前价格估算
                        ticker = trade_api.get_ticker(symbol)
                        close_price = Decimal(ticker['lastPrice'])
                        entry_price = position['entry_price']
                        direction = position['direction']

                        # 计算盈亏
                        if direction == '多':
                            pnl = (close_price - entry_price) * current_qty
                        else:
                            pnl = (entry_price - close_price) * current_qty

                        # 更新统计
                        trade_date = datetime.now().date()
                        self.update_trade_statistics(symbol, pnl, trade_date)

                        # 更新交易记录状态
                        update_query = """
                            UPDATE trade_records
                            SET status = 'CLOSED',
                                close_price = %s,
                                close_time = %s,
                                pnl = %s,
                                updated_at = %s
                            WHERE id = %s
                        """
                        self.db._execute_query(update_query, (
                            close_price,
                            datetime.now(),
                            pnl,
                            datetime.now(),
                            position['id']
                        ))

                        logger.info(f"✅ {symbol} 平仓统计完成：{'盈利' if pnl > 0 else '亏损'} {abs(pnl):.2f} USDT")

                except Exception as e:
                    logger.error(f"检查 {symbol} 持仓状态失败：{str(e)}")
                    continue

        except Exception as e:
            logger.error(f"检查已平仓订单失败：{str(e)}")

    def generate_daily_report(self, yesterday: date) -> Optional[Dict[str, Any]]:
        """
        生成每日交易报告

        Args:
            yesterday: 昨天的日期

        Returns:
            报告数据字典，如果没有数据则返回 None
        """
        try:
            # 查询昨天的统计数据
            query = """
                SELECT signals_count, executed_count, win_count, loss_count
                FROM daily_execution_stats
                WHERE stat_date = %s
            """
            result = self.db._execute_one(query, (yesterday,))

            if not result:
                logger.info(f"昨天 ({yesterday}) 无统计数据")
                return None

            # 计算胜率
            total_executed = result['executed_count'] or 0
            win_count = result['win_count'] or 0
            win_rate = (win_count / total_executed * 100) if total_executed > 0 else None

            return {
                'date': yesterday,
                'signals_count': result['signals_count'],
                'executed_count': total_executed,
                'win_count': win_count,
                'loss_count': result['loss_count'],
                'win_rate': win_rate,
            }

        except Exception as e:
            logger.error(f"生成日报失败：{str(e)}")
            return None
