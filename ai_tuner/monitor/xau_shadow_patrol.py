"""Read-only XAU shadow observation patrol, scheduled by ai-tuner.

The patrol never mutates research/trading data and never triggers a replay.
"""

import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger


from .xau_runtime import runtime_issues


UTC = timezone.utc
PROFILES = ("xau_calibrated", "xau_original", "xau_aggressive")
OBSERVATION_START = datetime(2026, 10, 28, 9, tzinfo=UTC)
OBSERVATION_END = datetime(2026, 12, 23, 9, tzinfo=UTC)


def daily_patrol_trigger():
    """Run once each day at 07:45 China time, after the HH:20 decision."""
    return CronTrigger(hour=7, minute=45, timezone=ZoneInfo("Asia/Shanghai"))


class XAUShadowPatrol:
    def __init__(self, db, notifier, now=None):
        self.db = db
        self.notifier = notifier
        self.now = now or (lambda: datetime.now(UTC))

    async def run_once(self):
        now = self.now().astimezone(UTC)
        expected = min(now.replace(minute=0, second=0, microsecond=0), OBSERVATION_END - timedelta(hours=1))
        issues = runtime_issues(now)
        lines = [f"【XAU 影子巡检】{now:%Y-%m-%d %H:%M} UTC（每日巡检）"]

        events = await self.db.fetch_all(
            "SELECT DISTINCT ON (profile_id) profile_id, run_id, scheduled_at, "
            "data_quality_reason, decision, feature_snapshot "
            "FROM research.shadow_decision_events "
            "WHERE symbol = 'XAUUSDT' AND profile_id = ANY($1::text[]) "
            "ORDER BY profile_id, scheduled_at DESC, evaluated_at DESC",
            list(PROFILES),
        )
        by_profile = {row["profile_id"]: row for row in events}
        for profile in PROFILES:
            row = by_profile.get(profile)
            if not row:
                issues.append(f"{profile} 事件缺失")
                lines.append(f"{profile}: 事件缺失")
                continue
            scheduled = row["scheduled_at"].astimezone(UTC)
            quality = row.get("data_quality_reason")
            if scheduled < expected - timedelta(hours=2):
                issues.append(f"{profile} 事件延迟超过两周期")
            if quality:
                issues.append(f"{profile} 数据质量降级: {quality}")
            lines.append(
                f"{profile}: {scheduled:%m-%d %H:%M} UTC, "
                f"run={row['run_id']}, decision={row['decision']}, "
                f"quality={quality or '正常'}"
            )
            if profile == "xau_calibrated":
                snapshot = row.get("feature_snapshot") or {}
                if isinstance(snapshot, str):
                    snapshot = json.loads(snapshot)
                atr = snapshot.get("atr_filter") or {}
                history = atr.get("history_count")
                threshold = atr.get("final_threshold")
                lines.append(f"ATR: history_count={history}, final_threshold={threshold}")
                if (not isinstance(history, (int, float)) or history <= 0
                        or not isinstance(threshold, (int, float))
                        or not math.isfinite(threshold) or threshold <= 0):
                    issues.append("ATR 计算字段缺失")

        degraded = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM research.shadow_decision_events "
            "WHERE symbol = 'XAUUSDT' AND profile_id = ANY($1::text[]) "
            "AND evaluated_at >= $2 AND evaluated_at < $3 "
            "AND NULLIF(data_quality_reason, '') IS NOT NULL",
            list(PROFILES), now - timedelta(days=1), now,
        )
        if int((degraded or {}).get("n") or 0):
            issues.append("过去24小时存在数据质量降级，需核对期间事件")

        for interval in ("1h", "4h", "1d"):
            # Fixed interval tuple only; table names cannot be influenced by input.
            row = await self.db.fetch_one(
                f"SELECT COUNT(*) AS n, MAX(open_time) AS latest_open_time FROM (SELECT open_time FROM btc_eth.kline_xauusdt_{interval} "
                "ORDER BY open_time DESC LIMIT 100) recent"
            )
            count = int(row["n"]) if row else 0
            lines.append(f"K线 {interval}: {count}/100")
            latest = (row or {}).get("latest_open_time")
            lines.append(f"K线 {interval} 最新开盘时间: {latest}")
            if latest is None:
                issues.append(f"{interval} 行情新鲜度未核验")
            else:
                latest_at = latest if isinstance(latest, datetime) else datetime.fromtimestamp(float(latest) / 1000, UTC)
                # Weekend closures can span several days. Alert after a full four-day outage.
                if now - latest_at.replace(tzinfo=UTC) > timedelta(days=4):
                    issues.append(f"{interval} 行情超过四天未更新")
            if count < 100:
                issues.append(f"{interval} K线不足 100 根")

        runs = await self.db.fetch_all(
            "SELECT run_id, instance_id, status, started_at, ended_at "
            "FROM research.shadow_runs WHERE symbol = 'XAUUSDT' "
            "AND instance_id IN ('mtcps_xau_shadow', 'mtcps_xau_calibrated_shadow') "
            "ORDER BY started_at DESC"
        )
        active = [r for r in runs if r["status"] == "active"]
        lines.append(f"run 分段: active={len(active)}, closed={len(runs)-len(active)}")
        if not any(r["instance_id"] == "mtcps_xau_calibrated_shadow" for r in active):
            issues.append("校准版没有 active run")

        switches = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM (SELECT profile_id FROM research.shadow_decision_events "
            "WHERE symbol = 'XAUUSDT' AND profile_id = ANY($1::text[]) "
            "AND evaluated_at >= $2 AND evaluated_at < $3 "
            "GROUP BY profile_id HAVING COUNT(DISTINCT run_id) > 1) switched",
            list(PROFILES), now - timedelta(days=1), now,
        )
        if int((switches or {}).get("n") or 0):
            issues.append("过去24小时存在 run 切换")

        continuity = await self.db.fetch_one(
            "SELECT COUNT(*) FILTER (WHERE n > 1) AS duplicate_hours, "
            "COALESCE(SUM(n - 1) FILTER (WHERE n > 1), 0) AS duplicate_rows "
            "FROM (SELECT profile_id, scheduled_at, COUNT(*) AS n "
            "FROM research.shadow_decision_events WHERE symbol = 'XAUUSDT' "
            "AND profile_id = 'xau_calibrated' AND scheduled_at >= $1 "
            "AND scheduled_at < $2 GROUP BY profile_id, scheduled_at) grouped",
            OBSERVATION_START, OBSERVATION_END,
        )
        dup = int((continuity or {}).get("duplicate_hours") or 0)
        lines.append(f"校准版跨 run 重复小时: {dup}（历史基线 6）")
        if dup > 6:
            issues.append("新增跨 run 重复事件")
        gap_row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM generate_series($1::timestamptz, $2::timestamptz, "
            "interval '1 hour') AS slot(scheduled_at) "
            "WHERE NOT EXISTS (SELECT 1 FROM research.shadow_decision_events e "
            "WHERE e.profile_id = 'xau_calibrated' AND e.symbol = 'XAUUSDT' "
            "AND e.scheduled_at = slot.scheduled_at)",
            OBSERVATION_START,
            expected,
        )
        gaps = int((gap_row or {}).get("n") or 0)
        lines.append(f"校准版小时缺口: {gaps}（历史基线 4）")
        if gaps > 4:
            issues.append("新增校准版小时缺口")

        for table, column in (
            ("trade_records", "strategy"),
            ("strategy_open_positions", "strategy_id"),
            ("decision_evidence", "strategy_id"),
            ("position_claims", "strategy"),
        ):
            row = await self.db.fetch_one(
                f"SELECT COUNT(*) AS n FROM trading.{table} "
                f"WHERE {column} IN ('mtcps_xau_shadow', 'mtcps_xau_calibrated_shadow', "
                "'xau_original', 'xau_aggressive', 'xau_calibrated') "
                "AND symbol = 'XAUUSDT'"
            )
            count = int(row["n"]) if row else 0
            lines.append(f"trading.{table}: {count}")
            if count:
                issues.append(f"生产表 trading.{table} 出现 {count} 行")

        registration = await self.db.fetch_one(
            "SELECT expires_at, status, intervals FROM btc_eth.registered_symbols "
            "WHERE symbol = 'XAUUSDT'"
        )
        if registration:
            expires = registration["expires_at"].replace(tzinfo=UTC)
            days = (expires - now).total_seconds() / 86400
            lines.append(f"行情注册: {registration['status']}, 到期 {expires:%Y-%m-%d %H:%M} UTC, 剩余 {days:.1f} 天")
            if registration["status"] != "active" or days <= 0:
                issues.append("行情注册已失效")
            elif days < 7:
                lines.append("续期提醒: 需人工授权；一次续期仍需核对能否覆盖 12-23 观察终点")
        else:
            issues.append("XAUUSDT 行情注册缺失")

        lines.append("结论: " + ("；".join(issues) if issues else "正常"))
        message = "\n".join(lines)
        sent = await self.notifier.send(
            message=message, level="warning" if issues else "info", project="xau_monitor"
        )
        return {"ok": not issues, "sent": bool(sent), "message": message}
