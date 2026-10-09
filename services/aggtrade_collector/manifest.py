"""样本清单与开发/验证段划分标记（M0 退出条件，计划 §3.5）。

- 开发段：2026-10-07 20min 样本 + 轨道A REST 1~2 天回溯，标「已参与选参，不可称样本外」。
- 验证段：轨道B 自 2026-10-08（UTC+8）00:00 起 ≥30 完整自然日；本地开发库不纳入验证段。
清单为追加式 provenance，随导入/回溯动作更新。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

_BEIJING_TZ = timezone(timedelta(hours=8))
_MANIFEST_SCHEMA_VERSION = 1


def beijing_text(ms: int) -> str:
    """毫秒时间戳转北京时间 ISO 文本（面向用户显示口径）。"""
    return datetime.fromtimestamp(ms / 1000, _BEIJING_TZ).isoformat(timespec="milliseconds")


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _empty_manifest(db_path: Path, start_rule_beijing: str,
                    min_full_days: int) -> Dict[str, Any]:
    """初始化清单骨架（验证段口径来自配置，计划 §3.5）。"""
    return {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "updated_at_beijing": None,
        "db_path": str(db_path),
        "development_segment": {
            "label": "已参与选参，不可称样本外（计划 §3.5）",
            "sources": [],
        },
        "validation_segment": {
            "label": "轨道B：自落库器上线日起 ≥30 完整自然日；本地开发库不含验证段",
            "start_rule_beijing": start_rule_beijing,
            "min_full_days": min_full_days,
            "collected_in_this_db": False,
        },
        "db_stats": None,
    }


def load_or_create(path: Path, db_path: Path, start_rule_beijing: str,
                   min_full_days: int) -> Dict[str, Any]:
    """读取既有清单；不存在则按配置的验证段口径新建。"""
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        data["db_path"] = str(db_path)
        return data
    return _empty_manifest(db_path, start_rule_beijing, min_full_days)


def append_source(manifest: Dict[str, Any], entry: Dict[str, Any],
                  dedup_key: Optional[str] = None) -> bool:
    """追加一条开发段来源；dedup_key 命中则保留首次 provenance 不覆盖（幂等导入）。"""
    sources = manifest["development_segment"]["sources"]
    if dedup_key is not None:
        for old in sources:
            if old.get(dedup_key) == entry.get(dedup_key):
                return False
    sources.append(entry)
    return True


def refresh_db_stats(manifest: Dict[str, Any], conn: sqlite3.Connection,
                     symbol: str) -> None:
    """从库内实读两表范围与行数，写入清单统计。"""
    agg = conn.execute(
        "SELECT COUNT(*), MIN(trade_time_ms), MAX(trade_time_ms) "
        "FROM agg_trades WHERE symbol = ?", (symbol,)).fetchone()
    smp = conn.execute(
        "SELECT COUNT(*), MIN(sample_ms), MAX(sample_ms), "
        "SUM(CASE WHEN trade_count = 0 THEN 1 ELSE 0 END) "
        "FROM price_samples_1s WHERE symbol = ?", (symbol,)).fetchone()
    manifest["db_stats"] = {
        "symbol": symbol,
        "agg_trades": _stats_block(agg[0], agg[1], agg[2]),
        "price_samples_1s": {
            "rows": smp[0],
            "zero_trade_seconds": smp[3],
            **({"range_beijing": {"start": beijing_text(smp[1]),
                                  "end": beijing_text(smp[2])}}
               if smp[1] is not None else {}),
        },
    }


def _stats_block(count: int, start_ms: Optional[int], end_ms: Optional[int]) -> Dict:
    block: Dict[str, Any] = {"rows": count}
    if start_ms is not None:
        block["range_beijing"] = {
            "start": beijing_text(start_ms), "end": beijing_text(end_ms)}
        block["range_ms"] = {"start": start_ms, "end": end_ms}
    return block


def save(path: Path, manifest: Dict[str, Any]) -> None:
    """原子落盘清单（同目录临时文件 + os.replace，防写一半损坏 provenance）。

    UTF-8、缩进、排序键，保证可 diff。
    """
    manifest["updated_at_beijing"] = datetime.now(_BEIJING_TZ).isoformat(
        timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    os.replace(tmp_path, path)
