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

import os
from decimal import Decimal
import structlog
from datetime import datetime, timedelta, timezone
from typing import Set, Optional

from shared.condition_orders import get_open_orders, ensure_table
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
        position_lookback_days: float = 7.0,
        require_exchange_confirmation: bool = True,
        alert_on_missing_state: bool = True,
    ):
        """
        初始化孤儿条件单清理任务

        Args:
            db: 数据库管理器实例
            binance_client: Binance API 客户端，用于取消订单
            notification_client: 通知客户端实例
            stale_hours_threshold: 策略状态超时阈值（小时），超过此时间则视为策略异常
            position_lookback_days: _strategy_has_position 查询 trade_records 的回顾窗口（天），
                                    默认 7 天，越长越保守（越不容易误杀活条件单）
            require_exchange_confirmation: R08 开关：无状态分支是否强制以交易所持仓为准
                                           （True 时无法确认或无仓才清理；False 时回退到既有行为）
            alert_on_missing_state: R08 开关：状态缺失而保守保留保护单时是否发送告警
        """
        self.account_id = os.environ.get("TRADE_ACCOUNT_ID") or "default"
        self.db = db
        self.binance = binance_client
        self.notification_client = notification_client
        self.stale_hours_threshold = stale_hours_threshold
        self.position_lookback_days = position_lookback_days
        self.require_exchange_confirmation = require_exchange_confirmation
        self.alert_on_missing_state = alert_on_missing_state

    def _is_confirmed_no_position(self, symbol: str, exchange_positions: Optional[Set[str]]) -> bool:
        """R08-F1/F2：仅当「交易所明确返回且不含该 symbol」才算确认无仓。

        exchange_positions is None（查询失败/无法确认）→ 返回 False，保守保留保护单。
        """
        if exchange_positions is None:
            return False
        return symbol not in exchange_positions

    async def _alert_missing_state(self, alerts: list) -> None:
        """R08-F5：状态缺失而保守保留保护单时，发送聚合告警（可观测）"""
        if not alerts or not self.alert_on_missing_state:
            return
        msg_parts = [f"⚠️ 孤儿清理保守保留：{len(alerts)} 个条件单因策略状态缺失被保留（未撤单）"]
        for item in alerts[:15]:
            msg_parts.append(f"  ├─ {item}")
        if len(alerts) > 15:
            msg_parts.append(f"  └─ ... 还有 {len(alerts) - 15} 条")
        try:
            await self.notification_client.send(
                message="\n".join(msg_parts),
                level="warning",
                project="tuner",
            )
        except Exception as e:  # noqa: BLE001 - 告警失败不得影响清理主流程
            logger.warning("状态缺失告警发送失败", error=str(e))

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
            if not isinstance(positions, list):
                return None
            if self.binance.use_unified_account:
                account_positions = await self.binance.get_um_account_positions()
                if not isinstance(account_positions, list):
                    return None
                positions = positions + account_positions
            result = set()
            for pos in positions:
                amt = Decimal(str(pos['positionAmt']))
                if not amt.is_finite() or not pos['symbol']:
                    return None
                if amt != 0:  # 过滤掉持仓量为 0 的币种
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
            # jsonb 列可能返回 dict（asyncpg 默认）或 str（json.dumps 写入的场景）
            if isinstance(state_data, str):
                try:
                    import json
                    state_data = json.loads(state_data)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("策略状态 JSON 解析失败", strategy=sn, value=state_data[:200])
                    state_data = {}
            if not isinstance(state_data, dict):
                logger.warning("策略状态数据异常", strategy=sn, type=type(state_data).__name__, value=str(state_data)[:200])
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

    async def _strategy_has_position(self, strategy_name: str, symbol: str) -> bool:
        """
        判断某策略在某 symbol 上是否有活交易记录（应有条件单保护的持仓，或准备开仓的 ENTRY 单）。

        B2 修复核心辅助方法：在场景B（交易所无该 symbol 持仓）中，用于逐个订单确认
        该条件单是否真的是孤儿，避免批量 API 误杀其他策略的活条件单（如 ENTRY 类型
        准备开仓但尚未成交的条件单）。

        判断依据（满足任一即认为"有活交易"，应跳过清理）：
        1. trade_records 中该策略在该 symbol 上有最近（7天内）开仓记录但未找到对应平仓记录
        2. condition_orders 中该策略在该 symbol 上有 ENTRY 类型 OPEN 记录（准备开仓）

        Args:
            strategy_name: 策略名称
            symbol: 交易对

        Returns:
            True 表示该策略在该 symbol 上应有条件单保护，场景B应跳过；
            False 表示该策略在该 symbol 上确实无活交易，条件单是孤儿。
        """
        try:
            # 依据2：查 ENTRY 类型条件单（准备开仓，即使交易所暂无持仓也应保留）
            entry_rows = await self.db.fetch_all(
                """
                SELECT id FROM condition_orders
                WHERE strategy_name = $1 AND symbol = $2 AND order_type = 'ENTRY' AND status = 'OPEN' AND account_id=$3
                LIMIT 1
                """,
                strategy_name, symbol, self.account_id,
            )
            if entry_rows:
                logger.debug(
                    "策略在该 symbol 有 ENTRY 类型条件单，跳过清理",
                    strategy=strategy_name, symbol=symbol,
                )
                return True

            # 依据1：查 trade_records 最近 N 天开仓但未平仓（简单近似：查是否有最近开仓侧的记录）
            # 取配置窗口，查该策略在该 symbol 上是否有开仓方向的成交记录
            # 使用 naive datetime（与项目其他代码一致，asyncpg 绑定 timestamptz 需要 naive UTC）
            window_start = datetime.utcnow() - timedelta(days=self.position_lookback_days)
            open_pos_rows = await self.db.fetch_all(
                """
                SELECT id FROM trading.trade_records
                WHERE strategy = $1 AND symbol = $2
                AND executed_at >= $3 AND account_id=$4
                AND realized_pnl IS NULL
                AND order_type <> 'PNL_SUMMARY'
                LIMIT 1
                """,
                strategy_name, symbol, window_start, self.account_id,
            )
            if open_pos_rows:
                logger.debug(
                    "策略在该 symbol 有未平仓的交易记录，跳过清理",
                    strategy=strategy_name, symbol=symbol,
                )
                return True

            return False
        except Exception as e:
            # 查询异常时保守处理：返回 True 跳过清理，避免误杀
            logger.warning(
                "_strategy_has_position 查询异常，保守跳过清理",
                strategy=strategy_name, symbol=symbol, error=str(e),
            )
            return True

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

        if order.get("account_id") != self.account_id or not order.get("id"):
            return False, "缺少可核验的账户或记录身份"
        try:
            if algo_id is None and order_id is None:
                return False, "无订单ID"
            # Re-check immediately before mutation; earlier inventory can be stale
            # by the time this order is reached in a multi-symbol cleanup.
            positions = await self._get_exchange_positions()
            if not self._is_confirmed_no_position(symbol, positions):
                return False, "撤单前持仓未确认空仓"
            try:
                if algo_id is not None:
                    await self.binance.cancel_algo_order(symbol, int(algo_id))
                else:
                    await self.binance.cancel_order(symbol, str(order_id))
            except BinanceAPIError as exc:
                if exc.code != -2011:
                    raise
            if algo_id is not None:
                terminal = await self.binance.get_algo_order(int(algo_id))
                if str(terminal.get("algoId")) != str(algo_id):
                    return False, "条件单身份不符"
                status = terminal.get("algoStatus")
            else:
                terminal = await self.binance.get_order(symbol, int(order_id))
                if str(terminal.get("orderId")) != str(order_id):
                    return False, "订单身份不符"
                status = terminal.get("status")
            if terminal.get("symbol") != symbol or status not in {"CANCELED", "CANCELLED", "FILLED"}:
                return False, "订单终态未确认"
            state = "EXECUTED" if status == "FILLED" else "CANCELED"
            affected = await self.db.execute(
                "UPDATE condition_orders SET status=$1, updated_at=NOW() "
                "WHERE id=$2 AND account_id=$3 AND symbol=$4 AND status='OPEN'",
                state, order["id"], self.account_id, symbol,
            )
            return (True, None) if affected == "UPDATE 1" else (False, "订单状态已变化")
        except Exception as exc:
            logger.warning("取消终态未确认", strategy=strategy_name, symbol=symbol, error=str(exc))
            return False, str(exc)

    async def execute(self):
        """执行清理检查并取消孤儿条件单"""
        logger.info("开始孤儿条件单清理检查")

        try:
            # 1. 确保 condition_orders 表存在
            await self._ensure_table()

            # 2. 查询所有 OPEN 状态的条件单
            open_orders = await get_open_orders(self.db, account_id=self.account_id)

            # 3. 获取交易所实时持仓（作为最终判断依据）
            exchange_positions = await self._get_exchange_positions()
            if exchange_positions is None:
                logger.warning("交易所实时持仓获取失败，保留保护单")
            else:
                logger.info("交易所实时持仓已获取，将作为最终判断依据", count=len(exchange_positions))

            # 4. 查询各策略当前持仓状态
            strategy_states = await self._query_strategy_states()
            now = datetime.utcnow()

            canceled = []   # 成功取消的列表
            failed = []     # 取消失败的列表
            skipped = []    # 跳过（正常持仓中的条件单）
            no_state_alerts = []  # R08-F5：状态缺失而保守保留的告警条目

            if not open_orders:
                logger.info("数据库中无 OPEN 条件单")
                # 即使数据库无记录，也检查交易所是否有无持仓的币种需要清理
                open_orders = []

            # 5. 按币种分组订单，区分场景A和场景B
            # 场景A：策略崩溃/无状态 → 逐个取消（仅取消该策略的订单）
            # 场景B：交易所无持仓 + 策略无活交易记录 → 逐个取消（B2 修复：不再批量 API）
            stale_orders = []       # 场景A：策略长时间未更新的订单
            no_position_symbols = set()  # 场景B：交易所无持仓的币种

            for order in open_orders:
                sn = order['strategy_name']
                symbol = order['symbol']
                state = strategy_states.get(sn)

                # 场景A: 策略无状态记录
                # R08-F1/F2/F3：无状态分支必须先经交易所持仓前置检查，禁止仅凭「状态缺失」判孤儿
                if not state:
                    if self.require_exchange_confirmation or not self._is_confirmed_no_position(symbol, exchange_positions):
                        if not self._is_confirmed_no_position(symbol, exchange_positions):
                            reason = "无法确认持仓" if exchange_positions is None else "交易所确认有持仓"
                            skipped.append(
                                f"{sn} | {symbol} | {order['order_type']} | 无状态但保留: {reason}"
                            )
                            no_state_alerts.append(f"{sn} | {symbol} | {order['order_type']} | {reason}")
                            logger.warning(
                                "策略状态缺失，保守保留保护单（不撤单）",
                                strategy=sn, symbol=symbol, reason=reason,
                            )
                            continue
                        if await self._strategy_has_position(sn, symbol):
                            skipped.append(
                                f"{sn} | {symbol} | {order['order_type']} | 无状态但保留: 策略有活交易记录"
                            )
                            logger.warning(
                                "策略状态缺失但存在活交易记录，保守保留保护单（不撤单）",
                                strategy=sn, symbol=symbol,
                            )
                            continue
                    stale_orders.append(order)
                    continue

                # 场景A: 策略超过阈值未更新
                updated_at = state.get('updated_at')
                if updated_at:
                    # 统一为 naive datetime（asyncpg 从 timestamptz 返回 aware，
                    # 需要 strip tzinfo 才能与 naive utcnow() 做减法）
                    if updated_at.tzinfo is not None:
                        updated_at = updated_at.astimezone(timezone.utc).replace(tzinfo=None)
                    hours_since = (now - updated_at).total_seconds() / 3600
                    if hours_since > self.stale_hours_threshold:
                        # 如果交易所确认有持仓，跳过
                        if (not self._is_confirmed_no_position(symbol, exchange_positions)
                                or await self._strategy_has_position(sn, symbol)):
                            no_state_alerts.append(f"{sn} | {symbol} | 陈旧状态下缺少空仓确认")
                            skipped.append(f"{sn} | {symbol} | {order['order_type']} | 场景A跳过: 未确认可清理（策略{hours_since:.1f}h未更新）")
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

            # 7. 处理场景B：逐个取消交易所无持仓币种的孤儿条件单（B2 修复）
            #    不再用 DELETE /papi/v1/um/algo/allOpenOrders 批量 API（会一把梭干掉
            #    同一 symbol 上所有策略的条件单，包括其他策略的 ENTRY 活条件单）。
            #    改为：对每个 OPEN 订单逐个调用 _cancel_order，同时用 _strategy_has_position
            #    做策略归属检查，确认该策略在该 symbol 上无活交易才取消。
            for symbol in no_position_symbols:
                symbol_orders = [o for o in open_orders if o['symbol'] == symbol]
                for order in symbol_orders:
                    sn = order['strategy_name']
                    # B2 修复：逐个订单检查策略归属，避免误杀其他策略的活条件单
                    has_pos = await self._strategy_has_position(sn, symbol)
                    if has_pos:
                        skipped.append(
                            f"{sn} | {symbol} | {order['order_type']} | "
                            f"场景B跳过: 策略有活交易记录"
                        )
                        continue
                    # 确认无活交易，逐个取消
                    ok, err = await self._cancel_order(order)
                    if ok:
                        canceled.append(
                            f"{sn} | {symbol} | {order['order_type']} "
                            f"(algoId:{order.get('algo_id') or order.get('order_id')}) | "
                            f"场景B: 交易所无持仓+无活交易记录"
                        )
                    else:
                        failed.append(
                            f"{sn} | {symbol} | {order['order_type']} "
                            f"(algoId:{order.get('algo_id') or order.get('order_id')}) | "
                            f"场景B失败: {err}"
                        )

            # 8. 发送飞书通知
            # R08-F5：状态缺失而保守保留时单独告警（即使无取消/失败也可观测）
            await self._alert_missing_state(no_state_alerts)

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