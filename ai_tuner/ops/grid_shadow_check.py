"""Host-side grid shadow evidence inspection, with durable Feishu delivery state."""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import urllib.request

try:
    from ai_tuner.ops import digest_outbox
except ImportError:  # 以脚本方式直接运行时，当前目录即 ai_tuner/ops
    import digest_outbox

ROOT = Path(os.environ.get("TRADING_ROOT", "/root/trading_system"))
TZ = dt.timezone(dt.timedelta(hours=8))


def command(*args, input_text=None):
    return subprocess.run(args, input=input_text, text=True, capture_output=True,
                          check=True, timeout=120).stdout.strip()


def read_db(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def market_window(conn, now_ms, window_ms=72 * 3600000):
    """Use fully materialized seconds; ingestion latency is not a sample hole."""
    if window_ms <= 0 or window_ms % 1000:
        raise ValueError('market window must contain whole positive seconds')
    closed_end = now_ms // 1000 * 1000
    latest = conn.execute("SELECT MAX(sample_ms) FROM price_samples_1s "
                          "WHERE symbol='ETHUSDT' AND sample_ms<?", (closed_end,)).fetchone()[0]
    end = min(closed_end, latest // 1000 * 1000 + 1000) if latest is not None else closed_end
    start = end - window_ms
    first, last, count, invalid = conn.execute(
        "SELECT MIN(sample_ms),MAX(sample_ms),COUNT(*),"
        "SUM(CASE WHEN sample_ms%1000!=0 OR anchor_time_ms>sample_ms "
        "OR materialized_at_ms<sample_ms THEN 1 ELSE 0 END) FROM price_samples_1s "
        "WHERE symbol='ETHUSDT' AND sample_ms>=? AND sample_ms<?", (start, end)).fetchone()
    expected = window_ms // 1000
    return {'window_start_ms': start, 'window_end_ms': end, 'latest_sample_ms': latest,
            'samples_72h': count, 'expected_samples_72h': expected, 'invalid_samples': invalid or 0,
            'sample_coverage_complete': first == start and last == end - 1000 and count == expected and not invalid,
            'materialization_lag_ms': now_ms - latest if latest is not None else None}


def queue_diagnostics(conn, queue, end_ms, window_ms=72 * 3600000):
    reasons, expiries = [], []
    unclean = queue.get('unclean_instances', [])
    if unclean:
        reasons.append('历史进程非正常退出' + str(len(unclean)) + '次')
        for instance in unclean:
            last = conn.execute('SELECT MAX(observed_at_ms) FROM queue_audit_checkpoint '
                                'WHERE instance_id=? AND observed_at_ms<=?',
                                (instance['instance_id'], end_ms)).fetchone()[0]
            expiries.append(max(instance['started_at_ms'], last or instance['started_at_ms']) + window_ms + 1)
    if queue.get('gaps'):
        reasons.append('历史审计记录间断' + str(len(queue['gaps'])) + '处')
    if queue.get('failed_checkpoints_ms'):
        reasons.append('历史审计失败' + str(len(queue['failed_checkpoints_ms'])) + '次')
    if queue.get('episodes'):
        reasons.append('历史队列逐出' + str(queue.get('overflow_count', 0)) + '笔')
        expiries.extend(row['recovery_until_ms'] + window_ms + 1 for row in queue['episodes'])
    if not queue.get('queue_window_eligible') and not reasons:
        reasons.append('历史队列覆盖证据不足')
    return {'reasons': reasons, 'history_expires_ms': max(expiries) if expiries else None}


def collect(now_ms):
    names = ["trading_system-grid", "trading_system-grid-realtime",
             "trading_system-aggtrade-collector", "trading_system-grid-executor",
             "trading_system-grid-notifier", "trading_system-grid-pg-projector",
             "trading_system-grid-dashboard-exporter"]
    containers = json.loads(command("docker", "inspect", *names))
    failures = []
    identities = {}
    agg_path = None
    for row in containers:
        name = row["Name"].lstrip("/")
        identities[name] = {"id": row["Id"], "restarts": row["RestartCount"]}
        health = row["State"].get("Health", {}).get("Status")
        if row["State"]["Status"] != "running" or health != "healthy":
            failures.append(name + " " + str(health))
        if name == "trading_system-aggtrade-collector":
            agg_path = next(m["Source"] for m in row["Mounts"] if m["Destination"] == "/app/data")
    grid = read_db(ROOT / "data/grid/grid_realtime.sqlite3")
    checkpoint = dict(grid.execute(
        "SELECT * FROM queue_audit_checkpoint ORDER BY observed_at_ms DESC LIMIT 1").fetchone())
    session = dict(grid.execute("SELECT * FROM session ORDER BY session_id DESC LIMIT 1").fetchone())
    # Read thresholds from the actual running profile, without exporting environment secrets.
    cfg = json.loads(command("docker", "exec", "trading_system-grid-realtime", "python", "-c",
        "import yaml,json;print(json.dumps(yaml.safe_load(open('/app/strategies/grid/config.yaml'))['realtime_alert']))"))
    silence = cfg["health"]["max_silence_seconds"]
    if now_ms - checkpoint["observed_at_ms"] > silence * 1000:
        failures.append("checkpoint expired")
    for field, expected in (("feed_healthy", 1), ("storage_degraded", 0), ("audit_ok", 1)):
        if checkpoint[field] != expected:
            failures.append(field + "=" + str(checkpoint[field]))
    if checkpoint["recovery_until_ms"] > now_ms:
        failures.append("queue warmup")
    if session["pending_seq"] is not None:
        failures.append("pending reference")
    if now_ms - session["updated_at_ms"] > cfg["reference_sync"]["max_silence_seconds"] * 1000:
        failures.append("session heartbeat expired")
    reference = checkpoint["reference_status"]
    if reference not in ("VALID", "NO_ACTIVE_REFERENCE"):
        failures.append("reference=" + reference)
    if reference == "NO_ACTIVE_REFERENCE" and (session["current_reference_id"] is not None or session["current_seq"] != 0):
        failures.append("inconsistent no-active-reference")
    if reference == "VALID":
        ref = grid.execute("SELECT * FROM reference WHERE reference_id=?",
                           (session["current_reference_id"],)).fetchone()
        if (ref is None or ref["send_status"] != "SENT" or ref["session_id"] != session["session_id"]
                or now_ms - ref["effective_at_ms"] > cfg["reference"]["max_age_seconds"] * 1000):
            failures.append("numeric reference unverified")
    statuses = dict(grid.execute("SELECT status,count(*) FROM outbox GROUP BY status"))
    if cfg["mode"] != "shadow" or cfg.get("shadow_research", {}).get("enabled", False):
        failures.append("shadow boundary configuration")
    # SHADOW and explicit suppressed/rejected rows are not external delivery.
    if grid.execute("SELECT count(*) FROM outbox WHERE status='SENT'").fetchone()[0]:
        failures.append("formal outbox delivery")
    queue_module = load_module(ROOT / "strategies/grid/realtime/queue_audit.py", "grid_queue_audit")
    agg = read_db(next(Path(agg_path).rglob("ethusdt_aggtrades.sqlite")))
    market = market_window(agg, now_ms)
    latest = market['latest_sample_ms']
    if latest is None or now_ms - latest > 60000:
        time.sleep(11)
        market = market_window(agg, now_ms)
        latest = market['latest_sample_ms']
        if latest is None or int(time.time() * 1000) - latest > 60000:
            failures.append("market collector not advancing")
    # Market, queue and reference evidence use the identical completed window.
    # The separate freshness checks above still reject a stopped collector.
    start, end = market['window_start_ms'], market['window_end_ms']
    queue = queue_module.summarize_window(grid, start_ms=start, end_ms=end, max_gap_ms=5000)
    shadow = queue_module.summarize_shadow_window(grid, start_ms=start, end_ms=end, max_gap_ms=5000)
    recent = queue_module.summarize_window(grid, start_ms=end-3600000, end_ms=end, max_gap_ms=5000)
    diagnostics = queue_diagnostics(grid, queue, end)
    raw = agg.execute(
        "SELECT min(agg_trade_id),max(agg_trade_id),count(*),max(trade_time_ms) "
        "FROM agg_trades WHERE symbol='ETHUSDT'").fetchone()
    raw_contiguous = raw[2] > 0 and raw[2] == raw[1] - raw[0] + 1
    if not raw_contiguous:
        failures.append("retained trade IDs have gaps")
    invalid_samples = market['invalid_samples']
    if invalid_samples:
        failures.append("invalid second samples")
    unknown_events = grid.execute("SELECT count(*) FROM event WHERE decision_ms IS NULL").fetchone()[0]
    outcomes = []
    for event in grid.execute(
            "SELECT event_id,decision_ms,direction FROM event "
            "WHERE decision_ms>=? AND decision_ms<=? AND direction IN ('UP','DOWN')",
            (start, end - 300000)):
        evidence = grid.execute(
            "SELECT reference_status,feed_healthy,storage_degraded,audit_ok,recovery_until_ms,"
            "observed_at_ms FROM queue_audit_checkpoint WHERE observed_at_ms<=? "
            "ORDER BY observed_at_ms DESC LIMIT 1", (event["decision_ms"],)).fetchone()
        if (evidence is None or evidence["reference_status"] != "VALID"
                or not evidence["feed_healthy"] or evidence["storage_degraded"]
                or not evidence["audit_ok"] or evidence["recovery_until_ms"] > event["decision_ms"]
                or event["decision_ms"] - evidence["observed_at_ms"] > 5000):
            continue
        prices = []
        for at in (event["decision_ms"], event["decision_ms"] + 300000):
            point = agg.execute(
                "SELECT price,sample_ms,anchor_time_ms FROM price_samples_1s "
                "WHERE symbol='ETHUSDT' AND sample_ms<=? ORDER BY sample_ms DESC LIMIT 1",
                (at,)).fetchone()
            if (point is None or at - point["sample_ms"] > 2000
                    or point["sample_ms"] - point["anchor_time_ms"] > 2000):
                break
            prices.append(float(point["price"]))
        if len(prices) == 2 and min(prices) > 0:
            signed = (prices[1] / prices[0] - 1) * (1 if event["direction"] == "UP" else -1)
            outcomes.append({"event_id": event["event_id"], "at_ms": event["decision_ms"],
                             "direction": event["direction"], "directional_return_5m": signed})
    agg.close()
    grid.close()
    return {"observed_ms": now_ms, "failures": sorted(failures), "reference": reference,
            "containers": identities, "checkpoint": checkpoint, "session": session,
            "outbox": statuses, "queue_72h": queue, "queue_recent": recent,
            "queue_diagnostics": diagnostics, "shadow_72h": shadow,
            "market": {**market, "retained_raw_id_contiguous": raw_contiguous,
                       "unknown_event_times": unknown_events},
            "exploratory_5m": {"eligible_events": len(outcomes), "outcomes": outcomes,
                               "mean_directional_return": (sum(o["directional_return_5m"] for o in outcomes) / len(outcomes) if outcomes else None)},
            "evaluation": "仅 VALID 且行情、存储、队列审计通过的时段可用于比较。"
                          "NO_ACTIVE_REFERENCE 为无活动边界，不计入预警收益样本。"
                          "当前报告评估证据资格，风控改善须另做冻结参数的提前量、误报和回撤对比。"}


def conclusion(report):
    parts = ["本轮发现运行或数据异常，需先处理后再评估风控效果。" if report["failures"]
             else "本轮服务与实时数据检查未发现异常。"]
    if report["reference"] == "NO_ACTIVE_REFERENCE":
        parts.append("当前没有活动网格边界，属于正常等待，实时边界预警暂无评估对象。")
    elif report["reference"] == "VALID":
        parts.append("当前网格参考有效，可继续积累预警样本。")
    else:
        parts.append("当前网格参考尚不可用于预警效果评估。")
    complete = (report["market"]["samples_72h"] == report["market"]["expected_samples_72h"]
                and report["shadow_72h"].get("coverage_complete")
                and report["queue_72h"].get("queue_window_eligible"))
    if not complete:
        parts.append("过去72小时的数据覆盖或队列审计尚未全部通过，研究证据不足。")
        reasons = report.get('queue_diagnostics', {}).get('reasons', [])
        if reasons:
            parts.append('原因：' + '、'.join(reasons) + '。')
        if report.get('queue_recent', {}).get('queue_window_eligible'):
            parts.append('当前近1小时队列审计通过，历史不合格不代表当前仍在丢数据。')
    if report["exploratory_5m"]["eligible_events"] == 0:
        parts.append("目前没有合格预警样本，尚无法判断实时行情是否改善风控，需继续积累有效样本并完成对照回放。")
    else:
        parts.append("已有初步行情方向样本，风控改善效果仍需通过提前量、误报和回撤的对照回放验证。")
    return "结论：" + "".join(parts)


def render(report):
    time_text = dt.datetime.fromtimestamp(report["observed_ms"] / 1000, TZ).strftime("%Y-%m-%d %H:%M")
    diagnostic = report.get('queue_diagnostics', {})
    history_expiry = diagnostic.get('history_expires_ms')
    expiry_text = (dt.datetime.fromtimestamp(history_expiry/1000, TZ).strftime('%Y-%m-%d %H:%M:%S')
                   if history_expiry else None)
    market_end = report['market'].get('window_end_ms')
    end_text = (dt.datetime.fromtimestamp(market_end/1000, TZ).strftime('%Y-%m-%d %H:%M:%S')
                if market_end is not None else '未知')
    recent = report.get('queue_recent', {}).get('queue_window_eligible')
    return ("网格实盘与实时行情巡检\n" + time_text + "\n"
            + "异常：" + ("；".join(report["failures"]) or "无") + "\n"
            + "实时参考：" + report["reference"] + "\n"
            + "72小时秒样本：" + str(report["market"]["samples_72h"]) + "/259200\n"
            + '统计截止已落库秒：' + end_text + '\n'
            + "72小时审计记录覆盖：" + str(report["shadow_72h"].get("coverage_complete")) + "\n"
            + '72小时有效参考可用时间：' + format(report['shadow_72h'].get('numeric_reference_availability', 0), '.1%') + '\n'
            + "72小时队列资格：" + ('通过' if report['queue_72h'].get('queue_window_eligible') else '未通过') + "\n"
            + '队列资格原因：' + ('、'.join(diagnostic.get('reasons', [])) or '无') + '\n'
            + ('历史记录预计移出窗口：' + expiry_text + '（届时复核）\n' if expiry_text else '')
            + '当前队列（近1小时）：' + ('通过' if recent else '未通过' if recent is False else '未知') + '\n'
            + "有效事件的5分钟方向收益样本：" + str(report["exploratory_5m"]["eligible_events"]) + "\n"
            + "该指标为行情方向代理，完整止损收益对比仍需配对回放。\n"
            + report["evaluation"] + "\n\n" + conclusion(report))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="直接发送飞书，默认只写入 07:35 汇总 outbox")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    state_path = ROOT / "data/grid/server_inspection.json"
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    report = collect(int(time.time() * 1000))
    date = dt.datetime.now(TZ).date().isoformat()
    identity = {"report_format_version": 3,
                "failures": report["failures"], "reference": report["reference"],
                "containers": report["containers"]}
    should_send = previous.get("identity") != identity or previous.get("sent_day") != date
    payload = json.dumps(report, ensure_ascii=False).replace("'", "''")
    sql = ("CREATE TABLE IF NOT EXISTS trading.grid_shadow_audit "
           "(observed_ms bigint PRIMARY KEY, report jsonb NOT NULL);"
           "INSERT INTO trading.grid_shadow_audit VALUES (" + str(report["observed_ms"])
           + ",'" + payload + "'::jsonb) ON CONFLICT DO NOTHING;")
    command("docker", "exec", "-i", "trading_system-postgres", "psql", "-v", "ON_ERROR_STOP=1",
            "-U", "trading_user", "-d", "trading_platform", input_text=sql)
    message = render(report)
    if args.dry_run:
        print(message)
        return
    if not args.send:
        digest_outbox.publish(
            "grid",
            message,
            status="warn" if report["failures"] else "ok",
            summary=f"{len(report['failures'])} 项异常" if report["failures"] else "正常",
        )
    elif should_send:
        request = urllib.request.Request(os.environ["GRID_INSPECTION_WEBHOOK"],
            data=json.dumps({"msg_type": "text", "content": {"text": message}}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
        if result.get("code", result.get("StatusCode", -1)) != 0:
            raise RuntimeError("Feishu delivery rejected")
        previous = {"identity": identity, "sent_day": date, "sent_ms": report["observed_ms"]}
        temp = state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(previous))
        os.chmod(temp, 0o600)
        os.replace(temp, state_path)
    print(json.dumps({"ok": not report["failures"], "sent": should_send,
                      "reference": report["reference"], "failures": report["failures"]}))


def report_failure():
    """巡检执行失败时写 outbox；写入失败才回退直发。"""
    message = "网格服务器巡检执行失败，请检查 trading-grid-shadow.service 日志。"
    try:
        digest_outbox.publish("grid", message, status="error", summary="执行失败")
        return
    except (OSError, ValueError):
        pass
    request = urllib.request.Request(os.environ["GRID_INSPECTION_WEBHOOK"],
        data=json.dumps({"msg_type": "text", "content": {"text": message}}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        json.load(response)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Send an operational failure without logging credential-bearing exception text.
        report_failure()
        raise
