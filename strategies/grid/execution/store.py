"""Durable decision handoff for the separate order-grid executor.

Migration is explicit. The signal producer and executor refuse incompatible schemas.
"""

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Mapping, Optional

from strategies.grid.market_state import MarketState


SCHEMA_VERSION = 1


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.row_factory = sqlite3.Row
    return conn


def migrate(path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        if row and row[0] != str(SCHEMA_VERSION):
            raise RuntimeError("execution schema version mismatch")
        conn.execute("INSERT OR IGNORE INTO schema_meta(key,value) VALUES('version',?)",
                     (str(SCHEMA_VERSION),))
        conn.execute("""CREATE TABLE IF NOT EXISTS decisions (
            decision_id TEXT PRIMARY KEY,
            calculated_at_ms INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS decisions_time ON decisions(calculated_at_ms,decision_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS execution_cursor (
            id INTEGER PRIMARY KEY CHECK(id=1), decision_id TEXT REFERENCES decisions(decision_id)
        )""")
        conn.execute("INSERT OR IGNORE INTO execution_cursor(id,decision_id) VALUES(1,NULL)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _validate(payload: Mapping) -> None:
    if payload.get("symbol") != "ETHUSDT":
        raise ValueError("decision symbol must be ETHUSDT")
    state = payload.get("state")
    if state not in MarketState.__members__:
        raise ValueError("invalid decision state")
    if not isinstance(payload.get("calculated_at_ms"), int) or payload["calculated_at_ms"] <= 0:
        raise ValueError("invalid decision timestamp")
    times = payload.get("last_closed_kline_ms")
    if not isinstance(times, dict) or set(times) != {"15m", "1h", "4h"}:
        raise ValueError("missing Kline close timestamps")
    if any(not isinstance(value, int) or value < 0 for value in times.values()):
        raise ValueError("invalid Kline close timestamp")


class DecisionStore:
    def __init__(self, path):
        self.conn = _connect(Path(path))
        try:
            row = self.conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if row is None or row[0] != str(SCHEMA_VERSION):
                raise RuntimeError("execution schema version mismatch")
            self.conn.execute("SELECT decision_id FROM decisions LIMIT 1")
        except sqlite3.Error as exc:
            self.conn.close()
            raise RuntimeError("execution schema missing; run migration") from exc

    def close(self):
        self.conn.close()

    def publish(self, payload: Mapping, config_hash: str) -> str:
        _validate(payload)
        if not config_hash:
            raise ValueError("config hash required")
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256((canonical + config_hash).encode()).hexdigest()
        decision_id = "grid-" + digest[:32]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT MAX(calculated_at_ms) FROM decisions").fetchone()
            if row[0] is not None and payload["calculated_at_ms"] < row[0]:
                raise ValueError("decision timestamp moved backwards")
            self.conn.execute("""INSERT OR IGNORE INTO decisions
                (decision_id,calculated_at_ms,payload_json,config_hash,created_at_ms)
                VALUES(?,?,?,?,?)""", (decision_id, payload["calculated_at_ms"], canonical,
                                      config_hash, int(time.time() * 1000)))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return decision_id

    def cursor(self) -> Optional[str]:
        row = self.conn.execute("SELECT decision_id FROM execution_cursor WHERE id=1").fetchone()
        return row[0]

    def next_decision(self) -> Optional[dict]:
        cursor = self.cursor()
        if cursor is None:
            row = self.conn.execute("""SELECT * FROM decisions
                ORDER BY rowid LIMIT 1""").fetchone()
        else:
            current = self.conn.execute("SELECT rowid FROM decisions WHERE decision_id=?",
                                        (cursor,)).fetchone()
            if current is None:
                raise RuntimeError("execution cursor has no decision")
            row = self.conn.execute("""SELECT * FROM decisions WHERE rowid>?
                ORDER BY rowid LIMIT 1""", (current[0],)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        _validate(payload)
        payload["decision_id"] = row["decision_id"]
        payload["config_hash"] = row["config_hash"]
        return payload

    def acknowledge(self, decision_id: str) -> None:
        next_item = self.next_decision()
        if next_item is None or next_item["decision_id"] != decision_id:
            raise ValueError("decision is not next")
        self.conn.execute("UPDATE execution_cursor SET decision_id=? WHERE id=1", (decision_id,))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    args = parser.parse_args()
    migrate(args.db_path)
