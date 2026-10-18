# -*- coding: utf-8 -*-
"""功能矩阵 5 + 6：ReadinessAuditor

- 矩阵 5：config purpose_driven_mvp.readiness 的 8 个键都被真实读取（改配置改门禁）
- 矩阵 6：hrs 缺口报告（trade_signals 表缺失 / 无 K 线表 / 无快照表 → 结构化 gaps 不崩）
- 覆盖 auditor 全部查询分支、异常分支与策略选择逻辑（DB 用 FakeDB 桩）
"""

from datetime import timedelta

import pytest

from ai_tuner.readiness.auditor import ReadinessAuditor
from ai_tuner.readiness.models import CandidateReadiness, ReadinessResult
from tests.test_ai_tuner_mvp.conftest import BASELINE_RESPONSES, FakeDB


@pytest.fixture
def config(tuner_config):
    """真实系统配置（auditor 只读 purpose_driven_mvp 段）"""
    return tuner_config


@pytest.fixture
def auditor(config, fake_db):
    """全达标基线审计器"""
    return ReadinessAuditor(fake_db, config)


# ===========================================================================
# 矩阵 5：阈值全部来自 config（配置驱动而非硬编码）
# ===========================================================================


class TestConfigDrivenThresholds:
    async def test_baseline_all_pass_selects_first_candidate(self, auditor, fake_db, as_of, config):
        """基线数据下全部 7 项门禁通过 → SELECT 且选中 config candidates 的第一个"""
        result = await auditor.run(as_of=as_of)
        assert result.decision == "SELECT"
        assert result.selected_strategy == config["purpose_driven_mvp"]["candidates"][0]
        assert result.window_days == 90
        assert all(cr.eligible for cr in result.candidates.values())

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
        assert any("交易字段完整度" in f and "110.0%" in f for f in failures)

    async def test_min_config_identity_coverage_from_config(self, config, fake_db, as_of):
        config["purpose_driven_mvp"]["readiness"]["min_config_identity_coverage"] = 0.91
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        failures = result.candidates["btc_eth_aggressive"].failures
        assert any("配置身份覆盖" in f and "90.0%" in f and "91.0%" in f for f in failures)

    async def test_min_kline_coverage_from_config(self, config, fake_db, as_of):
        config["purpose_driven_mvp"]["readiness"]["min_kline_coverage"] = 1.01
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        failures = result.candidates["btc_eth_aggressive"].failures
        assert any("K线覆盖" in f and "101.0%" in f for f in failures)

    async def test_min_external_snapshot_coverage_no_fallback(self, config, fake_db, as_of):
        """M4：快照阈值无兜底，config 设 0.5 而实际 0 → 失败（证明无硬编码兜底）"""
        config["purpose_driven_mvp"]["readiness"]["min_external_snapshot_coverage"] = 0.5
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        failures = result.candidates["btc_eth_aggressive"].failures
        assert any("外部快照覆盖" in f and "50.0%" in f for f in failures)

    async def test_min_regime_count_from_config(self, config, fake_db, as_of):
        config["purpose_driven_mvp"]["readiness"]["min_regime_count"] = 6
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        failures = result.candidates["btc_eth_aggressive"].failures
        assert any("regime_count(proxy) 5 < 6" in f for f in failures)

    async def test_window_days_drives_kline_expectation(self, config, as_of):
        """window_days 真实生效：同样 100 根 K 线，窗口 1 天达标、90 天不达标"""
        responses = dict(BASELINE_RESPONSES)
        responses["FROM btc_eth.klines_1h"] = {"cnt": 100}
        config["purpose_driven_mvp"]["readiness"]["window_days"] = 1
        ok_result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        # 1 天窗口预期 24 根，100 根 → 覆盖率封顶 1.0 → 通过
        assert not any("K线覆盖" in f for f in ok_result.candidates["btc_eth_aggressive"].failures)

        config["purpose_driven_mvp"]["readiness"]["window_days"] = 90
        fail_result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        # 90 天窗口预期 2160 根，100 根 → 覆盖率约 4.6% → 失败
        assert any("K线覆盖" in f for f in fail_result.candidates["btc_eth_aggressive"].failures)

    async def test_identity_baseline_from_config_not_hardcoded(self, config, fake_db, as_of):
        """M2：identity baseline 来自 config（改 baseline 即改门禁结果）"""
        config["purpose_driven_mvp"]["identity_ratio_baseline"] = 0.5
        config["purpose_driven_mvp"]["readiness"]["min_config_identity_coverage"] = 0.6
        result = await ReadinessAuditor(fake_db, config).run(as_of=as_of)
        failures = result.candidates["btc_eth_aggressive"].failures
        assert any("配置身份覆盖 50.0% < 60.0%" in f for f in failures)

    async def test_window_start_passed_to_sql(self, auditor, fake_db, as_of):
        """窗口以绑定变量 $2/$3 传入 SQL（参数化，无字面量拼接）"""
        await auditor.run(as_of=as_of)
        trade_sql = next(s for s in fake_db.sql_log if "closed_trades" in s)
        assert "$2" in trade_sql and "$3" in trade_sql

    async def test_as_of_defaults_to_now(self, auditor, fake_db):
        """as_of 为 None 时取当前 UTC 时间"""
        result = await auditor.run()
        assert result.as_of.endswith("+00:00")


# ===========================================================================
# 矩阵 6：hrs 缺口报告（数据缺失不崩、gaps 结构化）
# ===========================================================================


class TestHrsGapReporting:
    async def test_all_data_sources_missing_produces_structured_gaps(self, config, as_of):
        """全部数据源探测失败：signals=0 / kline=0 / snapshot=0，gaps 逐项说明且不崩"""
        db = FakeDB(responses={})  # 空路由：所有探测返回空
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        for cr in result.candidates.values():
            assert cr.effective_signals == 0
            assert cr.kline_coverage == 0.0
            assert cr.external_snapshot_coverage == 0.0
            assert isinstance(cr.gaps, list) and cr.gaps
            assert any("trade_signals 表缺失" in g for g in cr.gaps)
            assert any("K 线表未探测到" in g for g in cr.gaps)
            assert any("无历史外部快照表" in g for g in cr.gaps)
            assert any("regime_count 是 proxy" in g for g in cr.gaps)
            assert any("inferred" in g for g in cr.gaps)
            assert result.decision == "STOP"

    async def test_hrs_signal_gap_via_empty_schema_probe(self, config, as_of):
        """hrs 无 trade_signals 表 → effective_signals 记 0 + gap（模拟 hrs schema 缺失场景）"""
        responses = dict(BASELINE_RESPONSES)
        # 信号 schema 探测返回空列表（所有 schema 都没有该表）
        responses["table_name = 'trade_signals'"] = []
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        hrs = result.candidates["hrs"]
        assert hrs.effective_signals == 0
        assert any("trade_signals 表缺失" in g for g in hrs.gaps)

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
# 查询分支与异常分支全覆盖
# ===========================================================================


class TestQueryBranches:
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

    async def test_signal_count_query_failure_records_gap(self, config, as_of):
        """信号计数查询异常 → 0 + gap 说明（非静默）"""
        responses = dict(BASELINE_RESPONSES)
        responses["FROM btc_eth.trade_signals"] = None
        db = FakeDB(responses=responses, errors={"FROM btc_eth.trade_signals": RuntimeError("超时")})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.effective_signals == 0
        assert any("trade_signals 查询失败" in g for g in cr.gaps)

    async def test_signal_schema_probe_failure_treated_as_missing(self, config, as_of):
        """schema 探测查询异常 → 视为表缺失"""
        db = FakeDB(errors={"table_name = 'trade_signals'": RuntimeError("权限不足")})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.effective_signals == 0
        assert any("trade_signals 表缺失" in g for g in cr.gaps)

    async def test_signal_schema_multiple_rows_takes_first_valid(self, config, as_of):
        """多 schema 都有 trade_signals 时取第一个且列校验通过的 schema"""
        responses = dict(BASELINE_RESPONSES)
        responses["table_name = 'trade_signals'"] = [
            {"table_schema": "new_coin"},
            {"table_schema": "btc_eth"},
        ]
        # 计数路由指向第一个 schema（new_coin），证明计数 SQL 使用探测到的 schema
        responses["FROM new_coin.trade_signals"] = {"cnt": 200}
        db = FakeDB(responses)
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        # 计数 SQL 应指向第一个 schema（new_coin）
        assert any("FROM new_coin.trade_signals" in s for s in db.sql_log)
        assert result.candidates["btc_eth_aggressive"].effective_signals == 200

    async def test_signal_schema_column_check_fails_returns_none(self, config, as_of):
        """所有候选 schema 都没有 strategy_id 列 → 视为缺失"""
        responses = dict(BASELINE_RESPONSES)
        responses["column_name = 'strategy_id'"] = None  # 列校验返回 None
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.effective_signals == 0
        assert any("trade_signals 表缺失" in g for g in cr.gaps)

    async def test_regime_query_failure_silent_zero(self, config, as_of):
        """regime 查询异常 → 静默记 0（不阻断）"""
        db = FakeDB(errors={"AS syms": RuntimeError("boom")})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        assert result.candidates["btc_eth_aggressive"].regime_count == 0

    async def test_regime_row_none_zero(self, config, as_of):
        responses = dict(BASELINE_RESPONSES)
        responses["AS syms"] = None
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        assert result.candidates["btc_eth_aggressive"].regime_count == 0

    async def test_kline_schema_missing_gap(self, config, as_of):
        responses = dict(BASELINE_RESPONSES)
        responses["ORDER BY table_schema"] = None
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.kline_coverage == 0.0
        assert any("K 线表未探测到" in g for g in cr.gaps)

    async def test_kline_schema_probe_failure_gap(self, config, as_of):
        db = FakeDB(errors={"ORDER BY table_schema": RuntimeError("down")})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.kline_coverage == 0.0
        assert any("K 线表未探测到" in g for g in cr.gaps)

    async def test_kline_table_name_missing_gap(self, config, as_of):
        responses = dict(BASELINE_RESPONSES)
        responses["table_name ILIKE"] = None
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert any("未识别到 K 线表名" in g for g in cr.gaps)

    async def test_kline_timestamp_column_missing_gap(self, config, as_of):
        responses = dict(BASELINE_RESPONSES)
        responses["column_name IN"] = None
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert any("未识别到时间戳列" in g for g in cr.gaps)

    async def test_kline_count_failure_gap(self, config, as_of):
        db = FakeDB(errors={"FROM btc_eth.klines_1h": RuntimeError("query timeout")})
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.kline_coverage == 0.0
        assert any("K 线查询失败" in g for g in cr.gaps)

    async def test_kline_coverage_capped_at_one(self, config, as_of):
        """实际 bar 数超出预期时覆盖率封顶 1.0"""
        responses = dict(BASELINE_RESPONSES)
        responses["FROM btc_eth.klines_1h"] = {"cnt": 99999}
        result = await ReadinessAuditor(FakeDB(responses), config).run(as_of=as_of)
        assert result.candidates["btc_eth_aggressive"].kline_coverage == 1.0

    async def test_kline_zero_window_coverage_zero(self, config, as_of):
        """window_days=0 → 预期 bar 数为 0 → 覆盖率记 0（除零守卫）"""
        config["purpose_driven_mvp"]["readiness"]["window_days"] = 0
        result = await ReadinessAuditor(FakeDB(), config).run(as_of=as_of)
        assert result.candidates["btc_eth_aggressive"].kline_coverage == 0.0

    async def test_kline_table_name_probe_exception_gap(self, config, as_of):
        """K 线表名探测异常 → coverage 0 + gap（表名探测 SQL 精确匹配，不影响 schema 探测）"""
        db = FakeDB(
            responses=BASELINE_RESPONSES,
            # 键 "table_schema = $1 AND table_name ILIKE" 只命中表名探测 SQL，
            # 不会命中 schema 探测 SQL（其 WHERE 是 table_schema = ANY($1) 后接换行）
            errors={"table_schema = $1 AND table_name ILIKE": RuntimeError("x")},
        )
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.kline_coverage == 0.0
        assert any("未识别到 K 线表名" in g for g in cr.gaps)

    async def test_kline_timestamp_probe_exception_gap(self, config, as_of):
        """K 线时间戳列探测异常 → coverage 0 + gap"""
        db = FakeDB(
            responses=BASELINE_RESPONSES,
            errors={"column_name IN": RuntimeError("y")},
        )
        result = await ReadinessAuditor(db, config).run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.kline_coverage == 0.0
        assert any("未识别到时间戳列" in g for g in cr.gaps)

    async def test_external_snapshot_always_zero_with_gap(self, auditor, as_of):
        """快照表不存在 → coverage 恒 0 且 gap 说明（预期行为）"""
        result = await auditor.run(as_of=as_of)
        cr = result.candidates["btc_eth_aggressive"]
        assert cr.external_snapshot_coverage == 0.0
        assert any("无历史外部快照表" in g for g in cr.gaps)


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
        signal_idx = next(i for i, g in enumerate(gaps) if "trade_signals" in g)
        kline_idx = next(i for i, g in enumerate(gaps) if "K 线" in g)
        snapshot_idx = next(i for i, g in enumerate(gaps) if "外部快照" in g)
        assert signal_idx < kline_idx < snapshot_idx

    def test_to_dict_nested_candidates(self, as_of):
        """to_dict 展开嵌套 dataclass（报告 JSON 化前提）"""
        cr = CandidateReadiness(
            strategy_id="s", closed_trades=1, effective_signals=1, regime_count=1,
            complete_trade_ratio=1.0, config_identity_ratio=1.0,
            kline_coverage=1.0, external_snapshot_coverage=0.0,
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
