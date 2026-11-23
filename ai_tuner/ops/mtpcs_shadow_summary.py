"""Read the shadow ledger inside its container and emit an aggregate snapshot."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3
import sys
from pathlib import Path

VARIANTS = ("btc_eth", "btc_eth_aggressive")
BASE_ARMS = ("trend", "mean_reversion", "trend_fibonacci", "trend_ordinary")
START = datetime(2026, 11, 23, 6)
HOUR = timedelta(hours=1)

ledger = Path(sys.argv[1]).resolve()
conn = sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)
rows = conn.execute("SELECT at, state, records FROM hours ORDER BY at").fetchall()
checked_at = datetime.now(timezone.utc)
expected = (checked_at - timedelta(minutes=10)).replace(minute=0, second=0, microsecond=0, tzinfo=None)

slots = []
states = Counter()
rejects = Counter()
candidates = Counter()
returns = defaultdict(list)
entry_gaps = Counter()
alignment = Counter()
incomplete = []
for stamp, _, payload in rows:
    at = datetime.fromisoformat(stamp)
    slots.append(at)
    records = json.loads(payload)
    if len({(r["variant"], r["symbol"]) for r in records}) != 10:
        incomplete.append(stamp)
    for record in records:
        variant = record["variant"]
        decision = record["candidate"]
        state = decision.get("market_state")
        states[variant, state] += 1
        if state == "STRONG_TREND" and (reason := decision.get("reject_reason")):
            rejects[variant, reason.split("(")[0]] += 1
        for arm in record["arms"]:
            candidates[variant, arm] += 1
        if variant == VARIANTS[1] and "mean_reversion" in record["arms"]:
            if record["alignment_passed"]:
                alignment["allowed"] += 1
            else:
                alignment[record["alignment_reason"] or "missing_reason"] += 1
        for arm, exit in record["exits"].items():
            if exit["net_r"] is None:
                entry_gaps[variant, arm] += 1
            else:
                returns[variant, arm].append(exit["net_r"])

unique = set(slots)
missing = []
probe = START
while probe <= expected:
    if probe not in unique and (probe != expected or checked_at.minute >= 12):
        missing.append(probe.isoformat())
    probe += HOUR

state = json.loads(rows[-1][1])["positions"] if rows else {}
arms = []
for variant in VARIANTS:
    for arm in (*BASE_ARMS, *(("mean_reversion_aligned",) if variant == VARIANTS[1] else ())):
        pnl = returns[variant, arm]
        series = [x for x in pnl if math.isfinite(x)]
        equity = peak = drawdown = 0.0
        for gain in series:
            equity += gain
            peak = max(equity, peak)
            drawdown = max(drawdown, peak - equity)
        current = [value for key, value in state.items()
                   if key.startswith(variant + ":") and key.endswith(":" + arm)]
        arms.append(dict(
            variant=variant, arm=arm, candidates=candidates[variant, arm], closed=len(series),
            wins=sum(x > 0 for x in series), net_r=round(equity, 6) if series else None,
            mean_r=round(equity / len(series), 6) if series else None,
            closed_sequence_drawdown_r=round(drawdown, 6) if series else None,
            pending=sum("pending" in v for v in current),
            open=sum("position" in v for v in current),
            entry_gaps=entry_gaps[variant, arm],
        ))

print(json.dumps(dict(
    checked_at_utc=checked_at.isoformat(), first_utc=slots[0].isoformat() if slots else None,
    last_utc=slots[-1].isoformat() if slots else None, expected_latest_utc=expected.isoformat(),
    hours=len(slots), missing_count=len(missing), missing_hours_utc=missing[-48:],
    incomplete_hours_utc=incomplete, regimes=[(v, s, n) for (v, s), n in states.items()],
    trend_rejections=[(v, r, n) for (v, r), n in rejects.most_common()],
    arms=arms, alignment=dict(alignment),
), ensure_ascii=False, allow_nan=False))
