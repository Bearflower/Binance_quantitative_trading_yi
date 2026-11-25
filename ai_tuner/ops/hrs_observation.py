"""07:35 HRS 运行验收与研究观察，只读取生产证据。"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

try:
    from ai_tuner.ops import digest_outbox
except ImportError:
    import digest_outbox

ROOT = Path("/root/trading_system")
STORE = ROOT / "research/hrs-mode-observer/observations/receipts.sqlite"
OBSERVER = "trading_system-hrs-research-observer"


def output(args, timeout=20):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout).stdout


def fresh(stamp, now, max_age):
    elapsed = (now - dt.datetime.fromisoformat(stamp)).total_seconds()
    return -10 <= elapsed <= max_age


def acceptance(now):
    report = json.loads(output(["docker", "exec", "trading_system-hrs-acceptance",
                                "cat", "/app/logs/hrs-acceptance.json"]))
    issues = []
    if not fresh(report["generated_at"], now, 45 * 60):
        issues.append("验收报告过期")
    if report["database_snapshot_coherent"] is not True:
        issues.append("验收快照不一致")
    if issues:
        reason = report.get("source_error_type") or "、".join(issues)
        return f"P0 验收 · 本期进度未知（{reason}）。", issues
    t, f = report["technical"], report["financial"]
    verified = sum(s.get("status") == "RECONCILED" and s.get("engineering_status") == "READY"
                   for s in f["samples"])
    if min(f["observed_closed_lifecycles"], f["required_lifecycles"]) > verified:
        issues.append("闭仓待对账")
    text = (f"P0 验收 · 连续合格 {t['consecutive_complete_hours']}/{t['required_complete_hours']} 小时，"
            f"完整对账闭仓 {verified}/{f['required_lifecycles']}，实际观察闭仓 {f['observed_closed_lifecycles']}。")
    return text, issues


def quotes(now, path=STORE):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=4)) as db:
        db.execute("BEGIN")
        seen, = db.execute("SELECT value FROM scan_state WHERE key='scanned_at'").fetchone()
        counts = dict(db.execute("SELECT phase,count(*) FROM receipts WHERE status='ok' GROUP BY phase"))
        missing, = db.execute("SELECT count(*) FROM receipts WHERE status<>'ok'").fetchone()
        pair_count, = db.execute("""SELECT count(*) FROM receipts e JOIN receipts x USING(evidence_id)
            WHERE e.phase='entry' AND x.phase='exit_6h' AND e.status='ok' AND x.status='ok'""").fetchone()
        pending, overdue = db.execute("""SELECT count(*),count(CASE WHEN julianday(due_at)
            < julianday(?) THEN 1 END) FROM tasks""", ((now-dt.timedelta(seconds=60)).isoformat(),)).fetchone()
    issues = []
    if not fresh(seen, now, 120):
        issues.append("盘口心跳过期")
    if overdue:
        issues.append(f"盘口待采逾期 {overdue}")
    text = (f"LV-RM 盘口累计 · 入场 {counts.get('entry', 0)}，六小时退出 {counts.get('exit_6h', 0)}，"
            f"同信号双侧齐全 {pair_count}，缺失 {missing}，待采 {pending}。")
    if pair_count:
        text += " 已有配对样本可在本地按每边额外 0/5 基点复算。"
    return text, issues


def modes(now):
    revision = output(["docker", "inspect", "-f", '{{index .Config.Labels "hrs.observer.revision"}}',
                       OBSERVER]).strip()
    if not re.fullmatch("[0-9a-f]{40}", revision):
        raise ValueError("invalid observer revision")
    folder = ROOT / ".releases" / "hrs-mode-report" / revision
    if not folder.is_dir():
        folder = ROOT / ".deploy_tmp" / ("hrs-mode-observer-" + revision)
    report = json.loads(output([sys.executable, str(folder / "release/hrs-observer/activate.py"), str(folder),
                                revision, "--report-start", (now-dt.timedelta(days=7)).isoformat(),
                                "--report-end", now.isoformat()], timeout=55))
    values = []
    for key in ("standard", "emm", "semi_emm", "lv_rm", "unknown"):
        row = report["modes"][key]["live"]
        pnl = row["net_usdt"] if row["verified_net_count"] else "—"
        values.append(f"{key} {row['verified_net_count']}/{row['observed_closed']} 笔 {pnl}")
    coverage = "完整" if report["full_window_financial_coverage"] else "部分"
    return (f"近七日实盘 · 已核净额/观察闭仓笔数，净额 USDT（覆盖{coverage}）\n"
            + "｜".join(values)), []


# Read only scan identities. Outcomes and holdout returns are outside this query.
SCAN_PROBE = """
import asyncio, datetime as dt, json, os, sys
import asyncpg
async def main():
    db = await asyncpg.connect(os.environ['DATABASE_URL'], timeout=5, command_timeout=5)
    try:
        async with db.transaction(readonly=True, isolation='repeatable_read'):
            rows = {}
            for date in sys.argv[1:]:
                start = dt.datetime.fromisoformat(date + 'T00:00:00+08:00')
                row = await db.fetchrow(
                    "SELECT event_time,run_id FROM hrs.hrs_decision_evidence "
                    "WHERE kind='scan_start' AND event_time >= $1 AND event_time <= $2 "
                    "AND available_at <= now() AND event_time <= now() ORDER BY event_time,evidence_id LIMIT 1",
                    start, start + dt.timedelta(hours=1))
                rows[date] = [row['event_time'].isoformat(), row['run_id']] if row else None
            print(json.dumps(rows, default=str))
    finally:
        await db.close()
asyncio.run(main())
"""


def boundary_scans(now):
    day = now.astimezone(digest_outbox.TZ).date().isoformat()
    dates = [d for d in ("2026-11-24", "2026-12-01", "2027-01-01", "2027-02-01") if d <= day]
    if not dates:
        return "P1 固定边界 · 首个研究窗口尚未开始。", []
    rows = json.loads(output(["docker", "exec", OBSERVER, "python", "-c", SCAN_PROBE, *dates], timeout=30))
    values, warnings = [], []
    for date in dates:
        if rows[date]:
            stamp, run_id = rows[date]
            clock = dt.datetime.fromisoformat(stamp).astimezone(digest_outbox.TZ)
            values.append(f"{date} {clock:%H:%M:%S}（{run_id}）")
        else:
            warnings.append(f"{date} 容差内缺首扫描")
    return "P1 固定边界 · 已观察首扫描 " + ("、".join(values) or "暂无") + "。", warnings


def milestones(now):
    day = now.astimezone(digest_outbox.TZ).date().isoformat()
    if day < "2026-11-24":
        p1 = "待 11/24 00:00 固定首扫描"
    elif day < "2026-12-01":
        p1 = "研究窗内，核对 11/24 零点后第一实际扫描及一小时容差"
    else:
        p1 = "核验 11/24、12/01 固定首扫描，按 24 候选、每组至少 20 闭环及三档成本验证后本地冻结"
    if day >= "2026-12-14":
        p1 += "；核对 r8 政策衔接"
    if day >= "2026-12-18":
        p1 += "；核对满 30 天的真实权益覆盖"
    if day >= "2027-02-01":
        p1 += "；12 月和 1 月留出已到期，待按冻结凭证验证"
    if day < "2026-12-01":
        p2 = "发现期，12 月独立观察"
    elif now < dt.datetime(2027, 1, 1, 6, 1, tzinfo=digest_outbox.TZ):
        p2 = "12 月独立样本采集中，等待末笔退出窗口"
    else:
        p2 = "12 月样本已到导出时点，待本地核验双锚、成本与集中度"
    return f"P1 止盈止损 · {p1}。\nP2 回调研究 · {p2}。"


def prepare_digest(*, dry_run):
    now = dt.datetime.now(digest_outbox.TZ)
    lines, issues = [], []
    for label, reader in (("验收报告", acceptance), ("盘口", quotes), ("四模式账本", modes),
                          ("固定首扫描", boundary_scans)):
        try:
            line, warnings = reader(now)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
            issues.append(f"{label}读取失败（{type(exc).__name__}）")
        else:
            lines.append(line)
            issues.extend(warnings)
    lines.append(milestones(now))
    if issues:
        lines.append("读取告警 · " + "、".join(issues))
    entry = dict(text="\n".join(lines), status="warn" if issues else "ok", summary="、".join(issues))
    if not dry_run:
        digest_outbox.publish("hrs_observation", **entry, now=now)
    return entry
