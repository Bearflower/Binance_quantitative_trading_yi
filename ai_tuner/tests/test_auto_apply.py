"""
测试 AI 调优自动审批流程

覆盖场景：
1. 自动应用配置（正向流程）
2. 自动应用配置失败（异常流程）
3. 自动应用禁用的场景
4. config_path 为空时的处理
5. send_auto_applied_notification 方法测试

测试策略：
- 使用 unittest.mock 模拟所有外部依赖
- 每个测试用例独立，互不依赖
- 通过 mock 前置步骤使执行流到达自动应用逻辑（步骤12）
"""

import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")

from ai_tuner.notifier.messenger import Messenger
from ai_tuner.scheduler.weekly_job import WeeklyTuningJob


# ===================================================================
# 夹具
# ===================================================================


@pytest.fixture
def base_config():
    """基础配置，approval.auto_apply.enabled = True"""
    return {
        "approval": {
            "auto_apply": {
                "enabled": True,
            },
        },
        "strategies": [],
        "memory": {
            "context_window_size": 3,
        },
    }


def _build_mock_adapter():
    """构建模拟的策略适配器，让 collect() 返回有交易数据的报告"""
    adapter = MagicMock()

    # 模拟 collect() 异步方法，返回有交易数据的报告
    mock_report = MagicMock()
    mock_report.performance.total_trades = 10
    mock_report.model_dump.return_value = {
        "performance": {"total_trades": 10},
        "meta": {"week_start": "2026-08-04", "week_end": "2026-08-10"},
    }
    adapter.collect = AsyncMock(return_value=mock_report)

    # 模拟 get_current_params
    adapter.get_current_params.return_value = {"param1": 0.3, "param2": 0.7}

    return adapter


def _create_job(config, apply_overrides_return=True, adjustments=None):
    """
    创建 WeeklyTuningJob 实例，所有依赖均 Mock

    模拟前置步骤（1-11），使执行流到达步骤12（自动应用逻辑）。

    Args:
        config: 系统配置字典
        apply_overrides_return: apply_overrides 的返回值
        adjustments: AI 建议的参数调整值，默认为 {"param1": 0.5}

    Returns:
        (job, 各 mock 的引用字典)
    """
    if adjustments is None:
        adjustments = {"param1": 0.5}

    # 创建所有依赖的 Mock
    job = WeeklyTuningJob(
        config=config,
        db_manager=MagicMock(),
        notification_client=MagicMock(),
        llm_client=MagicMock(),
        db_handler=MagicMock(),
        cost_tracker=MagicMock(),
        messenger=MagicMock(),
        config_operator=MagicMock(),
        rollback_manager=MagicMock(),
    )

    # 将异步方法设为 AsyncMock
    job.db_handler.save_memory = AsyncMock(return_value=42)
    job.db_handler.mark_applied = AsyncMock(return_value=True)
    job.messenger.send_tuning_card = AsyncMock(return_value=True)
    job.messenger.send_auto_applied_notification = AsyncMock(return_value=True)
    job.messenger.send_error_notification = AsyncMock(return_value=True)
    job.llm_client.call_llm = AsyncMock(
        return_value='{"adjustments": {"param1": 0.5}}'
    )
    job.effect_tracker.track_and_fill = AsyncMock(return_value={})
    job.learning_signal_generator.build_learning_instructions = AsyncMock(
        return_value=""
    )
    job.context_builder.build_context = AsyncMock(return_value="")

    # 模拟 _load_adapter 返回可用的适配器
    job._load_adapter = MagicMock(return_value=_build_mock_adapter())

    # 模拟 _build_prompts 返回非空提示词
    job._build_prompts = MagicMock(return_value=("system_prompt", "user_prompt"))

    # 模拟 response_parser
    job.response_parser.parse_response = MagicMock(
        return_value={
            "adjustments": adjustments,
            "summary": "测试调优摘要",
            "reasons": "测试调优理由",
            "expected_impact": "测试预期影响",
            "confidence": 0.85,
        }
    )
    job.response_parser.validate_adjustments = MagicMock(
        return_value={"errors": [], "validated": adjustments}
    )

    # 模拟 diff_generator
    job.diff_generator.generate_diff = MagicMock(return_value="+ param1: 0.3 -> 0.5")

    # 模拟 version_manager
    job.version_manager.generate_new_version = MagicMock(return_value="v1.1")

    # 模拟 context_enhancer
    job.context_enhancer.build_feedback_context = MagicMock(return_value="")

    # 设置 config_operator.apply_overrides 的返回值
    job.config_operator.apply_overrides = MagicMock(
        return_value=apply_overrides_return
    )

    mocks = {
        "config_operator": job.config_operator,
        "db_handler": job.db_handler,
        "messenger": job.messenger,
        "response_parser": job.response_parser,
        "diff_generator": job.diff_generator,
    }

    return job, mocks


# ===================================================================
# 测试用例
# ===================================================================


class TestAutoApply:
    """自动应用配置测试"""

    @pytest.mark.asyncio
    async def test_auto_apply_success(self, base_config):
        """
        场景1: 自动应用配置（正向流程）

        - auto_apply.enabled = true
        - config_path 不为空
        - apply_overrides 返回 True
        - 验证：mark_applied 被调用且参数为 approved_by="auto_apply"
        - 验证：send_auto_applied_notification 被调用
        - 验证：日志记录"调优建议已自动应用"
        """
        job, mocks = _create_job(base_config)

        strategy_cfg = {
            "strategy_id": "btc_eth",
            "name": "MTPCS策略",
            "adapter_class": "ai_tuner.adapters.mtpcs_adapter.MTPCSAdapter",
            "config_path": "strategies/btc_eth/config.yaml",
        }

        with patch("ai_tuner.scheduler.weekly_job.logger") as mock_logger:
            result = await job._tune_single_strategy(strategy_cfg)

        # 验证返回 success
        assert result == "success"

        # 验证 apply_overrides 被调用
        mocks["config_operator"].apply_overrides.assert_called_once_with(
            "strategies/btc_eth/config.yaml", {"param1": 0.5}
        )

        # 验证 mark_applied 被调用且参数为 approved_by="auto_apply"
        mocks["db_handler"].mark_applied.assert_called_once_with(
            42, approved_by="auto_apply"
        )

        # 验证 send_auto_applied_notification 被调用
        mocks["messenger"].send_auto_applied_notification.assert_called_once_with(
            strategy_name="MTPCS策略",
            strategy_id="btc_eth",
            diff_text="+ param1: 0.3 -> 0.5",
        )

        # 验证日志记录"调优建议已自动应用"
        mock_logger.info.assert_any_call(
            "调优建议已自动应用", strategy_id="btc_eth", memory_id=42
        )

    @pytest.mark.asyncio
    async def test_auto_apply_failure(self, base_config):
        """
        场景2: 自动应用配置失败（异常流程）

        - auto_apply.enabled = true
        - config_path 不为空
        - apply_overrides 返回 False
        - 验证：send_error_notification 被调用
        - 验证：日志记录"自动应用配置失败"
        """
        job, mocks = _create_job(base_config, apply_overrides_return=False)

        strategy_cfg = {
            "strategy_id": "btc_eth",
            "name": "MTPCS策略",
            "adapter_class": "ai_tuner.adapters.mtpcs_adapter.MTPCSAdapter",
            "config_path": "strategies/btc_eth/config.yaml",
        }

        with patch("ai_tuner.scheduler.weekly_job.logger") as mock_logger:
            result = await job._tune_single_strategy(strategy_cfg)

        # 验证返回 success（整体流程不中断）
        assert result == "success"

        # 验证 apply_overrides 被调用
        mocks["config_operator"].apply_overrides.assert_called_once()

        # 验证 mark_applied 不被调用
        mocks["db_handler"].mark_applied.assert_not_called()

        # 验证 send_auto_applied_notification 不被调用
        mocks["messenger"].send_auto_applied_notification.assert_not_called()

        # 验证 send_error_notification 被调用
        mocks["messenger"].send_error_notification.assert_called_once_with(
            strategy_name="MTPCS策略",
            strategy_id="btc_eth",
            error_message="自动应用配置失败，请查看日志",
        )

        # 验证日志记录"自动应用配置失败"
        mock_logger.error.assert_any_call(
            "自动应用配置失败", strategy_id="btc_eth", memory_id=42
        )

    @pytest.mark.asyncio
    async def test_auto_apply_disabled(self, base_config):
        """
        场景3: 自动应用禁用的场景

        - auto_apply.enabled = false
        - 验证：apply_overrides 不被调用
        - 验证：mark_applied 不被调用
        - 验证：send_auto_applied_notification 不被调用
        """
        # 修改配置，禁用自动应用
        config = base_config.copy()
        config["approval"]["auto_apply"]["enabled"] = False

        job, mocks = _create_job(config)

        strategy_cfg = {
            "strategy_id": "btc_eth",
            "name": "MTPCS策略",
            "adapter_class": "ai_tuner.adapters.mtpcs_adapter.MTPCSAdapter",
            "config_path": "strategies/btc_eth/config.yaml",
        }

        result = await job._tune_single_strategy(strategy_cfg)

        # 验证返回 success
        assert result == "success"

        # 验证 apply_overrides 不被调用
        mocks["config_operator"].apply_overrides.assert_not_called()

        # 验证 mark_applied 不被调用
        mocks["db_handler"].mark_applied.assert_not_called()

        # 验证 send_auto_applied_notification 不被调用
        mocks["messenger"].send_auto_applied_notification.assert_not_called()

        # 验证 send_error_notification 也不被调用
        mocks["messenger"].send_error_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_apply_no_config_path(self, base_config):
        """
        场景4: config_path 为空时的处理

        - auto_apply.enabled = true
        - config_path = None
        - 验证：apply_overrides 不被调用
        - 验证：日志记录"策略缺少 config_path，无法自动应用"
        """
        job, mocks = _create_job(base_config)

        strategy_cfg = {
            "strategy_id": "btc_eth",
            "name": "MTPCS策略",
            "adapter_class": "ai_tuner.adapters.mtpcs_adapter.MTPCSAdapter",
            "config_path": None,
        }

        with patch("ai_tuner.scheduler.weekly_job.logger") as mock_logger:
            result = await job._tune_single_strategy(strategy_cfg)

        # 验证返回 success
        assert result == "success"

        # 验证 apply_overrides 不被调用
        mocks["config_operator"].apply_overrides.assert_not_called()

        # 验证 mark_applied 不被调用
        mocks["db_handler"].mark_applied.assert_not_called()

        # 验证 send_auto_applied_notification 不被调用
        mocks["messenger"].send_auto_applied_notification.assert_not_called()

        # 验证日志记录"策略缺少 config_path，无法自动应用"
        mock_logger.warning.assert_any_call(
            "策略缺少 config_path，无法自动应用", strategy_id="btc_eth"
        )

    @pytest.mark.asyncio
    async def test_auto_apply_empty_config_path(self, base_config):
        """
        场景4补充: config_path 为空字符串时的处理

        - auto_apply.enabled = true
        - config_path = ""（空字符串）
        - 验证：apply_overrides 不被调用
        - 验证：日志记录"策略缺少 config_path，无法自动应用"
        """
        job, mocks = _create_job(base_config)

        strategy_cfg = {
            "strategy_id": "btc_eth",
            "name": "MTPCS策略",
            "adapter_class": "ai_tuner.adapters.mtpcs_adapter.MTPCSAdapter",
            "config_path": "",
        }

        with patch("ai_tuner.scheduler.weekly_job.logger") as mock_logger:
            result = await job._tune_single_strategy(strategy_cfg)

        # 验证返回 success
        assert result == "success"

        # 验证 apply_overrides 不被调用
        mocks["config_operator"].apply_overrides.assert_not_called()

        # 验证日志记录"策略缺少 config_path，无法自动应用"
        mock_logger.warning.assert_any_call(
            "策略缺少 config_path，无法自动应用", strategy_id="btc_eth"
        )


class TestSendAutoAppliedNotification:
    """send_auto_applied_notification 方法测试"""

    @pytest.fixture
    def mock_notification_client(self):
        """模拟 NotificationClient"""
        client = MagicMock()
        client.send = AsyncMock(return_value=True)
        return client

    @pytest.fixture
    def messenger(self, mock_notification_client):
        """创建 Messenger 实例"""
        return Messenger(notification_client=mock_notification_client)

    @pytest.mark.asyncio
    async def test_method_signature(self, messenger):
        """
        验证方法签名正确

        - 方法接收 strategy_name, strategy_id, diff_text 三个参数
        - 返回 bool 类型
        """
        result = await messenger.send_auto_applied_notification(
            strategy_name="测试策略",
            strategy_id="test_strategy",
            diff_text="+ param1: 0.3 -> 0.5",
        )

        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_message_content(self, messenger, mock_notification_client):
        """
        验证消息内容格式正确

        - 包含"【调优已自动应用】"
        - 包含"AI 自动生效"
        - 包含策略名称
        - 包含变更清单
        """
        await messenger.send_auto_applied_notification(
            strategy_name="MTPCS策略",
            strategy_id="btc_eth",
            diff_text="+ param1: 0.3 -> 0.5",
        )

        # 获取发送的消息内容
        call_args = mock_notification_client.send.call_args
        message = call_args[1]["message"] if "message" in call_args[1] else call_args[0][0]

        # 验证关键文本
        assert "【调优已自动应用】" in message
        assert "AI 自动生效" in message
        assert "MTPCS策略" in message
        assert "+ param1: 0.3 -> 0.5" in message

        # 验证消息级别为 info
        assert call_args[1]["level"] == "info"

        # 验证 project 为 tuner
        assert call_args[1]["project"] == "tuner"

    @pytest.mark.asyncio
    async def test_send_success_return_true(self, messenger, mock_notification_client):
        """
        验证发送成功时返回 True
        """
        result = await messenger.send_auto_applied_notification(
            strategy_name="MTPCS策略",
            strategy_id="btc_eth",
            diff_text="+ param1: 0.3 -> 0.5",
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_send_failure_return_false(self, messenger, mock_notification_client):
        """
        验证发送异常时返回 False 并记录日志
        """
        mock_notification_client.send = AsyncMock(side_effect=Exception("网络错误"))

        with patch("ai_tuner.notifier.messenger.logger") as mock_logger:
            result = await messenger.send_auto_applied_notification(
                strategy_name="MTPCS策略",
                strategy_id="btc_eth",
                diff_text="+ param1: 0.3 -> 0.5",
            )

        assert result is False

        # 验证日志记录错误
        mock_logger.error.assert_called_once_with(
            "发送自动应用通知失败", error="网络错误"
        )

    @pytest.mark.asyncio
    async def test_message_contains_current_time(self, messenger, mock_notification_client):
        """
        验证消息中包含当前时间（格式为 YYYY-MM-DD HH:MM）
        """
        with patch("ai_tuner.notifier.messenger.datetime") as mock_datetime:
            fixed_now = datetime(2026, 8, 12, 15, 30, 0)
            mock_datetime.now.return_value = fixed_now

            await messenger.send_auto_applied_notification(
                strategy_name="MTPCS策略",
                strategy_id="btc_eth",
                diff_text="+ param1: 0.3 -> 0.5",
            )

            # 获取发送的消息内容
            call_args = mock_notification_client.send.call_args
            message = call_args[1]["message"] if "message" in call_args[1] else call_args[0][0]

            # 验证时间格式
            assert "2026-08-12 15:30" in message