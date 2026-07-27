"""
DeepSeek API 封装
使用 OpenAI 兼容 SDK 调用 DeepSeek-V4-Pro 模型（deepseek-v4-pro）

支持思考模式、推理强度控制、指数退避重试（最多3次），
异常时记录日志并返回空字符串。
"""

import asyncio
from typing import Any, Callable, Dict, Optional

import structlog
from openai import AsyncOpenAI

logger = structlog.get_logger()


class LLMClient:
    """
    DeepSeek LLM 客户端

    封装 OpenAI 兼容 SDK，提供异步 API 调用能力。
    支持思考模式（thinking mode）、推理强度控制（reasoning_effort）、
    自动重试、指数退避、Token 用量统计回调。
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化 LLM 客户端

        Args:
            config: AI 引擎配置字典，包含 api_key, base_url, model,
                    temperature, max_tokens, max_retries,
                    thinking_mode（是否开启思考模式）, reasoning_effort（推理强度）
        """
        api_key = self._resolve_env_var(config.get("api_key", ""))
        self.base_url = config.get("base_url", "https://api.deepseek.com")
        self.model = config.get("model", "deepseek-v4-pro")
        self.temperature = float(config.get("temperature", 0.3))
        self.max_tokens = int(config.get("max_tokens", 2048))
        self.max_retries = int(config.get("max_retries", 3))

        # 思考模式配置
        self.thinking_enabled = config.get("thinking_enabled", True)
        self.reasoning_effort = config.get("reasoning_effort", "high")

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.base_url,
        )

        # Token 用量回调（可选，由 cost_tracker 设置）
        self._usage_callback: Optional[Callable] = None

        logger.info(
            "LLM客户端初始化完成",
            model=self.model,
            base_url=self.base_url,
            thinking_enabled=self.thinking_enabled,
            reasoning_effort=self.reasoning_effort,
        )

    def set_usage_callback(self, callback: Callable) -> None:
        """
        设置 Token 用量回调函数

        Args:
            callback: 回调函数，签名为 callback(model, prompt_tokens, completion_tokens, total_tokens)
        """
        self._usage_callback = callback

    async def call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
    ) -> str:
        """
        调用 DeepSeek API 获取 AI 响应

        支持指数退避重试：2s, 4s, 8s
        如果开启了思考模式，则通过 extra_body 传递 thinking 参数，
        temperature 等采样参数在思考模式下会被忽略（设置不报错但不生效）。

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            max_retries: 最大重试次数，默认使用配置值
            reasoning_effort: 推理强度（high/max），覆盖配置默认值

        Returns:
            AI 响应的文本内容（含 reasoning_content 和 content），异常时返回空字符串
        """
        retries = max_retries if max_retries is not None else self.max_retries
        effort = reasoning_effort or self.reasoning_effort

        # 构建请求参数
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self.max_tokens,
        }

        # 思考模式下：通过 extra_body 传递 thinking 和 reasoning_effort 参数
        if self.thinking_enabled:
            kwargs["extra_body"] = {
                "thinking": {"type": "enabled"},
                "reasoning_effort": effort,
            }
        else:
            # 非思考模式：使用 temperature 控制输出
            kwargs["temperature"] = self.temperature

        for attempt in range(retries):
            try:
                response = await self.client.chat.completions.create(**kwargs)

                # 获取思考链内容（思考模式下返回）
                reasoning_content = ""
                if hasattr(response.choices[0].message, "reasoning_content"):
                    reasoning_content = response.choices[0].message.reasoning_content or ""

                content = response.choices[0].message.content
                if not content:
                    content = ""

                # 记录 Token 用量
                usage = response.usage
                if usage and self._usage_callback:
                    self._usage_callback(
                        model=self.model,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                    )

                logger.info(
                    "LLM调用成功",
                    attempt=attempt + 1,
                    thinking_mode=self.thinking_enabled,
                    reasoning_effort=effort,
                    reasoning_length=len(reasoning_content),
                    prompt_tokens=usage.prompt_tokens if usage else 0,
                    completion_tokens=usage.completion_tokens if usage else 0,
                    content_length=len(content),
                )

                # 将 reasoning_content 和 content 一起返回，用分隔符区分
                # 分隔符包含随机后缀降低内容碰撞风险
                if reasoning_content:
                    delimiter = "____END_REASONING____"
                    return f"__REASONING__\n{reasoning_content}\n{delimiter}\n__CONTENT__\n{content}"
                return content

            except Exception as e:
                wait_time = 2 ** (attempt + 1)  # 2s, 4s, 8s
                logger.error(
                    "LLM调用失败",
                    attempt=attempt + 1,
                    max_retries=retries,
                    model=self.model,
                    error=str(e),
                    wait_seconds=wait_time,
                )
                if attempt < retries - 1:
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("LLM调用全部重试失败", total_attempts=retries)
                    return ""

        return ""

    @staticmethod
    def _resolve_env_var(value: str) -> str:
        """
        解析环境变量占位符 ${VAR_NAME}

        Args:
            value: 可能包含环境变量占位符的字符串

        Returns:
            解析后的值
        """
        import os
        import re

        def replace_env(match):
            var_expr = match.group(1)
            # 支持 ${VAR:default} 格式
            if ":" in var_expr:
                var_name, default = var_expr.split(":", 1)
                return os.getenv(var_name, default)
            return os.getenv(var_expr, "")

        return re.sub(r"\$\{([^}]+)\}", replace_env, value)