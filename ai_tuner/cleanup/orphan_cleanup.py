"""
孤儿条件单清理任务（阶段二）

每 30 分钟执行一次，根据 condition_orders 表与交易所实时持仓对比，
自动取消孤儿条件单并发送飞书通知。

处理场景：
- 场景A: 策略崩溃（>2 小时未更新） — 取消该策略所有 OPEN 条件单
- 场景B: 策略正常但交易所无该 symbol 持仓 — 取消该 symbol 的 OPEN 条件单

判断依据（v2.0）：
  场景B 优先使用交易所实时持仓数据（get_position API），
  仅当交易所 API 调用失败时，回退到 strategy_states 表的快照数据。
  彻底解决因 strategy_states 数据延迟或解析异常导致的误判问题。
"""

import asyncio
import structlog
from datetime import datetime, timezone
from typing import Set, Optional

from shared.condition_orders import get_open_orders, mark_order_canceled, mark_order_executed, ensure_table
from shared.binance_api import BinanceClient, BinanceAPIError
from shared.notification import NotificationClient

logger = structlog.get_logger()


class OrphanCleanupJob:
    """孤儿条件单清理任务"""

    # 验证间隔（秒）：批量取消后等待多久再验证
    _VERIFY_INTERVAL_SECONDS = 1

    def __init__(
        self,
        db,
        binance_client: BinanceClient,
        notification_client: NotificationClient,
        stale_hours_threshold: float = 2.0,
    ):
        """
        初始化孤儿条件单清理任务

        Args:
            db: 数据库管理器实例
            binance_client: Binance API 客户端，用于取消订单
            notification_client: 通知客户端实例
            stale_hours_threshold: 策略状态超时阈值（小时），超过此时间则视为策略异常
        """
        self.db = db
        self.binance = binance_client
        self.notification_client = notification_client
        self.stale_hours_threshold = stale_hours_threshold

    async def _ensure_table(self):
        """确保 condition_orders 表存在"""
        await ensure_table(self.db)

    async def _get_exchange_positions(self) -> Optional[Set[str]]:
        """
        从交易所获取实时持仓（所有有持仓的 symbol 集合）

        Returns:
            set: 有持仓的 symbol 集合（如 {"ETHUSDT", "BTCUSDT"}）
            None: API 调用失败时返回 None，表示无法获取实时数据
        """
        try:
            positions = await self.binance.get_position()
            result = set()
            for pos in positions:
                amt = float(pos.get('positionAmt', 0))
                if abs(amt) > 0.0001:  # 过滤掉持仓量为 0 的币种
                    result.add(pos['symbol'])
            if result:
                logger.info("交易所实时持仓获取成功", symbols=list(result), count=len(result))
            else:
                logger.info("交易所实时持仓为空")
            return result
        except BinanceAPIError as e:
            logger.warning("获取交易所实时持仓失败（API错误）", error_code=e.code, error=e.message)
            return None
        except Exception as e:
            logger.warning("获取交易所实时持仓失败", error=str(e))
            return None

    async def _query_strategy_states(self):
        """
        查询各策略当前持仓状态

        Returns:
            dict: {strategy_name: {"symbols": set, "updated_at": datetime}}
        """
        rows = await self.db.fetch_all("""
            SELECT strategy_name, state_data, updated_at
            FROM strategy_states
            WHERE state_key = 'main'
            ORDER BY strategy_name
        """)
        result = {}
        for row in rows:
            sn = row['strategy_name']
            state_data = row.get('state_data', {})
            # 调试：检查 state_data 的类型和内容
            state_data_type = type(state_data).__name__
            if not isinstance(state_data, dict):
                logger.warning("策略状态数据异常", strategy=sn, type=state_data_type, value=str(state_data)[:200])
                state_data = {}
            positions = state_data.get('positions', {})
            if not isinstance(positions, dict):
                logger.warning("策略持仓数据异常", strategy=sn, type=type(positions).__name__, value=str(positions)[:200])
                positions = {}
            symbols = set(positions.keys())
            result[sn] = {
                "symbols": symbols,
                "updated_at": row['updated_at'],
            }
        return result

    async def _cancel_order(self, order):
        """
        取消单个条件单/订单，并更新 condition_orders 表状态

        Args:
            order: condition_orders 表中的一条记录 (dict)

        Returns:
            (bool, str): (是否成功, 错误信息或 None)
        """
        strategy_name = order['strategy_name']
        symbol = order['symbol']
        algo_id = order.get('algo_id')
        order_id = order.get('order_id')
        order_type = order.get('order_type', '')

        try:
            if algo_id is not None:
                # 条件单用 cancel_algo_order
                await self.binance.cancel_algo_order(symbol, int(algo_id))
                logger.info("孤儿条件单已取消（首次调用）", strategy=strategy_name, symbol=symbol,
                            algo_id=algo_id, type=order_type)

                # 验证：再次取消同一订单，确认是否真的已取消
                # 如果第二次返回 -2011（订单不存在），说明第一次取消成功
                # 如果第二次也返回成功，说明 API 可能返回了虚假成功，订单未实际取消
                await asyncio.sleep(self._VERIFY_INTERVAL_SECONDS)  # 等待后验证
                try:
                    await self.binance.cancel_algo_order(symbol, int(algo_id))
                    # 第二次调用也返回成功（无异常）—— 异常情况！
                    # 说明 API 可能返回了虚假的"成功"响应，订单未实际取消
                    logger.warning("条件单取消验证失败：API 再次返回成功，订单可能未实际取消",
                                  strategy=strategy_name, symbol=symbol,
                                  algo_id=algo_id, type=order_type)
                    # 不标记为已取消，留待下次清理周期重试
                    return False, "API 返回虚假成功，订单未实际取消，等待下次重试"
                except BinanceAPIError as v:
                    if v.code == -2011:
                        # -2011 表示订单已不存在，确认取消成功
                        logger.info("条件单取消验证通过：订单已不存在",
                                    strategy=strategy_name, symbol=symbol, algo_id=algo_id)
                    else:
                        # 其他错误，重新抛出
                        raise
            elif order_id is not None:
                # 普通订单用 cancel_order
                await self.binance.cancel_order(symbol, str(order_id))
                logger.info("孤儿普通订单已取消", strategy=strategy_name, symbol=symbol,
                            order_id=order_id, type=order_type)
            else:
                logger.warning("订单无 ID，跳过", strategy=strategy_name, symbol=symbol)
                return False, "无订单ID"

            # 标记为已取消
            await mark_order_canceled(self.db, order_id=order_id, algo_id=algo_id)
            return True, None

        except BinanceAPIError as e:
            if e.code == -2011:
                # 订单已不存在（-2011）视为取消成功
                await mark_order_canceled(self.db, order_id=order_id, algo_id=algo_id)
                return True, None
            if e.code == -2021:
                # 条件单已执行（algo 已不存在或已触发）
                await mark_order_executed(self.db, algo_id=algo_id, order_id=order_id)
                return True, "订单已执行"
            logger.warning("取消失败", strategy=strategy_name, symbol=symbol,
                           error_code=e.code, error=e.message)
            return False, f"[{e.code}] {e.message}"
        except Exception as e:
            error_str = str(e)
            # 兜底：字符串匹配方式处理非 BinanceAPIError 异常
            if "-2011" in error_str or "Order does not exist" in error_str:
                await mark_order_canceled(self.db, order_id=order_id, algo_id=algo_id)
                return True, None
            if "algo" in error_str.lower() and (
                "not exist" in error_str.lower() or "invalid" in error_str.lower()
            ):
                await mark_order_executed(self.db, algo_id=algo_id, order_id=order_id)
                return True, "订单已执行"
            logger.warning("取消失败", strategy=strategy_name, symbol=symbol, error=error_str)
            return False, error_str

    async def _bulk_cancel_orders(
        self,
        symbol: str,
        orders: list,
        canceled: list,
        failed: list,
        scenario: str,
    ) -> bool:
        """
        批量取消指定交易对的所有条件单，并更新数据库状态

        使用 DELETE /papi/v1/um/algo/allOpenOrders 端点，一次取消该币种的所有 OPEN 条件单。
        相比逐个取消，此端点更可靠，不会出现"API 返回成功但订单未实际取消"的问题，
        同时也能清理数据库中未记录的条件单（如容器重启后丢失跟踪的订单）。

        Args:
            symbol: 交易对名称
            orders: 该交易对在 condition_orders 表中的 OPEN 订单列表
            canceled: 成功取消的列表（追加）
            failed: 取消失败的列表（追加）
            scenario: 场景描述（用于通知消息）

        Returns:
            bool: 是否成功
        """
        try:
            await self.binance.cancel_all_algo_orders(symbol)
            logger.info("批量取消条件单成功", symbol=symbol, scenario=scenario, db_order_count=len(orders))

            # 验证：逐个确认条件单是否真的已取消
            # 先收集验证通过的订单，再统一处理，避免验证失败时回滚 canceled 列表
            verified_canceled = []
            for order in orders:
                algo_id = order.get('algo_id')
                order_id = order.get('order_id')
                if algo_id is not None:
                    await asyncio.sleep(self._VERIFY_INTERVAL_SECONDS)  # 等待后验证
                    try:
                        await self.binance.cancel_algo_order(symbol, int(algo_id))
                        # 第二次调用也返回成功（无异常）—— 异常情况！
                        # 说明批量取消可能未实际生效，该订单仍存在
                        logger.warning(
                            "批量取消后订单仍存在，标记为未取消",
                            symbol=symbol, algo_id=algo_id, type=order.get('order_type', '')
                        )
                        # 标记为验证失败，加入 failed 列表，不加入 verified_canceled
                        failed.append(
                            f"{order['strategy_name']} | {symbol} | {order['order_type']} "
                            f"(algoId:{algo_id}) | {scenario}失败: 批量取消未实际生效"
                        )
                    except BinanceAPIError as v:
                        if v.code == -2011:
                            # -2011 表示订单已不存在，确认取消成功
                            logger.debug("验证通过：订单已取消", symbol=symbol, algo_id=algo_id)
                            verified_canceled.append(order)
                        else:
                            # 其他错误，标记为失败
                            logger.warning("验证异常", symbol=symbol, algo_id=algo_id,
                                           error_code=v.code, error=v.message)
                            failed.append(
                                f"{order['strategy_name']} | {symbol} | {order['order_type']} "
                                f"(algoId:{algo_id}) | {scenario}失败: [{v.code}] {v.message}"
                            )
                else:
                    # 没有 algo_id 的订单（普通订单），无法用此方式验证，直接视为成功
                    verified_canceled.append(order)

            # 统一处理验证通过的订单：标记数据库状态 + 加入 canceled 列表
            for order in verified_canceled:
                sn = order['strategy_name']
                algo_id = order.get('algo_id')
                order_id = order.get('order_id')
                await mark_order_canceled(self.db, order_id=order_id, algo_id=algo_id)
                canceled.append(
                    f"{sn} | {symbol} | {order['order_type']} "
                    f"(algoId:{algo_id or order_id}) | {scenario}"
                )

            # 验证失败的订单，不回滚数据库状态，留待下次清理周期发现并处理
            # 额外记录：即使数据库中没有该币种的订单，批量取消也清理了交易所上的残留订单
            if not orders:
                canceled.append(f"(无DB记录) | {symbol} | 批量清理 | {scenario}")

            return True
        except BinanceAPIError as e:
            logger.warning("批量取消条件单失败", symbol=symbol, error_code=e.code, error=e.message)
            failed.append(f"批量取消 | {symbol} | 全部 | {scenario}失败: [{e.code}] {e.message}")
            return False
        except Exception as e:
            logger.warning("批量取消条件单失败", symbol=symbol, error=str(e))
            failed.append(f"批量取消 | {symbol} | 全部 | {scenario}失败: {str(e)}")
            return False

    async def execute(self):
        """执行清理检查并取消孤儿条件单"""
        logger.info("开始孤儿条件单清理检查")

        try:
            # 1. 确保 condition_orders 表存在
            await self._ensure_table()

            # 2. 查询所有 OPEN 状态的条件单
            open_orders = await get_open_orders(self.db)

            # 3. 获取交易所实时持仓（作为最终判断依据）
            exchange_positions = await self._get_exchange_positions()
            if exchange_positions is None:
                logger.warning("交易所实时持仓获取失败，回退到 strategy_states 快照数据")
            else:
                logger.info("交易所实时持仓已获取，将作为最终判断依据", count=len(exchange_positions))

            # 4. 查询各策略当前持仓状态
            strategy_states = await self._query_strategy_states()
            now = datetime.now(timezone.utc)

            canceled = []   # 成功取消的列表
            failed = []     # 取消失败的列表
            skipped = []    # 跳过（正常持仓中的条件单）

            if not open_orders:
                logger.info("数据库中无 OPEN 条件单")
                # 即使数据库无记录，也检查交易所是否有无持仓的币种需要清理
                open_orders = []

            # 5. 按币种分组订单，区分场景A和场景B
            # 场景A：策略崩溃/无状态 → 逐个取消（仅取消该策略的订单）
            # 场景B：交易所无持仓 → 批量取消（取消该币种的所有订单，含未记录订单）
            stale_orders = []       # 场景A：策略长时间未更新的订单
            no_position_symbols = set()  # 场景B：交易所无持仓的币种

            for order in open_orders:
                sn = order['strategy_name']
                symbol = order['symbol']
                state = strategy_states.get(sn)

                # 场景A: 策略无状态记录
                if not state:
                    stale_orders.append(order)
                    continue

                # 场景A: 策略超过阈值未更新
                updated_at = state.get('updated_at')
                if updated_at:
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=timezone.utc)
                    hours_since = (now - updated_at).total_seconds() / 3600
                    if hours_since > self.stale_hours_threshold:
                        # 如果交易所确认有持仓，跳过
                        if exchange_positions is not None and symbol in exchange_positions:
                            skipped.append(f"{sn} | {symbol} | {order['order_type']} | 场景A跳过: 交易所确认有持仓（策略{hours_since:.1f}h未更新，但持仓有效）")
                            continue
                        stale_orders.append(order)
                        continue

                # 场景B：交易所持仓判断
                if exchange_positions is not None:
                    if symbol in exchange_positions:
                        skipped.append(f"{sn} | {symbol} | {order['order_type']} | 交易所确认有持仓")
                    else:
                        no_position_symbols.add(symbol)

            # 6. 处理场景A：逐个取消策略崩溃的订单（含验证）
            for order in stale_orders:
                sn = order['strategy_name']
                symbol = order['symbol']
                ok, err = await self._cancel_order(order)
                if ok:
                    canceled.append(f"{sn} | {symbol} | {order['order_type']} (algoId:{order.get('algo_id') or order.get('order_id')}) | 场景A: 策略崩溃")
                else:
                    failed.append(f"{sn} | {symbol} | {order['order_type']} (algoId:{order.get('algo_id') or order.get('order_id')}) | 场景A失败: {err}")

            # 7. 处理场景B：批量取消交易所无持仓币种的所有条件单
            #    使用 DELETE /papi/v1/um/algo/allOpenOrders 一次性清理，
            #    同时也能清理数据库中未记录的条件单（彻底解决孤儿单问题）
            for symbol in no_position_symbols:
                symbol_orders = [o for o in open_orders if o['symbol'] == symbol]
                await self._bulk_cancel_orders(
                    symbol=symbol,
                    orders=symbol_orders,
                    canceled=canceled,
                    failed=failed,
                    scenario="场景B: 交易所无持仓（批量取消）",
                )

            # 8. 发送飞书通知
            if canceled or failed:
                msg_parts = ["孤儿条件单自动清理完成"]
                if canceled:
                    msg_parts.append(f"\n✅ 成功取消：{len(canceled)} 个")
                    for c in canceled[:15]:  # 最多显示 15 条，避免消息过长
                        msg_parts.append(f"  ├─ {c}")
                    if len(canceled) > 15:
                        msg_parts.append(f"  └─ ... 还有 {len(canceled) - 15} 条")
                if failed:
                    msg_parts.append(f"\n❌ 失败：{len(failed)} 个")
                    for f_item in failed:
                        msg_parts.append(f"  └─ {f_item}")
                if skipped:
                    msg_parts.append(f"\n⏭️ 跳过（正常持仓）：{len(skipped)} 个")

                msg = "\n".join(msg_parts)
                logger.warning("孤儿条件单清理完成", canceled=len(canceled), failed=len(failed), skipped=len(skipped))
                await self.notification_client.send(
                    message=msg,
                    level="warning",
                    project="tuner"
                )
            else:
                logger.info("孤儿条件单清理检查完成，所有条件单正常")

        except Exception as e:
            logger.error("孤儿条件单清理检查失败", error=str(e), exc_info=True)