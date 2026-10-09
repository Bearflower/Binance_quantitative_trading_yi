"""SQLite 落库层：建库、逐笔幂等写入、1s 稠密物化、滚动清理、缺口判定。

口径权威：docs/plans/grid_realtime_alert_实施计划.md §1.2/§1.3（S3 定稿）。
本模块只操作本地行情库，不碰 grid_realtime.sqlite3。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .models import insert_params

_MS_PER_SECOND = 1000
_MS_PER_HOUR = 3600 * _MS_PER_SECOND

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_UPSERT_SQL = (
    "INSERT OR IGNORE INTO agg_trades "
    "(agg_trade_id, symbol, price, quantity, first_trade_id, "
    "last_trade_id, trade_time_ms, is_buyer_maker, received_at_ms) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# 折叠决策点所需逐笔：T 落在 (下界, close]（基本 SQL，不依赖窗口函数）
_TRADES_FOR_POINTS_SQL = (
    "SELECT trade_time_ms, agg_trade_id, price FROM agg_trades "
    "WHERE symbol = ? AND trade_time_ms > ? AND trade_time_ms <= ? "
    "ORDER BY trade_time_ms, agg_trade_id"
)


def open_db(db_path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    """打开（必要时创建）行情库并确保 schema 就绪。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等执行 schema DDL。"""
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))


def upsert_agg_trades(conn: sqlite3.Connection, trades: Sequence[Dict]) -> int:
    """批量幂等写入逐笔，返回实际新增行数（事务由调用方提交）。"""
    if not trades:
        return 0
    before = conn.execute("SELECT total_changes()").fetchone()[0]
    conn.executemany(_UPSERT_SQL, [insert_params(t) for t in trades])
    after = conn.execute("SELECT total_changes()").fetchone()[0]
    return after - before


def get_latest_agg_trade_id(conn: sqlite3.Connection, symbol: str) -> "Optional[int]":
    """库内该品种最大 agg_trade_id，空表返回 None（续传定位，§1.4）。"""
    row = conn.execute(
        "SELECT MAX(agg_trade_id) FROM agg_trades WHERE symbol = ?", (symbol,)
    ).fetchone()
    return row[0]


def get_trade_time_bounds(conn: sqlite3.Connection, symbol: str):
    """返回该品种逐笔的 (min_trade_time_ms, max_trade_time_ms)，空表返回 (None, None)。"""
    row = conn.execute(
        "SELECT MIN(trade_time_ms), MAX(trade_time_ms) FROM agg_trades WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    return row[0], row[1]

def materialize_closed_seconds(conn: sqlite3.Connection, symbol: str,
                               materialized_at_ms: int,
                               up_to_ms: "Optional[int]" = None) -> int:
    """对已关闭整秒决策点做稠密物化（需求 §5.3/S3 口径），返回新增样本行数。

    p(t)=不晚于决策点 t.000 的最后一笔成交；关闭上界=floor(maxT)（该点窗口已有
    成交即关闭信号，maxT 恰为整秒时其本身也在 T<=t 内；与审计脚本 sample 点口径
    一致）；只增不改，幂等。
    """
    min_t, max_t = get_trade_time_bounds(conn, symbol)
    if max_t is None:
        return 0
    close_ms = _resolve_close_ms(max_t, up_to_ms)
    first_point, prev_anchor, lower_bound = _resume_point(conn, symbol, min_t)
    if first_point is None or first_point > close_ms:
        return 0
    rows = conn.execute(
        _TRADES_FOR_POINTS_SQL, (symbol, lower_bound, close_ms)
    )
    samples = _fold_dense_points(first_point, close_ms, prev_anchor, rows)
    return _insert_samples(conn, symbol, materialized_at_ms, samples)


def _resolve_close_ms(max_t: int, up_to_ms: "Optional[int]") -> int:
    """可物化决策点上界：floor(maxT)；maxT 本身在 T<=close 内（整秒时计入该点）。"""
    default_close = (max_t // _MS_PER_SECOND) * _MS_PER_SECOND
    if up_to_ms is None:
        return default_close
    return min(default_close, (up_to_ms // _MS_PER_SECOND) * _MS_PER_SECOND)


def _resume_point(conn, symbol, min_t):
    """定位首个决策点、前锚点与逐笔下界；首笔之前的决策点不落行（S3-ii）。

    首个决策点 = floor(首笔T)+1s（同审计脚本起点）。SQL 下界语义均为 T > bound：
    resume 取 last_sec；首跑取 floor(首笔)-1ms，使恰在 floor.000 的首笔也纳入。
    """
    last_sec = conn.execute(
        "SELECT MAX(sample_ms) FROM price_samples_1s WHERE symbol = ?", (symbol,)
    ).fetchone()[0]
    if last_sec is not None:
        row = conn.execute(
            "SELECT last_trade_id, price, anchor_time_ms "
            "FROM price_samples_1s WHERE sample_ms = ? AND symbol = ?",
            (last_sec, symbol),
        ).fetchone()
        return (last_sec + _MS_PER_SECOND,
                (row["last_trade_id"], row["price"], row["anchor_time_ms"]),
                last_sec)
    first_point = (min_t // _MS_PER_SECOND) * _MS_PER_SECOND + _MS_PER_SECOND
    return first_point, None, first_point - _MS_PER_SECOND - 1


def _fold_dense_points(first_point, close_ms, prev_anchor, rows):
    """折叠逐笔为稠密决策点行：T 按窗口 (t-1s,t] 归属（ceil），空缺点 count=0。"""
    point_sec = first_point
    anchor = prev_anchor
    count = 0
    last_trade = None
    samples: List[tuple] = []
    for row in rows:
        trade_time_ms, trade_id, price = row[0], row[1], row[2]
        # 归属窗口 (t-1s, t]：T 恰为整秒边界时属于 t 本身（ceil）；
        # 首笔落在 floor.000 的边角统一提升到首个决策点（与审计起点一致）
        trade_point = max(
            ((trade_time_ms + _MS_PER_SECOND - 1) // _MS_PER_SECOND) * _MS_PER_SECOND,
            first_point)
        while point_sec < trade_point:
            _append_closed(samples, point_sec, count, last_trade, anchor)
            point_sec += _MS_PER_SECOND
            count, last_trade = 0, None
        count += 1
        last_trade = (trade_id, price, trade_time_ms)
        anchor = last_trade
    while point_sec <= close_ms:
        _append_closed(samples, point_sec, count, last_trade, anchor)
        point_sec += _MS_PER_SECOND
        count, last_trade = 0, None
    return samples


def _append_closed(samples, sec, count, last_trade, anchor):
    """关闭决策点 sec：(sec-1s, sec] 有成交取最后一笔；无成交 count=0 沿用前锚点。"""
    if count > 0:
        trade_id, price, trade_time_ms = last_trade
        samples.append((sec, price, count, trade_id, trade_time_ms))
    elif anchor is not None:
        trade_id, price, trade_time_ms = anchor
        samples.append((sec, price, 0, trade_id, trade_time_ms))


def _insert_samples(conn, symbol, materialized_at_ms, samples) -> int:
    """INSERT OR IGNORE 批量写入样本（不自行提交，事务交调用方）。"""
    if not samples:
        return 0
    before = conn.execute("SELECT total_changes()").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO price_samples_1s "
        "(sample_ms, symbol, price, trade_count, last_trade_id, "
        "anchor_time_ms, materialized_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(s, symbol, p, c, tid, at, materialized_at_ms)
         for s, p, c, tid, at in samples],
    )
    after = conn.execute("SELECT total_changes()").fetchone()[0]
    return after - before


def materialize_and_cleanup(conn: sqlite3.Connection, symbol: str,
                            retention_hours: int, now_ms: int) -> Dict[str, int]:
    """单事务：先物化全部已关闭秒，再删过期逐笔（§1.3，防空洞）。"""
    with conn:
        materialized = materialize_closed_seconds(conn, symbol, now_ms)
        cutoff_ms = now_ms - retention_hours * _MS_PER_HOUR
        before = conn.execute("SELECT total_changes()").fetchone()[0]
        conn.execute(
            "DELETE FROM agg_trades WHERE symbol = ? AND trade_time_ms < ?",
            (symbol, cutoff_ms),
        )
        deleted = conn.execute("SELECT total_changes()").fetchone()[0] - before
    return {"materialized": materialized, "deleted": deleted, "cutoff_ms": cutoff_ms}


def has_gap(conn: sqlite3.Connection, symbol: str, since_ms: int) -> bool:
    """since_ms 之后 agg_trade_id 序列是否存在不连续（同品种 ID 应逐笔 +1，§1.5）。"""
    rows = conn.execute(
        "SELECT agg_trade_id FROM agg_trades WHERE symbol = ? AND trade_time_ms >= ? "
        "ORDER BY agg_trade_id",
        (symbol, since_ms),
    )
    prev_id = None
    for (trade_id,) in rows:
        if prev_id is not None and trade_id != prev_id + 1:
            return True
        prev_id = trade_id
    return False
