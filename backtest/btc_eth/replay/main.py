"""
影子回放器 CLI 入口。

调用示例：
    python -m backtest.btc_eth.replay.main \
        --events research_events.jsonl \
        --run-manifest run_manifest.json \
        --snapshot-dir ./snapshot/ \
        --profile-orig strategies/btc_eth/shadow/config/xau_original.yaml \
        --profile-aggr strategies/btc_eth/shadow/config/xau_aggressive.yaml \
        --output-dir ./reports/

安全：默认不连生产 PostgreSQL；DATABASE_URL 缺省时直接报错。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from datetime import timedelta, datetime

from .engine import ReplayEngine, _parse_ts
from .report import write_reports
from .snapshot import verify_snapshot


logger = logging.getLogger(__name__)


def validate_calibrated_market_data(bars, funding_events, start_at: datetime, end_at: datetime):
    """Check actual market rows, not only the manifest's self-reported gap count.

    Funding settlements may be more frequent than eight hours; a larger gap
    cannot be treated as zero funding without independent schedule evidence.
    """
    reasons = []
    minute = timedelta(minutes=1)
    window_bars = [bar for bar in bars if start_at <= _parse_ts(bar["open_time"]) < end_at]
    expected_minutes = int((end_at - start_at).total_seconds() // 60)
    if len(window_bars) != expected_minutes:
        reasons.append("minute_gap")
    if window_bars:
        if (_parse_ts(window_bars[0]["open_time"]) != start_at
                or _parse_ts(window_bars[-1]["open_time"]) != end_at - minute):
            if "minute_gap" not in reasons:
                reasons.append("minute_gap")
        for previous, current in zip(window_bars, window_bars[1:]):
            if _parse_ts(current["open_time"]) - _parse_ts(previous["open_time"]) != minute:
                if "minute_gap" not in reasons:
                    reasons.append("minute_gap")
                break
        for bar in window_bars:
            opened = _parse_ts(bar["open_time"])
            closed = _parse_ts(bar["close_time"])
            if not (opened + minute - timedelta(milliseconds=1) <= closed <= opened + minute):
                reasons.append("invalid_minute_close")
                break
    window_funding = [time for time, _ in funding_events if start_at <= time < end_at]
    if not window_funding:
        reasons.append("funding_missing")
    else:
        checkpoints = [start_at, *window_funding, end_at]
        if (len(window_funding) != len(set(window_funding))
                or any(later <= earlier or later - earlier > timedelta(hours=8)
                       for earlier, later in zip(checkpoints, checkpoints[1:]))):
            reasons.append("funding_gap")
        # A <=8h gap does not prove completeness: one missing 4h settlement
        # looks exactly like a legitimate 8h gap. The current snapshot format
        # has no independently verifiable historical settlement schedule.
        reasons.append("funding_schedule_unverified")
    return reasons


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="XAUUSDT MTCPS 影子回放器（本地离线）")
    parser.add_argument("--events", required=True, help="决策事件 JSON Lines 文件（来自 shadow 导出）")
    parser.add_argument("--run-manifest", required=True, help="run manifest JSON 文件")
    parser.add_argument("--snapshot-dir", required=True,
                        help="快照目录（含 1m K 线 / funding / exchangeInfo / manifest.json）")
    parser.add_argument("--profile-orig", default="", help="原版 profile 冻结配置 (xau_original.yaml)")
    parser.add_argument("--profile-aggr", default="", help="激进版 profile 冻结配置 (xau_aggressive.yaml)")
    parser.add_argument("--profile-calibrated", default="", help="校准版 profile 冻结配置 (xau_calibrated.yaml)")
    parser.add_argument("--coverage", default="", help="校准版 reconcile_runs 生成的 coverage.json")
    parser.add_argument("--replay-config", default=None, help="回放 config.yaml；缺省用 replay/config.yaml")
    parser.add_argument("--output-dir", required=True, help="报告输出目录")
    parser.add_argument("--skip-snapshot-verify", action="store_true", help="跳过 manifest 文件 SHA-256 校验")
    parser.add_argument(
        "--allow-gaps",
        action="store_true",
        help="允许快照存在数据缺口时继续回放（默认 False；"
             "缺口时 fail-closed 并拒绝产出准入结论）",
    )
    parser.add_argument("--no-db", action="store_true", help="显式声明不连 DB（默认如此；仅为防御）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 安全守卫：禁止连生产 PostgreSQL
    # 回放器只读本地 JSON / JSONL 文件，严禁回退连生产库
    if os.environ.get("DATABASE_URL"):
        logger.warning(
            "检测到 DATABASE_URL 环境变量=%s 会被忽略（回放器离线运行，不连 DB）",
            os.environ.get("DATABASE_URL", "")[:30] + "...",
        )

    # 默认回放配置
    default_replay_cfg = str(Path(__file__).resolve().parent / "config.yaml")
    replay_cfg = args.replay_config or default_replay_cfg

    snapshot_dir = Path(args.snapshot_dir)
    manifest_path = snapshot_dir / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # 快照文件路径解析：Binance USDⓈ-M TradFi 合约符号含斜杠变体；snapshot 里存的是原始符号
    symbol = manifest["symbol"]
    # snapshot.py 里落盘用 symbol.lower() + "_1m.jsonl" 等
    klines_path = snapshot_dir / Path(manifest["files"]["klines_1m"]["path"]).name
    funding_path = snapshot_dir / Path(manifest["files"]["funding_rates"]["path"]).name
    exchange_info_path = snapshot_dir / Path(manifest["files"]["exchange_info"]["path"]).name

    # data_completeness：记录快照数据质量，传递给 write_reports 决定 admission 契约
    # 默认 complete=True；仅当 verify 跑完后覆盖。
    data_completeness = {"gaps": None, "complete": False}

    if not args.skip_snapshot_verify:
        verify = verify_snapshot(str(manifest_path))
        if not verify["files_ok"]:
            logger.error("快照校验失败：%s", verify["mismatches"])
            return 2

        gaps = verify["gaps"]
        data_completeness = {"gaps": gaps, "complete": gaps == 0}

        if gaps > 0:
            if not args.allow_gaps:
                # —— fail-closed：数据不完整且未显式允许 → 拒绝产出准入结论 ——
                # 退出码 3（区别于其他错误码 2/4），stdout/stderr 都打印结构化错误信息，
                # 方便 CI 脚本判定"准入被拒绝"而非运行时崩溃。
                err_msg = (
                    f"快照数据不完整（gaps={gaps}），"
                    f"未显式传 --allow-gaps；按设计要求 fail-closed。"
                )
                logger.error(err_msg)
                # stdout 也打印结构化 JSON（与正常成功路径同格式，便于调用方解析）
                print(json.dumps({
                    "admission": "insufficient_data",
                    "data_completeness": data_completeness,
                    "error": err_msg,
                }, indent=2, ensure_ascii=False))
                return 3
            else:
                logger.warning(
                    "快照存在 %d 处缺口（已显式传 --allow-gaps，继续回放；"
                    "报告将标注数据不完整）",
                    gaps,
                )

    with open(args.run_manifest, "r", encoding="utf-8") as f:
        run_manifest = json.load(f)

    is_calibrated = set(run_manifest.get("config_hashes", {})) == {"xau_calibrated"}
    if is_calibrated:
        if not args.profile_calibrated or not args.coverage or args.skip_snapshot_verify:
            logger.error("校准版必须提供 --profile-calibrated 与 --coverage，且不能跳过快照校验")
            return 2
        import hashlib
        coverage_path = Path(args.coverage)
        events_path = Path(args.events)
        if (hashlib.sha256(coverage_path.read_bytes()).hexdigest() != run_manifest.get("coverage_sha256")
                or hashlib.sha256(events_path.read_bytes()).hexdigest() != run_manifest.get("decision_events_sha256")):
            logger.error("校准版 coverage 或事件文件哈希不匹配")
            return 2
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        if (coverage.get("profile_id") != "xau_calibrated"
                or coverage.get("symbol") != "XAUUSDT"
                or _parse_ts(run_manifest.get("data_cutoff_at")) != _parse_ts(coverage["window_end"])):
            logger.error("校准版 coverage 身份不匹配")
            return 2
        data_completeness["decision_missing_hours"] = coverage.get("missing_hours", [])
        data_completeness["decision_quality_degraded_hours"] = coverage.get("quality_degraded_hours", [])
        data_completeness["decision_coverage"] = coverage
        data_completeness["decision_coverage_complete"] = (
            coverage.get("window_start") == "2026-10-28T09:00:00+00:00"
            and coverage.get("window_end") == "2026-12-23T09:00:00+00:00"
            and not coverage.get("missing_hours")
            and not coverage.get("quality_degraded_hours")
            and coverage.get("events_written") == coverage.get("expected_hours")
        )
    elif not (args.profile_orig and args.profile_aggr):
        logger.error("原版/激进版回放必须同时提供 --profile-orig 和 --profile-aggr")
        return 2

    engine = ReplayEngine(
        replay_config_path=replay_cfg,
        profile_config_path_original=args.profile_orig,
        profile_config_path_aggressive=args.profile_aggr,
        klines_1m_path=str(klines_path),
        funding_rates_path=str(funding_path),
        exchange_info_json_path=str(exchange_info_path),
        snapshot_manifest_path=str(manifest_path),
        run_manifest=run_manifest,
        profile_config_path_calibrated=args.profile_calibrated,
    )
    engine.load_events(args.events)
    if is_calibrated:
        start_at = _parse_ts(coverage["window_start"])
        end_at = _parse_ts(coverage["window_end"])
        expected = set()
        cursor = start_at
        while cursor < end_at:
            expected.add(cursor)
            cursor += timedelta(hours=1)
        actual = [_parse_ts(row["scheduled_at"]) for row in engine.events]
        missing = {_parse_ts(value) for value in coverage["missing_hours"]}
        if (len(actual) != coverage.get("events_written") or len(actual) != len(set(actual))
                or set(actual) != expected - missing or not missing.issubset(expected)):
            logger.error("校准版 coverage 与决策事件逐小时集合不一致")
            return 2
        market_errors = validate_calibrated_market_data(
            engine.klines_1m_sorted, engine.funding_events, start_at, end_at)
        hard_errors = [reason for reason in market_errors if reason != "funding_schedule_unverified"]
        if hard_errors:
            logger.error("校准版行情快照不完整：%s", market_errors)
            return 2
        data_completeness["funding_schedule_verified"] = False
        data_completeness["funding_schedule_reason"] = "historical_settlement_schedule_unavailable"
        data_completeness["complete"] = False
        logger.warning("资金费历史结算日程不可独立核验；仅产出参考指标，准入固定为 insufficient_data")
    engine_result = engine.run()

    outputs = write_reports(
        engine_result=engine_result,
        run_manifest=run_manifest,
        output_dir=args.output_dir,
        replay_config_path=replay_cfg,
        data_completeness=data_completeness,
    )

    logger.info("回放完成：admission=%s data_completeness=%s",
                outputs["admission"], data_completeness)
    print(json.dumps({
        "admission": outputs["admission"],
        "data_completeness": data_completeness,
        "md_report": outputs["md_path"],
        "json_report": outputs["json_path"],
        "replay_run_id": engine_result["engine_metadata"]["replay_run_id"],
        "invariant_violations": len(engine_result["engine_metadata"]["invariant_violations"]),
    }, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
