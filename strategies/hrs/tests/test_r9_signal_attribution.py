"""
R9: 影子信号归属隔离——按唯一 signal_id 精准更新（先红后绿）

验收标准见 docs/research/strategy_remediation/requirements_R7-R9_2026-10-16.md §3 R9
架构设计见 docs/research/strategy_remediation/architecture_R7-R9_2026-10-16.md §2.3

测试收紧的 FakeDB：
  - INSERT 必须带 RETURNING id，生成唯一递增 id 回填
  - UPDATE 必须按 WHERE id=$1 精准匹配（不再接受 symbol+direction 宽匹配）
  - signal_id=None 时 _update_signal_resolution 跳过更新（fail-safe）
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from strategies.hrs.strategy import HRSStrategy


# ==================== 收紧后的 FakeDB ====================


class TightSignalDB:
    """R9 收紧版 signal sink Fake。

    关键收紧点（需求 §1.4-3）：
      1. INSERT 必须带 RETURNING id，生成唯一递增 id，fetch_one 返回 row["id"]
      2. execute() 对 UPDATE hrs.hrs_signals 的 SQL 必须含 WHERE id=$1，否则 AssertionError
      3. UPDATE 只按 id 精准匹配一行，不再接受 symbol+direction 宽范围
      4. 支持预设 rows（用于「历史未决信号共存」场景）

    INSERT 参数映射（对齐生产 _log_signal_to_db 的 12 个位置参数，0-based）：
      args[0]=symbol, args[1]=direction, args[2]=total_score,
      args[3]=technical_score, args[4]=sentiment_score, args[5]=contract_score,
      args[6]=current_price, args[7]=resolution, args[8]=bb_position,
      args[9]=rsi_value, args[10]=entry_mode, args[11]=signal_origin
    """

    def __init__(self, pre_rows: Optional[List[Dict[str, Any]]] = None):
        self.rows: List[Dict[str, Any]] = list(pre_rows or [])
        self._next_id: int = max((r.get("id", 0) for r in self.rows), default=0) + 1
        # 记录每次 UPDATE 的参数（便于断言）
        self.update_calls: List[Dict[str, Any]] = []

    def _build_insert_row(self, args: tuple) -> Dict[str, Any]:
        """由 INSERT 位置参数构造信号行（execute / fetch_one 共用，消除重复）。"""
        return dict(
            symbol=args[0], direction=args[1],
            status="triggered", resolution=args[7],
            entry_mode=args[10] if len(args) > 10 else None,
            signal_origin=args[11] if len(args) > 11 else None,
        )

    async def execute(self, sql: str, *args, **kwargs) -> None:
        """INSERT / UPDATE / ALTER 的统一入口。

        - INSERT：如果 SQL 含 RETURNING，抛 AssertionError（调用方应该用 fetch_one）
        - UPDATE：必须 WHERE id=$1，否则 AssertionError；只更新 id 匹配的行
        - ALTER TABLE：静默通过（_ensure_db_schema 幂等迁移）
        """
        upper = sql.upper()
        if "INSERT INTO HRS.HRS_SIGNALS" in upper:
            if "RETURNING" in upper:
                # 调用方误把 RETURNING INSERT 交给了 execute → 必须用 fetch_one
                raise AssertionError(
                    "INSERT ... RETURNING 必须用 fetch_one 而非 execute "
                    f"(SQL: {sql[:80]}...)"
                )
            # 旧版不含 RETURNING 的 INSERT 先放行（修复前的过渡）
            # 但修复后应该全部走 fetch_one
            row = dict(id=None, **self._build_insert_row(args))
            self.rows.append(row)
        elif "UPDATE HRS.HRS_SIGNALS" in upper:
            # 收紧：必须按 WHERE id=$1 精准匹配
            if "WHERE ID = $1" not in upper:
                raise AssertionError(
                    "R9: UPDATE hrs_signals 必须按 WHERE id=$1 精准匹配，"
                    f"禁止宽范围 symbol+direction（SQL: {sql[:100]}...）"
                )
            signal_id = args[0]
            resolution = args[1]
            self.update_calls.append(dict(signal_id=signal_id, resolution=resolution))
            for row in self.rows:
                if row["id"] == signal_id and row["status"] == "triggered":
                    row["status"] = "resolved"
                    row["resolution"] = resolution
                    return
            # 找不到匹配行 → 静默跳过（与真实 DB 行为一致）
        elif "ALTER TABLE" in upper:
            # _ensure_db_schema 的幂等迁移，FakeDB 不维护列 schema
            return
        elif "CREATE TABLE" in upper:
            return
        else:
            raise AssertionError(f"未预期的 SQL: {sql[:100]}")

    async def fetch_one(self, sql: str, *args, **kwargs) -> Optional[Dict[str, Any]]:
        """只处理 INSERT ... RETURNING id。

        调用方向：_log_signal_to_db 改用 fetch_one 执行 INSERT RETURNING。
        """
        upper = sql.upper()
        if "INSERT INTO HRS.HRS_SIGNALS" in upper and "RETURNING" in upper:
            new_id = self._next_id
            self._next_id += 1
            row = dict(id=new_id, **self._build_insert_row(args))
            self.rows.append(row)
            return {"id": new_id}
        if "SELECT" in upper:
            # 预留：_ensure_db_schema 可能查列，本轮不依赖
            return None
        raise AssertionError(f"fetch_one 未预期的 SQL: {sql[:100]}")


# ==================== 测试夹具 ====================


def _make_strategy(fake_db: TightSignalDB, emm_mode: str = "shadow") -> HRSStrategy:
    """构造最小依赖的 HRSStrategy 实例。"""
    s = object.__new__(HRSStrategy)
    s.config = {"emm": {"execution_mode": emm_mode}}
    s.db = fake_db
    # 超时过滤 mock 直接放行（本轮不测超时）
    s._filter_timeout_signals = AsyncMock(side_effect=lambda signals, direction: signals)
    s.candidate_pool = MagicMock()
    s.candidate_pool.get_short_candidates.return_value = set()
    s.candidate_pool.get_long_candidates.return_value = set()
    s._send_cycle_summary = AsyncMock()
    return s


async def _run_finalize(strategy: HRSStrategy, signals: List[Dict]) -> None:
    """调 _finalize_and_execute_signals 主链（short_signals 放 signals，long_signals 空）。"""
    await strategy._finalize_and_execute_signals(
        short_signals=signals, long_signals=[],
        source_symbols=[s["symbol"] for s in signals],
    )


# ==================== 回归用例 ====================


class TestR9SignalAttribution:
    """R9: 影子信号归属隔离——三条验收断言的回归用例。"""

    @pytest.mark.asyncio
    async def test_r9_production_and_shadow_get_distinct_resolution(self):
        """同币同向 production(standard)+shadow(emm) → ['executed', 'shadow']。

        这是 reproduce_findings.py signal_probe 的反转场景。
        修复前（宽范围 UPDATE）：两条均被标 'executed'（影子被污染）→ 测试红
        修复后（按 id 精准更新）：production='executed', shadow='shadow' → 测试绿
        """
        fake_db = TightSignalDB()
        strategy = _make_strategy(fake_db)
        # 注入 execute_signal：standard → 正常执行；emm → shadow_blocked=True
        async def _execute(signal):
            signal["shadow_blocked"] = signal.get("entry_mode") == "emm"
            return True
        strategy.execute_signal = _execute

        signals = [
            dict(symbol="TESTUSDT", direction="short", score=8,
                 entry_mode="standard", signal_origin="main", current_price=100),
            dict(symbol="TESTUSDT", direction="short", score=6,
                 entry_mode="emm", signal_origin="shadow_scoring", current_price=100),
        ]
        await _run_finalize(strategy, signals)

        # 断言：两条信号的 resolution 严格分离
        resolutions = [r["resolution"] for r in fake_db.rows]
        assert set(resolutions) == {"executed", "shadow"}, (
            f"R9 污染残留：期望 ['executed','shadow']，实际 {resolutions}"
        )

    @pytest.mark.asyncio
    async def test_r9_multiple_shadows_each_get_own_resolution(self):
        """同周期多条 shadow → 各自 resolution='shadow'，互不覆盖。

        修复前：宽范围 UPDATE 把同币同向多条 shadow 一起改了 → 测试红
        修复后：按 id 精准更新 → 每条均正确标 'shadow' → 测试绿
        """
        fake_db = TightSignalDB()
        strategy = _make_strategy(fake_db)
        async def _execute(signal):
            signal["shadow_blocked"] = True  # 全部影子
            return True
        strategy.execute_signal = _execute

        signals = [
            dict(symbol="TESTUSDT", direction="short", score=8,
                 entry_mode="emm", signal_origin="shadow_scoring_a", current_price=100),
            dict(symbol="TESTUSDT", direction="short", score=7,
                 entry_mode="emm", signal_origin="shadow_scoring_b", current_price=100),
            dict(symbol="TESTUSDT", direction="short", score=5,
                 entry_mode="semi_emm", signal_origin="semi_shadow", current_price=100),
        ]
        await _run_finalize(strategy, signals)

        for row in fake_db.rows:
            assert row["resolution"] == "shadow", (
                f"R9 多条影子污染：id={row['id']} resolution={row['resolution']!r}，"
                "应为 'shadow'"
            )

    @pytest.mark.asyncio
    async def test_r9_update_only_touches_target_row_when_history_exists(self):
        """存在历史未决（status='triggered'）行时，本轮更新不误伤历史行。

        修复前：WHERE symbol+direction 宽范围 → 历史行一起被改 → 测试红
        修复后：WHERE id=$1 精准匹配 → 历史行保持 triggered → 测试绿
        """
        # 预设一条历史未决信号（id=1，同币同向）
        fake_db = TightSignalDB(pre_rows=[
            dict(id=1, symbol="TESTUSDT", direction="short",
                 status="triggered", resolution=None,
                 entry_mode="standard", signal_origin="old_cycle"),
        ])
        strategy = _make_strategy(fake_db)
        async def _execute(signal):
            signal["shadow_blocked"] = False
            return True
        strategy.execute_signal = _execute

        # 本轮产生一条新 production 信号
        signals = [
            dict(symbol="TESTUSDT", direction="short", score=9,
                 entry_mode="standard", signal_origin="main", current_price=100),
        ]
        await _run_finalize(strategy, signals)

        # 断言：历史行(id=1) 仍保持 triggered
        old_row = next(r for r in fake_db.rows if r["id"] == 1)
        assert old_row["status"] == "triggered", (
            f"R9 误伤历史行：历史行 id=1 status={old_row['status']!r}，"
            "应为 'triggered'"
        )
        assert old_row["resolution"] is None, (
            f"R9 误伤历史行：历史行 id=1 resolution={old_row['resolution']!r}，"
            "应为 None"
        )


class TestR9InsertFailSafe:
    """R9 fail-safe：INSERT 失败（signal_id 缺失）时绝不发起 UPDATE。

    覆盖 _log_signal_to_db 异常路径（except → return None，不回填 signal_id）
    与 _update_signal_resolution 的 signal_id is None fail-safe 分支
    （绝不回退 symbol+direction 宽范围 WHERE）。
    """

    @pytest.mark.asyncio
    async def test_insert_failure_never_updates_db(self):
        """fetch_one 对 INSERT 抛异常 → signal_id 未回填 → UPDATE 次数必须为 0。"""

        class BrokenInsertDB(TightSignalDB):
            """INSERT 持久化失败（模拟 DB 不可用），execute 仍可记录。"""

            async def fetch_one(self, sql: str, *args, **kwargs):
                if "INSERT INTO HRS.HRS_SIGNALS" in sql.upper():
                    raise RuntimeError("模拟 DB 不可用")
                return None

        fake_db = BrokenInsertDB()
        strategy = _make_strategy(fake_db)

        async def _execute(signal):
            signal["shadow_blocked"] = False
            return True
        strategy.execute_signal = _execute

        signals = [
            dict(symbol="TESTUSDT", direction="short", score=9,
                 entry_mode="standard", signal_origin="main", current_price=100),
        ]
        await _run_finalize(strategy, signals)

        # 关键断言 1：signal_id 缺失 → fail-safe 跳过 → 一次 UPDATE 都不发生
        assert fake_db.update_calls == [], (
            f"signal_id 缺失时必须跳过 UPDATE（fail-safe），实际 {fake_db.update_calls}"
        )
        # 关键断言 2：信号 dict 未回填 signal_id（INSERT 失败路径 return None）
        assert signals[0].get("signal_id") is None, (
            "INSERT 失败时 signal_id 不应被回填"
        )


class TestR9TimeoutAfterPersist:
    """R9 顺序对调：先落库拿到 signal_id，再超时过滤按 id 标 timeout。

    覆盖生产关键调整（strategy.py:1396-1406 + 3905-3908）：
      旧顺序「先过滤后落库」→ 超时信号未落库、无 signal_id → 只能宽范围 UPDATE；
      新顺序「先落库后过滤」→ 超时信号先 INSERT 回填 signal_id，再按 id 精准标 timeout。
    本用例让 _filter_timeout_signals 走真实实现（不 mock 直通），
    直接断言「INSERT 先行 + timeout 按 id 命中」。
    """

    @pytest.mark.asyncio
    async def test_timeout_signal_persisted_then_marked_by_id(self):
        fake_db = TightSignalDB()
        strategy = _make_strategy(fake_db)

        # 恢复真实 _filter_timeout_signals（夹具 mock 成了直通），
        # 并构造一条已超时的信号时间戳（48h 前首次触发，超过 24 根K线阈值）
        del strategy._filter_timeout_signals  # 去掉实例上的 AsyncMock，回落类方法
        strategy.abandon_timeout_bars = 24
        first_trigger = (
            datetime.now(timezone.utc) - timedelta(hours=48)
        ).isoformat()
        strategy._signal_timestamps = {"TESTUSDT": {"short": first_trigger}}

        # execute_signal 记录调用：超时信号被过滤后不应执行
        executed: List[Dict] = []

        async def _execute(signal):
            executed.append(signal)
            return True
        strategy.execute_signal = _execute

        signals = [
            dict(symbol="TESTUSDT", direction="short", score=9,
                 entry_mode="standard", signal_origin="main", current_price=100),
        ]
        await _run_finalize(strategy, signals)

        # 断言 1：信号先被 INSERT（行存在且 signal_id 已回填到同一 dict 引用）
        assert len(fake_db.rows) == 1, f"超时信号也应先落库，实际 rows={fake_db.rows}"
        row = fake_db.rows[0]
        assert row["symbol"] == "TESTUSDT" and row["direction"] == "short"
        assert signals[0].get("signal_id") is not None, (
            "R9 顺序对调：超时信号必须先落库拿到 signal_id"
        )
        # 断言 1b：归属列映射正确（0-based args[10]=entry_mode, args[11]=signal_origin；
        # 若 off-by-one 则 entry_mode 会错取到 signal_origin 的 'main'）
        assert row["entry_mode"] == "standard", (
            f"entry_mode 列映射错位，实际 {row['entry_mode']!r}（应来自 args[10]）"
        )
        assert row["signal_origin"] == "main", (
            f"signal_origin 列映射错位，实际 {row['signal_origin']!r}（应来自 args[11]）"
        )

        # 断言 2：最终 resolution 为 'timeout'，且 UPDATE 以该 id 精准命中
        assert fake_db.update_calls, "超时信号必须触发一次 resolution 更新"
        assert len(fake_db.update_calls) == 1
        update = fake_db.update_calls[0]
        assert update["resolution"] == "timeout", (
            f"超时信号应标 'timeout'，实际 {update['resolution']!r}"
        )
        assert update["signal_id"] == signals[0]["signal_id"], (
            "UPDATE 必须以落库回填的 signal_id 精准命中（非宽范围）"
        )
        assert row["resolution"] == "timeout", (
            f"DB 行最终 resolution 应为 'timeout'，实际 {row['resolution']!r}"
        )

        # 断言 3：超时信号被过滤，不应进入 execute_signal
        assert executed == [], "超时信号被过滤后不应执行 execute_signal"
