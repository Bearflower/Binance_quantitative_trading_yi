"""
事件驱动回放引擎。

输入：
  - manifest dict（run manifest）
  - decision events JSON Lines 文件（已由采集器冻结，不重算）
  - 1m 快照 manifest + K 线文件 + funding rate 文件 + exchangeInfo
  - profile 冻结配置（xau_original.yaml / xau_aggressive.yaml）
  - replay config.yaml（费用/滑点/pilot 参数）

输出：
  - per-profile 交易明细列表（replay_trades）
  - per-profile 指标（由 report.py 消费）

全程 Decimal、稳定排序、确定性 replay_run_id。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple

import yaml

from shared.config_loader import compute_config_hash

from . import ENGINE_VERSION
from .matching import (
    round_price, round_quantity, clamp_notional,
    market_fill_price, parse_symbol_filters, find_first_crossing_index,
)
from .fees import (
    taker_fee_notional, slippage_cost,
    calculate_pnl, sum_funding_fee,
)
from .state import VirtualState
from .counterfactual import scan_triggers
# run_all_invariants 已在批次三 B 中停用——入场处误用会触发 check_no_double_close 伪违规，
# 平仓处（_execute_exit 剩余分支 / _partial_close）已改为显式调用
# check_no_double_close / check_quantity_in_range

logger = logging.getLogger(__name__)


# UTC 时区（Binance API / 回放快照都用 UTC）
UTC = timezone.utc


def _parse_ts(raw: Any) -> datetime:
    """把 JSON Lines 里的各种时间格式统一成 UTC aware datetime。
    支持 ISO string（带或不带 Z）、Unix ms / s（整数）。
    """
    if raw is None:
        raise ValueError("时间字段为 None")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, (int, float)):
        # > 10^12 → 毫秒
        v = int(raw)
        if v > 1_000_000_000_000:
            return datetime.fromtimestamp(v / 1000, tz=UTC)
        return datetime.fromtimestamp(v, tz=UTC)
    s = str(raw)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # 去掉微秒，再试
        dt = datetime.strptime(s.split(".")[0], "%Y-%m-%dT%H:%M:%S")
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def stable_sort_key(event: Dict[str, Any]) -> Tuple:
    """稳定排序：scheduled_at → profile_id → symbol。"""
    scheduled_at = _parse_ts(event.get("scheduled_at") or event.get("evaluated_at"))
    return (
        scheduled_at,
        str(event.get("profile_id", "")),
        str(event.get("symbol", "")),
    )


def compute_replay_run_id(manifest_sha256: str, engine_version: str, input_cutoff_at: datetime) -> str:
    """确定性派生 replay_run_id。"""
    h = hashlib.sha256()
    h.update(manifest_sha256.encode("utf-8"))
    h.update(engine_version.encode("utf-8"))
    h.update(input_cutoff_at.isoformat().encode("utf-8"))
    return h.hexdigest()[:16]


class ReplayEngine:
    """本地确定性回放引擎。"""

    def __init__(
        self,
        replay_config_path: str,
        profile_config_path_original: str,
        profile_config_path_aggressive: str,
        # snapshot 相关（文件路径）
        klines_1m_path: str,
        funding_rates_path: str,
        exchange_info_json_path: str,
        snapshot_manifest_path: str,
        # run manifest（dict 或文件路径）
        run_manifest: Dict[str, Any],
        engine_version: str = ENGINE_VERSION,
    ):
        # -------- 加载 config --------
        with open(replay_config_path, "r", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)
        with open(profile_config_path_original, "r", encoding="utf-8") as f:
            self.profile_cfg_orig = yaml.safe_load(f)
        with open(profile_config_path_aggressive, "r", encoding="utf-8") as f:
            self.profile_cfg_aggr = yaml.safe_load(f)

        self.run_manifest = run_manifest
        self.engine_version = engine_version

        # -------- profile 配置哈希校验（fail-closed） --------
        # run_manifest 必须提供 config_hashes，且必须包含 xau_original / xau_aggressive
        # 两个 profile 的哈希值，否则拒绝启动（避免配置漂移导致回放结果与生产不一致）
        manifest_hashes = self.run_manifest.get("config_hashes")
        if not manifest_hashes or not isinstance(manifest_hashes, dict):
            raise ValueError(
                "run_manifest 缺少 config_hashes 键（profile 配置哈希校验未通过）"
            )
        for pid, cfg in (
            ("xau_original", self.profile_cfg_orig),
            ("xau_aggressive", self.profile_cfg_aggr),
        ):
            expected = manifest_hashes.get(pid)
            if not expected:
                raise ValueError(
                    f"run_manifest.config_hashes 缺少 pid={pid}（profile 配置哈希校验未通过）"
                )
            actual = compute_config_hash(cfg)
            if actual != expected:
                raise ValueError(
                    f"profile 配置哈希不匹配 pid={pid} "
                    f"expected={expected[:16]}... actual={actual[:16]}... "
                    f"请检查是否使用了正确的冻结 profile 配置"
                )

        # -------- 加载快照 --------
        with open(klines_1m_path, "r", encoding="utf-8") as f:
            self.klines_1m = self._load_klines_json(f.read())
        with open(funding_rates_path, "r", encoding="utf-8") as f:
            self.funding_events = self._load_funding_json(f.read())
        with open(exchange_info_json_path, "r", encoding="utf-8") as f:
            ei = json.load(f)

        with open(snapshot_manifest_path, "r", encoding="utf-8") as f:
            self.snapshot_manifest = json.load(f)
        self.manifest_sha256 = self.snapshot_manifest.get("sha256", "")
        manifest_symbol = self.snapshot_manifest.get("symbol")
        # 耦合 #4b：按 manifest["symbol"] 精确查找 exchangeInfo 条目，
        # 不再默认取 symbols[0]——可能拿到别的合约的 tickSize/stepSize/minNotional。
        symbol_entry = self._find_symbol_entry(ei, manifest_symbol)
        self.symbol_filters = parse_symbol_filters(symbol_entry)
        self.tick_size = self.symbol_filters["tickSize"]
        self.step_size = self.symbol_filters["stepSize"]
        self.min_notional = self.symbol_filters["minNotional"]

        # pilot 约束（从各自 profile 冻结配置读取——跨 profile 独立，禁止互相泄漏）
        self.pilot_cfg: Dict[str, Dict[str, Any]] = {
            "xau_original": self.profile_cfg_orig.get("pilot"),
            "xau_aggressive": self.profile_cfg_aggr.get("pilot"),
        }
        # fail-closed：每个 profile 必须有 pilot 段
        for _pid, _pilot_cfg in self.pilot_cfg.items():
            if _pilot_cfg is None:
                raise KeyError(
                    f"profile {_pid} 缺少 pilot 段（仓位约束配置缺失）"
                )
        # per-profile 的 max_margin / max_lev（禁止跨 profile 共用同一值）
        self._profile_max_margin: Dict[str, Decimal] = {
            pid: Decimal(str(cfg["max_single_position_margin_usdt"]))
            for pid, cfg in self.pilot_cfg.items()
        }
        self._profile_max_lev: Dict[str, int] = {
            pid: int(cfg["max_leverage"])
            for pid, cfg in self.pilot_cfg.items()
        }

        # 费用配置
        self.taker_bps = Decimal(str(self.cfg["fees"]["taker_bps"]))
        self.slippage_baseline = Decimal(str(self.cfg["slippage"]["baseline_bps"]))
        self.limit_entry_no_extra_slip = bool(
            self.cfg["slippage"].get("limit_entry_no_extra_slippage", True)
        )
        self.limit_expiry_seconds = int(self.cfg["entry"]["limit_order_expiry_seconds"])

        # 反事实停牌标记的亏损红线（单一真源：config.yaml counterfactual.halt_lines_pct）
        cf_cfg = self.cfg.get("counterfactual") or {}
        if "halt_lines_pct" not in cf_cfg:
            raise KeyError(
                "config.yaml 缺少 counterfactual.halt_lines_pct 键"
            )
        self.halt_lines_pct: List[int] = list(cf_cfg["halt_lines_pct"])

        # 阻断 #3：trailing 波动率调节因子（从 config 读取，禁止硬编码 1.0）
        # 快照仅 30 天 1m，无法确定性重算生产所需约 44 天日线 ATR 中位数，
        # 本期固定中性值 1.0（与生产 fallback 一致）；未来扩展后可改为动态计算
        trailing_cfg_yaml = self.cfg.get("trailing", {}) or {}
        self.vol_adj_override = Decimal(str(
            trailing_cfg_yaml.get("volatility_adjustment_override", "1.0")
        ))

        # 阻断 #5：per-profile 开仓序列计数器（position_id 确定性派生用）
        self._open_seq: Dict[str, int] = {
            "xau_original": 0,
            "xau_aggressive": 0,
        }

        # 两个 profile 的状态（独立）
        initial_equity = Decimal(str(
            self.run_manifest.get("initial_virtual_equity_usdt")
            or self.profile_cfg_orig.get("instance", {}).get("initial_virtual_equity_usdt", 300)
        ))
        self.states: Dict[str, VirtualState] = {
            "xau_original": self._make_state(self.profile_cfg_orig, initial_equity),
            "xau_aggressive": self._make_state(self.profile_cfg_aggr, initial_equity),
        }

        # 交易明细（per-profile 列表）
        self.replay_trades: Dict[str, List[Dict[str, Any]]] = {
            "xau_original": [],
            "xau_aggressive": [],
        }

        # 未成交订单跟踪（per-profile）
        self.pending_limit_orders: Dict[str, List[Dict[str, Any]]] = {
            "xau_original": [],
            "xau_aggressive": [],
        }

        # 事件源 K 线时间戳 → 索引映射（快速查找）
        self._kline_index_by_open_time = {k["open_time"]: i for i, k in enumerate(self.klines_1m)}
        # 排序好的 1m K 线
        self.klines_1m_sorted = sorted(self.klines_1m, key=lambda k: k["open_time"])

        # 事件列表（跑 run() 前由外部设置 events ）
        self.events: List[Dict[str, Any]] = []

        # 不变量违规记录
        self.invariant_violations: List[Dict[str, Any]] = []

    # ---------------- 构造辅助 ----------------

    @staticmethod
    def _load_klines_json(text: str) -> List[Dict[str, Any]]:
        """快照的 1m K 线可以是：每行一条 JSON Lines，或单个 JSON array。"""
        text = text.strip()
        if text.startswith("["):
            return json.loads(text)
        result = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            result.append(json.loads(line))
        return result

    @staticmethod
    def _load_funding_json(text: str) -> List[Tuple[datetime, Decimal]]:
        """历史 funding rate：每行或数组，元素形如
        {fundingTime: 1700000000000, fundingRate: "0.0001"}。"""
        text = text.strip()
        if text.startswith("["):
            arr = json.loads(text)
        else:
            arr = []
            for line in text.splitlines():
                line = line.strip()
                if line:
                    arr.append(json.loads(line))
        out = []
        for item in arr:
            t = _parse_ts(item.get("fundingTime") or item.get("funding_time"))
            rate = Decimal(str(item.get("fundingRate") or item.get("funding_rate", "0")))
            out.append((t, rate))
        out.sort(key=lambda x: x[0])
        return out

    def _make_state(self, profile_cfg: Dict[str, Any], initial_equity: Decimal) -> VirtualState:
        pilot = profile_cfg.get("pilot", {})
        # frequency_control 可能在 risk 下或顶级；先通用查找
        freq = self._deep_get(profile_cfg, "strategy.risk.frequency_control", {})
        if not freq:
            freq = self._deep_get(profile_cfg, "strategy.risk.position_sizing", {})
        return VirtualState(
            max_single_position_margin_usdt=Decimal(str(pilot.get("max_single_position_margin_usdt", 30))),
            max_concurrent_positions=int(pilot.get("max_concurrent_positions", 1)),
            max_leverage=int(pilot.get("max_leverage", 3)),
            initial_equity=initial_equity,
            equity=initial_equity,
            daily_trade_count={},
            max_daily_trades=int(freq.get("max_daily_symbol_trades", 1)),
            symbol_cooldown_hours=int(freq.get("symbol_cooldown_hours", 72)),
            consecutive_loss_pause_threshold=int(freq.get("consecutive_loss_pause", 2)),
            pause_duration_hours=int(freq.get("pause_duration_hours", 24)),
        )

    @staticmethod
    def _deep_get(d: Dict[str, Any], dotted_path: str, default=None):
        cur = d
        for key in dotted_path.split("."):
            if not isinstance(cur, dict):
                return default
            cur = cur.get(key)
            if cur is None:
                return default
        return cur

    @staticmethod
    def _find_symbol_entry(exchange_info: Dict[str, Any], manifest_symbol: Optional[str]) -> Dict[str, Any]:
        """按 manifest['symbol'] 在 exchangeInfo['symbols'] 中查找精确匹配项。

        耦合 #4b：此前直接取 symbols[0]，可能拿到别的合约的 tickSize/stepSize/minNotional。
        现在必须按 manifest 里声明的 symbol 精确匹配，取不到 → fail-closed 抛 ValueError，
        避免静默用错交易规则。
        """
        symbols = exchange_info.get("symbols", [])
        if not manifest_symbol:
            raise ValueError("snapshot manifest 缺少 symbol 字段，无法精确匹配 exchangeInfo")
        for s in symbols:
            if s.get("symbol") == manifest_symbol:
                # 校验关键字段存在且 > 0
                filters = {f["filterType"]: f for f in s.get("filters", [])}
                pf = filters.get("PRICE_FILTER", {})
                lot = filters.get("LOT_SIZE", {})
                min_notional_filter = filters.get("MIN_NOTIONAL", {})
                tick = Decimal(str(pf.get("tickSize", "0")))
                step = Decimal(str(lot.get("stepSize", "0")))
                min_not = Decimal(str(min_notional_filter.get("minNotional", "0")))
                if tick <= 0 or step <= 0 or min_not <= 0:
                    raise ValueError(
                        f"exchangeInfo symbol={manifest_symbol} 的 tickSize/stepSize/minNotional "
                        f"存在非正值：tickSize={tick}, stepSize={step}, minNotional={min_not}"
                    )
                return s
        raise ValueError(
            f"exchangeInfo 中找不到 manifest 声明的 symbol='{manifest_symbol}'，"
            f"可用 symbols: {[s.get('symbol') for s in symbols]}"
        )

    # ---------------- 批次三 C：决策事件汇总 ----------------

    def _build_decision_summary(self) -> Dict[str, Any]:
        """从 self.events 计算 per-profile 决策统计 + 原版/激进版同点决策差异。

        全程确定性：输入 self.events 已按 stable_sort_key 稳定排序，
        所有聚合用 dict 保证顺序、samples 列表按 scheduled_at 排序。
        """
        PER_PROFILES = ["xau_original", "xau_aggressive"]

        # per-profile 基础计数
        per_profile: Dict[str, Dict[str, Any]] = {}
        for pid in PER_PROFILES:
            per_profile[pid] = {
                "scheduled_runs": 0,
                "candidate": 0,
                "no_signal": 0,
                "analysis_error": 0,
                "no_signal_reasons": {},  # reason_code -> count
                "events_by_ts": {},       # scheduled_at iso -> event dict（给 diff 用）
            }

        for ev in self.events:
            pid = str(ev.get("profile_id", ""))
            if pid not in per_profile:
                continue
            scheduled_at = _parse_ts(ev.get("scheduled_at"))
            ts_iso = scheduled_at.isoformat()
            decision = ev.get("decision") or "no_signal"

            per_profile[pid]["scheduled_runs"] += 1
            if decision == "candidate":
                per_profile[pid]["candidate"] += 1
            elif decision == "analysis_error":
                per_profile[pid]["analysis_error"] += 1
            else:
                per_profile[pid]["no_signal"] += 1
                # 收集 no_signal 的 reason_codes
                reason_codes = ev.get("reason_codes") or ev.get("reason_code") or []
                if isinstance(reason_codes, str):
                    reason_codes = [reason_codes]
                for rc in reason_codes:
                    key = str(rc)
                    per_profile[pid]["no_signal_reasons"][key] = \
                        per_profile[pid]["no_signal_reasons"].get(key, 0) + 1

            # 按 scheduled_at 存一份用于跨 profile diff
            per_profile[pid]["events_by_ts"][ts_iso] = {
                "decision": decision,
                "direction": ev.get("direction"),
                "grade": ev.get("grade"),
            }

        # 算 analysis_success_rate，整理输出
        per_profile_out: Dict[str, Any] = {}
        for pid in PER_PROFILES:
            d = per_profile[pid]
            total = d["scheduled_runs"]
            if total > 0:
                success_rate = (total - d["analysis_error"]) / total
                computable = True
                sample_note = None
            else:
                # 无调度事件，分母=0 → 不再用 1.0 冒充成功率（阻断修复）
                success_rate = None
                computable = False
                sample_note = "no_scheduled_events"
            per_profile_out[pid] = {
                "scheduled_runs": total,
                "candidate": d["candidate"],
                "no_signal": d["no_signal"],
                "analysis_error": d["analysis_error"],
                "analysis_success_rate": success_rate,
                "analysis_success_rate_computable": computable,
                "analysis_sample_note": sample_note,
                "no_signal_reasons": d["no_signal_reasons"],
            }

        # —— same_timestamp_decision_diff ——
        orig_ts_map = per_profile["xau_original"]["events_by_ts"]
        aggr_ts_map = per_profile["xau_aggressive"]["events_by_ts"]
        common_ts = sorted(set(orig_ts_map.keys()) & set(aggr_ts_map.keys()))
        common_count = len(common_ts)
        differing_samples: List[Dict[str, Any]] = []
        differing_count = 0
        for ts in common_ts:
            orig_ev = orig_ts_map[ts]
            aggr_ev = aggr_ts_map[ts]
            # (decision, direction, grade) 三元组任一不同即视为差异
            triplet_diff = (
                orig_ev.get("decision") != aggr_ev.get("decision")
                or orig_ev.get("direction") != aggr_ev.get("direction")
                or orig_ev.get("grade") != aggr_ev.get("grade")
            )
            if triplet_diff:
                differing_count += 1
                if len(differing_samples) < 20:
                    differing_samples.append({
                        "scheduled_at": ts,
                        "original": orig_ev,
                        "aggressive": aggr_ev,
                    })

        differing_ratio = (differing_count / common_count) if common_count > 0 else 0.0

        return {
            "per_profile": per_profile_out,
            "same_timestamp_decision_diff": {
                "common_scheduled_count": common_count,
                "differing_count": differing_count,
                "differing_ratio": differing_ratio,
                "samples": differing_samples,
            },
        }


    # ---------------- 事件加载与准备 ----------------

    def load_events(self, decision_events_path: str) -> None:
        """读取 JSON Lines 事件文件并稳定排序。"""
        events: List[Dict[str, Any]] = []
        with open(decision_events_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                events.append(json.loads(line))
        self.events = sorted(events, key=stable_sort_key)
        logger.info("事件加载完成，共 %d 条", len(self.events))

    def run(self) -> Dict[str, Any]:
        """按时间顺序推进，处理每个决策事件。

        返回 {
            "xau_original": {...profile_result...},
            "xau_aggressive": {...profile_result...},
            "engine_metadata": {...},
        }
        """
        if not self.events:
            raise RuntimeError("尚未 load_events")

        # 数据截止时间：以 run_manifest 为准；若缺失则取最后一个 scheduled_at
        cutoff_ts = self.run_manifest.get("data_cutoff_at")
        if cutoff_ts is None:
            cutoff_ts = self.events[-1]["scheduled_at"] if self.events else datetime.now(UTC)
        cutoff_at = _parse_ts(cutoff_ts) if not isinstance(cutoff_ts, datetime) else cutoff_ts
        self.input_cutoff_at = cutoff_at

        replay_run_id = compute_replay_run_id(self.manifest_sha256, self.engine_version, cutoff_at)

        # K 线窗口按 cutoff 截断
        cutoff_idx = None
        for i, k in enumerate(self.klines_1m_sorted):
            t = _parse_ts(k["open_time"])
            if t >= cutoff_at:
                cutoff_idx = i
                break
        if cutoff_idx is None:
            cutoff_idx = len(self.klines_1m_sorted)
        usable_klines = self.klines_1m_sorted[:cutoff_idx]

        logger.info(
            "开始回放 replay_run_id=%s events=%d klines_1m=%d cutoff=%s",
            replay_run_id, len(self.events), len(usable_klines), cutoff_at.isoformat(),
        )

        for event in self.events:
            self._process_one_event(event, usable_klines, cutoff_at)

        # 期终兜底（阻断 #3 修复）：
        # 事件循环只处理到最后一个 scheduled_at，之后 cutoff 到之间
        # 可能还有挂单可撮合、持仓可跑退出规则；必须在强平前补齐。
        # 顺序：先结算挂单 → 再跑持仓退出到 cutoff → 最后强平
        # （使被结算挂单新开的仓位也能被纳入退出扫描与最终强平；
        #   _run_position_exit_up_to 内置游标机制，不会与事件循环已扫描区间重复）
        for pid in ("xau_original", "xau_aggressive"):
            self._settle_pending_limit_orders(pid, cutoff_at, usable_klines)
            self._run_position_exit_up_to(pid, cutoff_at, usable_klines, cutoff_at)

        # 期终强制标记：把所有仍开着的仓位按最后一根 K 线 close 平掉
        self._force_close_remaining_positions(usable_klines, cutoff_at)

        # Round4 修复（P2）：截止时仍未到期的 pending 挂单补记录。
        # _settle_pending_limit_orders 刚把过期的（cutoff_at >= expiry_at）记为 expired_unfilled
        # 并从 pending_limit_orders 中移除；剩下的全是 cutoff_at < expiry_at 仍未过期的，
        # 它们既没撮合也没进入结果——这里补记 reason="pending_at_cutoff"，避免报告漏样本。
        pending_at_cutoff_count = 0
        for pid in ("xau_original", "xau_aggressive"):
            still_pending = self.pending_limit_orders[pid]
            for order in still_pending:
                opened_at = _parse_ts(order["opened_at"])
                expiry_at = opened_at + timedelta(seconds=self.limit_expiry_seconds)
                if cutoff_at < expiry_at:
                    self._record_unfilled(pid, order, reason="pending_at_cutoff")
                    pending_at_cutoff_count += 1
            self.pending_limit_orders[pid] = []  # 清空，本次 run 结束无后续撮合

        # 批次三 C：从 self.events 计算 decision_summary（全程确定性，按 scheduled_at 稳定排序）
        decision_summary = self._build_decision_summary()

        # 生成 per-profile 结果
        results: Dict[str, Any] = {}
        for pid, state in self.states.items():
            ct = scan_triggers(
                state.equity_history, state.initial_equity,
                halt_lines_pct=self.halt_lines_pct,
            )
            results[pid] = {
                "trades": self.replay_trades[pid],
                "state": {
                    "final_equity": str(state.equity),
                    "realized_pnl": str(state.realized_pnl),
                    "fee_paid": str(state.fee_paid),
                    "funding_fee": str(state.funding_fee),
                    "slippage_cost": str(state.slippage_cost),
                },
                "counterfactual": ct.as_dict(),
                # 阻断 #4：equity_history 已升级为三元组 (at, marked_equity, has_open_position)
                "equity_history": [
                    (t.isoformat(), str(e), bool(h)) for t, e, h in state.equity_history
                ],
            }

        return {
            "engine_metadata": {
                "replay_run_id": replay_run_id,
                "engine_version": self.engine_version,
                "manifest_sha256": self.manifest_sha256,
                "input_cutoff_at": cutoff_at.isoformat(),
                "initial_equity": str(next(iter(self.states.values())).initial_equity),
                "symbol_filters": {
                    "tickSize": str(self.tick_size),
                    "stepSize": str(self.step_size),
                    "minNotional": str(self.min_notional),
                },
                "invariant_violations": self.invariant_violations,
                # 阻断 #4：快照缺口数量（报告 data_completeness 字段的上游来源）
                "snapshot_gaps": len(self.snapshot_manifest.get("gaps", [])),
                # 阻断 #3：trailing vol_adj 降级标注（报告读者知道 trailing 未做波动率调整）
                "trailing": {
                    "vol_adj_fallback_used": True,
                    "vol_adj_value": str(self.vol_adj_override),
                    "trailing_flat_close": True,   # 全平口径（§11 已决策）
                },
                # 批次三 C：决策事件汇总（消费 §9 报告指标）
                "decision_summary": decision_summary,
                # Round4 修复（P2）：截止时仍未过期的 pending 挂单数量（两 profile 合计）
                "pending_at_cutoff_count": pending_at_cutoff_count,
            },
            "xau_original": results["xau_original"],
            "xau_aggressive": results["xau_aggressive"],
        }

    # ---------------- 单个事件处理 ----------------

    def _process_one_event(self, event: Dict[str, Any], klines: List[Dict[str, Any]], cutoff_at: datetime) -> None:
        """处理一条决策事件：
          - 先处理所有 profile 的 pending_limit_orders（是否在该事件时间点之前已触发 / 过期）
          - 再按 profile 分别处理 candidate / no_signal / analysis_error
          - 最后让已持仓者检查当前这根 K 线的止损/止盈/移动止损/时间止损
        """
        scheduled_at = _parse_ts(event["scheduled_at"])
        profile_id = event["profile_id"]

        # 先处理该 profile 的 limit order：在 [上次处理时间, scheduled_at) 区间内撮合
        self._settle_pending_limit_orders(profile_id, scheduled_at, klines)

        # 已持仓的 profile：先让持仓在到达 scheduled_at 前的每根 K 线上跑退出规则
        self._run_position_exit_up_to(profile_id, scheduled_at, klines, cutoff_at)

        # 根据 decision 类型决定是否尝试入场
        decision = event.get("decision")
        if decision != "candidate":
            return

        # 撮合入场
        self._attempt_entry(profile_id, event, scheduled_at, klines)

    # ---------------- limit order 撮合 ----------------

    def _settle_pending_limit_orders(self, profile_id: str, up_to: datetime, klines: List[Dict[str, Any]]) -> None:
        """把 profile 内所有未过期的限价单在 [opened_at, up_to) 区间内撮合。
        过期的（opened_at + limit_expiry_seconds < up_to）标记为 expired_unfilled。
        """
        still_pending: List[Dict[str, Any]] = []
        for order in self.pending_limit_orders[profile_id]:
            opened_at = _parse_ts(order["opened_at"])
            expiry_at = opened_at + timedelta(seconds=self.limit_expiry_seconds)

            # 窗口结束 = min(expiry_at, up_to)
            search_end = min(expiry_at, up_to)
            if search_end <= opened_at:
                # 窗口为空：up_to <= opened_at（min 取到 up_to）
                if up_to >= expiry_at:
                    self._record_unfilled(profile_id, order, reason="expired_unfilled")
                else:
                    still_pending.append(order)
                continue

            # 找撮合窗口里的 K 线
            idx_start = self._find_kline_idx_after(klines, opened_at)
            idx_end = self._find_kline_idx_after(klines, search_end)
            idx_end = idx_end if idx_end is not None else len(klines)

            if idx_start is None or idx_start >= idx_end:
                # 窗口内无有效 K 线
                if up_to >= expiry_at:
                    self._record_unfilled(profile_id, order, reason="expired_unfilled")
                else:
                    still_pending.append(order)
                continue

            cross_idx = find_first_crossing_index(
                klines_1m=klines,
                limit_price=order["limit_price"],
                direction=order["direction"],
                search_start_idx=idx_start,
                search_end_idx=idx_end,
            )
            if cross_idx is not None:
                # 撮合成功
                cross_kline = klines[cross_idx]
                self._fill_entry(
                    profile_id=profile_id,
                    event=order["source_event"],
                    opened_at=opened_at,
                    fill_at=_parse_ts(cross_kline["open_time"]),
                    fill_price=order["limit_price"],  # 限价按 limit 价格成交，不加滑点
                    quantity=order["quantity"],
                    margin=order["margin"],
                    leverage=order["leverage"],
                    stop_loss=order.get("stop_loss"),
                    grade=order.get("grade"),
                    order_kind="LIMIT",
                    tp1=order.get("tp1"),
                    tp1_close_ratio=order.get("tp1_close_ratio"),
                    tp2=order.get("tp2"),
                    tp2_close_ratio=order.get("tp2_close_ratio"),
                    trailing=order.get("trailing"),
                    time_stop=order.get("time_stop"),
                    # 阻断 #3 / #5：从 order_record 读（_attempt_entry 生成后透传至此）
                    atr=order.get("atr"),
                    stop_loss_atr_multiplier=order.get("stop_loss_atr_multiplier"),
                    position_id=order.get("position_id"),
                )
            else:
                if up_to >= expiry_at:
                    self._record_unfilled(profile_id, order, reason="expired_unfilled")
                else:
                    still_pending.append(order)

        self.pending_limit_orders[profile_id] = still_pending

    def _record_unfilled(self, profile_id: str, order: Dict[str, Any], reason: str) -> None:
        """未成交的限价单，直接记一条"入场状态=未成交"的 trade，方便报告统计。"""
        self.replay_trades[profile_id].append({
            # 阻断 #5：未成交明细也带 position_id（报告聚合时 position_id is None 的不算）
            "position_id": order.get("position_id"),
            "entry_time": _parse_ts(order["opened_at"]).isoformat(),
            "entry_price": str(order["limit_price"]),
            "direction": order["direction"],
            "symbol": order.get("symbol", ""),
            "filled": False,
            "unfilled_reason": reason,
            "exit_reason": None,
            "net_pnl": "0",
            "fee": "0",
            "funding_fee": "0",
            "slippage": "0",
            "quantity": "0",
            "margin": str(order.get("margin", "0")),
            "leverage": str(order.get("leverage", "1")),
        })

    # ---------------- 撮合辅助 ----------------

    def _find_kline_idx_after(self, klines: List[Dict[str, Any]], after: datetime) -> Optional[int]:
        """返回 klines 中第一根 open_time >= after 的索引；没找到返回 None。
        klines 必须已按 open_time 升序。
        """
        lo, hi = 0, len(klines)
        while lo < hi:
            mid = (lo + hi) // 2
            t = _parse_ts(klines[mid]["open_time"])
            if t < after:
                lo = mid + 1
            else:
                hi = mid
        return lo if lo < len(klines) else None

    # ---------------- 持仓退出逻辑 ----------------

    def _run_position_exit_up_to(
        self,
        profile_id: str,
        up_to: datetime,
        klines: List[Dict[str, Any]],
        cutoff_at: datetime,
    ) -> None:
        """把当前持仓推进到 up_to：每根 1m K 线检查止损/止盈/移动止损/时间止损。

        游标机制（阻断 #1 修复）：
          pos["exit_scan_cursor"] 记录已评估的最后一根 K 线的 open_time（datetime）。
          - 首次扫描（cursor is None）：从 opened_at 起第一根开始（含成交当根——_find_kline_idx_after 取 open_time >= after 的第一根）
          - 后续扫描（cursor 非 None）：只扫 open_time **严格大于** cursor 的 K 线
          - 每评估完一根就更新游标；函数因触发退出事件而 return 后，下次自然从下一根继续
          - 保证同一仓位所有退出事件的 at 时间严格单调递增，永不回扫

          游标存放于 pos dict（而非 state）：
          部分平仓原地修改 pos（_partial_close 不重建对象）、全平后 pos=None、
          新入场由 on_entry_approved 重建全新 dict → 游标生命周期天然绑定单次持仓。

        同一根 K 线内的语义保持不变（保守优先级，阻断 #1 前已确立）：
          STOP_LOSS → TP1 → TP2 → trailing → TIME_STOP；
          命中哪个就 return 哪个，同一根只触发最先命中的事件。

        阻断 #3：每根 K 线先更新 running peak（highest_price / lowest_price），
        供 trailing 按「历史最高浮盈」分档使用（非当前 K 线内部极值，这是原 bug 根因）。
        阻断 #4：退出判定后若未平仓，按 marked_equity（=已实现 equity + 未实现浮盈）
        snapshot 进 equity_history，让最大回撤和反事实停牌标记基于盯市曲线。
        """
        state = self.states[profile_id]
        if not state.has_open_position():
            return

        pos = state.open_position

        # ---- 游标定位 ----
        cursor = pos.get("exit_scan_cursor")
        opened_at = _parse_ts(pos["opened_at"])
        if cursor is None:
            # 首次扫描：从 opened_at 起第一根开始（含成交当根；保持旧语义）
            idx_start = self._find_kline_idx_after(klines, opened_at)
        else:
            # 后续扫描：只扫 open_time 严格大于 cursor 的 K 线
            idx_start = self._find_kline_idx_after(
                klines, cursor + timedelta(milliseconds=1)
            )
        idx_end = self._find_kline_idx_after(klines, up_to)
        if idx_start is None or idx_start >= len(klines):
            return
        idx_end = idx_end if idx_end is not None else len(klines)

        for i in range(idx_start, idx_end):
            k = klines[i]
            k_open_time = _parse_ts(k["open_time"])
            k_high = Decimal(str(k["high"]))
            k_low = Decimal(str(k["low"]))
            k_close = Decimal(str(k["close"]))
            entry_price = pos["entry_price"]

            # 阻断 #3：先更新 running peak（历史最高/最低）
            direction = pos["direction"]
            if direction == "LONG":
                pos["highest_price"] = max(pos.get("highest_price", entry_price), k_high)
            else:
                pos["lowest_price"] = min(pos.get("lowest_price", entry_price), k_low)

            # 退出判定（保守优先级不变）
            result = self._check_and_close_on_bar(profile_id, k, k_open_time)

            # 无论是否触发退出，先推进游标到当前根
            # 这样即使 return，下次调用也不会再扫这根
            pos["exit_scan_cursor"] = k_open_time

            # 阻断 #4：未平仓 → 盯市快照
            if result is None:
                mark = k_close
                qty = pos["quantity"]
                if direction == "LONG":
                    unrealized = (mark - entry_price) * qty
                else:
                    unrealized = (entry_price - mark) * qty
                marked_equity = state.equity + unrealized
                state.snapshot_marked_equity(k_open_time, marked_equity, True)
            else:
                # 已平仓（全平或部分平仓），后续 K 线不再执行
                return

    def _check_and_close_on_bar(self, profile_id: str, k: Dict[str, Any], at: datetime) -> Optional[str]:
        """检查一根 K 线是否触发某个退出事件。
        保守顺序：同一根 K 线同时穿止损和止盈且无法判序 → 止损先发生。
        返回 close_reason 或 None（未触发）。
        """
        state = self.states[profile_id]
        pos = state.open_position
        if pos is None:
            return None

        direction = pos["direction"]
        k_high = Decimal(str(k["high"]))
        k_low = Decimal(str(k["low"]))
        k_open = Decimal(str(k["open"]))
        k_close = Decimal(str(k["close"]))
        # 本根 K 线的 MAE / MFE 记录
        if direction == "LONG":
            mfe = k_high - pos["entry_price"]
            mae = pos["entry_price"] - k_low
        else:
            mfe = pos["entry_price"] - k_low
            mae = k_high - pos["entry_price"]

        # 1) 止损
        stop_loss = pos.get("stop_loss")
        if stop_loss is not None and stop_loss > 0:
            if direction == "LONG" and k_low <= stop_loss:
                self._close_position(profile_id, at, stop_loss, "STOP_LOSS", mae=mae, mfe=mfe)
                return "STOP_LOSS"
            if direction == "SHORT" and k_high >= stop_loss:
                self._close_position(profile_id, at, stop_loss, "STOP_LOSS", mae=mae, mfe=mfe)
                return "STOP_LOSS"

        # 2) TP1 / TP2：按 config 里的 tp1_atr_multiplier 和 tp1_close_ratio 直接计算 TP 价格
        hit = self._maybe_partial_tp(pos, direction, k_high, k_low, profile_id, at, "TP1", mae, mfe)
        if hit is not None:
            return hit
        hit = self._maybe_partial_tp(pos, direction, k_high, k_low, profile_id, at, "TP2", mae, mfe)
        if hit is not None:
            return hit

        # 3) 移动止损（阻断 #3：完整实现动态 trailing，对齐生产 strategy.py L4543-4650）
        trailing_cfg = pos.get("trailing")
        if trailing_cfg and trailing_cfg.get("enabled", True):
            result = self._check_dynamic_trailing(profile_id, pos, k, at, direction, pos["entry_price"], k_high, k_low)
            if result is not None:
                return result

        # 4) 时间止损
        time_stop_cfg = pos.get("time_stop")
        if time_stop_cfg:
            opened_at_ts = _parse_ts(pos["opened_at"])
            # fail-closed：冻结 profile 配置必须提供 time_stop.max_holding_hours，禁止静默兜底
            if "max_holding_hours" not in time_stop_cfg:
                raise KeyError(
                    f"profile={profile_id} time_stop 缺少 max_holding_hours 键"
                )
            max_hours = int(time_stop_cfg["max_holding_hours"])
            close_ratio = Decimal(str(time_stop_cfg.get("close_ratio", "1")))
            if at - opened_at_ts >= timedelta(hours=max_hours):
                close_qty = pos["quantity"] * close_ratio
                if close_qty >= pos["quantity"]:
                    self._close_position(profile_id, at, k_close, "TIME_STOP", mae=mae, mfe=mfe)
                else:
                    self._partial_close(profile_id, at, k_close, "TIME_STOP", close_qty, mae=mae, mfe=mfe)
                return "TIME_STOP"

        return None

    def _check_dynamic_trailing(
        self,
        profile_id: str,
        pos: Dict[str, Any],
        k: Dict[str, Any],
        at: datetime,
        direction: str,
        entry_price: Decimal,
        k_high: Decimal,
        k_low: Decimal,
    ) -> Optional[str]:
        """阻断 #3：完整动态 trailing 实现，对齐生产 strategy.py L4543-4650。

        核心差异（修复原 bug）：
        - 分档基准是「历史最高浮盈」（running peak），不是当前 K 线内部 best_pct
        - 读取嵌套配置（dynamic_trailing.activation.min_profit_pct / regression_tiers），
          原代码读扁平键 activation_min_profit_pct / retrace_ratio 恒默认
        - vol_adj 从 replay config 读取（禁止硬编码 1.0）
        - 全平（§11 已决策），平仓价取触发时的 final_stop（保守）
        """
        trailing_cfg = pos.get("trailing") or {}
        activation = trailing_cfg.get("activation", {}) or {}
        tiers = trailing_cfg.get("regression_tiers", []) or []

        # 1) 取 reference_price（峰值）+ 算峰值浮盈 pct
        if direction == "LONG":
            ref = pos.get("highest_price", entry_price)
            if ref is None or ref <= entry_price:
                return None  # 无正向峰值 → 不启 trailing
            pct = (ref - entry_price) / entry_price * Decimal("100")
        else:
            ref = pos.get("lowest_price", entry_price)
            if ref is None or ref >= entry_price:
                return None
            pct = (entry_price - ref) / entry_price * Decimal("100")
        pct = max(pct, Decimal("0"))
        pos["pending_profit_pct"] = str(pct)  # 诊断用

        # 2) 激活判定（单向保持：一旦 True 不再因浮盈回落而退出）
        # fail-closed：冻结 profile 配置必须提供 activation.min_profit_pct，禁止静默兜底
        if "min_profit_pct" not in activation:
            raise KeyError(
                f"profile={profile_id} trailing.activation 缺少 min_profit_pct 键"
            )
        min_profit = Decimal(str(activation["min_profit_pct"]))
        also_on_tp1 = bool(activation.get("also_on_tp1", True))
        tp1_hit = bool(pos.get("tp1_hit", False))
        profit_activated = pct >= min_profit
        tp1_activated = also_on_tp1 and tp1_hit
        if not profit_activated and not tp1_activated:
            if not pos.get("trailing_activated"):
                return None  # 未激活 → 不启 trailing
            # 已激活但浮盈回落 → 保持激活状态继续往下走

        pos["trailing_activated"] = True

        # 3) 分档（tier 列表按 profit_ceiling 升序；保本模式：
        #    pct < tiers[0].ceiling 且未 tp1_hit → stop=entry）
        if not tiers:
            logger.warning("profile=%s trailing regression_tiers 为空，跳过 trailing", profile_id)
            return None

        vol_adj = self.vol_adj_override  # 从 config 读取，禁止硬编码 1.0

        # fail-closed：每档 tier 必须提供 profit_ceiling 和 retrace_ratio，禁止静默兜底
        def _require_tier_keys(tier: Dict[str, Any], tier_idx: int) -> None:
            for _k in ("profit_ceiling", "retrace_ratio"):
                if _k not in tier:
                    raise KeyError(
                        f"profile={profile_id} trailing.regression_tiers[{tier_idx}] 缺少 {_k} 键"
                    )

        first_ceiling = Decimal(str(tiers[0]["profit_ceiling"]))
        if pct < first_ceiling and not tp1_hit:
            # 保本模式：stop=entry，tier_index=0
            tier_index = 0
            stop_price = entry_price
        else:
            tier_index = len(tiers) - 1
            for i, tier in enumerate(tiers):
                _require_tier_keys(tier, i)
                ceiling = Decimal(str(tier["profit_ceiling"]))
                if pct < ceiling:
                    tier_index = i
                    break
            _require_tier_keys(tiers[tier_index], tier_index)
            retrace_ratio = Decimal(str(tiers[tier_index]["retrace_ratio"]))

            if direction == "LONG":
                profit_per_unit = ref - entry_price
                allowed_retrace = profit_per_unit * retrace_ratio * vol_adj
                stop_price = ref - allowed_retrace
            else:
                profit_per_unit = entry_price - ref
                allowed_retrace = profit_per_unit * retrace_ratio * vol_adj
                stop_price = ref + allowed_retrace

        # 4) hard_stop 兜底（atr * stop_loss_atr_multiplier）
        atr_val = pos.get("atr")
        sl_mult_raw = pos.get("_stop_loss_atr_multiplier")
        stop_loss_mult = (
            Decimal(str(sl_mult_raw))
            if sl_mult_raw is not None and str(sl_mult_raw).strip()
            else Decimal("2.0")
        )
        # atr 和 stop_loss_mult 可能缺失（早期测试数据），回退跳过 hard_stop 兜底
        hard_stop_price = None
        if atr_val is not None and atr_val > 0 and stop_loss_mult > 0:
            if direction == "LONG":
                hard_stop_price = entry_price - atr_val * stop_loss_mult
            else:
                hard_stop_price = entry_price + atr_val * stop_loss_mult

        # 5) 最终 stop：与 hard_stop 取 MAX(MIN) + 单向移动保护
        final_stop = stop_price
        if hard_stop_price is not None:
            final_stop = max(final_stop, hard_stop_price) if direction == "LONG" else min(final_stop, hard_stop_price)

        old_trailing_stop = pos.get("trailing_stop_price")
        if old_trailing_stop is not None:
            # 单向移动：LONG 只升、SHORT 只降
            if direction == "LONG":
                final_stop = max(final_stop, old_trailing_stop)
            else:
                final_stop = min(final_stop, old_trailing_stop)
        pos["trailing_stop_price"] = final_stop

        # 6) 触发判定（保守：LONG 用 k_low，SHORT 用 k_high；单根 K 线不推断 tick 顺序）
        triggered = False
        if direction == "LONG" and k_low <= final_stop:
            triggered = True
        elif direction == "SHORT" and k_high >= final_stop:
            triggered = True

        if triggered:
            # §11 已决策：trailing 触发全平（close_ratio=1.0），
            # 平仓价取触发时的 final_stop（保守）
            logger.info(
                "动态 trailing 触发 profile=%s %s %s pct=%s final_stop=%s tp1_hit=%s",
                profile_id, direction, pos.get("symbol", ""),
                str(pct), str(final_stop), tp1_hit,
            )
            self._close_position(profile_id, at, final_stop, "TRAILING_STOP")
            return "TRAILING_STOP"

        return None

    def _maybe_partial_tp(
        self,
        pos: Dict[str, Any],
        direction: str,
        k_high: Decimal,
        k_low: Decimal,
        profile_id: str,
        at: datetime,
        which: str,
        mae: Any,
        mfe: Any,
    ) -> Optional[str]:
        """公共逻辑：检查 TP1 或 TP2 触发并执行部分平仓。

        参数 which: "TP1" 或 "TP2"，自动从 pos 取对应价格与 close_ratio。
        触发价已由 _attempt_entry 按 ATR × 倍率计算好并写入 pos。
        """
        price_key = "tp1" if which == "TP1" else "tp2"
        ratio_key = "tp1_close_ratio" if which == "TP1" else "tp2_close_ratio"
        tp_price = pos.get(price_key)
        if tp_price is None:
            return None
        # TP2 额外守卫：若 quantity 已被 TP1 清到 0 以下不应再触发
        if which == "TP2" and not pos.get("quantity", Decimal("0")) > 0:
            return None
        hit = (
            (direction == "LONG" and k_high >= tp_price)
            or (direction == "SHORT" and k_low <= tp_price)
        )
        if not hit:
            return None
        close_ratio = pos.get(ratio_key, Decimal("1"))
        close_qty = pos["quantity"] * close_ratio
        self._partial_close(profile_id, at, tp_price, which, close_qty, mae=mae, mfe=mfe)
        return which

    # ---------------- 平仓执行 ----------------

    def _close_position(
        self,
        profile_id: str,
        at: datetime,
        exit_price: Decimal,
        reason: str,
        mae=None,
        mfe=None,
    ) -> None:
        """全平一个持仓。"""
        state = self.states[profile_id]
        pos_before = state.open_position
        if pos_before is None:
            # 不变量：无持仓但收到平仓
            self.invariant_violations.append({
                "profile_id": profile_id,
                "time": at.isoformat(),
                "violation": "unprotected_close",
            })
            return

        qty = pos_before["quantity"]
        exit_qty = qty
        self._execute_exit(profile_id, at, exit_price, exit_qty, reason, mae, mfe)

    def _partial_close(
        self,
        profile_id: str,
        at: datetime,
        exit_price: Decimal,
        reason: str,
        close_qty: Decimal,
        mae=None,
        mfe=None,
    ) -> None:
        """部分平仓（TP1 / TP2 / 时间止损部分 close）。"""
        state = self.states[profile_id]
        pos = state.open_position
        if pos is None:
            self.invariant_violations.append({
                "profile_id": profile_id,
                "time": at.isoformat(),
                "violation": "unprotected_close",
            })
            return
        close_qty = round_quantity(close_qty, self.step_size)
        close_qty = clamp_notional(close_qty, exit_price, self.min_notional)
        close_qty = round_quantity(close_qty, self.step_size)
        # 上限约束：clamp_notional 在 notional < minNotional 时会向上抬升数量（无上限），
        # 必须把 close_qty 截断到持仓量，避免平仓量 > 持仓量的"幻读盈亏"污染 equity。
        close_qty = min(close_qty, pos["quantity"])
        remaining_qty = pos["quantity"] - close_qty

        if close_qty <= 0:
            return

        self._execute_exit(profile_id, at, exit_price, close_qty, reason, mae, mfe)

        # 若 _execute_exit 内部判定 remaining <= 0（常见原因：min_notional clamp
        # 把 close_qty 向上抬到超过持仓量）会走 on_position_closed 把 open_position 置 None。
        # 此时"部分平仓"实际演变为整仓平仓，直接返回，不再访问已置空的 open_position。
        if state.open_position is None:
            return

        # 剩余数量 > 0：保留持仓，更新 quantity
        state.open_position["quantity"] = remaining_qty
        # 记录新的 TP1 / TP2：按剩余数量 * 原始 close_ratio 调整
        # 简化：TP1 之后若还剩余，说明 tp1_close_ratio < 1，保留的 tp1 价格不变，
        #      之后再到 tp1 不再触发——这更接近真实逻辑（只触发一次）。
        #      把 pos["tp1"] 设为 None 即可避免重复触发。
        if reason == "TP1":
            state.open_position["tp1"] = None
            # 阻断 #3：TP1 触发后置 tp1_hit=True，让 trailing 的 also_on_tp1 激活语义生效
            state.open_position["tp1_hit"] = True
        elif reason == "TP2":
            state.open_position["tp2"] = None
        elif reason == "TIME_STOP":
            state.open_position["time_stop"] = None

        # 耦合 #6b：部分平仓后只检查 quantity step 合法性。
        # 不调 run_all_invariants 全量检查——check_no_double_close 只要 pos_after 非 None 就报违规，
        # 但部分平仓后持仓仍在是正常的；check_notional_above_min 对剩余仓位同样误报
        # （剩余 notional 低于 minNotional 合法——引擎撮合时 clamp_notional 只对 close_qty
        # 做向上抬升保证平仓批次合法，剩余仓位可能 < minNotional 等下次 TP2/止损触发）。
        from .invariants import check_quantity_in_range
        v = check_quantity_in_range(close_qty, self.step_size)
        if v:
            self.invariant_violations.append({
                "profile_id": profile_id,
                "time": at.isoformat(),
                "violation": v,
            })

    def _execute_exit(
        self,
        profile_id: str,
        at: datetime,
        exit_price: Decimal,
        close_qty: Decimal,
        reason: str,
        mae=None,
        mfe=None,
    ) -> None:
        """实际执行一次平仓：算盈亏、费用、资金费、滑点、更新状态、写 replay_trades。"""
        state = self.states[profile_id]
        pos = state.open_position

        direction = pos["direction"]
        entry_price = pos["entry_price"]
        notional = close_qty * exit_price

        # 退出价格先舍入
        exit_price_r = round_price(exit_price, self.tick_size)

        # 滑点（退出）：限价 / 市价都按 baseline，可在 config 里改
        slip = slippage_cost(notional, self.slippage_baseline, direction)

        # 手续费：双边合计 = 出场手续费 + 入场手续费分摊（taker 每边 5 bps，spec 主口径）
        # 入场手续费在 _fill_entry 时按 fill_price * quantity 计算，按每单位数量存入
        # entry_fee_per_qty，平仓时按 close_qty 分摊；部分平仓多次后总和精确等于总入场费
        fee_exit = taker_fee_notional(notional, self.taker_bps)
        entry_fee_per_qty = pos.get("entry_fee_per_qty") or Decimal("0")
        fee = fee_exit + entry_fee_per_qty * close_qty

        # 资金费：过滤 [opened_at, at] 区间的 funding_events
        opened_at = _parse_ts(pos["opened_at"])
        funding_in_range = [
            (t, r) for (t, r) in self.funding_events
            if opened_at <= t <= at
        ]
        funding = sum_funding_fee(close_qty, entry_price, exit_price_r, direction, funding_in_range)

        # PnL
        pnl = calculate_pnl(direction, entry_price, exit_price_r, close_qty)

        # 净 PnL
        net_pnl = pnl + fee + funding + slip

        # Round4 修复：整笔仓位级累计净盈亏（含本批的手续费/资金费/滑点）。
        # 无论部分平仓还是全平都累加——on_position_closed 只在全平时调用，
        # 用累计值做连续亏损判定。
        pos["position_net_pnl"] = pos.get("position_net_pnl", Decimal("0")) + net_pnl

        # —— 持仓级 MAE/MFE：优先用 running peak（持仓生命周期内的历史最高/最低）——
        # 由 _run_position_exit_up_to 逐根 1m K 线更新 pos["highest_price"] / ["lowest_price"]
        # 缺失时回退用当前 K 线级 mae/mfe（_check_and_close_on_bar 传入），再回退 None
        highest = pos.get("highest_price")
        lowest = pos.get("lowest_price")
        if highest is not None and lowest is not None:
            if direction == "LONG":
                pos_mfe = max(Decimal("0"), highest - entry_price)
                pos_mae = max(Decimal("0"), entry_price - lowest)
            else:
                pos_mfe = max(Decimal("0"), entry_price - lowest)
                pos_mae = max(Decimal("0"), highest - entry_price)
        else:
            # 极老测试路径无 running peak，回退用 K 线级 mae/mfe
            pos_mfe = mfe if mfe is not None else None
            pos_mae = mae if mae is not None else None

        # 交易明细
        # 阻断 #5：position_id 让报告能按仓位生命周期聚合，而非每条 exit 算一笔
        # 批次三 A：同时带 entry_quantity（入场总数量，不随部分平仓变化），
        # 让报告 _aggregate_positions 拿到正确的入场数量，不再误用首批次 close_qty
        position_id = pos.get("position_id")
        self.replay_trades[profile_id].append({
            "position_id": position_id,
            "entry_time": _parse_ts(pos["opened_at"]).isoformat(),
            "entry_price": str(entry_price),
            "exit_time": at.isoformat(),
            "exit_price": str(exit_price_r),
            "direction": direction,
            "symbol": pos["symbol"],
            "filled": True,
            "unfilled_reason": None,
            "exit_reason": reason,
            "quantity": str(close_qty),   # 本批次平仓量
            # 批次三 A：入场总数量（兜底极老路径无 entry_quantity 时用当前 pos quantity）
            "entry_quantity": str(pos.get("entry_quantity") or pos["quantity"]),
            "leverage": str(pos["leverage"]),
            "margin": str(pos["margin"]),
            "realized_pnl": str(pnl),
            "fee": str(fee),
            "funding_fee": str(funding),
            "slippage": str(slip),
            "net_pnl": str(net_pnl),
            # 持仓级 MAE/MFE（running peak 派生，优先；回退 K 线级）
            "mae": str(pos_mae) if pos_mae is not None else None,
            "mfe": str(pos_mfe) if pos_mfe is not None else None,
            "holding_seconds": int((at - _parse_ts(pos["opened_at"])).total_seconds()),
        })

        # 状态更新
        pos_before = state.open_position
        remaining = pos["quantity"] - close_qty
        if remaining <= 0:
            state.on_position_closed(
                at=at,
                realized_pnl=pnl,
                fee=fee,
                funding=funding,
                slippage=slip,
                close_reason=reason,
                position_net_pnl=pos.get("position_net_pnl", Decimal("0")),
            )
            # 耦合 #6b：整仓平仓后跑不变量——检查 double_close（pos_after 应为 None）
            # 和 quantity step。
            # 跳过 check_notional_above_min：整仓最后一批 close_qty 可能被截断到 remaining_qty，
            # 其 notional 可能 < minNotional（这是正常关闭，不是撮合阶段的合法性问题）。
            from .invariants import check_no_double_close, check_quantity_in_range
            v = check_no_double_close(pos_before, state.open_position, f"{profile_id}:close:{reason}")
            if v:
                self.invariant_violations.append({
                    "profile_id": profile_id,
                    "time": at.isoformat(),
                    "violation": v,
                })
            v = check_quantity_in_range(close_qty, self.step_size)
            if v:
                self.invariant_violations.append({
                    "profile_id": profile_id,
                    "time": at.isoformat(),
                    "violation": v,
                })
        else:
            # 部分平仓：只更新已实现盈亏 + 手续费 + 滑点，但不清 open_position
            state.realized_pnl += pnl
            state.fee_paid += abs(fee) if fee < 0 else Decimal("0")
            state.funding_fee += funding  # 正=收入、负=支出，净额累计
            state.slippage_cost += abs(slip) if slip < 0 else Decimal("0")
            state.equity += pnl + fee + funding + slip
            state.snapshot_equity(at)

    # ---------------- 入场尝试 ----------------

    def _attempt_entry(
        self,
        profile_id: str,
        event: Dict[str, Any],
        scheduled_at: datetime,
        klines: List[Dict[str, Any]],
    ) -> None:
        """把 candidate 信号转换成 pending_limit_orders 或市价立即成交。

        缺陷 2 修复：atr 从 event['stop_loss_rule']['atr_value'] 读取，
        回退 event['feature_snapshot']['atr']；都缺失时 fail-closed 跳过入场。
        """
        state = self.states[profile_id]

        symbol = event["symbol"]
        direction = event["direction"]
        grade = event.get("grade")
        reference_price = Decimal(str(event.get("reference_price") or "0"))
        intended_type = event.get("intended_order_type") or "LIMIT"
        intended_entry_price_raw = event.get("intended_entry_price")

        # ---- 读取 atr（缺陷 2 修复：禁止恒为 0.01 的静默兜底） ----
        atr = self._extract_atr(event)
        if atr is None:
            logger.warning(
                "决策事件缺少 atr，跳过入场（fail-closed） profile=%s symbol=%s",
                profile_id, symbol,
            )
            return

        # 从 profile 冻结配置读退出规则
        profile_cfg = self.profile_cfg_orig if profile_id == "xau_original" else self.profile_cfg_aggr
        level_cfg = self._get_risk_level(profile_cfg, grade)
        if level_cfg is None:
            # grade 不在 S/A/B/C 里 → 用 A 作为 fallback
            level_cfg = self._get_risk_level(profile_cfg, "A")

        # fail-closed：冻结 profile 配置必须提供退出规则全部必需键，禁止静默兜底
        if "stop_loss_atr_multiplier" not in level_cfg:
            raise KeyError(
                f"profile={profile_id} level[{grade or 'A'}] 缺少 stop_loss_atr_multiplier 键"
            )
        stop_loss_atr = Decimal(str(level_cfg["stop_loss_atr_multiplier"]))

        if "partial_take_profit" not in level_cfg:
            raise KeyError(
                f"profile={profile_id} level[{grade or 'A'}] 缺少 partial_take_profit 段"
            )
        partial_tp = level_cfg["partial_take_profit"]
        for _k in ("tp1_atr_multiplier", "tp1_close_ratio",
                   "tp2_atr_multiplier", "tp2_close_ratio"):
            if _k not in partial_tp:
                raise KeyError(
                    f"profile={profile_id} partial_take_profit 缺少 {_k} 键"
                )
        tp1_atr = Decimal(str(partial_tp["tp1_atr_multiplier"]))
        tp1_ratio = Decimal(str(partial_tp["tp1_close_ratio"]))
        tp2_atr = Decimal(str(partial_tp["tp2_atr_multiplier"]))
        tp2_ratio = Decimal(str(partial_tp["tp2_close_ratio"]))
        trailing_cfg = level_cfg.get("dynamic_trailing", {})
        time_stop_cfg = level_cfg.get("time_stop", {})

        # 入场价
        if intended_entry_price_raw is not None:
            limit_price = Decimal(str(intended_entry_price_raw))
        else:
            limit_price = reference_price
        limit_price_r = round_price(limit_price, self.tick_size)

        # 计算退出价格
        if direction == "LONG":
            stop_loss_price = round_price(limit_price_r - atr * stop_loss_atr, self.tick_size)
            tp1_price = round_price(limit_price_r + atr * tp1_atr, self.tick_size)
            tp2_price = round_price(limit_price_r + atr * tp2_atr, self.tick_size)
        else:
            stop_loss_price = round_price(limit_price_r + atr * stop_loss_atr, self.tick_size)
            tp1_price = round_price(limit_price_r - atr * tp1_atr, self.tick_size)
            tp2_price = round_price(limit_price_r - atr * tp2_atr, self.tick_size)

        # 先检查门槛
        # pilot 约束：最多 30 USDT margin，1 并发，3x leverage
        # margin = notional / leverage
        # 我们先用 quantity 来自 config.symbol_config.position_ratio_s * available_margin（简化）
        available = state.available_margin()
        # per-profile pilot 约束（禁止跨 profile 共用）
        margin = min(self._profile_max_margin[profile_id], available)
        if margin <= 0:
            return

        leverage = self._profile_max_lev[profile_id]
        notional = margin * leverage
        # 用 limit_price_r 估算 quantity
        raw_qty = notional / limit_price_r
        raw_qty = clamp_notional(raw_qty, limit_price_r, self.min_notional)
        quantity = round_quantity(raw_qty, self.step_size)
        if quantity <= 0:
            return

        required_margin = quantity * limit_price_r / leverage

        gate = state.check_entry_eligibility(
            at=scheduled_at,
            symbol=symbol,
            direction=direction,
            required_margin=required_margin,
        )
        if not gate["allowed"]:
            # 记录 rejection_reason（永久保留）
            self.replay_trades[profile_id].append({
                "entry_time": scheduled_at.isoformat(),
                "entry_price": str(limit_price_r),
                "direction": direction,
                "symbol": symbol,
                "filled": False,
                "unfilled_reason": f"rejected:{gate['rejection_reasons']}",
                "exit_reason": None,
                "net_pnl": "0",
                "fee": "0",
                "funding_fee": "0",
                "slippage": "0",
                "quantity": str(quantity),
                "margin": str(required_margin),
                "leverage": str(leverage),
            })
            return

        # 门槛通过 —— 根据 intended_order_type 决定撮合

        # 阻断 #5：生成确定性 position_id —— per-profile 序列计数器 + fill 时间 + direction
        # 安全访问：hasattr 兼容测试用 __new__ 绕过 __init__ 的场景
        if not hasattr(self, "_open_seq"):
            self._open_seq = {"xau_original": 0, "xau_aggressive": 0}
        self._open_seq[profile_id] = self._open_seq.get(profile_id, 0) + 1
        open_seq = self._open_seq[profile_id]
        position_id = f"{profile_id}@{scheduled_at.strftime('%Y%m%dT%H%M%S')}@{direction}@{open_seq}"

        # 阻断 #3：stop_loss_atr_multiplier 存入 pos（trailing hard_stop 兜底计算用）
        # 在 _fill_entry 里通过 state 传入

        order_record = {
            "source_event": event,
            "symbol": symbol,
            "direction": direction,
            "grade": grade,
            "limit_price": limit_price_r,
            "quantity": quantity,
            "margin": required_margin,
            "leverage": leverage,
            "stop_loss": stop_loss_price,
            "tp1": tp1_price,
            "tp1_close_ratio": tp1_ratio,
            "tp2": tp2_price,
            "tp2_close_ratio": tp2_ratio,
            "trailing": trailing_cfg,
            "time_stop": time_stop_cfg,
            # 阻断 #3 / #5：trailing 硬止损兜底参数 + position_id
            "atr": atr,
            "stop_loss_atr_multiplier": str(stop_loss_atr),
            "position_id": position_id,
        }

        if intended_type in ("MARKET", "market"):
            # 市价：下一根 K 线立即撮合
            fill_at = self._find_next_kline_open(scheduled_at, klines)
            if fill_at is None:
                # 无下一根 K 线 → 未成交
                self._record_unfilled(
                    profile_id,
                    {**order_record, "opened_at": scheduled_at.isoformat()},
                    reason="no_klines",
                )
                return
            fill_kline = klines[fill_at]
            fill_time = _parse_ts(fill_kline["open_time"])
            fill_price = market_fill_price(Decimal(str(fill_kline["open"])), self.slippage_baseline, direction)
            self._fill_entry(
                profile_id=profile_id,
                event=event,
                opened_at=scheduled_at,
                fill_at=fill_time,
                fill_price=round_price(fill_price, self.tick_size),
                quantity=quantity,
                margin=required_margin,
                leverage=leverage,
                stop_loss=stop_loss_price,
                grade=grade,
                order_kind="MARKET",
                tp1=tp1_price,
                tp1_close_ratio=tp1_ratio,
                tp2=tp2_price,
                tp2_close_ratio=tp2_ratio,
                trailing=trailing_cfg,
                time_stop=time_stop_cfg,
                # 阻断 #3 / #5 新增参数
                atr=atr,
                stop_loss_atr_multiplier=str(stop_loss_atr),
                position_id=position_id,
            )
        else:
            # 限价：加入 pending
            order_record["opened_at"] = scheduled_at
            self.pending_limit_orders[profile_id].append(order_record)

    @staticmethod
    def _extract_atr(event: Dict[str, Any]) -> Optional[Decimal]:
        """从冻结的决策事件里提取 atr（缺陷 2 修复）。

        冻结事件没有顶层 'atr' 字段。优先读 stop_loss_rule.atr_value（collector
        在 shadow_decision_events.stop_loss_rule 序列化时写入 atr_value），
        回退 feature_snapshot.atr。两者都缺失或脏数据 → 返回 None，由调用方
        fail-closed（拒该笔入场，记入未成交原因，而非让整轮回放崩溃）。
        """
        def _safe_positive_decimal(raw: Any) -> Optional[Decimal]:
            """安全解析 ATR 值——捕获脏字符串 / None / 非法类型，
            返回 None 由上层 fail-closed。"""
            if raw is None:
                return None
            try:
                val = Decimal(str(raw))
            except (ValueError, ArithmeticError, TypeError):
                logger.warning(
                    "ATR 字段脏数据，解析失败，拒该笔入场（fail-closed） raw=%r",
                    raw,
                )
                return None
            if val <= 0:
                return None
            return val

        # 1) 优先 stop_loss_rule.atr_value
        sl_rule = event.get("stop_loss_rule")
        if isinstance(sl_rule, dict):
            raw = sl_rule.get("atr_value")
            parsed = _safe_positive_decimal(raw)
            if parsed is not None:
                return parsed
        # 2) 回退 feature_snapshot.atr
        fs = event.get("feature_snapshot")
        if isinstance(fs, dict):
            raw = fs.get("atr")
            parsed = _safe_positive_decimal(raw)
            if parsed is not None:
                return parsed
        # 都没有 → fail-closed
        return None

    def _find_next_kline_open(self, after: datetime, klines: List[Dict[str, Any]]) -> Optional[int]:
        """找到 after 之后第一根 K 线索引。"""
        return self._find_kline_idx_after(klines, after)

    def _get_risk_level(self, profile_cfg: Dict[str, Any], grade: Optional[str]) -> Dict[str, Any]:
        levels = self._deep_get(profile_cfg, "strategy.risk.signal_levels", {})
        return levels.get(grade or "A", levels.get("A", {}))

    # ---------------- 入场执行 ----------------

    def _fill_entry(
        self,
        profile_id: str,
        event: Dict[str, Any],
        opened_at: datetime,     # 接口兼容：fill_at 已用于实际写入，opened_at 未参与后续计算
        fill_at: datetime,
        fill_price: Decimal,
        quantity: Decimal,
        margin: Decimal,
        leverage: int,
        stop_loss: Decimal,
        grade: Optional[str],
        order_kind: str,         # 接口兼容：函数体不区分市价/限价，kind 信息已体现在 pos 字典其他字段
        tp1: Optional[Decimal] = None,
        tp1_close_ratio: Optional[Decimal] = None,
        tp2: Optional[Decimal] = None,
        tp2_close_ratio: Optional[Decimal] = None,
        trailing: Optional[Dict[str, Any]] = None,
        time_stop: Optional[Dict[str, Any]] = None,
        # 阻断 #3 / #5 新增参数
        atr: Optional[Decimal] = None,
        stop_loss_atr_multiplier: Optional[str] = None,
        position_id: Optional[str] = None,
    ) -> None:
        """撮合成功、写入 open_position。

        阻断 #3：初始化 highest_price/lowest_price=fill_price（running peak 起点）、
        atr 存入 pos 供 trailing hard_stop 兜底计算。
        阻断 #5：position_id 让报告能按仓位生命周期聚合。
        耦合 #6b：开仓后跑不变量（quantity step / notional min）。
        """
        state = self.states[profile_id]
        pos_before = state.open_position
        if pos_before is not None:
            # 不变量：不该有持仓但又入场
            self.invariant_violations.append({
                "profile_id": profile_id,
                "time": fill_at.isoformat(),
                "violation": "position_already_open",
            })
            return

        state.on_entry_approved(
            at=fill_at,
            symbol=event["symbol"],
            direction=event["direction"],
            quantity=quantity,
            entry_price=fill_price,
            margin=margin,
            leverage=leverage,
            stop_loss_price=stop_loss,
            grade=grade,
            tp1=tp1,
            tp1_close_ratio=tp1_close_ratio,
            tp2=tp2,
            tp2_close_ratio=tp2_close_ratio,
            trailing=trailing,
            time_stop=time_stop,
            # 阻断 #3：running peak 初始化 + atr
            highest_price=fill_price,
            lowest_price=fill_price,
            atr=atr,
        )

        # 阻断 #5 / 阻断 #3：引擎侧也写入 position_id 和 _stop_loss_atr_multiplier
        # （state.on_entry_approved 不存 _stop_loss_atr_multiplier，由引擎直接写入 pos 字典）
        if state.open_position is not None:
            state.open_position["position_id"] = position_id
            if stop_loss_atr_multiplier is not None:
                state.open_position["_stop_loss_atr_multiplier"] = stop_loss_atr_multiplier

        # 入场手续费：按本次成交数量计算 taker 手续费
        # 与 _execute_exit 出场手续费（已收）合并，形成完整 roundtrip 手续费口径
        # 按每单位数量存入持仓字典，供平仓时按 close_qty 分摊
        entry_notional = fill_price * quantity
        entry_fee = taker_fee_notional(entry_notional, self.taker_bps)
        if quantity > 0 and state.open_position is not None:
            state.open_position["entry_fee_per_qty"] = entry_fee / quantity

        # 批次三 B：入场处只跑显式适用的两个不变量（quantity step + notional min），
        # 不再调 run_all_invariants——后者在 pos_after 非 None 时会调 check_no_double_close，
        # 返回「平仓后持仓仍存在」伪违规（每次成功入场都会记一条），
        # 使准入门槛 ⑤ 永远失败。
        # position_already_open 已由上面的手工守卫（L1285 附近）覆盖。
        if state.open_position is not None:
            from .invariants import check_quantity_in_range, check_notional_above_min
            for v in (
                check_quantity_in_range(quantity, self.step_size),
                check_notional_above_min(quantity, fill_price, self.min_notional),
            ):
                if v:
                    self.invariant_violations.append({
                        "profile_id": profile_id,
                        "time": fill_at.isoformat(),
                        "violation": v,
                    })

        logger.info(
            "撮合成功 profile=%s %s %s qty=%s price=%s at=%s tp1=%s tp2=%s trailing=%s time_stop=%s pid=%s",
            profile_id, event["direction"], event["symbol"],
            str(quantity), str(fill_price), fill_at.isoformat(),
            "Y" if tp1 is not None else "-",
            "Y" if tp2 is not None else "-",
            "Y" if trailing else "-",
            "Y" if time_stop else "-",
            position_id or "-",
        )

    # ---------------- 期终强制标记 ----------------

    def _force_close_remaining_positions(self, klines: List[Dict[str, Any]], cutoff_at: datetime) -> None:
        """所有仍开着的 profile 持仓都按最后一根可用 K 线 close 平掉。"""
        last_kline_idx = self._find_kline_idx_after(klines, cutoff_at)
        if last_kline_idx is None:
            last_kline_idx = len(klines)
        if last_kline_idx == 0:
            return
        last_kline = klines[last_kline_idx - 1]
        close_at = _parse_ts(last_kline["open_time"])
        close_price = Decimal(str(last_kline["close"]))
        for pid, state in self.states.items():
            if state.has_open_position():
                self._close_position(pid, close_at, close_price, "FINAL_CLOSE")
