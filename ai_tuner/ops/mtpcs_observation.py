"""Prepare the MTPCS section for the existing 07:35 daily digest."""

import argparse
from datetime import datetime
import json
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


def beijing(stamp):
    if not stamp:
        return "暂无"
    from datetime import timezone
    parsed = datetime.fromisoformat(stamp)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(
        digest_outbox.TZ).strftime("%m-%d %H:%M")


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


def arm_line(row):
    label = ARMS[row["arm"]]
    closed = row["closed"]
    text = f"{label} {row['candidates']}候选/{closed}结算"
    if holding := row["open"] + row["pending"]:
        text += f"/{holding}在途"
    if closed:
        text += (f"，净 {row['net_r']:+.2f}R，胜 {row['wins']}/{closed}，"
                 f"结算回撤 {row['closed_sequence_drawdown_r']:.2f}R")
    return text


def render(entries, shadow, errors):
    lines = ["首笔实盘入场", *first_lines(entries), "", "影子账"]
    if shadow:
        lines.append(f"{beijing(shadow['first_utc'])} 至 {beijing(shadow['last_utc'])}，"
                     f"{shadow['hours']} 个小时，缺 {shadow['missing_count']} 小时，"
                     f"不完整 {len(shadow['incomplete_hours_utc'])} 小时")
        for variant, name in STRATEGIES.items():
            items = [arm_line(row) for row in shadow["arms"] if row["variant"] == variant]
            lines.append(name + " · " + "｜".join(items))
        aligned = shadow["alignment"]
        allowed = aligned.get("allowed", 0)
        lines.append(f"激进版方向一致性 · 通过 {allowed}，拦截 {sum(aligned.values()) - allowed}")
        if not all(row["closed"] for row in shadow["arms"]):
            lines.append("样本继续积累，未结算组暂无收益。")
    else:
        lines.append("本轮影子账读取失败。")
    if errors:
        lines.append("读取告警 · " + "、".join(errors))
    return "\n".join(lines)


def prepare(state_dir, ledger, *, persist):
    state_file = state_dir / "first_entries.json"
    cached = {}
    entries, shadow, errors = {}, None, []
    try:
        if state_file.exists():
            cached = json.loads(state_file.read_text())
        verified = {key: row for key, row in cached.items() if row and row["state"] == "verified"}
        entries = verified
        if set(verified) != set(STRATEGIES):
            result = read_json(
                ["docker", "exec", "-i", "trading_system-btc_eth_aggressive", "python", "-"],
                source=(OPS / "mtpcs_entry_probe.py").read_bytes(), timeout=100,
            )
            entries = merge_entries(verified, result["strategies"])
            if persist:
                write_evidence(state_file, entries)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        errors.append(f"成交与归因（{type(exc).__name__}）")
    try:
        shadow = read_json([sys.executable, str(OPS / "mtpcs_shadow_summary.py"), str(ledger)], timeout=24)
        if shadow["missing_count"] or shadow["incomplete_hours_utc"]:
            errors.append("影子数据有缺口")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        errors.append(f"影子账（{type(exc).__name__}）")
    if any(row and row["state"] != "verified" for row in entries.values()):
        errors.append("已有成交待对账")
    return render(entries, shadow, errors), errors


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
