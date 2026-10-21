# -*- coding: utf-8 -*-
"""功能矩阵 5 + 6：ReadinessAuditor

- 矩阵 5：config purpose_driven_mvp.readiness 的 8 个键都被真实读取（改配置改门禁）
- 矩阵 6：hrs 缺口报告（信号/K线/快照 → 结构化 gaps 不崩）
- 覆盖 auditor 全部查询分支、异常分支与策略选择逻辑（DB 用 FakeDB 桩）

v2 修复说明（2026-10-18）：
- 信号表探测从「遍历已知 schema 找 trade_signals + 校验 strategy_id 列」
  改为「schema 直接是 strategy_id + ILIKE '%signal%' 探测表 + 候选时间列集合」
- K 线表定位从「任意取第一张」改为「先查主力合约 symbol 再按 symbol 找表」
- 时间列类型探测 + 绑定值转换（naive 去 tzinfo / aware 保留 UTC）消除 asyncpg 冲突
"""

import pytest

from ai_tuner.readiness.auditor import ReadinessAuditor
from ai_tuner.readiness.models import CandidateReadiness, ReadinessResult
from tests.test_ai_tuner_mvp.conftest import BASELINE_RESPONSES, FakeDB


@pytest.fixture
def auditor(config, fake_db):
    """全达标基线审计器"""
    return ReadinessAuditor(fake_db, config)


# ===========================================================================
# 矩阵 5：阈值全部来自 config（配置驱动而非硬编码）
# ===========================================================================


class TestConfigDrivenThresholds:
    async def test_baseline_all_pass_selects_first_candidate(self, auditor, fake_db, as_of, config):
        """基线数据下：aggressive 因快照 not_required 而全过门禁 → SELECT
        （hrs 快照 required 但 coverage=0 → ineligible，不影响 aggressive 的选择）"""
        result = await auditor.run(as_of=as_of)
        assert result.decision == "SELECT"
        assert result.selected_strategy == config["purpose_driven_mvp"]["candidates"][0]
        assert result.window_days == 90
        assert result.candidates["btc_eth_aggressive"].eligible
        assert not result.candidates["hrs"].eligible
        # 验证 aggressive 的快照 requirement/reason
        aggr = result.candidates["btc_eth_aggressive"]
        assert aggr.external_snapshot_requirement == "not_required_for_current_manifest"
        assert aggr.external_snapshot_reason == "not_required_for_current_manifest"

    async def test_hrs_snapshot_required_gate_blocks_eligibility(self, auditor, as_of):
        """hrs 快照 required + coverage=0 → ineligible，failures 含 required_historical_external_snapshots_missing"""
        result = await auditor.run(as_of=as_of)
        hrs = result.candidates["hrs"]
        assert not hrs.eligible
        assert hrs.external_snapshot_requirement == "required"
        assert hrs.external_snapshot_reason == "required_historical_external_snapshots_missing"
        assert any("required_historical_external_snapshots_missing" in f for f in hrs.failures)

    async def test_min_closed_trades_from_config(self, config, fake_db, as_of):
        """min_closed_trades 改为 101 → 出现对应失败项（阈值生效）"""
        config["purpose_driven_mvp"]["readiness"]["min_closed_trades"] = 101
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        assert any("已平仓交易 100 < 101" in f for f in result.candidates["btc_eth_aggressive"].failures)

    async def test_min_effective_signals_from_config(self, config, fake_db, as_of):
        config["purpose_driven_mvp"]["readiness"]["min_effective_signals"] = 201
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        assert any("有效信号 200 < 201" in f for f in result.candidates["btc_eth_aggressive"].failures)

    async def test_min_trade_field_completeness_from_config(self, config, fake_db, as_of):
        config["purpose_driven_mvp"]["readiness"]["min_trade_field_completeness"] = 1.1
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        failures = result.candidates["btc_eth_aggressive"].failures
        assert any("交易字段完整度" in f and "110.00%" in f for f in failures)

    async def test_min_config_identity_coverage_from_config(self, config, fake_db, as_of):
        config["purpose_driven_mvp"]["readiness"]["min_config_identity_coverage"] = 0.91
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        failures = result.candidates["btc_eth_aggressive"].failures
        assert any("配置身份覆盖" in f and "90.00%" in f and "91.00%" in f for f in failures)

    async def test_min_kline_coverage_from_config(self, config, fake_db, as_of):
        config["purpose_driven_mvp"]["readiness"]["min_kline_coverage"] = 1.01
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        failures = result.candidates["btc_eth_aggressive"].failures
        assert any("K线覆盖" in f and "101.00%" in f for f in failures)

    async def test_min_external_snapshot_coverage_no_fallback(self, config, fake_db, as_of):
        """per_strategy 覆盖 aggressive: required+0.5，coverage=0 触发 failure"""
        per = config["purpose_driven_mvp"]["readiness"]["per_strategy"]
        per["btc_eth_aggressive"]["external_snapshot_requirement"] = "required"
        per["btc_eth_aggressive"]["min_external_snapshot_coverage"] = 0.5
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        failures = result.candidates["btc_eth_aggressive"].failures
        assert any("外部快照覆盖" in f and "50.00%" in f for f in failures)
        assert any("required_historical_external_snapshots_missing" in f for f in failures)

    async def test_min_regime_count_from_config(self, config, fake_db, as_of):
        config["purpose_driven_mvp"]["readiness"]["min_regime_count"] = 6
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        failures = result.candidates["btc_eth_aggressive"].failures
        assert any("regime_count(proxy) 5 < 6" in f for f in failures)

    async def test_window_days_drives_kline_expectation(self, config, as_of):
        """window_days 真实生效：同样 actual=100，窗口 1 天达标、90 天不达标。

        新口径走 generate_series，FakeDB 的 COUNT(*) AS expected 路由键返回
        (actual, expected)——我们按窗口天数手工指定 expected 来模拟不同窗口长度。
        """
        base = dict(BASELINE_RESPONSES)

        # 1 天窗口：预期 24 根（aggressive 默认 1h interval），actual=100 → 覆盖≈4.17 → 封顶 1.0 → 通过
        responses_short = dict(base)
        responses_short["COUNT(*) AS expected"] = {"actual": 100, "expected": 24}
        config["purpose_driven_mvp"]["readiness"]["window_days"] = 1
        ok_result = await ReadinessAuditor(FakeDB(responses_short), config).run(as_of=as_of)
        assert not any("K线覆盖" in f for f in ok_result.candidates["btc_eth_aggressive"].failures)

        # 90 天窗口：预期 2160 根，actual=100 → 覆盖≈4.6% → 不通过
        responses_long = dict(base)
        responses_long["COUNT(*) AS expected"] = {"actual": 100, "expected": 2160}
        config["purpose_driven_mvp"]["readiness"]["window_days"] = 90
        fail_result = await ReadinessAuditor(FakeDB(responses_long), config).run(as_of=as_of)
        assert any("K线覆盖" in f for f in fail_result.candidates["btc_eth_aggressive"].failures)

    async def test_identity_baseline_from_config_not_hardcoded(self, config, fake_db, as_of):
        """M2：identity baseline 来自 config（改 baseline 即改门禁结果）"""
        config["purpose_driven_mvp"]["identity_ratio_baseline"] = 0.5
        config["purpose_driven_mvp"]["readiness"]["min_config_identity_coverage"] = 0.6
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        failures = result.candidates["btc_eth_aggressive"].failures
        assert any("配置身份覆盖 50.00% < 60.00%" in f for f in failures)

    async def test_window_start_passed_to_sql(self, auditor, fake_db, as_of):
        """窗口以绑定变量 $2/$3 传入 SQL（参数化，无字面量拼接）"""
        await auditor.run(as_of=as_of)
        trade_sql = next(s for s in fake_db.sql_log if "COUNT(*) AS closed_trades" in s)
        assert "$2" in trade_sql and "$3" in trade_sql

    async def test_as_of_defaults_to_now(self, auditor, fake_db):
        """as_of 为 None 时取当前 UTC 时间"""
        result = await auditor.run()
        assert result.as_of.endswith("+00:00")


# ===========================================================================
# 时区绑定转换（V1 新增：naive 列去 tz / aware 列保留 UTC）
# ===========================================================================


class TestTimezoneBinding:
    """验证 _bind_window 按列类型正确转换时间绑定值（消除 asyncpg 冲突的核心修复）"""

    async def test_naive_column_receives_naive_value(self, fake_db, config, as_of):
        """trading.trade_records.executed_at 是 naive → 绑定值应不带 tzinfo"""
        await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        # 找到 trade_records 主查询的调用参数
        trade_call = next(
            c for c in fake_db.call_args_log
            if "COUNT(*) AS closed_trades" in c[1]
        )
        _, sql, args = trade_call
        # args[0] = strategy_id, args[1] = window_start（应 naive）, args[2] = window_end（应 naive）
        ws, we = args[1], args[2]
        assert ws.tzinfo is None, f"naive 列应收到 naive 值，实际 tzinfo={ws.tzinfo}"
        assert we.tzinfo is None, f"naive 列应收到 naive 值，实际 tzinfo={we.tzinfo}"

    async def test_aware_column_receives_aware_utc(self, config, as_of):
        """hrs.hrs_signals.triggered_at 是 aware → 绑定值应带 UTC tzinfo"""
        db = FakeDB()
        await ReadinessAuditor(db, config).run(as_of=as_of)
        # 找 hrs 信号计数查询（SQL 含 FROM hrs.hrs_signals）
        hrs_signal_call = next(
            c for c in db.call_args_log
            if "FROM hrs.hrs_signals" in c[1]
        )
        _, sql, args = hrs_signal_call
        ws, we = args[0], args[1]
        assert ws.tzinfo is not None, "aware 列应收到 aware 值"
        assert we.tzinfo is not None, "aware 列应收到 aware 值"
        assert ws.utcoffset() is not None, "应为 UTC 或带 offset"


# ===========================================================================
# 矩阵 6：缺口报告（数据缺失不崩、gaps 结构化）
# ===========================================================================


class TestHrsGapReporting:
    async def test_all_data_sources_missing_produces_structured_gaps(self, config, as_of):
        """全部数据源探测失败：signals=0 / kline=0 / snapshot=0，gaps 逐项说明且不崩"""
        db = FakeDB(responses={})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        for cr in result.candidates.values():
            assert cr.effective_signals == 0
            assert cr.kline_coverage == 0.0
            assert cr.external_snapshot_coverage == 0.0
            assert isinstance(cr.gaps, list) and cr.gaps
            # 新文案："schema xxx 不存在" 或 "schema xxx 下无信号表"
            assert any("无信号表" in g or "schema" in g.lower() for g in cr.gaps)
            # K 线缺口文案：table_missing / universe_not_configured / historical_universe_missing（新口径）
            assert any(
                "table_missing" in g
                or "universe_not_configured" in g
                or "historical_universe_missing" in g
                for g in cr.gaps
            ), cr.gaps
            assert any("regime_count 是 proxy" in g for g in cr.gaps)
            assert any("inferred" in g for g in cr.gaps)
        # aggressive 快照 not_required → gap 含 not_required 文案
        aggr = result.candidates["btc_eth_aggressive"]
        assert any("not_required_for_current_manifest" in g for g in aggr.gaps)
        # hrs 快照 required → gap 含原始快照表缺失文案
        hrs = result.candidates["hrs"]
        assert any("无历史外部快照表" in g for g in hrs.gaps)
        assert result.decision == "STOP"

    async def test_hrs_schema_probe_returns_empty(self, config, as_of):
        """模拟 hrs schema 不存在（schema 探测返回 None）→ effective_signals 记 0"""
        responses = dict(BASELINE_RESPONSES)
        # 覆盖 schema 存在性探测：让所有策略的 schema 探测都返回 None
        responses["FROM information_schema.schemata WHERE schema_name = $1"] = None
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        hrs = result.candidates["hrs"]
        assert hrs.effective_signals == 0
        assert any("schema" in g.lower() and "不存在" in g for g in hrs.gaps)

    async def test_gap_report_does_not_crash_run(self, config, as_of):
        """缺口只进报告不抛异常：run 正常返回完整结果"""
        db = FakeDB(responses={})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        assert isinstance(result, ReadinessResult)
        assert set(result.candidates.keys()) == {"btc_eth_aggressive", "hrs"}
        # 报告可序列化
        data = result.to_dict()
        assert data["decision"] == "STOP"
        assert "btc_eth_aggressive" in data["candidates"]


# ===========================================================================
# 信号表探测与查询（V1 重写：aggressive/hrs 各走各自 schema/表/时间列）
# ===========================================================================


class TestSignalDetection:
    async def test_aggressive_trade_signals_detected_and_counted(self, fake_db, config, as_of):
        """aggressive 策略：schema 直接用 btc_eth_aggressive → 表 trade_signals → 时间列 created_at"""
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.effective_signals == 200
        # 验证 FakeDB 确实发起了 aggressive 的信号计数查询
        assert any("FROM btc_eth_aggressive.trade_signals" in s for s in fake_db.sql_log)
        # 验证没有发起旧的 btc_eth.trade_signals 查询（不属于 aggressive）
        assert not any("FROM btc_eth.trade_signals" in s for s in fake_db.sql_log)

    async def test_hrs_uses_triggered_at_not_signal_time(self, fake_db, config, as_of):
        """hrs 策略：schema 直接用 hrs → 表 hrs_signals → 时间列 triggered_at
        （不再用旧代码虚构的 signal_time 列）"""
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        cr = result.candidates["hrs"]
        assert cr.effective_signals == 200
        # 计数 SQL 必须含 triggered_at 关键词（证明探测正确）
        assert any("triggered_at" in s and "FROM hrs.hrs_signals" in s
                   for s in fake_db.sql_log)

    async def test_signal_count_query_failure_records_gap(self, config, as_of):
        """信号计数查询异常 → 0 + gap 说明（非静默）"""
        db = FakeDB(errors={"FROM btc_eth_aggressive.trade_signals": RuntimeError("超时")})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.effective_signals == 0
        assert any("信号计数查询失败" in g for g in cr.gaps)

    async def test_signal_schema_probe_failure_treated_as_missing(self, config, as_of):
        """schema 探测查询异常 → 视为表不存在 → 信号记 0 + gap"""
        db = FakeDB(errors={
            "FROM information_schema.schemata WHERE schema_name = $1": RuntimeError("权限不足")
        })
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        # schema 探测异常 → 返回 False → 信号计数记 0 + gap
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.effective_signals == 0
        assert any("schema" in g.lower() and "不存在" in g for g in cr.gaps)

    async def test_signal_table_probe_failure(self, config, as_of):
        """信号表探测（ILIKE '%signal%'）异常 → 信号记 0 + gap"""
        db = FakeDB(errors={
            "WHERE table_schema = 'btc_eth_aggressive' AND table_name ILIKE": RuntimeError("boom")
        })
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.effective_signals == 0
        # 探测异常 → 返回 None → gap 文案含信号表相关内容
        assert any("无信号表" in g or "查询失败" in g for g in cr.gaps)


# ===========================================================================
# K 线覆盖：逐 symbol generate_series + 加权覆盖（阶段二 Task 3 新口径）
#
# 旧口径"主力合约抽样"已废弃——新口径走 kline_coverage.compute_kline_coverage
# 内部：universe resolver → 逐 symbol × interval → generate_series COUNT DISTINCT open_time。
# 以下测试全部验证新口径行为，不再断言主力合约相关 SQL。
# ===========================================================================


class TestKlineDominantSymbol:
    async def test_kline_coverage_via_generate_series_not_dominant_symbol(self, fake_db, config, as_of):
        """新口径：K 线覆盖用 generate_series SQL，不再按主力合约取表。"""
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        # 全达标基线：coverage=1.0
        assert cr.kline_coverage == 1.0
        # 验证：FakeDB 里出现 generate_series SQL（新口径）
        assert any("generate_series" in s for s in fake_db.sql_log), (
            "K 线覆盖应使用 generate_series SQL"
        )
        # 验证：**没有**主力合约 GROUP BY 子串（旧口径已退出主流程）
        assert not any("GROUP BY symbol ORDER BY COUNT(*) DESC" in s for s in fake_db.sql_log), (
            "主力合约探测应已退出 K 线覆盖主流程"
        )

    async def test_all_tables_missing_produces_zero_with_table_missing_gap(self, config, as_of):
        """所有 K 线表不存在 → coverage=0 + gap 含 table_missing"""
        responses = dict(BASELINE_RESPONSES)
        # 表存在性探测返回 None → 所有 pair 按缺表计
        responses["SELECT 1 FROM information_schema.tables"] = None
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.kline_coverage == 0.0
        assert any("table_missing" in g for g in cr.gaps), cr.gaps

    async def test_kline_compute_exception_produces_zero_coverage(self, config, as_of):
        """generate_series 计数异常 → _query_count catch 后返回 (0,0) → coverage=0（不崩）"""
        db = FakeDB(errors={"COUNT(*) AS expected": RuntimeError("query timeout")})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.kline_coverage == 0.0

    async def test_kline_coverage_capped_at_one(self, config, as_of):
        """baseline 全达标：所有 pair actual=expected → aggregate=1.0

        aggregate_coverage 本身不封顶（sum(actual)/sum(expected)），
        但 baseline FakeDB responses 让 actual==expected → 比值正好 1.0。
        """
        result = await ReadinessAuditor(FakeDB(), config).run(as_of=as_of)
        assert result.candidates["btc_eth_aggressive"].kline_coverage == 1.0

    async def test_kline_zero_window_coverage_zero(self, config, as_of):
        """window_days=0 → 预期桶数为 0 → coverage=0（除零守卫）

        FakeDB 的 COUNT(*) AS expected 路由固定返回 2160，
        但 window_days=0 时 generate_series 实际返回 0 行。
        这里覆盖 FakeDB 让 COUNT(*) 返回 0/0 来模拟真实行为。
        """
        config["purpose_driven_mvp"]["readiness"]["window_days"] = 0
        responses = dict(BASELINE_RESPONSES)
        responses["COUNT(*) AS expected"] = {"actual": 0, "expected": 0}
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        assert result.candidates["btc_eth_aggressive"].kline_coverage == 0.0


# ===========================================================================
# 其他查询分支与异常分支（trade_records / regime / 快照）
# ===========================================================================


class TestOtherQueryBranches:
    async def test_trade_records_row_none_defaults(self, config, as_of):
        """trade_records 查询返回 None → (0, 1.0)（新库无数据视为字段完整）"""
        responses = dict(BASELINE_RESPONSES)
        responses["AS closed_trades"] = None
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.closed_trades == 0
        assert cr.complete_trade_ratio == 1.0

    async def test_trade_records_null_fields_coerced(self, config, as_of):
        """行内字段为 NULL 时按 0 兜底"""
        responses = dict(BASELINE_RESPONSES)
        responses["AS closed_trades"] = {"closed_trades": None, "complete_ratio": None}
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.closed_trades == 0
        assert cr.complete_trade_ratio == 1.0

    async def test_trade_records_query_failure_zeros_and_fails(self, config, as_of):
        """trade_records 查询异常 → (0, 0.0) → 触发 trades 与完整度两项失败"""
        db = FakeDB(errors={"AS closed_trades": RuntimeError("连接断开")})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.closed_trades == 0
        assert cr.complete_trade_ratio == 0.0
        assert any("已平仓交易 0 < 20" in f for f in cr.failures)
        assert any("交易字段完整度" in f for f in cr.failures)

    async def test_regime_query_failure_silent_zero(self, config, as_of):
        """regime 查询异常 → 静默记 0（不阻断）"""
        db = FakeDB(errors={"COUNT(DISTINCT symbol) AS syms": RuntimeError("boom")})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        assert result.candidates["btc_eth_aggressive"].regime_count == 0

    async def test_regime_row_none_zero(self, config, as_of):
        responses = dict(BASELINE_RESPONSES)
        responses["COUNT(DISTINCT symbol) AS syms"] = None
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        assert result.candidates["btc_eth_aggressive"].regime_count == 0

    async def test_external_snapshot_always_zero_with_gap(self, auditor, as_of):
        """快照表不存在 → coverage 恒 0；aggressive not_required → 自定义 gap 文案"""
        result = await auditor.run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.external_snapshot_coverage == 0.0
        assert any("not_required_for_current_manifest" in g for g in cr.gaps)


# ===========================================================================
# 策略选择（确定性顺序）
# ===========================================================================


class TestSelectStrategy:
    @staticmethod
    def make_candidate(eligible):
        return CandidateReadiness(
            strategy_id="x",
            closed_trades=1, effective_signals=1, regime_count=1,
            complete_trade_ratio=1.0, config_identity_ratio=1.0,
            kline_coverage=1.0, external_snapshot_coverage=1.0,
            failures=[] if eligible else ["人为失败"],
        )

    def test_first_eligible_wins(self):
        candidates = {
            "a": self.make_candidate(eligible=False),
            "b": self.make_candidate(eligible=True),
        }
        assert ReadinessAuditor._select_strategy(candidates) == "b"

    def test_order_preserved_from_config(self):
        """两个都合格时选 config 顺序的第一个（dict 插入序）"""
        candidates = {
            "first": self.make_candidate(eligible=True),
            "second": self.make_candidate(eligible=True),
        }
        assert ReadinessAuditor._select_strategy(candidates) == "first"

    def test_none_eligible_returns_none(self):
        candidates = {
            "a": self.make_candidate(eligible=False),
            "b": self.make_candidate(eligible=False),
        }
        assert ReadinessAuditor._select_strategy(candidates) is None

    def test_empty_candidates_returns_none(self):
        assert ReadinessAuditor._select_strategy({}) is None

    async def test_run_stop_when_all_fail(self, config, as_of):
        """全候选不合格 → STOP 且 selected 为 None，报告含失败原因"""
        db = FakeDB(responses={})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        assert result.decision == "STOP"
        assert result.selected_strategy is None
        assert all(not cr.eligible for cr in result.candidates.values())
        assert all(cr.failures for cr in result.candidates.values())


# ===========================================================================
# gaps 组装细节
# ===========================================================================


class TestGapsBuilding:
    async def test_identity_full_ratio_no_inferred_note(self, config, fake_db, as_of):
        """identity baseline = 1.0 时不追加 inferred 标注，但保留 proxy 标注"""
        config["purpose_driven_mvp"]["identity_ratio_baseline"] = 1.0
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert not any("inferred" in g for g in cr.gaps)
        assert any("regime_count 是 proxy" in g for g in cr.gaps)

    async def test_gap_order_signals_kline_snapshot(self, config, as_of):
        """gaps 顺序：signal → kline → snapshot → identity → proxy"""
        db = FakeDB(responses={})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        gaps = result.candidates["btc_eth_aggressive"].gaps
        # 新文案：信号探测失败 → "schema xxx 不存在" 或 "无信号表"
        signal_idx = next(i for i, g in enumerate(gaps)
                          if "无信号表" in g or ("schema" in g.lower() and "不存在" in g))
        # 新口径 K 线缺口用 table_missing / universe_not_configured 等
        kline_idx = next(i for i, g in enumerate(gaps)
                         if "table_missing" in g or "universe_not_configured" in g)
        snapshot_idx = next(i for i, g in enumerate(gaps) if "外部快照" in g)
        assert signal_idx < kline_idx < snapshot_idx

    def test_to_dict_nested_candidates(self, as_of):
        """to_dict 展开嵌套 dataclass（报告 JSON 化前提）"""
        cr = CandidateReadiness(
            strategy_id="s", closed_trades=1, effective_signals=1, regime_count=1,
            complete_trade_ratio=1.0, config_identity_ratio=1.0,
            kline_coverage=1.0, external_snapshot_coverage=0.0,
            external_snapshot_requirement="required",
            external_snapshot_reason="required_historical_external_snapshots_missing",
            failures=["f"], gaps=["g"],
        )
        rr = ReadinessResult(
            as_of=as_of.isoformat(), window_days=90, decision="STOP",
            selected_strategy=None, candidates={"s": cr},
        )
        data = rr.to_dict()
        assert data["candidates"]["s"]["failures"] == ["f"]
        assert data["candidates"]["s"]["gaps"] == ["g"]
        assert data["window_days"] == 90
        assert data["candidates"]["s"]["external_snapshot_requirement"] == "required"
        assert data["candidates"]["s"]["external_snapshot_reason"] == "required_historical_external_snapshots_missing"


# ===========================================================================
# 策略级外部快照门禁（per_strategy 覆盖）
# ===========================================================================


class TestPerStrategySnapshotGate:
    def test_per_strategy_missing_key_raises(self, fake_db):
        """per_strategy 条目缺必备键 → ValueError（不静默）"""
        bad_config = {
            "purpose_driven_mvp": {
                "candidates": ["btc_eth_aggressive"],
                "identity_ratio_baseline": 0.90,  # 强制键
                "readiness": {
                    "window_days": 90,
                    "min_closed_trades": 20,
                    "min_effective_signals": 100,
                    "min_trade_field_completeness": 0.95,
                    "min_config_identity_coverage": 0.90,
                    "min_kline_coverage": 0.98,
                    "min_external_snapshot_coverage": 0.95,
                    "min_regime_count": 3,
                    "external_snapshot_requirement": "required",
                    "per_strategy": {
                        "btc_eth_aggressive": {"min_external_snapshot_coverage": 0.0},
                        # 缺 external_snapshot_requirement
                    },
                },
            },
        }
        with pytest.raises(ValueError, match="per_strategy.btc_eth_aggressive 缺键"):
            ReadinessAuditor(fake_db, bad_config)

    async def test_global_default_used_when_no_per_strategy(self, config, fake_db, as_of):
        """未在 per_strategy 中声明的策略 → 使用全局默认阈值 0.95 + required"""
        del config["purpose_driven_mvp"]["readiness"]["per_strategy"]["btc_eth_aggressive"]
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        aggr = result.candidates["btc_eth_aggressive"]
        assert not aggr.eligible
        assert aggr.external_snapshot_requirement == "required"
        assert aggr.external_snapshot_reason == "required_historical_external_snapshots_missing"
        assert any("required_historical_external_snapshots_missing" in f for f in aggr.failures)

    async def test_resolve_snapshot_policy_not_required(self, config, fake_db, as_of):
        """per_strategy aggressive: not_required + 0.0 → 不因快照 coverage 低而失败"""
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        aggr = result.candidates["btc_eth_aggressive"]
        assert aggr.external_snapshot_coverage == 0.0
        assert aggr.external_snapshot_requirement == "not_required_for_current_manifest"
        assert not any("外部快照覆盖" in f for f in aggr.failures)

    async def test_resolve_snapshot_policy_required_gate(self, config, fake_db, as_of):
        """per_strategy hrs: required + 0.95, coverage=0 → failure"""
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        hrs = result.candidates["hrs"]
        assert hrs.external_snapshot_requirement == "required"
        assert hrs.external_snapshot_reason == "required_historical_external_snapshots_missing"
        assert any("required_historical_external_snapshots_missing" in f for f in hrs.failures)


# ===========================================================================
# MTPCS 信号持久化审计口径：cancelled 不计入 effective_signals
# ===========================================================================


class TestEffectiveSignalsExcludesCancelled:
    """验证 _query_signal_count 按 strategy_id 查表决定是否追加 status 过滤：

    - btc_eth_aggressive 已声明 → AND status IN ('pending','executed') 生效
    - hrs 未声明 → 保持纯 COUNT(*)（hrs 状态域 triggered/executed/timeout/conflict，
      与 MTPCS 系口径不同，本次不改）
    - K 线覆盖调用方 → extra_condition 空串，SQL 不变

    FakeDB 路由键为纯子串匹配（见 conftest.FakeDB._match）：
    aggressive SQL 含 FROM btc_eth_aggressive.trade_signals + status IN (...) → 命中
    hrs SQL  含 FROM hrs.hrs_signals（无 status IN）→ 命中
    K 线 SQL 含 FROM btc_eth.kline_btcusdt_1h → 命中
    """

    async def test_aggressive_signal_count_sql_contains_status_filter(self, config, as_of):
        """aggressive 已声明 effective_signal_statuses → SQL 必须含
        AND status IN ('pending','executed')"""
        custom_db = FakeDB()
        await ReadinessAuditor(custom_db, config).run(as_of=as_of)
        signal_sqls = [
            s for s in custom_db.sql_log
            if "FROM btc_eth_aggressive.trade_signals" in s
        ]
        assert signal_sqls, "应至少有一条 aggressive 信号计数 SQL"
        for sql in signal_sqls:
            assert "status IN ('pending','executed')" in sql, (
                f"aggressive 信号计数 SQL 应含 status 过滤，实际: {sql}"
            )

    async def test_hrs_signal_count_sql_does_not_contain_status_filter(self, config, as_of):
        """hrs 未声明 effective_signal_statuses → SQL 必须**不含**任何 status IN。

        这是本次修复的核心边界约束：hrs 的 status 域为
        triggered / executed / timeout / conflict（见 strategies/hrs/strategy.py
        L3023-3065 建表），与 MTPCS 的 pending/executed/cancelled 完全不同口径，
        本次改动不触及 hrs。
        """
        custom_db = FakeDB()
        await ReadinessAuditor(custom_db, config).run(as_of=as_of)
        hrs_signal_sqls = [
            s for s in custom_db.sql_log
            if "FROM hrs.hrs_signals" in s
        ]
        assert hrs_signal_sqls, "应至少有一条 hrs 信号计数 SQL"
        for sql in hrs_signal_sqls:
            assert "status IN" not in sql, (
                f"hrs 信号计数 SQL 应不含 status 过滤（保持 COUNT(*) 口径），实际: {sql}"
            )

    async def test_kline_count_sql_does_not_contain_status_filter(self, config, as_of):
        """K 线覆盖走 generate_series + COUNT(DISTINCT open_time)，
        SQL 中不应出现 status IN (...) 过滤——否则会炸掉覆盖度统计。"""
        custom_db = FakeDB()
        await ReadinessAuditor(custom_db, config).run(as_of=as_of)
        # 新口径：K 线覆盖 SQL 含 generate_series（两种：count + missing）
        kline_sqls = [
            s for s in custom_db.sql_log
            if "generate_series" in s
        ]
        assert kline_sqls, "应至少有一条 generate_series K 线覆盖 SQL"
        for sql in kline_sqls:
            assert "status IN" not in sql, (
                f"K 线覆盖 SQL 应不含 status 过滤，实际: {sql}"
            )
        # 仅对 count SQL（含 COUNT(*) AS expected）断言 COUNT(DISTINCT 去重
        count_sqls = [s for s in kline_sqls if "COUNT(*) AS expected" in s]
        for sql in count_sqls:
            assert "COUNT(DISTINCT" in sql, (
                f"K 线覆盖计数 SQL 应用 COUNT(DISTINCT open_time) 去重，实际: {sql}"
            )

    async def test_cancelled_rows_excluded_from_effective_signals(self, config, as_of):
        """aggressive 已声明 status 过滤，FakeDB 按带 status IN 的 SQL 返回 150，
        验证 effective_signals 取 150。"""
        from tests.test_ai_tuner_mvp.conftest import BASELINE_RESPONSES

        responses = dict(BASELINE_RESPONSES)
        # 路由键为纯子串匹配，aggressive SQL 含 "FROM btc_eth_aggressive.trade_signals"
        responses["FROM btc_eth_aggressive.trade_signals"] = {"cnt": 150}

        db = FakeDB(responses)
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        # effective_signals 应该是 150（被 cancelled 排除后）
        assert cr.effective_signals == 150
        # min_effective_signals=100，150>=100，不应有"有效信号不足"失败
        assert not any("有效信号" in f and "150 <" in f for f in cr.failures)


# ===========================================================================
# effective_signal_statuses 配置校验：非法值必须构造阶段即抛 ValueError
# ===========================================================================


class TestEffectiveSignalStatusesConfigValidation:
    """effective_signal_statuses 是可选键，但**若存在则必须格式合法**，
    在 ReadinessAuditor.__init__ 阶段即抛 ValueError，不静默降级。"""

    @staticmethod
    def _base_config():
        """完整合法的最小 config purpose_driven_mvp.readiness 段"""
        return {
            "purpose_driven_mvp": {
                "candidates": ["btc_eth_aggressive"],
                "identity_ratio_baseline": 0.90,
                "readiness": {
                    "window_days": 90,
                    "min_closed_trades": 20,
                    "min_effective_signals": 100,
                    "min_trade_field_completeness": 0.95,
                    "min_config_identity_coverage": 0.90,
                    "min_kline_coverage": 0.98,
                    "min_external_snapshot_coverage": 0.95,
                    "min_regime_count": 3,
                    "external_snapshot_requirement": "required",
                    "per_strategy": {
                        "btc_eth_aggressive": {
                            "min_external_snapshot_coverage": 0.0,
                            "external_snapshot_requirement": "not_required_for_current_manifest",
                        },
                        "hrs": {
                            "min_external_snapshot_coverage": 0.95,
                            "external_snapshot_requirement": "required",
                        },
                    },
                },
            },
        }

    def test_absent_key_defaults_to_empty_dict(self, fake_db):
        """effective_signal_statuses 缺失 → 所有策略走 COUNT(*) 默认路径"""
        cfg = self._base_config()
        # 不写 effective_signal_statuses 键
        auditor = ReadinessAuditor(fake_db, cfg)
        assert auditor.effective_signal_statuses == {}

    def test_none_value_defaults_to_empty_dict(self, fake_db):
        """值显式设 None → 也降级为 {}"""
        cfg = self._base_config()
        cfg["purpose_driven_mvp"]["readiness"]["effective_signal_statuses"] = None
        auditor = ReadinessAuditor(fake_db, cfg)
        assert auditor.effective_signal_statuses == {}

    def test_non_dict_value_raises(self, fake_db):
        """整体类型不是 dict → ValueError"""
        cfg = self._base_config()
        cfg["purpose_driven_mvp"]["readiness"]["effective_signal_statuses"] = ["pending", "executed"]
        with pytest.raises(ValueError, match="必须是 dict"):
            ReadinessAuditor(fake_db, cfg)

    def test_strategy_value_not_list_raises(self, fake_db):
        """某策略的值不是 list → ValueError"""
        cfg = self._base_config()
        cfg["purpose_driven_mvp"]["readiness"]["effective_signal_statuses"] = {
            "btc_eth_aggressive": "pending,executed",
        }
        with pytest.raises(ValueError, match="必须是非空 list"):
            ReadinessAuditor(fake_db, cfg)

    def test_strategy_empty_list_raises(self, fake_db):
        """list 为空 → ValueError（空集合 IN () 是 SQL 语法错误）"""
        cfg = self._base_config()
        cfg["purpose_driven_mvp"]["readiness"]["effective_signal_statuses"] = {
            "btc_eth_aggressive": [],
        }
        with pytest.raises(ValueError, match="必须是非空 list"):
            ReadinessAuditor(fake_db, cfg)

    def test_illegal_char_in_status_raises(self, fake_db):
        """status 串含非法字符（分号/单引号等）→ ValueError（防 SQL 注入）"""
        cfg = self._base_config()
        cfg["purpose_driven_mvp"]["readiness"]["effective_signal_statuses"] = {
            "btc_eth_aggressive": ["pending", "executed'; DROP TABLE users"],
        }
        with pytest.raises(ValueError, match="非法 status 串"):
            ReadinessAuditor(fake_db, cfg)

    def test_status_not_string_raises(self, fake_db):
        """list 里混了 int → ValueError"""
        cfg = self._base_config()
        cfg["purpose_driven_mvp"]["readiness"]["effective_signal_statuses"] = {
            "btc_eth_aggressive": ["pending", 123],
        }
        with pytest.raises(ValueError, match="非法 status 串"):
            ReadinessAuditor(fake_db, cfg)

    def test_valid_statuses_accepted(self, fake_db):
        """合法 dict[list[str]] → 正常解析（下划线和数字都允许）"""
        cfg = self._base_config()
        cfg["purpose_driven_mvp"]["readiness"]["effective_signal_statuses"] = {
            "btc_eth_aggressive": ["pending", "executed"],
            "btc_eth": ["executed"],
        }
        auditor = ReadinessAuditor(fake_db, cfg)
        assert auditor.effective_signal_statuses == {
            "btc_eth_aggressive": ["pending", "executed"],
            "btc_eth": ["executed"],
        }


# ===========================================================================
# 门禁失败文案百分比精度：必须保留两位小数，避免 "98.0% < 98.0%" 自相矛盾
# ===========================================================================


class TestGateRatioPrecision:
    """门禁 failure 文案和 gaps 说明中的百分比必须保留两位小数。"""

    def test_kline_failure_keeps_two_decimal_percentage(self, auditor):
        """kline_coverage=0.9796 < 阈值 0.98 → 文案必须显示 97.96% < 98.00%，
        不能因 .1% 四舍五入成 98.0% < 98.0% 自相矛盾。"""
        failures, _ = auditor._apply_gate_thresholds(
            closed_trades=100, signals_count=100, complete_ratio=1.0,
            identity_ratio=1.0, kline_coverage=0.9796, snapshot_coverage=1.0,
            regime_count=3, snapshot_threshold=0.95, snapshot_requirement="required",
        )
        assert any("K线覆盖 97.96% < 98.00%" in f for f in failures)

    def test_identity_failure_keeps_two_decimal_percentage(self, auditor):
        """identity_ratio=0.8999 < 阈值 0.90 → 文案保留两位小数（90.00%）。"""
        failures, _ = auditor._apply_gate_thresholds(
            closed_trades=100, signals_count=100, complete_ratio=1.0,
            identity_ratio=0.8999, kline_coverage=1.0, snapshot_coverage=1.0,
            regime_count=3, snapshot_threshold=0.95, snapshot_requirement="required",
        )
        assert any("配置身份覆盖 89.99%" in f for f in failures)

    def test_snapshot_failure_keeps_two_decimal_percentage(self, auditor):
        """snapshot_coverage=0.9799 < snapshot_threshold=0.98 → 文案保留两位。"""
        failures, _ = auditor._apply_gate_thresholds(
            closed_trades=100, signals_count=100, complete_ratio=1.0,
            identity_ratio=1.0, kline_coverage=1.0, snapshot_coverage=0.9799,
            regime_count=3, snapshot_threshold=0.98, snapshot_requirement="required",
        )
        assert any("外部快照覆盖 97.99% < 98.00%" in f for f in failures)

    def test_gaps_snapshot_not_required_keeps_two_decimal(self):
        """_build_gaps_list 里 snapshot coverage=0.9876 也应保留两位小数。"""
        gaps = ReadinessAuditor._build_gaps_list(
            signal_gaps=[], kline_gaps=[], snapshot_gaps=[], regime_gaps=[],
            identity_ratio=1.0, snapshot_coverage=0.9876,
            snapshot_requirement="not_required_for_current_manifest",
        )
        assert any("coverage=98.76%" in g for g in gaps)
