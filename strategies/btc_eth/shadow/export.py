"""影子决策事件导出 CLI（架构方案 §5 export.py）

服务器侧用：从 research schema 按 run_id 导出决策事件为 JSON Lines +
run manifest JSON，供本地回放器消费。

用法：
    python -m strategies.btc_eth.shadow.export --run-id xau-shadow-... \
        --output-dir /tmp/exported

导出契约（回放器依赖，必须严格一致）：
  - decision_events.jsonl：每行一个决策事件对象（与 shadow_decision_events 表列对齐）
  - run_manifest.json：{"run_id","instance_id","symbol","started_at","ended_at","status",
    "initial_virtual_equity_usdt","git_commit","git_sha","config_hashes",
    "data_cutoff_at"}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any, Dict, List, Optional

import structlog

from shared.database import DatabaseManager

from .instance import connect_db_from_env, iso_or_none


logger = structlog.get_logger()


def _row_to_export_event(row: Dict[str, Any]) -> Dict[str, Any]:
    """将 shadow_decision_events 查询行转换为导出契约字段"""
    def _json_or(val: Any) -> Any:
        if val is None:
            return None
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return val
        if hasattr(val, "isoformat"):
            return val.isoformat()
        return val

    return {
        "run_id": row.get("run_id"),
        "profile_id": row.get("profile_id"),
        "symbol": row.get("symbol"),
        "scheduled_at": iso_or_none(row.get("scheduled_at")),
        "evaluated_at": iso_or_none(row.get("evaluated_at")),
        "decision": row.get("decision"),
        "direction": row.get("direction"),
        "grade": row.get("grade"),
        "score": float(row["score"]) if row.get("score") is not None else None,
        "reference_price": _float_or_none(row.get("reference_price")),
        "intended_order_type": row.get("intended_order_type"),
        "intended_entry_price": _float_or_none(row.get("intended_entry_price")),
        "stop_loss_rule": _json_or(row.get("stop_loss_rule")),
        "tp1_rule": _json_or(row.get("tp1_rule")),
        "tp2_rule": _json_or(row.get("tp2_rule")),
        "trailing_rule": _json_or(row.get("trailing_rule")),
        "time_stop_rule": _json_or(row.get("time_stop_rule")),
        "feature_snapshot": _json_or(row.get("feature_snapshot")),
        "reason_codes": _json_or(row.get("reason_codes")) or [],
        "data_quality_reason": row.get("data_quality_reason"),
        "config_hash": row.get("config_hash"),
        "git_commit": row.get("git_commit"),
        "source_kline_close_times": _json_or(row.get("source_kline_close_times")) or {},
    }


def _float_or_none(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _row_to_run_manifest(row: Dict[str, Any]) -> Dict[str, Any]:
    """将 shadow_runs 查询行转换为 run manifest 契约"""
    config_hashes = row.get("config_hashes")
    if isinstance(config_hashes, str):
        try:
            config_hashes = json.loads(config_hashes)
        except (json.JSONDecodeError, TypeError):
            config_hashes = {}

    return {
        "run_id": row.get("run_id"),
        "instance_id": row.get("instance_id"),
        "symbol": row.get("symbol"),
        "started_at": iso_or_none(row.get("started_at")),
        "ended_at": iso_or_none(row.get("ended_at")),
        "status": row.get("status"),
        "initial_virtual_equity_usdt": _float_or_none(row.get("initial_virtual_equity_usdt")),
        "git_commit": row.get("git_commit"),
        "git_sha": row.get("git_sha"),
        "config_hashes": config_hashes or {},
        "data_cutoff_at": iso_or_none(row.get("data_cutoff_at")),
    }


async def export_run(
    *,
    db: DatabaseManager,
    run_id: str,
    output_dir: str,
) -> Dict[str, Any]:
    """执行导出

    Returns:
        摘要 dict：run_id, events_exported, manifest_path, events_path
    """
    from .repository import ShadowRepository

    repo = ShadowRepository(db)
    run = await repo.find_run_by_id(run_id=run_id)
    if run is None:
        raise SystemExit(f"run_id={run_id} 不存在")

    events = await repo.find_events_by_run(run_id=run_id)

    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)

    # 1. 写 run_manifest.json
    manifest_path = os.path.join(output_dir, "run_manifest.json")
    manifest = _row_to_run_manifest(run)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    logger.info("run_manifest 已写出", path=manifest_path)

    # 2. 写 decision_events.jsonl
    events_path = os.path.join(output_dir, "decision_events.jsonl")
    exported = 0
    with open(events_path, "w", encoding="utf-8") as f:
        for row in events:
            event = _row_to_export_event(row)
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            exported += 1
    logger.info("decision_events 已写出", path=events_path, count=exported)

    return {
        "run_id": run_id,
        "events_exported": exported,
        "manifest_path": manifest_path,
        "events_path": events_path,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="导出影子 run 的决策事件（JSONL + manifest）"
    )
    parser.add_argument("--run-id", required=True, help="要导出的 run_id")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="输出目录（默认 ./exports/{run_id}）",
    )
    return parser


async def _async_main(argv: Optional[List[str]] = None) -> None:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    db = connect_db_from_env()
    output_dir = args.output_dir or os.path.join("./exports", args.run_id)

    connected = False
    try:
        # 必须先显式连接：DatabaseManager 构造时 pool=None，不调用 connect() 后续 fetch_one/
        # fetch_all 会因 pool=None 抛 AttributeError，原代码漏调导致真实运行失败、错误被
        # 仓储层吞掉后误报 run_id 不存在（阻断项 2 根因）。
        await db.connect()
        connected = True

        result = await export_run(
            db=db,
            run_id=args.run_id,
            output_dir=output_dir,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        # disconnect 必须对"未连接 / 连接失败"也安全：DatabaseManager.disconnect()
        # 内部已 if self.pool: 才 close；此处再包一层 try/except 确保即使 pool=None 也不抛
        # 二次异常，避免掩盖原始 connect/export 错误。
        try:
            if connected:
                await db.disconnect()
        except Exception as exc:
            logger.warning(
                "db.disconnect 时出现异常（非致命，忽略）",
                error=str(exc),
            )


def main(argv: Optional[List[str]] = None) -> None:
    asyncio.run(_async_main(argv))


if __name__ == "__main__":
    main()
