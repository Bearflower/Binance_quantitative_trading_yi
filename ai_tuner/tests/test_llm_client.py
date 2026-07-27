"""
测试 LLMClient
覆盖思考模式、推理强度、重试逻辑、返回值格式
"""

import sys
sys.path.insert(0, ".")

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from types import SimpleNamespace

from ai_tuner.engine.llm_client import LLMClient


@pytest.fixture
def base_config():
    return {
        "api_key": "test-key",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-pro",
        "thinking_enabled": True,
        "reasoning_effort": "high",
        "temperature": 0.3,
        "max_tokens": 2048,
        "max_retries": 2,
    }


@pytest.fixture
def llm_client(base_config):
    with patch("ai_tuner.engine.llm_client.AsyncOpenAI") as mock_openai:
        client = LLMClient(base_config)
        client.client = mock_openai.return_value
        yield client


def _mock_response(content: str, reasoning: str = "", usage=None):
    """构造模拟的 API 响应"""
    msg = MagicMock()
    msg.content = content
    msg.reasoning_content = reasoning

    choice = MagicMock()
    choice.message = msg

    resp = MagicMock()
    resp.choices = [choice]

    if usage:
        resp.usage = SimpleNamespace(
            prompt_tokens=usage.get("prompt_tokens", 100),
            completion_tokens=usage.get("completion_tokens", 50),
            total_tokens=usage.get("total_tokens", 150),
        )
    else:
        resp.usage = None

    return resp


class TestThinkingModeEnabled:
    """测试思考模式开启时的行为"""

    async def test_extra_body_thinking_enabled(self, llm_client):
        """思考模式开启时，应传递 extra_body 含 thinking 和 reasoning_effort"""
        llm_client.client.chat.completions.create = AsyncMock(
            return_value=_mock_response('{"summary": "test"}')
        )
        await llm_client.call_llm("system", "user")
        call_kwargs = llm_client.client.chat.completions.create.call_args.kwargs
        assert call_kwargs["extra_body"] == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }

    async def test_reasoning_effort_passed(self, llm_client):
        """思考模式开启时，extra_body 中应包含 reasoning_effort"""
        llm_client.client.chat.completions.create = AsyncMock(
            return_value=_mock_response('{"summary": "test"}')
        )
        await llm_client.call_llm("system", "user")
        call_kwargs = llm_client.client.chat.completions.create.call_args.kwargs
        assert call_kwargs["extra_body"]["reasoning_effort"] == "high"

    async def test_reasoning_effort_override(self, llm_client):
        """应支持调用时覆盖 reasoning_effort（在 extra_body 中）"""
        llm_client.client.chat.completions.create = AsyncMock(
            return_value=_mock_response('{"summary": "test"}')
        )
        await llm_client.call_llm("system", "user", reasoning_effort="max")
        call_kwargs = llm_client.client.chat.completions.create.call_args.kwargs
        assert call_kwargs["extra_body"]["reasoning_effort"] == "max"

    async def test_no_temperature_in_thinking_mode(self, llm_client):
        """思考模式下不应传递 temperature"""
        llm_client.client.chat.completions.create = AsyncMock(
            return_value=_mock_response('{"summary": "test"}')
        )
        await llm_client.call_llm("system", "user")
        call_kwargs = llm_client.client.chat.completions.create.call_args.kwargs
        assert "temperature" not in call_kwargs

    async def test_reasoning_content_returned(self, llm_client):
        """思考模式下，应返回 __REASONING__/__CONTENT__ 格式"""
        reasoning_text = "分析本周数据\n1. 胜率偏低\n2. 建议调整阈值"
        content_text = '{"summary": "test"}'
        llm_client.client.chat.completions.create = AsyncMock(
            return_value=_mock_response(content_text, reasoning=reasoning_text)
        )
        result = await llm_client.call_llm("system", "user")
        assert result.startswith("__REASONING__")
        assert "__CONTENT__" in result
        assert reasoning_text in result
        assert content_text in result


class TestThinkingModeDisabled:
    """测试思考模式关闭时的行为"""

    async def test_temperature_passed(self, base_config):
        """关闭思考模式时，应传递 temperature"""
        config = dict(base_config, thinking_enabled=False)
        with patch("ai_tuner.engine.llm_client.AsyncOpenAI"):
            client = LLMClient(config)
            client.client.chat.completions.create = AsyncMock(
                return_value=_mock_response('{"summary": "test"}')
            )
            await client.call_llm("system", "user")
            call_kwargs = client.client.chat.completions.create.call_args.kwargs
            assert call_kwargs["temperature"] == 0.3

    async def test_no_extra_body_in_non_thinking(self, base_config):
        """关闭思考模式时，不应传递 extra_body"""
        config = dict(base_config, thinking_enabled=False)
        with patch("ai_tuner.engine.llm_client.AsyncOpenAI"):
            client = LLMClient(config)
            client.client.chat.completions.create = AsyncMock(
                return_value=_mock_response('{"summary": "test"}')
            )
            await client.call_llm("system", "user")
            call_kwargs = client.client.chat.completions.create.call_args.kwargs
            assert "extra_body" not in call_kwargs

    async def test_no_reasoning_flag_in_non_thinking(self, base_config):
        """关闭思考模式，且无 reasoning_content 时，应返回纯文本"""
        config = dict(base_config, thinking_enabled=False)
        with patch("ai_tuner.engine.llm_client.AsyncOpenAI"):
            client = LLMClient(config)
            client.client.chat.completions.create = AsyncMock(
                return_value=_mock_response('{"summary": "test"}')
            )
            result = await client.call_llm("system", "user")
            assert result == '{"summary": "test"}'


class TestRetryLogic:
    """测试重试逻辑"""

    async def test_retry_on_failure(self, llm_client):
        """首次失败后应重试，第二次成功"""
        llm_client.client.chat.completions.create = AsyncMock(
            side_effect=[
                Exception("API error"),
                _mock_response('{"summary": "test"}'),
            ]
        )
        result = await llm_client.call_llm("system", "user")
        assert result == '{"summary": "test"}'
        assert llm_client.client.chat.completions.create.call_count == 2

    async def test_all_retries_fail(self, llm_client):
        """全部重试失败应返回空字符串"""
        llm_client.client.chat.completions.create = AsyncMock(
            side_effect=Exception("API error")
        )
        result = await llm_client.call_llm("system", "user")
        assert result == ""
        assert llm_client.client.chat.completions.create.call_count == 2  # max_retries

    async def test_usage_callback_invoked(self, llm_client):
        """Token 用量回调应被正确调用"""
        callback = MagicMock()
        llm_client.set_usage_callback(callback)
        usage = {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300}
        llm_client.client.chat.completions.create = AsyncMock(
            return_value=_mock_response("ok", usage=usage)
        )
        await llm_client.call_llm("system", "user")
        callback.assert_called_once_with(
            model="deepseek-v4-pro",
            prompt_tokens=200,
            completion_tokens=100,
            total_tokens=300,
        )


class TestConfigInitialization:
    """测试配置初始化"""

    def test_default_model(self):
        """默认模型应为 deepseek-v4-pro"""
        with patch("ai_tuner.engine.llm_client.AsyncOpenAI"):
            client = LLMClient({"api_key": "test"})
            assert client.model == "deepseek-v4-pro"

    def test_thinking_enabled_default(self):
        """thinking_enabled 默认应为 True"""
        with patch("ai_tuner.engine.llm_client.AsyncOpenAI"):
            client = LLMClient({"api_key": "test"})
            assert client.thinking_enabled is True

    def test_reasoning_effort_default(self):
        """reasoning_effort 默认应为 high"""
        with patch("ai_tuner.engine.llm_client.AsyncOpenAI"):
            client = LLMClient({"api_key": "test"})
            assert client.reasoning_effort == "high"


class TestEnvVarResolution:
    """测试环境变量解析"""

    def test_resolve_env_var_basic(self):
        """应解析 ${VAR_NAME} 格式"""
        with patch("ai_tuner.engine.llm_client.AsyncOpenAI") as mock_openai, \
             patch.dict("os.environ", {"DEEPSEEK_API_KEY": "my-key"}, clear=True):
            client = LLMClient({"api_key": "${DEEPSEEK_API_KEY}"})
            # 检查 AsyncOpenAI 是否用正确的 api_key 初始化
            actual_api_key = client._resolve_env_var("${DEEPSEEK_API_KEY}")
            assert actual_api_key == "my-key"

    def test_resolve_env_var_with_default(self):
        """应解析 ${VAR:default} 格式"""
        result = LLMClient._resolve_env_var("${NOT_EXIST:default_val}")
        assert result == "default_val"

    def test_resolve_env_var_not_found(self):
        """环境变量不存在且无默认值时返回空"""
        result = LLMClient._resolve_env_var("${NOT_EXIST}")
        assert result == ""

    def test_resolve_no_placeholder(self):
        """没有占位符时直接返回原值"""
        result = LLMClient._resolve_env_var("plain-value")
        assert result == "plain-value"