"""
持仓管理模块
负责 HRS 策略的持仓跟踪、止损止盈监控、平仓回调等
"""
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import structlog

from shared.binance_api import BinanceClient


logger = structlog.get_logger()


class PositionManager:
    """
    持仓管理器

    功能：
    - 持仓跟踪（币种、方向、数量、开仓价、开仓时间）
    - 分批止盈管理（第一目标30%、第二目标40%、移动止盈30%）
    - 时间止损监控
    - 平仓后自动取消未触发条件单
    """

    def __init__(
        self,
        config: Dict[str, Any],
        binance_api: BinanceClient,
        db: Optional[Any] = None,
    ):
        """
        初始化持仓管理器

        Args:
            config: 配置字典
            binance_api: 币安API客户端
            db: 数据库管理器实例（可选，用于持久化条件单记录）
        """
        self.config = config
        self.binance_api = binance_api
        self.db = db

        trading_config = config.get("trading", {})

        # 分批止盈配置
        batch_config = trading_config.get("batch_take_profit", {})
        self.target1_atr_multiplier = batch_config.get("target1_atr_multiplier", 1.5)
        self.target1_close_percent = batch_config.get("target1_close_percent", 0.30)
        self.target2_atr_multiplier = batch_config.get("target2_atr_multiplier", 3.5)
        self.target2_close_percent = batch_config.get("target2_close_percent", 0.40)
        # P1-7: 移动止盈ATR倍数优先从 trading.trailing 读取，回退到 batch_take_profit
        trailing_config = trading_config.get("trailing", {})
        self.trailing_stop_atr_multiplier = trailing_config.get(
            "atr_multiplier",
            batch_config.get("trailing_stop_atr_multiplier", 1.5),
        )

        # 时间止损
        time_stop_config = trading_config.get("time_stop", {})
        self.max_holding_hours = time_stop_config.get("max_holding_hours", 72)

        # 持仓检测容差
        detection_config = trading_config.get("position_detection", {})
        self.qty_tolerance_ratio = detection_config.get("qty_tolerance_ratio", 0.01)
        self.qty_tolerance_absolute = detection_config.get("qty_tolerance_absolute", 0.0001)
        # 持仓数量低于该值视为全部平仓/零持仓
        self.zero_qty_threshold = detection_config.get("zero_qty_threshold", 0.0001)

        # 持仓跟踪
        # {symbol: {"direction": "short"/"long", "entry_price": float, "entry_time": datetime,
        #   "entry_quantity": float, "atr": float, "target1_reached": bool, "target2_reached": bool,
        #   "best_price": float, "remaining_quantity": float}}
        self._positions: Dict[str, Dict[str, Any]] = {}

        # P1-6: 上次检测到的交易所持仓数量，用于检测止盈单成交
        # {symbol: float}
        self._last_tracked_qty: Dict[str, float] = {}

        # FR-01: 持仓未建立时的条件单 algoId 缓冲（symbol -> {role: algoId}）
        # 开仓保护单创建在 add_position 之前，需暂存避免丢失
        self._pending_algo_ids: Dict[str, Dict[str, int]] = {}

        logger.info(
            "持仓管理器初始化完成",
            target1_atr_multiplier=self.target1_atr_multiplier,
            target2_atr_multiplier=self.target2_atr_multiplier,
            max_holding_hours=self.max_holding_hours,
        )

    def add_position(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        quantity: float,
        atr: float,
    ) -> None:
        """
        添加持仓跟踪

        Args:
            symbol: 交易对
            direction: 方向 ('short' 或 'long')
            entry_price: 入场价格
            quantity: 数量
            atr: ATR值
        """
        self._positions[symbol] = {
            "direction": direction,
            "entry_price": entry_price,
            "entry_time": datetime.now(timezone.utc),
            "entry_quantity": quantity,
            "atr": atr,
            "target1_reached": False,
            "target2_reached": False,
            "best_price": entry_price,  # 做空记录最低价，做多记录最高价
            "remaining_quantity": quantity,
            # FR-01: 合并开仓阶段暂存的 pending algoId（保护单创建早于 add_position）
            "algo_ids": self._pending_algo_ids.pop(symbol, {}),  # role -> algoId, role: "sl"/"tp1"/"tp2"
        }
        # P1-6: 初始化跟踪数量
        self._last_tracked_qty[symbol] = quantity
        logger.info(
            "添加持仓跟踪",
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            quantity=quantity,
            atr=atr,
        )

    def remove_position(self, symbol: str) -> None:
        """
        移除持仓跟踪

        Args:
            symbol: 交易对
        """
        if symbol in self._positions:
            del self._positions[symbol]
            # P1-6: 清理跟踪数量
            self._last_tracked_qty.pop(symbol, None)
            logger.info("移除持仓跟踪", symbol=symbol)

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取持仓信息

        Args:
            symbol: 交易对

        Returns:
            持仓信息字典
        """
        return self._positions.get(symbol)

    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """获取所有持仓"""
        return self._positions.copy()

    def has_position(self, symbol: str) -> bool:
        """
        检查是否有该币种的持仓

        Args:
            symbol: 交易对

        Returns:
            是否有持仓
        """
        return symbol in self._positions

    def get_direction(self, symbol: str) -> Optional[str]:
        """
        获取持仓方向

        Args:
            symbol: 交易对

        Returns:
            方向 ('short'/'long')，无持仓返回 None
        """
        pos = self._positions.get(symbol)
        return pos["direction"] if pos else None

    def update_best_price(self, symbol: str, current_price: float) -> None:
        """
        更新最佳价格

        做空：记录最低价
        做多：记录最高价

        Args:
            symbol: 交易对
            current_price: 当前价格
        """
        pos = self._positions.get(symbol)
        if not pos:
            return

        if pos["direction"] == "short":
            if current_price < pos["best_price"]:
                pos["best_price"] = current_price
        else:
            if current_price > pos["best_price"]:
                pos["best_price"] = current_price

    def check_trailing_stop(self, symbol: str, current_price: float) -> bool:
        """
        检查移动止盈触发

        Args:
            symbol: 交易对
            current_price: 当前价格

        Returns:
            True: 触发移动止盈
        """
        pos = self._positions.get(symbol)
        if not pos or not pos["target2_reached"]:
            return False

        atr = pos.get("atr", 0)
        if atr <= 0:
            return False

        threshold = atr * self.trailing_stop_atr_multiplier

        if pos["direction"] == "short":
            # 做空：从最低价反弹超过阈值
            bounce = current_price - pos["best_price"]
            return bounce >= threshold
        else:
            # 做多：从最高价回撤超过阈值
            drawdown = pos["best_price"] - current_price
            return drawdown >= threshold

    def check_time_stop(self, symbol: str) -> bool:
        """
        检查时间止损

        Args:
            symbol: 交易对

        Returns:
            True: 触发时间止损
        """
        pos = self._positions.get(symbol)
        if not pos:
            return False

        if pos["target1_reached"]:
            return False

        holding_hours = (datetime.now(timezone.utc) - pos["entry_time"]).total_seconds() / 3600
        return holding_hours >= self.max_holding_hours

    def reset_time_stop(self, symbol: str) -> None:
        """
        重置时间止损计时器（将 entry_time 设为当前时间）

        当时间止损触发但重新分析后认为仍值得持有，调用此方法
        延长持仓时间，避免立即平仓。

        Args:
            symbol: 交易对
        """
        pos = self._positions.get(symbol)
        if pos:
            pos["entry_time"] = datetime.now(timezone.utc)
            logger.info("时间止损已重置", symbol=symbol)

    def mark_target_reached(self, symbol: str, target: int) -> None:
        """
        标记止盈目标达成

        Args:
            symbol: 交易对
            target: 目标级别（1 或 2）
        """
        pos = self._positions.get(symbol)
        if not pos:
            return

        if target == 1:
            pos["target1_reached"] = True
            pos["remaining_quantity"] *= (1 - self.target1_close_percent)
            logger.info("第一目标达成", symbol=symbol)
        elif target == 2:
            pos["target2_reached"] = True
            pos["remaining_quantity"] *= (1 - self.target2_close_percent)
            logger.info("第二目标达成", symbol=symbol)

    def add_algo_id(self, symbol: str, role: str, algo_id: int) -> None:
        """
        记录条件单algoId（FR-01 修复）

        持仓已建立 → 写入 pos["algo_ids"]；
        持仓未建立（开仓保护单早于 add_position）→ 写入 pending 缓冲，
        add_position 时合并，避免 algoId 静默丢失。

        Args:
            symbol: 交易对
            role: 角色 ("sl"/"tp1"/"tp2")
            algo_id: Binance返回的algoId
        """
        pos = self._positions.get(symbol)
        if pos is not None:
            pos.setdefault("algo_ids", {})[role] = algo_id
        else:
            self._pending_algo_ids.setdefault(symbol, {})[role] = algo_id

        # 异步记录到 condition_orders 表
        if self.db is not None:
            asyncio.ensure_future(self._record_condition_order(symbol, role, algo_id))

    async def _record_condition_order(self, symbol: str, role: str, algo_id: int) -> None:
        """
        异步记录条件单到 condition_orders 数据库表

        Args:
            symbol: 交易对
            role: 角色 ("sl"/"tp1"/"tp2")
            algo_id: Binance 返回的 algoId
        """
        try:
            from shared.condition_orders import record_condition_order
            order_type_map = {"sl": "STOP_LOSS", "tp1": "TAKE_PROFIT", "tp2": "TAKE_PROFIT"}
            order_type = order_type_map.get(role, "STOP_LOSS")
            await record_condition_order(self.db, "hrs", symbol, algo_id=algo_id, order_type=order_type)
        except Exception as e:
            logger.warning("异步记录条件单失败", symbol=symbol, role=role, algo_id=algo_id, error=str(e))

    def get_algo_ids(self, symbol: str) -> List[int]:
        """
        获取持仓所有条件单algoId列表

        Args:
            symbol: 交易对

        Returns:
            algoId列表
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return []
        return list(pos.get("algo_ids", {}).values())

    def has_algo_id(self, symbol: str, role: str) -> bool:
        """
        检查某个角色的条件单是否已有 algoId 记录

        Args:
            symbol: 交易对
            role: 角色 ("sl"/"tp1"/"tp2")

        Returns:
            True: 已有该角色的条件单记录
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return False
        return role in pos.get("algo_ids", {})

    def clear_algo_ids(self, symbol: str) -> None:
        """
        清空本地 algo_ids 记录（FR-05/07）

        批量取消成功后调用，避免本地记录与交易所事实不一致；
        后续补单/重建会通过 add_algo_id 重新写入。

        Args:
            symbol: 交易对
        """
        pos = self._positions.get(symbol)
        if pos:
            pos["algo_ids"] = {}

    async def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        """
        取消该交易对所有 OPEN 条件单（FR-07）

        - 优先调用 binance_api.cancel_all_algo_orders(symbol)（批量，仅统一账户）
        - 批量不可用/失败 → 回退按本地 algo_ids 逐个取消，记录 warning
        - 任一方式成功 → 清空本地 algo_ids

        Args:
            symbol: 交易对

        Returns:
            取消结果统计 {"total": int, "cancelled": int, "failed": int, "method": "batch"|"individual"}
        """
        # 1. 优先批量取消（仅统一账户）
        if getattr(self.binance_api, "use_unified_account", False):
            try:
                batch_result = await self.binance_api.cancel_all_algo_orders(symbol)
                if batch_result.get("code") == 200:
                    self.clear_algo_ids(symbol)
                    return {
                        "total": batch_result.get("total", 0),
                        "cancelled": batch_result.get("cancelled", 0),
                        "failed": batch_result.get("failed", 0),
                        "method": "batch",
                    }
                logger.warning(
                    "批量取消条件单返回异常状态码",
                    symbol=symbol,
                    result=batch_result,
                )
            except Exception as e:
                logger.warning(
                    "批量取消条件单失败，回退到逐个取消",
                    symbol=symbol,
                    error=str(e),
                )

        # 2. 回退：按本地 algo_ids 逐个取消
        return await self._cancel_individual_orders(symbol)

    async def _cancel_individual_orders(self, symbol: str) -> Dict[str, Any]:
        """
        按本地 algo_ids 逐个取消条件单（批量接口不可用/失败时的回退路径，FR-07）

        Args:
            symbol: 交易对

        Returns:
            取消结果统计
        """
        result = {"total": 0, "cancelled": 0, "failed": 0, "method": "individual"}
        algo_ids = self.get_algo_ids(symbol)
        result["total"] = len(algo_ids)
        for algo_id in algo_ids:
            try:
                await self.binance_api.cancel_algo_order(symbol, algo_id)
                result["cancelled"] += 1
            except Exception as cancel_err:
                result["failed"] += 1
                logger.debug(
                    "取消单个条件单失败",
                    symbol=symbol,
                    algo_id=algo_id,
                    error=str(cancel_err),
                )

        # 全部取消成功才清空本地（部分失败则保留记录，下一轮补单重试仍可取消，FR-07）
        if algo_ids and result["failed"] == 0:
            self.clear_algo_ids(symbol)

        logger.info(
            "取消条件单完成",
            symbol=symbol,
            total=result["total"],
            cancelled=result["cancelled"],
            failed=result["failed"],
        )
        return result

    def detect_take_profit_fills(
        self,
        symbol: str,
        exchange_qty: float,
    ) -> Optional[int]:
        """
        P1-6: 通过对比持仓数量变化检测止盈单成交

        对比交易所实际持仓数量与上次跟踪数量：
        - 100% -> 70%：target1 成交，标记 target1_reached
        - 70% -> 30%：target2 成交，标记 target2_reached 并激活移动止损
        - 0%：全部平仓

        Args:
            symbol: 交易对
            exchange_qty: 交易所当前持仓数量

        Returns:
            达成的目标级别（1/2/0），0 表示全部平仓，None 表示无变化
        """
        pos = self._positions.get(symbol)
        if not pos:
            return None

        last_qty = self._last_tracked_qty.get(symbol)
        if last_qty is None:
            # 首次跟踪，记录当前数量
            self._last_tracked_qty[symbol] = exchange_qty
            return None

        # 允许微小误差（从配置读取）
        qty_tolerance = max(last_qty * self.qty_tolerance_ratio, self.qty_tolerance_absolute)

        if exchange_qty < self.zero_qty_threshold:
            # 全部平仓
            self._last_tracked_qty[symbol] = exchange_qty
            logger.info("持仓全部平仓", symbol=symbol, last_qty=last_qty)
            return 0

        if exchange_qty < last_qty - qty_tolerance:
            # 持仓减少，检测止盈目标（子函数：_detect_target_filled）
            self._last_tracked_qty[symbol] = exchange_qty
            return self._detect_target_filled(symbol, pos, last_qty, exchange_qty)

        return None

    def _detect_target_filled(
        self,
        symbol: str,
        pos: Dict[str, Any],
        last_qty: float,
        current_qty: float,
    ) -> Optional[int]:
        """
        检测持仓减少对应的止盈目标级别（子函数，供 detect_take_profit_fills 调用）

        Args:
            symbol: 交易对
            pos: 持仓记录
            last_qty: 上次跟踪数量
            current_qty: 当前交易所持仓数量

        Returns:
            达成的目标级别（1/2），无目标达成返回 None
        """
        if not pos["target1_reached"]:
            # 第一目标达成
            self.mark_target_reached(symbol, 1)
            logger.info(
                "检测到第一目标止盈成交",
                symbol=symbol,
                last_qty=last_qty,
                current_qty=current_qty,
            )
            return 1
        if not pos["target2_reached"]:
            # 第二目标达成
            self.mark_target_reached(symbol, 2)
            logger.info(
                "检测到第二目标止盈成交，激活移动止损",
                symbol=symbol,
                last_qty=last_qty,
                current_qty=current_qty,
            )
            return 2
        return None

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于持久化）"""
        positions = {}
        for symbol, pos in self._positions.items():
            p = dict(pos)
            if isinstance(p["entry_time"], datetime):
                p["entry_time"] = p["entry_time"].isoformat()
            positions[symbol] = p
        return {"positions": positions}

    def from_dict(self, data: Dict[str, Any]) -> None:
        """从字典恢复状态"""
        positions = data.get("positions", {})
        for symbol, pos in positions.items():
            p = dict(pos)
            et = p.get("entry_time")
            if isinstance(et, str):
                p["entry_time"] = datetime.fromisoformat(et)
            self._positions[symbol] = p