"""Prepare the MTPCS section for the existing 07:35 daily digest."""

import argparse
from datetime import date, datetime, timezone
import json
from math import ceil
from pathlib import Path
import subprocess
import sys
import tempfile

try:
    from ai_tuner.ops import digest_outbox
except ImportError:  # Host service runs this file as a script.
    import digest_outbox

OPS = Path(__file__).resolve().parent
STATE = Path("/var/lib/trading-digest/mtpcs")
LEDGER = Path("/root/trading_system/data/mtpcs-direction-shadow/entries.sqlite3")
STRATEGIES = {"btc_eth": "原版", "btc_eth_aggressive": "激进版"}
ARMS = {
    "trend": "趋势基线", "mean_reversion": "均值回归",
    "trend_fibonacci": "斐波那契", "trend_ordinary": "普通回调",
    "mean_reversion_aligned": "只加方向一致性",
}
INTENTS = {"trend": "趋势", "mean_reversion": "均值回归"}
CHECKS = {
    "intent": "开仓意图", "signal_matches_order": "信号与订单关联",
    "signed_fill_quantity_matches": "已成交数量", "fact_ledger_has_all_signed_fills": "分笔成交账本",
    "exclusive_owner": "唯一策略属主",
}


def beijing(stamp, fmt="%m-%d %H:%M"):
    if not stamp:
        return "暂无"
    parsed = datetime.fromisoformat(stamp)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(
        digest_outbox.TZ).strftime(fmt)


def read_json(command, *, source=None, timeout=60):
    result = subprocess.run(command, input=source, capture_output=True, timeout=timeout, check=True)
    return json.loads(result.stdout)


def write_evidence(path, evidence):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        temp = Path(handle.name)
        try:
            json.dump(evidence, handle, ensure_ascii=False)
            handle.flush()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def merge_entries(previous, observed):
    if set(observed) != set(STRATEGIES):
        raise ValueError("Unexpected MTPCS strategy set")
    merged = dict(observed)
    for key, row in previous.items():
        if row and row["state"] == "verified":
            next_row = observed[key]
            if next_row and row["order_id"] != next_row["order_id"]:
                raise ValueError("Previously verified first order does not match")
            merged[key] = row
    return merged


def first_lines(entries):
    lines = []
    for key, name in STRATEGIES.items():
        if key not in entries:
            lines.append(f"{name} · 本轮实盘证据读取失败")
            continue
        row = entries[key]
        if row is None:
            lines.append(f"{name} · 等待发布后首笔真实入场成交")
            continue
        verified = row["state"] == "verified"
        intent = INTENTS.get(row.get("intent"), "待核")
        direction = {"LONG": "多", "SHORT": "空"}.get(row.get("direction"), "待核")
        lines.append(f"{name} · {'已验收' if verified else '已成交待对账'}，"
                     f"{beijing(row['filled_at_utc'])} {row['symbol']} {direction} {intent}，"
                     f"订单 {row['order_id']}，成交 {row['filled_qty']}")
        if not verified:
            missing = [title for check, title in CHECKS.items() if row["checks"].get(check) is not True]
            if missing:
                lines.append("   待补 " + "、".join(missing))
    return lines


def observation_period(start, cutoff):
    fmt = "%Y-%m-%d %H:%M"
    return f"北京时间 {beijing(start, fmt)} 至 {beijing(cutoff, fmt)}"


def win_rate(row):
    closed = row["closed"]
    if not closed:
        return "胜率暂无"
    wins = row["wins"]
    return f"胜率 {wins / closed:.1%}（{wins}/{closed}）"


def arm_line(row):
    label = ARMS[row["arm"]]
    closed = row["closed"]
    text = f"{label} {row['candidates']}候选/{closed}结算"
    if holding := row["open"] + row["pending"]:
        text += f"/{holding}在途"
    text += f"，{win_rate(row)}"
    if closed:
        text += f"，净 {row['net_r']:+.2f}R，结算回撤 {row['closed_sequence_drawdown_r']:.2f}R"
    return text


# The first look is descriptive. Scheduled evaluation has a separate time window.
FIRST_LOOK = 30
MIN_RATE_DAYS = 3
EARLIEST_REVIEW = date(2027, 2, 1)
REPLAY_DAYS = 33
REPLAY_REVERSION_COUNTS = {"btc_eth": (11, 4), "btc_eth_aggressive": (104, 103)}


def eta(row, shadow):
    remaining = FIRST_LOOK - row["closed"]
    if remaining <= 0:
        return "已达初看样本"
    prefix = f"剩{remaining}笔"
    if row["arm"].startswith("trend") and not row.get("candidates"):
        return f"{prefix}，估时待候选"
    days = shadow.get("recent_complete_days", 0)
    if days >= MIN_RATE_DAYS:
        closes = row.get("recent_closed", 0)
        return (f"{prefix}，约{ceil(remaining * days / closes)}天"
                if closes else f"{prefix}，估时待结算")
    if row["arm"] == "mean_reversion":
        fastest, slowest = REPLAY_REVERSION_COUNTS[row["variant"]]
        low, high = (ceil(remaining * REPLAY_DAYS / n) for n in (fastest, slowest))
        span = f"{low}" if low == high else f"{low}–{high}"
        return f"{prefix}，历史回放约{span}天"
    return f"{prefix}，估时待完整观察日"


def daily_lines(entries, shadow):
    verified = sum(row is not None and row["state"] == "verified" for row in entries.values())
    lines = [f"当日日报结论 · 两版首笔实盘归因已验收 {verified}/2"]
    day = shadow.get("daily")
    if day is None:
        return lines
    if not day["covered_hours"]:
        return lines + [f"{day['date'][5:]} 无可用观察小时，当日结论待数据。"]
    lines.append(f"{day['date'][5:]} 采集 {day['covered_hours']}/24 小时，趋势候选 {day['trend_candidates']}，"
                 f"斐波那契/普通回调 {day['bands'][0]}/{day['bands'][1]}，回归结算 {day['reversion_closed']}。")
    if day["reversion_closed"]:
        items = [f"{STRATEGIES[k]} {v['net_r']:+.2f}R" for k, v in day.get("reversion_by_variant", {}).items()
                 if v["closed"]]
        if items:
            lines.append("当日回归结算净 R · " + "｜".join(items))
    lines.append(f"方向一致性通过 {day['alignment_passed']}、拦截 {day['alignment_blocked']}"
                 + ("，暂未观察到筛选差异。" if day["alignment_blocked"] == 0 else "。"))
    if not day["trend_candidates"]:
        lines.append("趋势组当日暂无候选，回调比例继续观察。")
    if any(row["closed"] < FIRST_LOOK for row in shadow["arms"]):
        lines.append("样本仍在积累，尚未形成分支优劣结论。")
    return lines


def overheat_lines(study):
    if not study:
        return ["过热单项对照 · 等待首个完整采集小时"]
    window = observation_period(study["start_utc"], study["cutoff_utc"])
    lines = [
        f"过热同期对照 · {window}（两组共同截止）",
        f"{study['observed_hours']} 小时，{study['candidates']} 候选，"
        f"原版重度过热阈值拦截 {study['blocked']} 个。",
    ]
    blocked = study["blocked_net_r"]
    if blocked is not None:
        lines.append(f"被拦候选在基线结算 {study['blocked_closes']} 笔，净 {blocked:+.2f}R。")
    else:
        lines.append("被拦候选在基线暂无结算。")
    parts = []
    for key, label in (("baseline", "同期激进基线"), ("filter", "只加过热禁开")):
        arm = study[key]
        part = f"{label} {arm['closed']} 笔结算，{win_rate(arm)}"
        if arm["closed"]:
            part += f"，净 {arm['net_r']:+.2f}R、结算回撤 {arm['closed_sequence_drawdown_r']:.2f}R"
        parts.append(part)
    return lines + ["｜".join(parts)]


def render(entries, shadow, errors, archive=None):
    lines = ["首笔实盘入场", *first_lines(entries)]
    if archive:
        cut = datetime.fromtimestamp(archive["covered_until_ms"] / 1000, timezone.utc)
        matches = archive["matched"]
        lines.append(f"订单归档 · 扫描至 {beijing(cut.isoformat())}，本轮原版 {matches['btc_eth']} 笔、"
                     f"激进版 {matches['btc_eth_aggressive']} 笔，已入库 {archive['persisted']} 笔。")
    lines.append("")
    if shadow:
        window = shadow["full_window"]
        lines.append(f"全量影子账 · {observation_period(window['start_utc'], window['cutoff_utc'])}")
        lines.append(f"采集 {shadow['hours']} 小时，缺 {shadow['missing_count']} 小时，"
                     f"不完整 {len(shadow['incomplete_hours_utc'])} 小时")
        for variant, name in STRATEGIES.items():
            items = [arm_line(row) for row in shadow["arms"] if row["variant"] == variant]
            lines.append(name + " · " + "｜".join(items))
        aligned = shadow["alignment"]
        allowed = aligned.get("allowed", 0)
        lines.append(f"激进版方向一致性 · 通过 {allowed}，拦截 {sum(aligned.values()) - allowed}")
        lines.extend(overheat_lines(shadow.get("overheat_study")))
        lines.extend(("", f"还要等多久 · 各组以 {FIRST_LOOK} 笔结算作初看，"
                      f"近7天有 {shadow.get('recent_complete_days', 0)} 个完整观察日"))
        for variant, name in STRATEGIES.items():
            items = [(ARMS[row["arm"]] + " " + eta(row, shadow))
                     for row in shadow["arms"] if row["variant"] == variant]
            if items:
                lines.append(name + " · " + "｜".join(items))
        if shadow.get("as_of_day"):
            remaining = (EARLIEST_REVIEW - date.fromisoformat(shadow["as_of_day"])).days
            if remaining > 0:
                lines.append(f"正式比较 · 距最早复评日 {remaining} 天，须有两个完整留出月和可比样本。")
            else:
                lines.append("正式比较 · 核对两个完整留出月及可比样本。")
        lines.extend(("", *daily_lines(entries, shadow)))
        if not all(row["closed"] for row in shadow["arms"]):
            lines.append("样本继续积累，未结算组暂无收益。")
    else:
        lines.append("本轮影子账读取失败。")
    if errors:
        lines.append("读取告警 · " + "、".join(errors))
    return "\n".join(lines)


def prepare(state_dir, ledger, *, persist):
    try:
        mode = read_json(["docker", "exec", "ai-tuner", "python", "-c",
                          "import json; from shared.binance_account_mode import configured_pm; "
                          "print(json.dumps('pm' if configured_pm() else 'classic'))"])
    except (OSError, ValueError, subprocess.SubprocessError):
        mode = None
    if mode == "classic":
        state_dir = state_dir / "classic"
    state_file = state_dir / "first_entries.json"
    cached = {}
    entries, shadow, archive, errors = {}, None, None, []
    try:
        if mode not in ('pm', 'classic'):
            raise ValueError("调优器账户模式未知")
        if state_file.exists():
            cached = json.loads(state_file.read_text())
        verified = {key: row for key, row in cached.items() if row and row["state"] == "verified"}
        entries = verified
        if set(verified) != set(STRATEGIES):
            result = read_json(
                ["docker", "exec", "-i", "trading_system-btc_eth_aggressive", "python", "-", mode],
                source=(OPS / "mtpcs_entry_probe.py").read_bytes(), timeout=100,
            )
            entries = merge_entries(verified, result["strategies"])
            if persist:
                write_evidence(state_file, entries)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        errors.append(f"成交与归因（{type(exc).__name__}）")
    try:
        if mode not in ('pm', 'classic'):
            raise ValueError("调优器账户模式未知")
        checkpoint = state_dir / "order_archive_checkpoint.json"
        cursor = json.loads(checkpoint.read_text())["covered_until_ms"] if checkpoint.exists() else ""
        archive = read_json(
            ["docker", "exec", "-i", "ai-tuner", "python", "-", str(cursor),
             "persist" if persist else "preview", mode],
            source=(OPS / "mtpcs_order_archive.py").read_bytes(), timeout=100,
        )
        if persist:
            write_evidence(checkpoint, {"covered_until_ms": archive["covered_until_ms"]})
        if not archive["up_to_date"]:
            errors.append("订单归档仍在追补")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        archive = None
        errors.append(f"订单归档（{type(exc).__name__}）")
    try:
        shadow = read_json([sys.executable, str(OPS / "mtpcs_shadow_summary.py"), str(ledger)], timeout=24)
        if shadow["missing_count"] or shadow["incomplete_hours_utc"]:
            errors.append("影子数据有缺口")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        errors.append(f"影子账（{type(exc).__name__}）")
    if any(row and row["state"] != "verified" for row in entries.values()):
        errors.append("已有成交待对账")
    return render(entries, shadow, errors, archive), errors


def prepare_digest(*, dry_run):
    text, errors = prepare(STATE, LEDGER, persist=not dry_run)
    entry = dict(text=text, status="warn" if errors else "ok", summary="、".join(errors))
    if dry_run:
        return entry
    digest_outbox.publish("mtpcs", **entry)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=STATE)
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    text, errors = prepare(args.state_dir, args.ledger, persist=not args.dry_run)
    if args.dry_run:
        print(text)
        return
    digest_outbox.publish("mtpcs", text, status="warn" if errors else "ok",
                          summary="、".join(errors))


if __name__ == "__main__":
    main()
