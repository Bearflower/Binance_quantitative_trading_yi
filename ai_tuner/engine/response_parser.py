"""
JSON 响应解析与校验
负责解析 DeepSeek API 返回的 JSON 响应，验证格式和参数合法性

安全措施：
1. JSON 格式校验 → 解析失败则丢弃 + 告警
2. 参数白名单校验 → 不在白名单的参数直接拒绝
3. 参数范围校验 → 超出预设边界的值截断到边界值
4. 空建议检测 → 如果 AI 建议"维持不变"，也记录到记忆库
"""

import json
import re
from typing import Any, Dict, List

import structlog

logger = structlog.get_logger()


class ResponseParser:
    """
    AI 响应解析器

    负责从 LLM 原始响应中提取 JSON 和思考链内容，验证格式和内容合法性。
    支持思考模式下返回的 __REASONING__...__CONTENT__ 格式。
    """

    # 必需字段列表
    REQUIRED_FIELDS: List[str] = ["reasons", "summary", "adjustments", "expected_impact"]

    def parse_response(self, raw_text: str) -> Dict[str, Any]:
        """
        从 LLM 原始响应中解析 JSON

        处理思考模式下的 __REASONING__...__CONTENT__ 格式，
        以及可能的 markdown 代码块包裹，提取纯净 JSON。

        Args:
            raw_text: LLM 返回的原始文本

        Returns:
            解析后的字典，包含 reasons, summary, adjustments, expected_impact 等字段
            以及 reasoning（思考链内容，如有）
            解析失败返回 {"error": "错误描述"}
        """
        if not raw_text or not raw_text.strip():
            logger.error("LLM响应为空")
            return {"error": "LLM 响应为空"}

        # 分离 reasoning_content 和 content
        reasoning = ""
        text_to_parse = raw_text
        if raw_text.startswith("__REASONING__"):
            # 使用 ____END_REASONING____ 作为结束标记，降低内容碰撞风险
            parts = raw_text.split("____END_REASONING____", 1)
            if len(parts) == 2:
                reasoning = parts[0].replace("__REASONING__", "", 1).strip()
                content_part = parts[1]
                # 去除 __CONTENT__ 标记
                content_part = content_part.replace("__CONTENT__", "", 1).strip()
                text_to_parse = content_part
            else:
                # 格式异常，尝试整体解析
                text_to_parse = raw_text.replace("__REASONING__", "").strip()

        # 尝试提取 JSON（处理 markdown 代码块包裹）
        json_text = self._extract_json(text_to_parse)

        if not json_text:
            logger.error("无法从LLM响应中提取JSON", raw_preview=raw_text[:200])
            return {"error": "无法从响应中提取 JSON"}

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as e:
            # 尝试修复常见 JSON 格式问题后重试解析
            fixed_json = self._fix_json(json_text)
            if fixed_json != json_text:
                try:
                    data = json.loads(fixed_json)
                    logger.info("JSON修复后解析成功", original_preview=json_text[:100])
                except json.JSONDecodeError:
                    logger.error("JSON解析失败（修复后仍失败）", error=str(e), json_preview=json_text[:200])
                    return {"error": f"JSON 解析失败: {str(e)}"}
            else:
                logger.error("JSON解析失败", error=str(e), json_preview=json_text[:200])
                return {"error": f"JSON 解析失败: {str(e)}"}

        if not isinstance(data, dict):
            logger.error("解析结果不是字典", type=str(type(data)))
            return {"error": "解析结果不是 JSON 对象"}

        # 验证必需字段
        missing_fields = [f for f in self.REQUIRED_FIELDS if f not in data]
        if missing_fields:
            logger.error("缺少必需字段", missing_fields=missing_fields)
            return {"error": f"缺少必需字段: {', '.join(missing_fields)}"}

        # 验证 adjustments 格式
        adjustments = data.get("adjustments", {})
        if not isinstance(adjustments, dict):
            logger.error("adjustments格式错误", type=str(type(adjustments)))
            return {"error": "adjustments 字段格式错误，应为字典"}

        # 验证每个 adjustment 项的格式
        for key, value in adjustments.items():
            if not isinstance(value, dict) or "from" not in value or "to" not in value:
                logger.warning(
                    "adjustment项格式不标准，尝试兼容",
                    key=key,
                    value=value,
                )
                # 兼容处理：如果 value 不是标准格式，尝试包装
                if isinstance(value, (int, float)):
                    adjustments[key] = {"from": None, "to": value}
                elif isinstance(value, dict) and "to" in value:
                    if "from" not in value:
                        adjustments[key]["from"] = None
                else:
                    logger.error("adjustment项格式无法解析", key=key, value=value)
                    return {"error": f"adjustments 中 {key} 格式错误"}

        data["adjustments"] = adjustments

        # 附上思考链内容（如有）
        if reasoning:
            data["reasoning"] = reasoning

        logger.info(
            "JSON解析成功",
            adjustments_count=len(adjustments),
            has_summary=bool(data.get("summary")),
            has_reasoning=bool(reasoning),
        )
        return data

    def validate_adjustments(
        self,
        adjustments: Dict[str, Any],
        adapter,
    ) -> Dict[str, Any]:
        """
        校验 AI 建议的参数调整

        调用适配器的 validate_params 方法进行白名单和范围校验。

        Args:
            adjustments: AI 建议的参数调整
            adapter: 策略适配器实例（BaseAdapter 子类）

        Returns:
            校验结果 {"valid": bool, "errors": list, "validated": dict}
        """
        if not adjustments:
            return {"valid": True, "errors": [], "validated": {}}

        try:
            result = adapter.validate_params(adjustments)
            if result["errors"]:
                logger.warning(
                    "参数校验发现问题",
                    strategy_id=adapter.strategy_id,
                    error_count=len(result["errors"]),
                    errors=result["errors"],
                )
            return result
        except Exception as e:
            logger.error(
                "参数校验异常",
                strategy_id=getattr(adapter, "strategy_id", "unknown"),
                error=str(e),
            )
            return {"valid": False, "errors": [f"校验异常: {str(e)}"], "validated": {}}

    @staticmethod
    def _extract_json(text: str) -> str:
        """
        从文本中提取 JSON 内容

        处理常见的 markdown 代码块包裹格式：
        ```json
        {...}
        ```

        Args:
            text: 原始文本

        Returns:
            提取的 JSON 字符串，如果无法提取返回空字符串
        """
        text = text.strip()

        # 尝试匹配 markdown 代码块 ```json ... ```
        pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # 尝试匹配花括号包裹的 JSON
        brace_start = text.find("{")
        if brace_start == -1:
            return ""

        # 从第一个 { 开始，找到匹配的 }
        depth = 0
        for i in range(brace_start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[brace_start : i + 1]

        # 如果花括号未闭合，返回从 { 到末尾
        return text[brace_start:]

    @staticmethod
    def _fix_json(text: str) -> str:
        """
        尝试修复常见 JSON 格式问题

        修复策略：
        1. 未闭合的字符串（末尾的字符串缺少闭合引号）→ 补全引号
        2. 未闭合的花括号 → 补全花括号

        Args:
            text: 原始 JSON 文本

        Returns:
            修复后的 JSON 文本，如无法修复返回原文本
        """
        text = text.strip()
        if not text:
            return text

        fixed = text

        # 1. 检查并修复未闭合的字符串
        in_string = False
        for i, ch in enumerate(fixed):
            if ch == '"' and (i == 0 or fixed[i - 1] != '\\'):
                in_string = not in_string
        if in_string:
            # 字符串未闭合，在末尾添加闭合引号
            fixed += '"'

        # 2. 补全未闭合的花括号
        open_braces = fixed.count('{') - fixed.count('}')
        if open_braces > 0:
            fixed += '}' * open_braces

        return fixed if fixed != text else text