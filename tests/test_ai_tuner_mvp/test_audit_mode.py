# -*- coding: utf-8 -*-
"""功能矩阵 3：audit 模式行为（关键决策：生成 + 推送，仅阻断写入）

- 候选策略（btc_eth_aggressive / hrs）mode=audit 下走完整流水线（采集→LLM→校验→飞书推送），
  但不调用 config_operator.apply_overrides、不调用 db_handler.mark_applied
- 非候选策略行为不变（auto_apply 启用时正常写入覆盖层）
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_tuner.adapters.base_adapter import StrategyReport
from ai_tuner.scheduler.weekly_job import WeeklyTuningJob
from tests.test_ai_tuner_mvp.conftest import PROJECT_ROOT, dump_envelope, make_legacy_payload


def make_report(trades=5):
    """构造非空交易报告（有交易才继续流水线）"""
    report = StrategyReport()
    report.meta.strategy_id = "btc_eth_aggressive"
    report.performance.total_trades = trades
    return report


def make_job_config(strategy_ids, mvp_mode="audit", selected_strategy=None):
    """构造最小调度配置：候选策略 + 指定 mvp 模式 + auto_apply 开启"""
    strategies = []
    for sid in strategy_ids:
        strategies.append({
            "strategy_id": sid,
            "name": f"{sid}名称",
            "adapter_class": f"ai_tuner.adapters.{sid}_adapter.Fake",
            "config_path": f"strategies/{sid}/config.yaml",
        })
    # MVP 候选声明 manifest（与真实 config.yaml 一致）
    strategies[0]["parameter_manifest"] = "ai_tuner/parameters/manifests/btc_eth_aggressive.yaml"
    return {
        "purpose_driven_mvp": {
            "mode": mvp_mode,
            "candidates": ["btc_eth_aggressive", "hrs"],
            "selected_strategy": selected_strategy,
        },
        "approval": {"auto_apply": {"enabled": True}},
        "strategies": strategies,
    }


def build_job(config):
    """构造注入全 mock 依赖的 WeeklyTuningJob，并 mock 反馈闭环组件"""
    job = WeeklyTuningJob(
        config=config,
        db_manager=MagicMock(),
        notification_client=None,
        llm_client=MagicMock(),
        db_handler=MagicMock(),
        cost_tracker=MagicMock(),
        messenger=MagicMock(),
        config_operator=MagicMock(),
        rollback_manager=MagicMock(),
    )
    # db_manager.fetch_one 抛错 → 统一绩效读取走"失败不阻断"分支
    job.db_manager.fetch_one = AsyncMock(side_effect=RuntimeError("测试桩：跳过绩效读取"))
    # 反馈闭环组件替换为 mock（它们是实例属性）
    job.effect_tracker = MagicMock()
    job.effect_tracker.track_and_fill = AsyncMock()
    job.context_enhancer = MagicMock()
    job.context_enhancer.build_feedback_context.return_value = "反馈上下文"
    job.learning_signal_generator = MagicMock()
    job.learning_signal_generator.build_learning_instructions = AsyncMock(return_value="学习指令")
    job.version_manager = MagicMock()
    job.version_manager.generate_new_version.return_value = "v_test.0"
    # 加载适配器替换为 mock adapter（由调用方再定制）
    job._load_adapter = MagicMock()
    # 记忆库与通知
    job.db_handler.save_memory = AsyncMock(return_value=42)
    job.db_handler.mark_applied = AsyncMock()
    job.messenger.send_tuning_card = AsyncMock()
    job.messenger.send_no_changes_notification = AsyncMock()
    job.messenger.send_auto_applied_notification = AsyncMock()
    job.messenger.send_error_notification = AsyncMock()
    job.llm_client.call_llm = AsyncMock()
    return job


def make_mock_adapter(strategy_id, validated, manifest=True):
    """mock 适配器：collect 返回非空报告、validate_params 返回指定结果"""
    adapter = MagicMock()
    adapter.strategy_id = strategy_id
    adapter.collect = AsyncMock(return_value=make_report())
    adapter.get_current_params.return_value = {"strategy.scoring.min_score": 60}
    adapter.validate_params.return_value = {
        "valid": True, "errors": [], "warnings": [], "validated": validated,
    }
    adapter._strategy_cfg = (
        {"parameter_manifest": "ai_tuner/parameters/manifests/btc_eth_aggressive.yaml"}
        if manifest else {}
    )
    return adapter


VALID_PROPOSAL = {"strategy.scoring.min_score": {"from": 60, "to": 62}}


# ===========================================================================
# 候选策略：完整流水线 + 拦截写入
# ===========================================================================


class TestCandidateAuditMode:
    async def test_candidate_generates_and_pushes_but_never_writes(self):
        """核心断言：生成建议 + 推送飞书 + 保存记忆，但不写覆盖层、不 mark_applied"""
        config = make_job_config(["btc_eth_aggressive"])
        job = build_job(config)
        adapter = make_mock_adapter("btc_eth_aggressive", VALID_PROPOSAL, manifest=True)
        job._load_adapter.return_value = adapter
        job.llm_client.call_llm.return_value = dump_envelope()

        result = await job._tune_single_strategy(config["strategies"][0])

        assert result == "success"
        # 生成与推送确实发生
        job.llm_client.call_llm.assert_awaited()
        adapter.collect.assert_awaited_once()
        job.db_handler.save_memory.assert_awaited_once()
        job.messenger.send_tuning_card.assert_awaited_once()
        # 关键：写入层一律未触碰
        job.config_operator.apply_overrides.assert_not_called()
        job.db_handler.mark_applied.assert_not_called()
        job.messenger.send_auto_applied_notification.assert_not_called()

    async def test_candidate_contract_mode_is_new(self):
        """有 manifest 的适配器以新契约解析响应"""
        config = make_job_config(["btc_eth_aggressive"])
        job = build_job(config)
        adapter = make_mock_adapter("btc_eth_aggressive", VALID_PROPOSAL, manifest=True)
        job._load_adapter.return_value = adapter
        job.llm_client.call_llm.return_value = dump_envelope()

        await job._tune_single_strategy(config["strategies"][0])

        # 新契约信封正常进入流水线（未被解析拒绝 → 无错误通知）
        job.messenger.send_error_notification.assert_not_called()
        job.db_handler.save_memory.assert_awaited_once()

    async def test_candidate_new_envelope_rejected_triggers_retry_then_error(self):
        """新契约信封非法时：先按新契约重试一次，再失败即报错返回"""
        config = make_job_config(["btc_eth_aggressive"])
        job = build_job(config)
        adapter = make_mock_adapter("btc_eth_aggressive", VALID_PROPOSAL, manifest=True)
        job._load_adapter.return_value = adapter
        # 两次都返回 objective 错误的信封
        job.llm_client.call_llm.return_value = dump_envelope(objective="wrong_objective")

        result = await job._tune_single_strategy(config["strategies"][0])

        assert result == "error"
        assert job.llm_client.call_llm.await_count == 2  # 重试一次
        job.messenger.send_error_notification.assert_awaited_once()

    async def test_candidate_hold_sends_no_change_notification_without_write(self):
        """HOLD（无 adjustments）→ 跳过 + 无变更通知，同样不写覆盖层"""
        config = make_job_config(["btc_eth_aggressive"])
        job = build_job(config)
        adapter = make_mock_adapter("btc_eth_aggressive", {}, manifest=True)
        job._load_adapter.return_value = adapter
        job.llm_client.call_llm.return_value = dump_envelope(decision="HOLD", adjustments={})

        result = await job._tune_single_strategy(config["strategies"][0])

        assert result == "skip"
        job.messenger.send_no_changes_notification.assert_awaited_once()
        job.config_operator.apply_overrides.assert_not_called()
        job.db_handler.mark_applied.assert_not_called()

    async def test_candidate_validation_rejects_bad_proposal_blocks_writes(self):
        """manifest 校验拒绝非法提案 → 提前 return 'error' + 推 error 通知，不写覆盖层"""
        config = make_job_config(["btc_eth_aggressive"])
        job = build_job(config)
        adapter = make_mock_adapter("btc_eth_aggressive", {}, manifest=True)
        # validate_params 报告非法（errors 非空）
        adapter.validate_params.return_value = {
            "valid": False,
            "errors": ["strategy.scoring.min_score: 目标值 71 超出范围"],
            "warnings": [],
            "validated": {},
        }
        job._load_adapter.return_value = adapter
        job.llm_client.call_llm.return_value = dump_envelope()

        result = await job._tune_single_strategy(config["strategies"][0])

        # 校验错误 → 提前返回 error，不是走 HOLD 分支
        assert result == "error"
        job.messenger.send_error_notification.assert_awaited_once()
        job.config_operator.apply_overrides.assert_not_called()
        job.db_handler.mark_applied.assert_not_called()


# ===========================================================================
# 非候选策略：行为不变
# ===========================================================================


class TestNonCandidateUnchanged:
    async def test_non_candidate_still_auto_applies(self):
        """非候选策略在 audit 模式下照常走旧契约 + 自动应用（行为不变）"""
        config = make_job_config(["btc_eth"])  # btc_eth 不在 candidates 中
        job = build_job(config)
        adapter = make_mock_adapter("btc_eth", {"scoring.min_score": 62}, manifest=False)
        job._load_adapter.return_value = adapter
        job.llm_client.call_llm.return_value = json.dumps(make_legacy_payload())
        job.config_operator.apply_overrides.return_value = True

        result = await job._tune_single_strategy(config["strategies"][0])

        assert result == "success"
        # 旧契约完整链路照常
        job.config_operator.apply_overrides.assert_called_once()
        job.db_handler.mark_applied.assert_awaited_once()
        job.messenger.send_auto_applied_notification.assert_awaited_once()

    async def test_non_candidate_auto_apply_failure_notifies_error(self):
        """非候选策略写覆盖层失败 → 错误通知（原有行为）"""
        config = make_job_config(["btc_eth"])
        job = build_job(config)
        adapter = make_mock_adapter("btc_eth", {"scoring.min_score": 62}, manifest=False)
        job._load_adapter.return_value = adapter
        job.llm_client.call_llm.return_value = json.dumps(make_legacy_payload())
        job.config_operator.apply_overrides.return_value = False

        result = await job._tune_single_strategy(config["strategies"][0])

        assert result == "success"
        job.messenger.send_error_notification.assert_awaited_once()


# ===========================================================================
# 周度汇总：候选与非候选文案区分
# ===========================================================================


class TestWeeklySummary:
    async def test_run_weekly_tuning_labels_candidates_as_audit_mode(self):
        """候选策略在周度汇总中标注 🛡️ 审计模式（未写入覆盖层）"""
        config = make_job_config(["btc_eth_aggressive", "btc_eth"])
        job = build_job(config)
        job._tune_single_strategy = AsyncMock(return_value="success")
        job.messenger.send_weekly_summary = AsyncMock()

        await job.run_weekly_tuning()

        job.messenger.send_weekly_summary.assert_awaited_once()
        kwargs = job.messenger.send_weekly_summary.await_args.kwargs
        details = kwargs["details"]
        assert "🛡️" in details and "未写入覆盖层" in details
        # 非候选保持普通文案
        assert "✅" in details

    async def test_run_weekly_tuning_skip_label_for_candidates(self):
        """候选策略 skip 时汇总文案也标注审计模式"""
        config = make_job_config(["btc_eth_aggressive"])
        job = build_job(config)
        job._tune_single_strategy = AsyncMock(return_value="skip")
        job.messenger.send_weekly_summary = AsyncMock()

        await job.run_weekly_tuning()

        kwargs = job.messenger.send_weekly_summary.await_args.kwargs
        assert "🛡️" in kwargs["details"] and "维持不变" in kwargs["details"]

    async def test_run_weekly_tuning_no_strategies(self):
        """无注册策略时直接返回"""
        job = build_job({"strategies": []})
        job.messenger.send_weekly_summary = AsyncMock()
        await job.run_weekly_tuning()
        job.messenger.send_weekly_summary.assert_not_awaited()


# ===========================================================================
# live 模式：selected candidate 能写，unselected 仍被拦截
# （迁移自原 test_mvp_routing.py，因该文件其余 4 用例已在此文件有等价覆盖）
# ===========================================================================


class TestCandidateLiveMode:
    async def test_live_selected_candidate_can_write(self):
        """live 模式 + selected_strategy=aggressive → 被选中的候选正常写覆盖层"""
        config = make_job_config(
            ["btc_eth_aggressive"],
            mvp_mode="live",
            selected_strategy="btc_eth_aggressive",
        )
        job = build_job(config)
        adapter = make_mock_adapter("btc_eth_aggressive", VALID_PROPOSAL, manifest=True)
        job._load_adapter.return_value = adapter
        job.llm_client.call_llm.return_value = dump_envelope()
        job.config_operator.apply_overrides.return_value = True

        result = await job._tune_single_strategy(config["strategies"][0])

        assert result == "success"
        # 被选中 → 写入层不被拦截
        job.config_operator.apply_overrides.assert_called_once()
        job.db_handler.mark_applied.assert_awaited_once()
        job.messenger.send_auto_applied_notification.assert_awaited_once()

    async def test_live_unselected_candidate_still_blocked(self):
        """live 模式 + selected=aggressive + strategy=hrs → 候选但未选中，仍被拦截写入"""
        config = make_job_config(
            ["btc_eth_aggressive"],
            mvp_mode="live",
            selected_strategy="btc_eth_aggressive",
        )
        # 把 strategy_id 改成 hrs（第二个候选，未被选中）
        config["strategies"][0]["strategy_id"] = "hrs"
        config["strategies"][0]["name"] = "HRS混合反转策略"
        # hrs 也是 MVP 候选，有 manifest
        config["strategies"][0]["parameter_manifest"] = "ai_tuner/parameters/manifests/hrs.yaml"

        job = build_job(config)
        adapter = make_mock_adapter("hrs", VALID_PROPOSAL, manifest=True)
        job._load_adapter.return_value = adapter
        job.llm_client.call_llm.return_value = dump_envelope()

        result = await job._tune_single_strategy(config["strategies"][0])

        # 未被选中 → is_mvp_blocked=True → 完整流水线跑但拦截写入
        assert result == "success"
        job.messenger.send_tuning_card.assert_awaited_once()  # 推送照常
        job.config_operator.apply_overrides.assert_not_called()  # 拦截写入
        job.db_handler.mark_applied.assert_not_called()
