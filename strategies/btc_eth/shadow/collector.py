"""影子采集器调度编排（架构方案 §5）

流程：
  1. 拉 XAUUSDT 的 1h/4h/1d 多周期 K 线（KLineService.get_multi_timeframe_data）
  2. 校验最后一根 K 线已闭合（close_time <= now 且时间戳连续无缺口）
     未闭合/缺失/断链 → 该周期记 data_quality_reason，不产 candidate（spec §12.1）
  3. 两 profile 用 asyncio.gather(..., return_exceptions=True) 并行分析
     任一 profile 异常只影响自己（记 analysis_error），另一继续（spec §12.1）
  4. 全量事件落库（含 candidate / no_signal / analysis_error）
  5. write failure 只降级为日志 + 告警，不 throw 回主循环

采集器只走无状态路径：_build_indicators → get_market_state → _analyze_stationary_signal。
不调 _check_stateful_execution_eligibility、不调 _calculate_position_size。
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

import structlog

from shared.kline_service import KLineService
from shared.readonly_client import ReadOnlyBinanceClient
from shared.config_loader import compute_config_hash
from strategies.btc_eth.strategy import BTCEthStrategy as OriginalBTCEthStrategy
from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as AggressiveBTCEthStrategy
from strategies.btc_eth.market_state import (
    get_market_state,
    get_market_state_behavior,
)

from .repository import ShadowRepository


logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# K 线闭合时间戳容差（毫秒）：允许服务器时钟微偏
# 注意：close_time 的口径来源是 kline_service（services/kline_service/api/routes.py:239）
#       返回的是 int(row["close_time"].timestamp() * 1000)，即毫秒单位。
#       整条链路必须统一毫秒；禁止按数值量级"猜单位"的启发式。
_KLINE_CLOSE_TOLERANCE_MS = 60_000


# ---------------------------------------------------------------------------
# K 线闭合校验
# ---------------------------------------------------------------------------
def _validate_kline_closed(
    klines: List[Dict], interval: str, now_ms: int
) -> Tuple[bool, Optional[str], Optional[int]]:
    """校验某一周期的 K 线数据完整性与最后一根闭合性

    Args:
        klines: get_klines 返回的 K 线列表（close_time 为毫秒时间戳，来源：kline_service）
        interval: 周期字符串（1h/4h/1d）
        now_ms: 当前 Unix 毫秒（调用方统一 clock 避免多函数各取）

    Returns:
        (is_valid, reason, last_close_time_ms)
        - is_valid=True 且 reason=None：数据完整闭合，last_close_time_ms 为最后一根 close_time
        - is_valid=False：reason 给出 data_quality_reason 文本

    单位说明：kline_service 返回的 close_time 是毫秒（int(row["close_time"].timestamp()*1000)），
    整条链路严格使用毫秒；禁止任何秒/毫秒混用的启发式猜测。
    """
    if not klines:
        return False, f"{interval}: K线列表为空", None

    # 1. 数量充足（至少 50 根才能计算指标）
    if len(klines) < 50:
        return False, f"{interval}: K线不足 {len(klines)}/50", klines[-1].get("close_time")

    # 2. 时间戳连续性校验（首尾）
    # 只检查最后 3 根时间戳单调递增 + 步长 ≈ 预期周期（毫秒）
    expected_step_ms = {
        "1h": 3_600_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
    }.get(interval, 3_600_000)

    last_three = klines[-3:] if len(klines) >= 3 else klines
    for i in range(1, len(last_three)):
        prev_ct = last_three[i - 1].get("close_time", 0)
        curr_ct = last_three[i].get("close_time", 0)
        if curr_ct <= prev_ct:
            return (
                False,
                f"{interval}: K线时间戳不递增 last_close_times={prev_ct}→{curr_ct}",
                klines[-1].get("close_time"),
            )
        gap = curr_ct - prev_ct - expected_step_ms
        if abs(gap) > 300_000:     # 5 分钟容差（毫秒）
            return (
                False,
                f"{interval}: K线时间戳断链 gap={gap}ms expected={expected_step_ms}ms",
                klines[-1].get("close_time"),
            )

    # 3. 最后一根闭合校验（close_time <= now_ms + tolerance_ms）
    last_close_time = klines[-1].get("close_time")
    if last_close_time is None:
        return False, f"{interval}: 最后一根无 close_time", None

    if last_close_time > now_ms + _KLINE_CLOSE_TOLERANCE_MS:
        return (
            False,
            f"{interval}: 最后一根未闭合 close_time={last_close_time} now={now_ms}",
            last_close_time,
        )

    return True, None, last_close_time


def _validate_all_intervals(
    multi_klines: Dict[str, List[Dict]],
    intervals: List[str],
    now_ms: int,
) -> Tuple[bool, Dict[str, str], Dict[str, int]]:
    """对全部周期 K 线做闭合校验

    Args:
        multi_klines: get_multi_timeframe_data 返回 {interval: klines}
        intervals: 要求的周期列表
        now_ms: 当前 Unix 毫秒（整条链路统一毫秒口径，来源见 _validate_kline_closed）

    Returns:
        (all_valid, reasons_by_interval, last_close_times)
    """
    reasons: Dict[str, str] = {}
    close_times: Dict[str, int] = {}
    all_valid = True

    for interval in intervals:
        klines = multi_klines.get(interval, [])
        valid, reason, last_ct = _validate_kline_closed(klines, interval, now_ms)
        if valid:
            close_times[interval] = last_ct
        else:
            all_valid = False
            reasons[interval] = reason
            if last_ct is not None:
                close_times[interval] = last_ct

    return all_valid, reasons, close_times


# ---------------------------------------------------------------------------
# Git commit / SHA 读取
# ---------------------------------------------------------------------------
def _read_git_info() -> Tuple[str, str]:
    """尝试从容器 /app/VERSION 或环境变量读取 git 信息

    读不到则返回空串（采集器仍可运行，decision event 会落 git_commit=""）。
    """
    git_commit = os.getenv("GIT_COMMIT", "")
    git_sha = os.getenv("GIT_SHA", "")

    # 尝试读容器 VERSION 文件
    version_paths = ["/app/VERSION", "VERSION"]
    for path in version_paths:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    content = f.read()
                for line in content.splitlines():
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip()
                    if key == "GIT_COMMIT" and not git_commit:
                        git_commit = val
                    elif key == "GIT_SHA" and not git_sha:
                        git_sha = val
            except Exception as exc:
                logger.debug("VERSION 文件读取失败，降级走环境变量", path=path, error=str(exc))
            break

    return git_commit, git_sha


# ---------------------------------------------------------------------------
# 影子 profile → 策略类 显式工厂映射（fail-closed，禁止静默回退原版）
# ---------------------------------------------------------------------------
# 两个 profile 对应两个独立策略类（同名 BTCEthStrategy，位于不同包）：
#   - xau_original   → strategies.btc_eth.strategy.BTCEthStrategy
#   - xau_aggressive → strategies.btc_eth_aggressive.strategy.BTCEthStrategy
# 契约：profile_id 不在此表 → 抛 ValueError（fail-closed，绝不准许静默回退原版）。
_PROFILE_TO_STRATEGY_CLASS: Dict[str, Any] = {
    "xau_original": OriginalBTCEthStrategy,
    "xau_aggressive": AggressiveBTCEthStrategy,
}


# ---------------------------------------------------------------------------
# 影子策略实例构造（只读 + db_manager=None）
# ---------------------------------------------------------------------------
def _build_shadow_strategy(
    *,
    profile_id: str,
    profile_config: Dict[str, Any],
    binance_client: ReadOnlyBinanceClient,
    kline_service: KLineService,
    notification_client=None,   # 影子不发飞书，允许 None
) -> Any:
    """按 profile_id 工厂化构造影子策略实例

    关键安全点：
      - profile_id 必须在 _PROFILE_TO_STRATEGY_CLASS 中存在，否则抛 ValueError（fail-closed）
      - binance_client 是 ReadOnlyBinanceClient（任何写接口 fail-closed）
      - db_manager 不传（FrequencyController/CapitalManager/CircuitBreaker 均以 db_manager=None
        构造，不会触发 DB 读/写）
      - config 里 instance.execution_mode=shadow_batch → parse_instance_config 自动强制
        real_ordering_enabled=False + state_reporting_enabled=False（双开关）

    Args:
        profile_id: profile 标识（xau_original / xau_aggressive）
        profile_config: 冻结 YAML 配置
        binance_client: ReadOnlyBinanceClient 实例
        kline_service: KLineService 实例
        notification_client: 通知客户端（允许 None）

    Returns:
        构造完成的策略实例（影子安全态）
    """
    # —— 工厂映射：profile_id → 策略类（fail-closed，缺失即报错）——
    strategy_cls = _PROFILE_TO_STRATEGY_CLASS.get(profile_id)
    if strategy_cls is None:
        raise ValueError(
            f"未知 profile_id={profile_id}，无法装配策略类。"
            f"已注册: {list(_PROFILE_TO_STRATEGY_CLASS.keys())}。"
            f"请检查 config 或补充 _PROFILE_TO_STRATEGY_CLASS 映射表。"
        )

    strategy = strategy_cls(
        config=profile_config,
        binance_client=binance_client,
        kline_service=kline_service,
        notification_client=notification_client,
        db_manager=None,          # 关键：影子不传 db，规避所有 DB 副作用
    )

    # 安全断言（双重保障：构造后再校验一次）
    assert strategy.execution_mode == "shadow_batch", (
        f"影子策略执行模式异常：expected shadow_batch, got {strategy.execution_mode}"
    )
    assert not strategy.real_ordering_enabled, (
        "影子策略 real_ordering_enabled 应为 False"
    )
    assert not strategy.state_reporting_enabled, (
        "影子策略 state_reporting_enabled 应为 False"
    )

    logger.info(
        "影子策略构造完成（只读安全态）",
        profile_id=profile_id,
        strategy_name=strategy.strategy_name,
        execution_mode=strategy.execution_mode,
        real_ordering_enabled=strategy.real_ordering_enabled,
    )
    return strategy


# ---------------------------------------------------------------------------
# 采集器主类
# ---------------------------------------------------------------------------
class ShadowCollector:
    """XAUUSDT MTCPS 影子采集器

    职责：一次调度周期内对 XAUUSDT 执行原版/激进版两个 profile 的
    无状态市场决策分析，并将全量决策事件落库到 research schema。
    """

    def __init__(
        self,
        *,
        run_id: str,
        instance_id: str,
        symbol: str,
        profiles: List[str],
        profile_configs: Dict[str, Dict[str, Any]],  # {profile_id: config_dict}
        repository: ShadowRepository,
        kline_service: KLineService,
        binance_client: ReadOnlyBinanceClient,
        git_commit: str = "",
        git_sha: str = "",
        kline_intervals: List[str] | None = None,
        initial_virtual_equity_usdt: float = 300,
    ):
        self.run_id = run_id
        self.instance_id = instance_id
        self.symbol = symbol
        self.profiles = profiles
        self.profile_configs = profile_configs
        self.repo = repository
        self.kline_service = kline_service
        self.binance_client = binance_client
        self.git_commit = git_commit
        self.git_sha = git_sha
        self.kline_intervals = kline_intervals or ["1h", "4h", "1d"]
        self.initial_virtual_equity_usdt = initial_virtual_equity_usdt

        # 预计算 config_hash（每个 profile 一个）
        self.config_hashes: Dict[str, str] = {
            pid: compute_config_hash(cfg) for pid, cfg in profile_configs.items()
        }

        # 预构造策略实例：在采集器生命周期内跨调度周期复用
        # —— 目的：保留 DynamicATRFilter 内部 _atr_history deque（进程内存状态），
        #   使 min_history_count（默认 100，影子配置可能 30）能在多次 run_once 后达阈值，
        #   口径与生产策略（同一进程内跨周期累积）保持一致。
        # —— 两 profile 独立实例：OriginalBTCEthStrategy / AggressiveBTCEthStrategy 各一份，
        #   互相隔离。db_manager=None → 无任何 DB 副作用。
        # —— fail-fast：profile_id 不在 _PROFILE_TO_STRATEGY_CLASS 映射表时，
        #   ValueError 直接向上抛，应用启动立即失败。
        self._strategies: Dict[str, Any] = {}
        for pid in self.profiles:
            self._strategies[pid] = _build_shadow_strategy(
                profile_id=pid,
                profile_config=self.profile_configs[pid],
                binance_client=self.binance_client,
                kline_service=self.kline_service,
            )

    # ------------------------------------------------------------------ 调度入口
    async def run_once(self) -> Dict[str, Any]:
        """执行一次完整采集周期

        Returns:
            摘要 dict（正常/异常分支键集统一）：
              run_id: str                     # 本次 run 标识
              events_total: int               # 本次周期产出的事件总数
              events_inserted: int            # 成功落库的事件数
              data_quality: Optional[str]     # 数据质量问题描述（正常为 None，K 线服务不可用时为错误原因）
        """
        # started_at: 本次 run 的真实触发时刻（用于 shadow_runs.started_at）
        started_at = datetime.utcnow()
        # scheduled_at: 对齐到 UTC 整点小时（唯一键幂等）
        # 依据：shadow_decision_events 唯一键 = (run_id, profile_id, symbol, scheduled_at)
        # 同一调度周期内（run_id 确定）多次重试必须共用同一个 scheduled_at；
        # 对齐整点即可保证同一小时内所有调度得到相同 scheduled_at。
        # 粒度选择：采集器按 APScheduler 每 1h 触发一次，小时粒度与调度频率同频。
        scheduled_at = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        # now_ms: 当前毫秒时间戳（整条链路统一毫秒口径，来源：kline_service 返回 close_time 毫秒）
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        # ------ 1. 确保 shadow_run 存在（幂等） ------
        await self.repo.upsert_shadow_run(
            run_id=self.run_id,
            instance_id=self.instance_id,
            symbol=self.symbol,
            started_at=started_at,
            initial_virtual_equity_usdt=self.initial_virtual_equity_usdt,
            git_commit=self.git_commit,
            git_sha=self.git_sha,
            config_hashes=self.config_hashes,
        )

        # ------ 2. 拉 K 线 ------
        try:
            multi_klines = await self.kline_service.get_multi_timeframe_data(
                symbol=self.symbol,
                intervals=self.kline_intervals,
            )
        except Exception as exc:
            logger.error(
                "K 线服务异常，整周期降级为 analysis_error",
                symbol=self.symbol, error=str(exc), exc_info=True,
            )
            await self._emit_data_quality_error(
                scheduled_at=scheduled_at,
                reason=f"K线服务不可用: {exc}",
            )
            return {
                "run_id": self.run_id,
                "events_total": 0,
                "events_inserted": 0,
                "data_quality": str(exc),
            }

        # ------ 3. K 线闭合校验 ------
        all_valid, dq_reasons, close_times = _validate_all_intervals(
            multi_klines, self.kline_intervals, now_ms,
        )

        # ------ 4. 策略实例跨周期复用：__init__ 中已预构造，此处直接取用 ------
        # （DynamicATRFilter._atr_history 在实例内存里累积，不可每轮重建；重要项 5）

        # ------ 5. 两 profile 并行分析 ------
        events: List[Dict[str, Any]] = []
        if not all_valid:
            # K 线数据质量问题：整周期两 profile 均记 data_quality_reason，不产 candidate
            dq_msg = "; ".join(f"{k}: {v}" for k, v in dq_reasons.items())
            logger.warning(
                "K线闭合校验失败，两 profile 均降级",
                symbol=self.symbol, reason=dq_msg, close_times=close_times,
            )
            for pid in self.profiles:
                events.append(self._make_event(
                    profile_id=pid,
                    scheduled_at=scheduled_at,
                    evaluated_at=datetime.utcnow(),
                    decision="no_signal",     # 数据质量问题 → no_signal（非 analysis_error）
                    reason_codes=["data_quality"],
                    data_quality_reason=dq_msg,
                    source_kline_close_times=close_times,
                ))
        else:
            # 数据完整闭合：并行分析
            results = await asyncio.gather(
                *(self._analyze_profile(
                    profile_id=pid,
                    strategy=self._strategies[pid],
                    multi_klines=multi_klines,
                    close_times=close_times,
                    scheduled_at=scheduled_at,
                ) for pid in self.profiles),
                return_exceptions=True,
            )
            for pid, result in zip(self.profiles, results):
                if isinstance(result, Exception):
                    # profile 自己炸了，另一 profile 继续
                    logger.error(
                        "profile 分析异常（不影响另一 profile）",
                        profile_id=pid, error=str(result), exc_info=True,
                    )
                    events.append(self._make_event(
                        profile_id=pid,
                        scheduled_at=scheduled_at,
                        evaluated_at=datetime.utcnow(),
                        decision="analysis_error",
                        reason_codes=["internal_exception"],
                        data_quality_reason=None,
                        source_kline_close_times=close_times,
                    ))
                else:
                    events.extend(result)   # _analyze_profile 返回 List[event]

        # ------ 6. 批量落库 ------
        inserted = await self.repo.insert_decision_events_batch(events)

        logger.info(
            "影子采集周期完成",
            run_id=self.run_id,
            events_total=len(events),
            events_inserted=inserted,
        )
        return {
            "run_id": self.run_id,
            "events_total": len(events),
            "events_inserted": inserted,
            "data_quality": None,
        }

    # ------------------------------------------------------------------ 单 profile 分析
    async def _analyze_profile(
        self,
        *,
        profile_id: str,
        strategy: Union[OriginalBTCEthStrategy, AggressiveBTCEthStrategy],
        multi_klines: Dict[str, List[Dict]],
        close_times: Dict[str, int],
        scheduled_at: datetime,
    ) -> List[Dict[str, Any]]:
        """单 profile 无状态分析，返回 1 条决策事件

        只走无状态路径（_build_indicators → get_market_state → _analyze_stationary_signal）。
        不调 _check_stateful_execution_eligibility、不调 _calculate_position_size。
        """
        symbol = self.symbol
        evaluated_at = datetime.utcnow()

        # ---- 1. 计算技术指标 ----
        try:
            indicators = strategy._build_indicators(multi_klines)
        except Exception as exc:
            logger.error("指标计算异常", profile_id=profile_id, symbol=symbol, error=str(exc))
            return [self._make_event(
                profile_id=profile_id,
                scheduled_at=scheduled_at,
                evaluated_at=evaluated_at,
                decision="analysis_error",
                reason_codes=["indicator_exception"],
                source_kline_close_times=close_times,
            )]

        # ---- 2. 市场状态识别 ----
        market_state_config = strategy.risk_config.get('market_state', {})
        market_state_name = 'RANGING'
        market_behavior: Dict[str, Any] = {}
        try:
            if market_state_config.get('enabled', True) and '4h' in multi_klines:
                close_4h_list = [float(k['close']) for k in multi_klines['4h']]
                import pandas as pd
                close_4h = pd.Series(close_4h_list)
                market_state_obj, _state_desc = get_market_state(
                    indicators['4h'],
                    close_prices=close_4h,
                    indicators_1d=indicators.get('1d'),
                    config=market_state_config,
                )
                market_behavior = get_market_state_behavior(market_state_obj, market_state_config)
                market_state_name = market_state_obj.value if market_state_obj else 'RANGING'
            else:
                fallback = market_state_config.get('fallback', {})
                market_behavior = {
                    'can_trade': fallback.get('can_trade', True),
                    'min_grade': fallback.get('min_grade', 'C'),
                    'vol_boost': fallback.get('vol_boost', 0.0),
                }
        except Exception as exc:
            logger.warning(
                "市场状态识别异常，降级为 RANGING + fallback",
                profile_id=profile_id, symbol=symbol, error=str(exc),
            )
            fallback = market_state_config.get('fallback', {})
            market_behavior = {
                'can_trade': fallback.get('can_trade', True),
                'min_grade': fallback.get('min_grade', 'C'),
            }

        # 市场状态行为禁止开仓 → 直接 no_signal
        if not market_behavior.get('can_trade', True):
            return [self._make_event(
                profile_id=profile_id,
                scheduled_at=scheduled_at,
                evaluated_at=evaluated_at,
                decision="no_signal",
                reason_codes=["market_state_disabled"],
                direction=None,
                source_kline_close_times=close_times,
            )]

        # ---- 3. 无状态市场决策 ----
        try:
            signal_components, reason = await strategy._analyze_stationary_signal(
                symbol, multi_klines, indicators, market_state_name, market_behavior,
            )
        except Exception as exc:
            logger.error(
                "_analyze_stationary_signal 异常",
                profile_id=profile_id, symbol=symbol, error=str(exc), exc_info=True,
            )
            return [self._make_event(
                profile_id=profile_id,
                scheduled_at=scheduled_at,
                evaluated_at=evaluated_at,
                decision="analysis_error",
                reason_codes=["stationary_signal_exception"],
                source_kline_close_times=close_times,
            )]

        # ---- 4. 组装事件 ----
        if signal_components is None:
            # no_signal：解析 reason 得到 reason_codes
            reason_codes = _parse_no_signal_reason(reason)
            return [self._make_event(
                profile_id=profile_id,
                scheduled_at=scheduled_at,
                evaluated_at=evaluated_at,
                decision="no_signal",
                reason_codes=reason_codes,
                direction=None,
                grade=None,
                score=None,
                source_kline_close_times=close_times,
            )]

        # candidate
        return [self._make_event_from_signal(
            profile_id=profile_id,
            scheduled_at=scheduled_at,
            evaluated_at=evaluated_at,
            signal_components=signal_components,
            close_times=close_times,
        )]

    # ------------------------------------------------------------------ 事件组装辅助
    def _make_event(
        self,
        *,
        profile_id: str,
        scheduled_at: datetime,
        evaluated_at: datetime,
        decision: str,
        reason_codes: List[str] | None = None,
        data_quality_reason: str | None = None,
        direction: str | None = None,
        grade: str | None = None,
        score: float | None = None,
        source_kline_close_times: Dict[str, int] | None = None,
        reference_price: float | None = None,
    ) -> Dict[str, Any]:
        """组装一条 shadow_decision_events 记录（research schema 契约）"""
        return {
            "run_id": self.run_id,
            "profile_id": profile_id,
            "symbol": self.symbol,
            "scheduled_at": scheduled_at,
            "evaluated_at": evaluated_at,
            "decision": decision,
            "direction": direction,
            "grade": grade,
            "score": score,
            "reference_price": reference_price,
            "intended_order_type": None,
            "intended_entry_price": None,
            "stop_loss_rule": None,
            "tp1_rule": None,
            "tp2_rule": None,
            "trailing_rule": None,
            "time_stop_rule": None,
            "feature_snapshot": None,
            "reason_codes": reason_codes or [],
            "data_quality_reason": data_quality_reason,
            "config_hash": self.config_hashes.get(profile_id, ""),
            "git_commit": self.git_commit,
            "source_kline_close_times": source_kline_close_times or {},
        }

    def _make_event_from_signal(
        self,
        *,
        profile_id: str,
        scheduled_at: datetime,
        evaluated_at: datetime,
        signal_components: Dict[str, Any],
        close_times: Dict[str, int],
    ) -> Dict[str, Any]:
        """从无状态 signal_components 组装 candidate 事件

        signal_components 字段：direction/grade/score/entry_price/initial_stop_loss/
        tp1_price/tp2_price/atr/market_state/price_change_1h
        """
        entry_price = signal_components.get("entry_price")
        stop_loss = signal_components.get("initial_stop_loss")
        tp1 = signal_components.get("tp1_price")
        tp2 = signal_components.get("tp2_price")

        atr = signal_components.get("atr")
        reference_price = float(entry_price) if entry_price is not None else None

        # 规则来源（简化：只存等级和 ATR 倍率供回放器参考）
        grade = signal_components.get("grade")
        risk_cfg = (
            self.profile_configs[profile_id]
            .get("strategy", {})
            .get("risk", {})
            .get("signal_levels", {})
            .get(grade, {})
        )
        sl_mult = risk_cfg.get("stop_loss_atr_multiplier") if atr else None
        tp1_mult = risk_cfg.get("partial_take_profit", {}).get("tp1_atr_multiplier") if atr else None
        tp2_mult = risk_cfg.get("partial_take_profit", {}).get("tp2_atr_multiplier") if atr else None

        return {
            "run_id": self.run_id,
            "profile_id": profile_id,
            "symbol": self.symbol,
            "scheduled_at": scheduled_at,
            "evaluated_at": evaluated_at,
            "decision": "candidate",
            "direction": signal_components.get("direction"),
            "grade": grade,
            "score": signal_components.get("score"),
            "reference_price": reference_price,
            "intended_order_type": "LIMIT",
            "intended_entry_price": float(entry_price) if entry_price is not None else None,
            "stop_loss_rule": _rule_dict("stop_loss", atr, sl_mult),
            "tp1_rule": _rule_dict("tp1", atr, tp1_mult),
            "tp2_rule": _rule_dict("tp2", atr, tp2_mult),
            "trailing_rule": risk_cfg.get("dynamic_trailing"),
            "time_stop_rule": risk_cfg.get("time_stop"),
            "feature_snapshot": {
                "market_state": signal_components.get("market_state"),
                "atr": float(atr) if atr is not None else None,
                "price_change_1h": float(signal_components.get("price_change_1h", 0)),
            },
            "reason_codes": [],
            "data_quality_reason": None,
            "config_hash": self.config_hashes.get(profile_id, ""),
            "git_commit": self.git_commit,
            "source_kline_close_times": close_times,
        }

    async def _emit_data_quality_error(
        self, *, scheduled_at: datetime, reason: str
    ) -> None:
        """K 线服务整体不可用时，为两 profile 各落一条 no_signal + data_quality_reason"""
        events = [
            self._make_event(
                profile_id=pid,
                scheduled_at=scheduled_at,
                evaluated_at=datetime.utcnow(),
                decision="no_signal",
                reason_codes=["data_quality"],
                data_quality_reason=reason,
            )
            for pid in self.profiles
        ]
        await self.repo.insert_decision_events_batch(events)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _rule_dict(kind: str, atr: Any, multiplier: Any) -> Optional[Dict[str, Any]]:
    """构建简化的规则来源字典（序列化后存入 rule 列）"""
    if atr is None or multiplier is None:
        return None
    atr_val = float(atr) if isinstance(atr, Decimal) else atr
    return {
        "kind": kind,
        "atr_multiplier": multiplier,
        "atr_value": atr_val,
    }


def _parse_no_signal_reason(reason: str) -> List[str]:
    """将 no_signal 的自然语言 reason 解析为结构化 reason_codes 列表"""
    if not reason:
        return ["unknown"]
    reason_lower = reason.lower()
    codes: List[str] = []
    mappings = [
        ("评分", "grade_filter"),
        ("score", "grade_filter"),
        ("最低评分", "grade_filter"),
        ("grade", "grade_filter"),
        ("趋势过滤", "trend_filter"),
        ("禁止入场", "prohibition"),
        ("ATR过滤", "atr_filter"),
        ("atr过滤", "atr_filter"),
        ("成交量过滤", "volume_filter"),
        ("市场状态", "market_state_grade"),
        ("震荡入场", "ranging_entry"),
        ("震荡市ADX", "ranging_adx"),
        ("过热降级", "overheat_downgrade"),
        ("最低允许等级", "grade_filter"),
    ]
    for keyword, code in mappings:
        if keyword in reason_lower or keyword in reason:
            if code not in codes:
                codes.append(code)
    if not codes:
        codes.append("no_signal_reason_not_parsed")
    return codes
