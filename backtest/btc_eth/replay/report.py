"""
报告与准入判定。

输入：engine.run() 返回的 dict（含 per-profile trades、state、counterfactual、engine_metadata）
输出：report_<run_id>.md + report_<run_id>.json（本地落盘）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Any

import yaml

from .counterfactual import pressure_test_final_equity

logger = logging.getLogger(__name__)

UTC = timezone.utc


def _d(v: Any) -> Decimal:
    if v is None:
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def _aggregate_positions(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """阻断 #5：把 engine 输出的明细 trade 按 position_id 聚合为仓位级。

    生产 DB 的 trade_records 是动作级多条流水（shared/trade_logger.py 的 insert_pnl_summary
    会在全平时额外 INSERT 一条 PNL_SUMMARY），而报告评估口径是「仓位生命周期聚合层」——
    两层语义并存是有意为之，本函数做的就是报告层聚合，不改引擎/DB 输出形态。

    聚合规则：
    - position_id 为 None 的明细（未成交订单、门槛拒绝）跳过，不进入 closed_trades
    - 同 position_id 下，entry_time/entry_price/direction/symbol/grade 等不变字段取首条
    - exit_time 取最后一条（最后一次退出批次的 at）
    - exit_reason 取最后一条的 reason（终结原因）
    - quantity 取首条（入场总数量，不是各批次平仓之和——批次之和应 = 首条 quantity）
    - realized_pnl/fee/funding/slippage/net_pnl 全部按各批次求和（不重算，批次 net_pnl 已算好）
    - holding_seconds 按最后一条的 exit_time - 首条 entry_time
    """
    # 按 position_id 分组
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        pid = t.get("position_id")
        if not pid:
            continue  # position_id=None 的明细（未成交/拒绝）跳过
        if t.get("filled") and t.get("exit_reason"):
            groups.setdefault(pid, []).append(t)

    result: List[Dict[str, Any]] = []
    for pid, exits in groups.items():
        if not exits:
            continue
        exits_sorted = sorted(exits, key=lambda x: x.get("exit_time", ""))
        first = exits_sorted[0]
        last = exits_sorted[-1]
        # 首条里的 entry_time/entry_price/quantity 就是入场时的值
        entry_time = first.get("entry_time", "")
        exit_time = last.get("exit_time", "")
        try:
            t_entry = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
            t_exit = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
            holding_seconds = int((t_exit - t_entry).total_seconds())
        except Exception:
            holding_seconds = int(last.get("holding_seconds", 0))

        agg_net_pnl = sum(_d(t.get("net_pnl", "0")) for t in exits_sorted)
        agg_realized = sum(_d(t.get("realized_pnl", "0")) for t in exits_sorted)
        agg_fee = sum(_d(t.get("fee", "0")) for t in exits_sorted)
        agg_funding = sum(_d(t.get("funding_fee", "0")) for t in exits_sorted)
        agg_slippage = sum(_d(t.get("slippage", "0")) for t in exits_sorted)

        # MAE/MFE 聚合：取各批次明细的最大值（None 安全跳过，全部为 None → None）
        mae_values = [_d(t["mae"]) for t in exits_sorted if t.get("mae") is not None]
        mfe_values = [_d(t["mfe"]) for t in exits_sorted if t.get("mfe") is not None]
        agg_mae = str(max(mae_values)) if mae_values else None
        agg_mfe = str(max(mfe_values)) if mfe_values else None

        result.append({
            "position_id": pid,
            "entry_time": entry_time,
            "entry_price": first.get("entry_price"),
            "exit_time": exit_time,
            "exit_price": last.get("exit_price"),
            "exit_reason": last.get("exit_reason"),
            "direction": first.get("direction"),
            "symbol": first.get("symbol"),
            "grade": first.get("grade"),
            # 批次三 A：入场总数量——用 engine 每条 exit 明细里的 entry_quantity 字段
            # （由 state.on_entry_approved 在入场时注入，不随部分平仓变化），
            # 不再错误地拿首条的 quantity（那是 TP1 批次平仓量）
            "quantity": first.get("entry_quantity") or first.get("quantity"),
            "margin": first.get("margin"),
            "leverage": first.get("leverage"),
            "realized_pnl": str(agg_realized),
            "fee": str(agg_fee),
            "funding_fee": str(agg_funding),
            "slippage": str(agg_slippage),
            "net_pnl": str(agg_net_pnl),
            "holding_seconds": holding_seconds,
            # MAE/MFE 聚合（各批次最大值，全部 None 则 None）
            "mae": agg_mae,
            "mfe": agg_mfe,
            "exits": [
                {
                    "exit_time": t.get("exit_time"),
                    "exit_price": t.get("exit_price"),
                    "exit_reason": t.get("exit_reason"),
                    "quantity": t.get("quantity"),
                    "realized_pnl": t.get("realized_pnl"),
                    "net_pnl": t.get("net_pnl"),
                }
                for t in exits_sorted
            ],
        })

    # 按 entry_time 排序，保证确定性
    result.sort(key=lambda x: x.get("entry_time", ""))
    return result


def compute_metrics(
    profile_result: Dict[str, Any],
    engine_meta: Dict[str, Any],
    replay_cfg: Optional[Dict[str, Any]] = None,
    decision_stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把 engine 返回的 per-profile 结果转成指标字典。

    阻断 #4：最大回撤基于盯市曲线（marked_equity，三元组第二字段）。
    阻断 #5：closed_trades 从 engine 明细按 position_id 聚合为仓位级，
    PF / 胜率 / avg_win / avg_loss / expectancy / max_consecutive_loss / worst_loss
    全部改用聚合 trade 的 net_pnl（= Σ 各批次 net_pnl_i）。
    批次三 D：total_notional 改为聚合仓位入场名义价值；新增 slippage_pressure 三档、
    decision_stats（从 engine.decision_summary.per_profile 对应 pid 传入）、
    rejection_reason_distribution（未成交明细解析）。
    耦合 #6b：no_invariant_violations 门槛继续消费 engine_metadata.invariant_violations。
    """
    trades = profile_result["trades"]
    state = profile_result["state"]
    counterfactual = profile_result["counterfactual"]

    # 阻断 #5：按 position_id 聚合为仓位级 closed_trades
    closed_trades = _aggregate_positions(trades)
    n_closed = len(closed_trades)
    n_unfilled = len([t for t in trades if not t.get("filled")])
    # 虚拟订单统计（P2 增强）：
    # n_filled_orders = 聚合后的已成交仓位数（= 真实"完成一次开-关仓"的次数）
    # n_unfilled 保持原有语义（含 pending_at_cutoff）
    # n_pending_at_cutoff 单独拎出，方便渲染"截止未到期挂单"
    n_filled_orders = n_closed
    n_pending_at_cutoff = len([
        t for t in trades
        if not t.get("filled") and t.get("unfilled_reason") == "pending_at_cutoff"
    ])
    n_total_virtual_orders = n_filled_orders + n_unfilled
    if n_total_virtual_orders > 0:
        fill_rate = float(n_filled_orders) / float(n_total_virtual_orders)
        fill_rate_computable = True
    else:
        fill_rate = None
        fill_rate_computable = False

    realized_pnl = _d(state["realized_pnl"])
    fee_paid = _d(state["fee_paid"])
    funding_fee = _d(state["funding_fee"])
    slippage_cost = _d(state["slippage_cost"])
    final_equity = _d(state["final_equity"])
    initial_equity = _d(engine_meta["initial_equity"])
    net_pnl = realized_pnl - fee_paid - slippage_cost + funding_fee  # 资金费正 = 收

    # 批次三 D：滑点配置（从 replay_cfg 读取，禁止硬编码）
    replay_cfg_ref = replay_cfg or {}
    slip_cfg = replay_cfg_ref.get("slippage", {})
    baseline_bps = Decimal(str(slip_cfg.get("baseline_bps", 2)))
    tiers_bps_list = slip_cfg.get("tiers_bps", [1, 2, 4])

    # 批次三 D：total_notional 改为「聚合仓位入场名义价值」——按 closed_trades 的
    # entry_price * quantity（= entry_quantity，入场总数量）求和，不再逐条 exit 明细重复累加。
    # admission 门槛 ⑦ 滑点压力继续用此口径
    # （§7.3 已裁定：三档是独立档位，非叠加 baseline）。
    total_notional = sum(
        _d(t["entry_price"]) * _d(t["quantity"]) for t in closed_trades
    )
    # 同时保留 total_exit_notional（各批次 exit 明细的 notional 累计，用于交叉校验）
    total_exit_notional = sum(
        _d(t["entry_price"]) * _d(t["quantity"])
        for t in trades if t.get("filled") and t.get("exit_reason")
    )

    # 批次三 D：三档滑点压力测试（独立档位，每档 absolute bps，非叠加 baseline）
    slippage_pressure: Dict[str, Any] = {
        "baseline_bps": int(baseline_bps),
        "tiers_bps": [int(t) for t in tiers_bps_list],
    }
    for tier in tiers_bps_list:
        tier_key = f"{int(tier)}bps"
        # 批次三 D（已裁定口径）：每档压力权益 = final_equity - 增量滑点成本
        # 增量 = total_notional * (tier - baseline_bps) / 10000
        # - tier > baseline → 额外扣钱（滑点变差）
        # - tier == baseline → 增量 0 → equity == final_equity
        # - tier < baseline → 加钱回来（滑点变好）
        added_bps = Decimal(str(tier)) - baseline_bps
        pressure_equity = pressure_test_final_equity(
            final_equity, total_notional, added_bps
        )
        slippage_pressure[tier_key] = str(pressure_equity)

    # 批次三 D：decision_stats 从 write_reports 层 engine.decision_summary.per_profile[pid] 取值后传入
    # 无数据时给空 dict，让报告里该字段永远存在
    decision_stats = decision_stats or {}

    # 批次三 D：rejection_reason_distribution（门槛拒绝 + 限价单过期未成交）
    rejection_reasons: Dict[str, int] = {}
    for t in trades:
        if t.get("filled") or t.get("exit_reason"):
            continue  # 只看未成交明细
        unfilled = t.get("unfilled_reason", "")
        if not unfilled:
            continue
        if unfilled.startswith("rejected:"):
            # 解析 "rejected:['reason1','reason2']" 格式——去掉前缀和括号后 split
            raw = unfilled[len("rejected:"):].strip()
            # 用简单 tokenizer：去掉 []，按逗号+空格 split，再 strip 掉引号
            cleaned = raw.strip("[]")
            if cleaned:
                for reason_token in cleaned.split(","):
                    reason = reason_token.strip().strip("'\"")
                    if reason:
                        rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
        elif unfilled == "expired_unfilled":
            rejection_reasons["expired_unfilled"] = rejection_reasons.get("expired_unfilled", 0) + 1
        else:
            # 其他如 no_klines、no_signal_limit_price 等
            rejection_reasons[unfilled] = rejection_reasons.get(unfilled, 0) + 1

    # 阻断 #5：胜率 / 盈亏比 / Profit Factor —— 全部改用聚合 trade 的 net_pnl（仓位级净盈亏）
    wins = [t for t in closed_trades if _d(t.get("net_pnl", "0")) > 0]
    losses = [t for t in closed_trades if _d(t.get("net_pnl", "0")) < 0]
    total_win = sum(_d(t.get("net_pnl", "0")) for t in wins)
    total_loss = abs(sum(_d(t.get("net_pnl", "0")) for t in losses))
    win_rate = (Decimal(len(wins)) / Decimal(n_closed)) if n_closed else Decimal("0")
    avg_win = (total_win / Decimal(len(wins))) if wins else Decimal("0")
    avg_loss = (total_loss / Decimal(len(losses))) if losses else Decimal("0")
    pf = (total_win / total_loss) if total_loss > 0 else (Decimal("inf") if total_win > 0 else Decimal("0"))
    # 全程 Decimal 运算，最终展示转 float
    expectancy_dec = (win_rate * avg_win + (Decimal("1") - win_rate) * (-avg_loss)) if n_closed else Decimal("0")

    # 最大回撤：遍历盯市 equity_history，对每个点计算历史 peak 到当前的百分比跌幅
    # 阻断 #4：equity_history 已升级为三元组 (iso_str, marked_equity_str, has_open_position_bool)
    eq_hist = profile_result.get("equity_history", [])
    max_dd = _compute_max_drawdown(eq_hist, initial_equity)

    # 最大连续亏损
    max_consec_loss = _compute_max_consecutive_loss(closed_trades)

    # 最差单笔
    worst_loss = min((_d(t["net_pnl"]) for t in closed_trades), default=Decimal("0"))

    # 平均持仓时长
    holding_secs = [int(t.get("holding_seconds", 0)) for t in closed_trades]
    avg_holding = (sum(holding_secs) / len(holding_secs)) if holding_secs else 0

    # MAE / MFE（取平均）
    maes = [_d(t["mae"]) for t in closed_trades if t.get("mae") is not None]
    mfes = [_d(t["mfe"]) for t in closed_trades if t.get("mfe") is not None]
    avg_mae = (sum(maes) / len(maes)) if maes else Decimal("0")
    avg_mfe = (sum(mfes) / len(mfes)) if mfes else Decimal("0")

    # 退出占比
    exit_counts: Dict[str, int] = {}
    for t in closed_trades:
        r = t.get("exit_reason") or "unknown"
        exit_counts[r] = exit_counts.get(r, 0) + 1

    # LONG / SHORT 分组
    long_trades = [t for t in closed_trades if t.get("direction") == "LONG"]
    short_trades = [t for t in closed_trades if t.get("direction") == "SHORT"]

    # 活跃 / 非活跃时段分组（从 replay_cfg 读取窗口，兜底 7/20）
    active, inactive = [], []
    start_h, end_h = (replay_cfg or {}).get("tradfi_active_hours_utc", [7, 20])
    for t in closed_trades:
        et = t.get("exit_time", "")
        try:
            dt = datetime.fromisoformat(et.replace("Z", "+00:00"))
        except Exception:
            dt = datetime.now(UTC)
        h = dt.hour
        if start_h <= h < end_h:
            active.append(t)
        else:
            inactive.append(t)

    # 准入门槛
    admission_checks = _admission_checks(
        closed_trades=closed_trades,
        net_pnl=net_pnl,
        pf=pf,
        max_dd_pct=max_dd,
        invariant_violations=engine_meta["invariant_violations"],
        final_equity=final_equity,
        initial_equity=initial_equity,
        total_notional=total_notional,
        replay_cfg=replay_cfg or {},
    )

    return {
        "n_closed_trades": n_closed,
        "n_unfilled": n_unfilled,
        # P2 增强：虚拟订单总数 / 成交率 / 截止未到期挂单数
        "n_filled_orders": n_filled_orders,
        "n_pending_at_cutoff": n_pending_at_cutoff,
        "n_total_virtual_orders": n_total_virtual_orders,
        "fill_rate": fill_rate,
        "fill_rate_computable": fill_rate_computable,
        "realized_pnl": str(realized_pnl),
        "fee_paid": str(fee_paid),
        "funding_fee": str(funding_fee),
        "slippage_cost": str(slippage_cost),
        "net_pnl": str(net_pnl),
        "final_equity": str(final_equity),
        # 批次三 D：入场名义价值（聚合仓位级）+ 退出批次累计（交叉校验）
        "total_notional": str(total_notional),
        "total_exit_notional": str(total_exit_notional),
        "win_rate": float(win_rate),
        "avg_win": str(avg_win),
        "avg_loss": str(avg_loss),
        "profit_factor": float(pf) if pf != Decimal("inf") else None,
        "expectancy": float(expectancy_dec),
        "max_drawdown_pct": float(max_dd),
        "max_consecutive_loss": max_consec_loss,
        "worst_loss": str(worst_loss),
        "avg_holding_seconds": avg_holding,
        "avg_mae": str(avg_mae),
        "avg_mfe": str(avg_mfe),
        "exit_breakdown": exit_counts,
        "long_trades": len(long_trades),
        "short_trades": len(short_trades),
        "active_trades": len(active),
        "inactive_trades": len(inactive),
        "counterfactual_15pct": counterfactual.get("15pct"),
        "counterfactual_25pct": counterfactual.get("25pct"),
        "counterfactual_30pct": counterfactual.get("30pct"),
        # 批次三 D：三档滑点压力测试
        "slippage_pressure": slippage_pressure,
        # 批次三 D：decision_stats（来自 engine.decision_summary.per_profile 对应 pid）
        "decision_stats": decision_stats,
        # 批次三 D：门槛拒绝原因 + 限价单过期未成交分布
        "rejection_reason_distribution": rejection_reasons,
        "admission_checks": admission_checks,
    }


def _compute_max_drawdown(equity_history: List, initial_equity: Any) -> float:
    """阻断 #2 + #4：遍历盯市 equity_history（三元组），返回最大回撤百分比。

    阻断 #2：以 initial_equity（账户起点）作为 peak 的下界，
    即使首条盯市权益已跌破初始权益，也能正确算出从初始权益开始的回撤。

    equity_history 现在是 List[Tuple[iso_str, marked_equity_str, has_open_position_bool]]，
    第二字段为 marked_equity（含未实现盈亏的盯市价）。遍历取第二字段做 peak-drawdown。
    没数据 → 返回 0.0；initial_equity <= 0 时按 peak > 0 保护处理。
    """
    if not equity_history:
        return 0.0
    # 阻断 #2：initial_equity 是账户起点，必须先进入 peak
    init_eq = _d(initial_equity)
    # 阻断 #4：三元组第二字段 = marked_equity（第一字段是 iso_str）
    first_row = equity_history[0]
    first_eq = _d(first_row[1] if len(first_row) >= 2 else first_row[0])
    # peak 初始化为 max(初始权益, 首条盯市权益)，确保回撤以账户真实起点为锚
    peak = max(init_eq, first_eq)
    max_dd = Decimal("0")
    for row in equity_history:
        eq = _d(row[1] if len(row) >= 2 else row[0])
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * Decimal("100") if peak > 0 else Decimal("0")
        if dd > max_dd:
            max_dd = dd
    return float(max_dd)


def _compute_max_consecutive_loss(closed_trades: List[Dict[str, Any]]) -> int:
    """阻断 #5：改用聚合 trade 的 net_pnl（仓位级净盈亏）。"""
    best = 0
    cur = 0
    for t in closed_trades:
        pnl = _d(t.get("net_pnl", "0"))
        if pnl < 0:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


def _admission_checks(
    closed_trades: List[Dict[str, Any]],
    net_pnl: Decimal,
    pf: Decimal,
    max_dd_pct: float,
    invariant_violations: List[Any],
    final_equity: Decimal,
    initial_equity: Decimal,
    total_notional: Decimal,
    replay_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """七项准入门槛逐条判定。阈值从 replay_cfg['admission'] 读取，禁止硬编码。"""
    adm = replay_cfg.get("admission", {})
    min_closed = int(adm.get("min_closed_trades", 12))
    min_pf = Decimal(str(adm.get("min_profit_factor", 1.20)))
    max_dd = Decimal(str(adm.get("max_drawdown_pct", 15)))
    cap_preservation_pct = Decimal(str(adm.get("capital_preservation_pct", 30)))
    baseline_bps = Decimal(str(replay_cfg.get("slippage", {}).get("baseline_bps", 2)))

    checks: Dict[str, Any] = {}

    # ① 有效已结交易 >= min_closed_trades
    n = len(closed_trades)
    checks["min_closed_trades"] = {
        "threshold": min_closed, "actual": n, "pass": n >= min_closed,
    }

    # ② 净盈利 > 0
    checks["net_profit_positive"] = {
        "threshold": "> 0", "actual": str(net_pnl), "pass": net_pnl > 0,
    }

    # ③ Profit Factor >= min_profit_factor（pf 可能为 Decimal("inf")，inf >= min_pf 自然过）
    pf_is_finite = pf != Decimal("inf")
    checks["profit_factor"] = {
        "threshold": float(min_pf),
        "actual": float(pf) if pf_is_finite else "inf",
        "pass": pf >= min_pf,
    }

    # ④ 最大回撤 <= max_drawdown_pct
    checks["max_drawdown"] = {
        "threshold": float(max_dd), "actual": max_dd_pct,
        "pass": Decimal(str(max_dd_pct)) <= max_dd,
    }

    # ⑤ 无不变量违反
    checks["no_invariant_violations"] = {
        "threshold": "无违反",
        "actual": len(invariant_violations),
        "pass": len(invariant_violations) == 0,
    }

    # ⑥ 扣除最佳单笔后无结构性亏损
    best_idx = None
    best_val = Decimal("-Infinity")
    for i, t in enumerate(closed_trades):
        v = _d(t["net_pnl"])
        if v > best_val:
            best_val = v
            best_idx = i
    remaining_pnl = sum(_d(t["net_pnl"]) for i, t in enumerate(closed_trades) if i != best_idx)
    checks["no_structural_loss_after_best_removed"] = {
        "threshold": "剩余组合净盈亏 >= 0",
        "actual": str(remaining_pnl),
        "pass": remaining_pnl >= Decimal("0"),
    }

    # ⑦ 2× 滑点压力下不触发最终亏损红线
    # multiplier 从 admission.slippage_pressure_multiplier 读取，兜底 2
    # multiplier=2 → 新增 baseline_bps（= 2bps）；mult=3 → 新增 2×baseline（= 4bps）
    mult = Decimal(str(adm.get("slippage_pressure_multiplier", 2)))
    added_bps = baseline_bps * (mult - Decimal("1"))
    pressure_equity = pressure_test_final_equity(final_equity, total_notional, added_bps)
    breach_line = initial_equity * (Decimal("1") - cap_preservation_pct / Decimal("100"))
    checks["no_capital_preservation_breach_at_2x_slippage"] = {
        "threshold": f"{int(cap_preservation_pct)}% 亏损红线（{mult}× 滑点压力）",
        "final_equity_baseline": str(final_equity),
        "final_equity_under_pressure": str(pressure_equity),
        "breach_line": str(breach_line),
        "pass": pressure_equity >= breach_line,
    }

    return checks


def decide_admission(
    metrics_orig: Dict[str, Any],
    metrics_aggr: Dict[str, Any],
) -> Dict[str, Any]:
    """返回 {"admission": "original" | "aggressive" | "none", ...}。
    两 profile 都过 → 选风险调整后期望更高、回撤更小者。
    """
    orig_pass = all(c["pass"] for c in metrics_orig["admission_checks"].values())
    aggr_pass = all(c["pass"] for c in metrics_aggr["admission_checks"].values())

    admission = "none"
    if orig_pass and aggr_pass:
        # 都过 → 选回撤小 / 期望高者
        orig_dd = metrics_orig["max_drawdown_pct"]
        aggr_dd = metrics_aggr["max_drawdown_pct"]
        orig_exp = metrics_orig["expectancy"]
        aggr_exp = metrics_aggr["expectancy"]
        # 主规则：回撤更小者优先；若相等或差异微小，比期望
        if orig_dd < aggr_dd:
            admission = "original"
        elif aggr_dd < orig_dd:
            admission = "aggressive"
        else:
            admission = "original" if orig_exp >= aggr_exp else "aggressive"
    elif orig_pass:
        admission = "original"
    elif aggr_pass:
        admission = "aggressive"

    return {
        "admission": admission,
        "original_passed": orig_pass,
        "aggressive_passed": aggr_pass,
    }


def write_reports(
    engine_result: Dict[str, Any],
    run_manifest: Dict[str, Any],
    output_dir: str,
    replay_config_path: Optional[str] = None,
    data_completeness: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """计算指标、准入判定，落盘 .md + .json。返回汇总 dict。

    Args:
        data_completeness: 快照数据完整性信息（来自 snapshot.verify_snapshot）。
            格式 {"gaps": int, "complete": bool}。
            - gaps==0 或 data_completeness 为 None → 正常走 decide_admission 契约
            - gaps>0 且 complete=False → admission 强制 "insufficient_data"，
              不再信任 decide_admission 给出的 "original"/"aggressive"/"none" 结论。
              main.py 的 fail-closed 已在 gaps>0 且未 --allow-gaps 时提前返回，
              此处为二级安全网，防止未来其他调用者绕过 main.py。
    """
    engine_meta = engine_result["engine_metadata"]

    # 加载回放 config（默认用同包 config.yaml）
    if replay_config_path is None:
        import os as _os
        replay_config_path = _os.path.join(_os.path.dirname(__file__), "config.yaml")
    replay_cfg: Dict[str, Any] = {}
    try:
        with open(replay_config_path, "r", encoding="utf-8") as f:
            replay_cfg = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("回放 config 加载失败（使用空 dict）：%s", e)

    metrics_orig = compute_metrics(
        engine_result["xau_original"], engine_meta, replay_cfg,
        decision_stats=engine_meta.get("decision_summary", {}).get("per_profile", {}).get("xau_original", {}),
    )
    metrics_aggr = compute_metrics(
        engine_result["xau_aggressive"], engine_meta, replay_cfg,
        decision_stats=engine_meta.get("decision_summary", {}).get("per_profile", {}).get("xau_aggressive", {}),
    )
    admission_result = decide_admission(metrics_orig, metrics_aggr)

    # —— 二级安全网：快照数据不完整 → admission 强制 insufficient_data ——
    if data_completeness is not None:
        gaps = data_completeness.get("gaps", 0)
        complete = data_completeness.get("complete", gaps == 0)
        if gaps > 0 and not complete:
            logger.warning(
                "快照数据不完整（gaps=%d），admission 强制 insufficient_data，"
                "不再信任 decide_admission 结果",
                gaps,
            )
            admission_result = {
                "admission": "insufficient_data",
                "original_passed": admission_result.get("original_passed", False),
                "aggressive_passed": admission_result.get("aggressive_passed", False),
            }

    run_id = run_manifest.get("run_id", "unknown")
    replay_run_id = engine_meta["replay_run_id"]

    json_report = {
        "run_id": run_id,
        "replay_run_id": replay_run_id,
        "engine_version": engine_meta["engine_version"],
        "manifest_sha256": engine_meta["manifest_sha256"],
        "input_cutoff_at": engine_meta["input_cutoff_at"],
        "initial_equity": engine_meta["initial_equity"],
        "symbol_filters": engine_meta["symbol_filters"],
        "invariant_violations": engine_meta["invariant_violations"],
        "data_completeness": data_completeness or {"gaps": 0, "complete": True},
        # 批次三 D：从 engine_metadata 透传的元数据
        "snapshot_gaps": engine_meta.get("snapshot_gaps", 0),
        "trailing": engine_meta.get("trailing", {}),
        "decision_summary": engine_meta.get("decision_summary", {}),
        "xau_original": metrics_orig,
        "xau_aggressive": metrics_aggr,
        "admission": admission_result,
    }

    import os
    os.makedirs(output_dir, exist_ok=True)

    json_path = os.path.join(output_dir, f"report_{run_id}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_report, f, indent=2, ensure_ascii=False)

    md = _build_markdown(json_report)
    md_path = os.path.join(output_dir, f"report_{run_id}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    logger.info("报告已写入：%s, %s", md_path, json_path)
    return {"md_path": md_path, "json_path": json_path, "admission": admission_result["admission"]}


def _build_markdown(report: Dict[str, Any]) -> str:
    """构造人读 Markdown。批次三 D：增加 §9 要求的决策统计、
    门槛拒绝、三档滑点、同点差异渲染。"""
    lines = []
    lines.append(f"# XAUUSDT MTCPS 影子回放报告")
    lines.append("")
    lines.append(f"- **run_id**: `{report['run_id']}`")
    lines.append(f"- **replay_run_id**: `{report['replay_run_id']}`")
    lines.append(f"- **engine_version**: {report['engine_version']}")
    lines.append(f"- **manifest_sha256**: `{report['manifest_sha256']}`")
    lines.append(f"- **input_cutoff_at**: {report['input_cutoff_at']}")
    lines.append(f"- **initial_equity**: {report['initial_equity']} USDT")
    # 批次三 D：trailing 降级标注、快照缺口
    trailing_meta = report.get("trailing", {})
    if trailing_meta:
        lines.append(
            f"- **trailing**: vol_adj_fallback={trailing_meta.get('vol_adj_fallback_used', True)}, "
            f"value={trailing_meta.get('vol_adj_value', '1.0')}, "
            f"flat_close={trailing_meta.get('trailing_flat_close', True)}"
        )
    lines.append(f"- **snapshot_gaps**: {report.get('snapshot_gaps', 0)}")
    lines.append("")
    lines.append("## 准入结论")
    admission = report["admission"]["admission"]
    if admission != "none":
        lines.append(f"**{admission}**")
    else:
        lines.append("**暂不准入 (none)**")
    lines.append(f"- 原版通过: {'是' if report['admission']['original_passed'] else '否'}")
    lines.append(f"- 激进版通过: {'是' if report['admission']['aggressive_passed'] else '否'}")
    lines.append("")

    for pid, label in [("xau_original", "原版 xau_original"), ("xau_aggressive", "激进版 xau_aggressive")]:
        m = report[pid]
        lines.append(f"## {label} 指标")
        lines.append("")
        lines.append(f"| 指标 | 值 |")
        lines.append(f"|------|----|")
        lines.append(f"| 有效已结交易 | {m['n_closed_trades']} |")
        lines.append(f"| 未成交 | {m['n_unfilled']} |")
        # P2 增强：虚拟订单总数 / 成交率 / 截止未到期挂单数
        if "n_total_virtual_orders" in m:
            lines.append(f"| 虚拟订单总数 | {m['n_total_virtual_orders']} |")
            # 成交率 None → 分母为 0，不可计算
            if m.get("fill_rate_computable") and m.get("fill_rate") is not None:
                lines.append(f"| 成交率 | {m['fill_rate']:.1%} |")
            else:
                lines.append(f"| 成交率 | 不可计算 |")
            lines.append(f"| 截止未到期挂单数 | {m['n_pending_at_cutoff']} |")
        # 批次三 D：入场名义价值（聚合仓位级）
        if "total_notional" in m:
            lines.append(f"| 入场名义价值（聚合仓位级） | {m['total_notional']} |")
        lines.append(f"| 净盈亏 | {m['net_pnl']} |")
        lines.append(f"| 手续费 | {m['fee_paid']} |")
        lines.append(f"| 资金费 | {m['funding_fee']} |")
        lines.append(f"| 滑点 | {m['slippage_cost']} |")
        lines.append(f"| 胜率 | {m['win_rate']:.1%} |")
        lines.append(f"| 盈亏比 (PF) | {m['profit_factor']} |")
        lines.append(f"| 单笔期望 | {m['expectancy']:.4f} |")
        lines.append(f"| 最大回撤 | {m['max_drawdown_pct']:.2f}% |")
        lines.append(f"| 最大连续亏损 | {m['max_consecutive_loss']} |")
        lines.append(f"| 平均持仓时长 | {m['avg_holding_seconds']:.0f} 秒 |")
        lines.append("")

        # 批次三 D：§9 决策统计（调度次数、分析成功率、候选信号数）
        ds = m.get("decision_stats", {}) or {}
        if ds:
            lines.append("### §9 决策统计")
            lines.append(f"- 调度次数（scheduled_runs）: {ds.get('scheduled_runs', 0)}")
            lines.append(f"- 候选信号数: {ds.get('candidate', 0)}")
            lines.append(f"- no_signal: {ds.get('no_signal', 0)}")
            lines.append(f"- analysis_error: {ds.get('analysis_error', 0)}")
            # 阻断修复：零调度事件时 analysis_success_rate=None 且 computable=False，
            # 不能用 .1% 格式化，否则 TypeError。改为显式渲染「不可计算」。
            if not ds.get("analysis_success_rate_computable", True) or ds.get("analysis_success_rate") is None:
                note = ds.get("analysis_sample_note", "样本不足")
                lines.append(f"- 分析成功率: 不可计算（样本不足：{note}）")
            else:
                lines.append(f"- 分析成功率: {ds['analysis_success_rate']:.1%}")
            # no_signal reason breakdown（如果有）
            reasons = ds.get("no_signal_reasons", {}) or {}
            if reasons:
                lines.append(f"- no_signal 原因分布:")
                for rc, cnt in sorted(reasons.items()):
                    lines.append(f"  - `{rc}`: {cnt}")
            lines.append("")

        # 批次三 D：门槛拒绝原因分布 + 限价单过期未成交
        rej = m.get("rejection_reason_distribution", {}) or {}
        if rej:
            lines.append("### 门槛拒绝原因分布（未成交订单）")
            for reason, cnt in sorted(rej.items()):
                lines.append(f"- `{reason}`: {cnt}")
            lines.append("")

        # 批次三 D：三档滑点压力测试
        slip_pressure = m.get("slippage_pressure", {}) or {}
        if slip_pressure:
            lines.append("### 三档滑点压力测试")
            lines.append(
                f"- baseline={slip_pressure.get('baseline_bps', 2)}bps, "
                f"tiers={slip_pressure.get('tiers_bps', [])}bps"
            )
            final_eq = m.get("final_equity", "?")
            lines.append(f"- final_equity (baseline={slip_pressure.get('baseline_bps', 2)}bps): {final_eq}")
            for tier_key in slip_pressure.get("tiers_bps", []):
                k = f"{tier_key}bps"
                lines.append(f"- equity@{k}: {slip_pressure.get(k, '?')}")
            lines.append("")

        lines.append("### 准入门槛")
        for key, v in m["admission_checks"].items():
            status = "✅" if v["pass"] else "❌"
            label = key
            # 兼容不同结构：有的 key 有 actual，有的有细字段
            if "actual" in v:
                lines.append(f"- {status} **{label}**: 阈值 {v['threshold']}，实际 {v['actual']}")
            elif "final_equity_under_pressure" in v:
                lines.append(
                    f"- {status} **{label}**: 基准权益 {v['final_equity_baseline']}, "
                    f"2× 滑点压力后 {v['final_equity_under_pressure']}, 红线 {v['breach_line']}"
                )
            else:
                lines.append(f"- {status} **{label}**: {v}")
        lines.append("")

    # 批次三 D：原版/激进同点决策差异
    ds_top = report.get("decision_summary", {}) or {}
    diff = ds_top.get("same_timestamp_decision_diff", {}) or {}
    common_cnt = diff.get("common_scheduled_count", 0)
    diff_cnt = diff.get("differing_count", 0)
    if common_cnt > 0:
        lines.append("## 原版 vs 激进版 同点决策差异")
        lines.append(f"- 共用 scheduled_at 数: {common_cnt}")
        lines.append(f"- (decision, direction, grade) 三元组不同的时点数: {diff_cnt} "
                     f"({diff.get('differing_ratio', 0):.1%})")
        samples = diff.get("samples", []) or []
        if samples:
            lines.append(f"- 样本（最多前 20 条）:")
            for s in samples:
                orig_d = s.get("original", {})
                aggr_d = s.get("aggressive", {})
                lines.append(
                    f"  - {s['scheduled_at']}: original={orig_d}, aggressive={aggr_d}"
                )
        lines.append("")

    lines.append("## 反事实停牌标记（不截断路径）")
    for pid, label in [("xau_original", "原版"), ("xau_aggressive", "激进版")]:
        m = report[pid]
        lines.append(
        f"- {label}: 15%={m['counterfactual_15pct']}, "
        f"25%={m['counterfactual_25pct']}, 30%={m['counterfactual_30pct']}"
    )
    lines.append("")

    if report["invariant_violations"]:
        lines.append("## ⚠️ 不变量违规")
        for v in report["invariant_violations"]:
            lines.append(f"- {v}")
        lines.append("")

    return "\n".join(lines)
