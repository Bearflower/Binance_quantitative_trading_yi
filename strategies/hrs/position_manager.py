"""
持仓管理模块
负责 HRS 策略的持仓跟踪、止损止盈监控、平仓回调等
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Any, Optional, List, Tuple

import structlog

from shared.binance_api import BinanceClient
from shared.config_loader import compute_config_hash
from shared.utils import to_aware_utc
from .position_exit_plan import validate_exit_plan


logger = structlog.get_logger()

# 本模块负责的策略名（写入/更新 condition_orders 时的归属键，避免散落魔法字符串）
_STRATEGY_NAME = "hrs"

# B5: 使用独立持仓时限的入场模式（LV-RM 有独立 time_stop 配置）
_LV_RM_ENTRY_MODE = "lv_rm"
# B5: 入场模式未知的哨兵值（存量仓缺少记录）——不编造模式，按标准时限继续复核
_ENTRY_MODE_UNKNOWN = "unknown"
# B5: 入场期配置指纹的截取长度（16 位足够区分版本，避免整串 SHA-256 落库）
_CONFIG_HASH_LENGTH = 16


class PositionManager:
    """
    持仓管理器

    功能：
    - 持仓跟踪（币种、方向、数量、开仓价、开仓时刻、入场模式）
    - 分批止盈管理（第一目标30%、第二目标40%、移动止盈30%）
    - 时间止损监控（基于下次复核时刻，不覆盖开仓时刻）
    - 平仓后自动取消未触发条件单

    B5 生命周期时间语义（两个时间字段，切勿混用）：
    - opened_at：不可变的真实开仓时刻，入场时确定，之后只读；
    - next_review_at：可变的下次时间止损复核时刻，延期时只前进、不改写 opened_at。
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

        # 时间止损（标准模式时限）
        time_stop_config = trading_config.get("time_stop", {})
        self.max_holding_hours = time_stop_config.get("max_holding_hours", 72)

        # B5: LV-RM 独立持仓时限（与 executor 同一读取口径；未配置时回退标准时限）
        lv_rm_time_stop = config.get("lv_rm", {}).get("time_stop", {})
        self.lv_rm_max_holding_hours = lv_rm_time_stop.get(
            "max_holding_hours", self.max_holding_hours
        )
        # B5: 入场期配置指纹（供决策证据链区分「当时的配置版本」），仅取前 16 位
        self.entry_config_hash: str = compute_config_hash(config)[:_CONFIG_HASH_LENGTH]

        # 持仓检测容差
        detection_config = trading_config.get("position_detection", {})
        self.qty_tolerance_ratio = detection_config.get("qty_tolerance_ratio", 0.01)
        self.qty_tolerance_absolute = detection_config.get("qty_tolerance_absolute", 0.0001)
        # 持仓数量低于该值视为全部平仓/零持仓
        self.zero_qty_threshold = detection_config.get("zero_qty_threshold", 0.0001)

        # 持仓跟踪
        # {symbol: {"direction": "short"/"long", "entry_price": float, "opened_at": datetime,
        #   "next_review_at": datetime, "position_id": str, "entry_mode": str,
        #   "entry_config_hash": str, "entry_quantity": float, "atr": float,
        #   "target1_reached": bool, "target2_reached": bool, "best_price": float,
        #   "remaining_quantity": float}}
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
        entry_mode: Optional[str] = None,
        opened_at: Optional[datetime] = None,
        risk_stop_price: Optional[float] = None,
        exit_ratios: Optional[Tuple[Decimal, Decimal]] = None,
        exit_plan: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        添加持仓跟踪

        Args:
            symbol: 交易对
            direction: 方向 ('short' 或 'long')
            entry_price: 入场价格
            quantity: 数量
            atr: ATR值
            entry_mode: 入场模式（standard/emm/semi_emm/lv_rm）；未提供记为 unknown
                （不编造模式，时间止损按标准时限复核）
            opened_at: 真实开仓时刻（tz-aware UTC）；未提供则取当前时刻
        """
        frozen = validate_exit_plan(exit_plan) if exit_plan is not None else None
        lifecycle = self._build_lifecycle_metadata(symbol, direction, entry_mode, opened_at)
        if frozen is not None:
            lifecycle['next_review_at'] = lifecycle['opened_at'] + timedelta(hours=frozen['params']['max_holding_hours'])
        self._positions[symbol] = {
            "direction": direction,
            "entry_price": entry_price,
            "entry_quantity": quantity,
            "atr": atr,
            "target1_reached": False,
            "target2_reached": False,
            "best_price": entry_price,  # 做空记录最低价，做多记录最高价
            "remaining_quantity": quantity,
            "risk_stop_price": risk_stop_price,
            "exit_plan": frozen,
            "exit_ratios": ([str(value) for value in exit_ratios]
                            if exit_ratios is not None else None),
            # FR-01: 合并开仓阶段暂存的 pending algoId（保护单创建早于 add_position）
            "algo_ids": self._pending_algo_ids.pop(symbol, {}),  # role -> algoId, role: "sl"/"tp1"/"tp2"
            **lifecycle,
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
            entry_mode=lifecycle["entry_mode"],
            position_id=lifecycle["position_id"],
        )

    def _position_holding_hours(self, pos):
        frozen = pos.get('exit_plan')
        return (validate_exit_plan(frozen)['params']['max_holding_hours'] if frozen is not None
                else self._resolve_holding_hours(pos.get('entry_mode')))

    def _resolve_holding_hours(self, entry_mode: Optional[str]) -> float:
        """按入场模式解析持仓时限（小时）：仅 lv_rm 用独立时限，其余（含未知）用标准时限。"""
        if entry_mode == _LV_RM_ENTRY_MODE:
            return self.lv_rm_max_holding_hours
        return self.max_holding_hours

    @staticmethod
    def _build_position_id(symbol: str, direction: str, opened_at: datetime) -> str:
        """构造确定性持仓生命周期身份：同币平仓后再开（新的开仓时刻）= 新周期。"""
        return f"hrs:{symbol}:{direction}:{int(opened_at.timestamp() * 1000)}"

    def _build_lifecycle_metadata(
        self,
        symbol: str,
        direction: str,
        entry_mode: Optional[str],
        opened_at: Optional[datetime],
    ) -> Dict[str, Any]:
        """构造 B5 持仓生命周期元数据（不可变开仓时刻 + 可变下次复核时刻 + 入场证据）"""
        opened = to_aware_utc(opened_at) or datetime.now(timezone.utc)
        mode = entry_mode or _ENTRY_MODE_UNKNOWN
        return {
            "position_id": self._build_position_id(symbol, direction, opened),
            "opened_at": opened,
            "next_review_at": opened + timedelta(hours=self._resolve_holding_hours(mode)),
            "entry_mode": mode,
            "entry_config_hash": self.entry_config_hash,
            "opening_time_uncertain": False,
            "entry_mode_unknown": mode == _ENTRY_MODE_UNKNOWN,
        }

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

        frozen = pos.get('exit_plan')
        atr = pos.get("atr", 0)
        if frozen is None and atr <= 0:
            return False

        frozen = pos.get('exit_plan')
        if frozen is not None:
            frozen = validate_exit_plan(frozen)
            atr = frozen['atr']
        multiplier = frozen['params']['trailing_atr_multiplier'] if frozen else self.trailing_stop_atr_multiplier
        threshold = atr * multiplier

        if pos["direction"] == "short":
            # 做空：从最低价反弹超过阈值
            bounce = current_price - pos["best_price"]
            return bounce >= threshold
        else:
            # 做多：从最高价回撤超过阈值
            drawdown = pos["best_price"] - current_price
            return drawdown >= threshold

    def _deadline_from_opened(self, pos: Dict[str, Any]) -> Optional[datetime]:
        """按不可变 opened_at 与模式时限推导复核时刻（元数据缺失时的兜底，不编造时间）。"""
        opened = to_aware_utc(pos.get("opened_at"))
        if opened is None:
            return None
        return opened + timedelta(hours=self._position_holding_hours(pos))

    def check_time_stop(self, symbol: str) -> bool:
        """
        检查时间止损（B5：比较下次复核时刻，而非用可能被重置的时间倒推开仓时长）

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

        deadline = to_aware_utc(pos.get("next_review_at")) or self._deadline_from_opened(pos)
        if deadline is None:
            # 时间元数据缺失，不误判为超时（保守保留持仓）
            return False
        return datetime.now(timezone.utc) >= deadline

    def reset_time_stop(self, symbol: str) -> None:
        """
        重置时间止损计时器（只前进下次复核时刻，绝不改写开仓时刻）

        当时间止损触发但重新分析后认为仍值得持有，调用此方法
        延长持仓时间，避免立即平仓。B5：开仓时刻 opened_at 保持不可变，
        否则收入匹配窗与持仓身份会被错误截短。

        Args:
            symbol: 交易对
        """
        pos = self._positions.get(symbol)
        if not pos:
            return
        hours = self._position_holding_hours(pos)
        pos["next_review_at"] = datetime.now(timezone.utc) + timedelta(hours=hours)
        logger.info(
            "时间止损已重置",
            symbol=symbol,
            next_review_at=pos["next_review_at"].isoformat(),
        )

    def mark_target_reached(
        self, symbol: str, target: int, actual_remaining_quantity: Optional[float] = None
    ) -> None:
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

        if actual_remaining_quantity is not None:
            pos["remaining_quantity"] = actual_remaining_quantity

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
            await record_condition_order(
                self.db, _STRATEGY_NAME, symbol, algo_id=algo_id, order_type=order_type,
                tp_level={"tp1": 1, "tp2": 2}.get(role),
            )
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

    def get_algo_id(self, symbol: str, role: str) -> Optional[int]:
        """
        获取指定角色的条件单 algoId

        Args:
            symbol: 交易对
            role: 角色 ("sl"/"tp1"/"tp2"/"tp2_trailing")

        Returns:
            algoId 或 None
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return None
        return pos.get("algo_ids", {}).get(role)

    def remove_algo_id(self, symbol: str, role: str) -> None:
        """
        移除指定角色的 algoId 记录（取消订单后调用）

        Args:
            symbol: 交易对
            role: 角色
        """
        pos = self._positions.get(symbol)
        if pos:
            pos.get("algo_ids", {}).pop(role, None)

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
        """按签名委托和本策略创建回执逐单清理，包括重启后丢失的本地角色。"""
        from shared.condition_orders import IncompleteAlgoCancellation, cancel_strategy_algos
        try:
            result = await cancel_strategy_algos(self.binance_api, symbol, tracked_ids=self.get_algo_ids(symbol))
        except IncompleteAlgoCancellation as exc:
            logger.warning("部分保护单撤单结果待核对", symbol=symbol, error=str(exc))
            return {**exc.result, "method": "individual"}
        except Exception as exc:
            logger.warning("保护单撤单待核对，保留追踪", symbol=symbol, error=str(exc))
            return {"total": 0, "cancelled": 0, "failed": 1, "method": "individual"}
        self.clear_algo_ids(symbol)
        return {**result, "method": "individual"}

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

    def sync_tracked_qty(self, symbol: str, qty: float) -> None:
        """将「上次跟踪数量」同步为交易所实际持仓量。

        用于补单等已按交易所量对账本地模型的场景：对账后调用，避免紧接其后的
        detect_take_profit_fills 把同一段减仓重复判定为止盈目标成交，从而二次
        缩减 remaining_quantity（造成本地剩余量与交易所不自洽）。
        """
        self._last_tracked_qty[symbol] = qty

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
            self.mark_target_reached(symbol, 1, actual_remaining_quantity=current_qty)
            logger.info(
                "检测到第一目标止盈成交",
                symbol=symbol,
                last_qty=last_qty,
                current_qty=current_qty,
            )
            return 1
        if not pos["target2_reached"]:
            # 第二目标达成
            self.mark_target_reached(symbol, 2, actual_remaining_quantity=current_qty)
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
            for key in ("opened_at", "next_review_at"):
                if isinstance(p.get(key), datetime):
                    p[key] = p[key].isoformat()
            # 回滚兼容：旧版本只认 entry_time 键，此处保留同名别名（值 = opened_at，
            # 不是独立语义字段）——新格式写入后旧版本仍可读取，无需迁移。
            p["entry_time"] = p.get("opened_at")
            positions[symbol] = p
        return {"positions": positions}

    def from_dict(self, data: Dict[str, Any]) -> None:
        """从字典恢复状态（B5：兼容只含旧 entry_time 的历史状态）"""
        positions = data.get("positions", {})
        for symbol, pos in positions.items():
            p = dict(pos)
            opened = self._restore_opened_at(p)
            # entry_time 仅作回滚别名，恢复后不再保留，避免与 opened_at 双字段并存
            p.pop("entry_time", None)
            p["opened_at"] = opened
            p["next_review_at"] = (
                to_aware_utc(p.get("next_review_at"))
                or opened + timedelta(hours=self._position_holding_hours(p))
            )
            p.setdefault("position_id", self._build_position_id(
                symbol, p.get("direction", ""), opened))
            self._positions[symbol] = p
            # 2026-10-10 修复：同步恢复上次跟踪数量。否则重启后首轮
            # detect_take_profit_fills 会走「首次跟踪」分支（last_qty 为 None），
            # 无法识别「已经全部平仓」的持仓，导致幽灵持仓残留并反复触发 -4509。
            self._last_tracked_qty[symbol] = p.get(
                "remaining_quantity", p.get("entry_quantity", 0)
            )

    def _restore_opened_at(self, pos: Dict[str, Any]) -> datetime:
        """恢复不可变开仓时刻（就地补全兼容标记）。

        D9 迁移口径：新状态有 opened_at 则信任；只有旧 entry_time 时（其值可能已被
        时间止损重置覆盖）只能将其作为 opened_at 并标记 opening_time_uncertain，
        不编造原始开仓时刻。entry_mode 缺失时同样标记 entry_mode_unknown，按标准时限复核。
        """
        raw_opened = pos.get("opened_at")
        opened = to_aware_utc(raw_opened)
        legacy_time = to_aware_utc(pos.get("entry_time"))
        uncertain = bool(pos.get("opening_time_uncertain")) or raw_opened is None
        if opened is None:
            opened = legacy_time or datetime.now(timezone.utc)
        if not pos.get("entry_mode"):
            pos["entry_mode"] = _ENTRY_MODE_UNKNOWN
            pos["entry_mode_unknown"] = True
        else:
            pos["entry_mode_unknown"] = bool(pos.get("entry_mode_unknown"))
        pos["opening_time_uncertain"] = uncertain
        return opened
