"""
JSON 响应解析与校验

双契约并行：
- 新契约（MVP 候选）: 8 字段严格信封 PROPOSE/HOLD/DATA_ERROR
- 旧契约（其他策略）: 维持 reasons/summary/adjustments/expected_impact 宽松格式不变

调用方通过 contract_mode 参数选择契约：
- "legacy": 旧契约，默认值，保持向后兼容
- "new": 新契约，严格校验 decision、objective、adjustments 一致性
"""

import json
import re
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


# --- 新契约常量 ---

# 唯一优化目标（固定字符串，LLM 必须原样输出）
OBJECTIVE = "maximize_risk_adjusted_net_return_under_drawdown_cap"

# 允许的 decision 枚举
DECISIONS = {"PROPOSE", "HOLD", "DATA_ERROR"}

# 新信封必需字段（8 个）
NEW_REQUIRED_FIELDS: List[str] = [
    "decision",
    "objective",
    "diagnosis",
    "target_metrics",
    "adjustments",
    "applicable_market_regimes",
    "expected_impact",
    "confidence",
]

# 旧契约必需字段（维持不变）
LEGACY_REQUIRED_FIELDS: List[str] = [
    "reasons",
    "summary",
    "adjustments",
    "expected_impact",
]


class ResponseParser:
    """
    AI 响应解析器

    通过 contract_mode 选择校验策略：
    - "legacy"（默认）: 旧契约，宽松校验，保持与其他策略兼容
    - "new": 新契约，严格校验 decision/objective/adjustments 一致性
    """

    def parse_response(
        self,
        raw_text: str,
        contract_mode: str = "legacy",
    ) -> Dict[str, Any]:
        """
        从 LLM 原始响应中提取 JSON 并按指定契约校验

        Args:
            raw_text: LLM 返回的原始文本（可能含 __REASONING__...__CONTENT__ 格式或 markdown 代码块）
            contract_mode: "legacy" 或 "new"

        Returns:
            解析后的字典；校验/解析失败返回 {"error": "..."}
        """
        if raw_text is None or not raw_text.strip():
            logger.error("LLM 响应为空")
            return {"error": "LLM 响应为空"}

        # --- 分离 reasoning ---
        reasoning, text_to_parse = self._separate_reasoning(raw_text)

        # --- 提取 JSON ---
        json_text = self._extract_json(text_to_parse)
        if not json_text:
            logger.info("__CONTENT__ 部分无 JSON，回退全文搜索")
            json_text = self._extract_json(raw_text)

        if not json_text:
            logger.error("无法从 LLM 响应中提取 JSON", raw_preview=raw_text[:200])
            return {"error": "无法从响应中提取 JSON"}

        # --- 解析 JSON ---
        data = self._parse_json_text(json_text)
        if isinstance(data, dict) and "error" in data:
            return data  # 解析失败，已返回 {"error": "..."}

        if not isinstance(data, dict):
            logger.error("解析结果不是字典", type=str(type(data)))
            return {"error": "解析结果不是 JSON 对象"}

        # --- 按契约校验 ---
        if contract_mode == "new":
            envelope_error = self._validate_new_envelope(data)
            if envelope_error:
                logger.error("新信封契约校验失败", error=envelope_error)
                return {"error": envelope_error}
        else:
            # legacy：必需字段检查
            missing = [f for f in LEGACY_REQUIRED_FIELDS if f not in data]
            if missing:
                logger.error("旧契约缺少必需字段", missing=missing)
                return {"error": f"缺少必需字段: {', '.join(missing)}"}

        # --- adjustments 格式兼容（legacy 用）---
        adjustments = data.get("adjustments", {})
        if not isinstance(adjustments, dict):
            logger.error("adjustments 格式错误", type=str(type(adjustments)))
            return {"error": "adjustments 字段格式错误，应为字典"}

        if contract_mode == "legacy":
            adjustments = self._normalize_legacy_adjustments(adjustments)
            if adjustments is None:
                return {"error": "adjustments 格式错误"}
        data["adjustments"] = adjustments

        # 附加 reasoning
        if reasoning:
            data["reasoning"] = reasoning

        logger.info(
            "JSON 解析成功",
            contract_mode=contract_mode,
            adjustments_count=len(adjustments),
        )
        return data

    # ---------------------------------------------------------------
    # 新信封契约校验
    # ---------------------------------------------------------------

    def _validate_new_envelope(self, data: Dict[str, Any]) -> Optional[str]:
        """
        严格校验新信封契约

        返回 None 表示全部通过；返回错误描述字符串表示失败。
        """
        # 1. 必需字段存在性
        missing = [f for f in NEW_REQUIRED_FIELDS if f not in data]
        if missing:
            return f"新信封缺少字段: {', '.join(missing)}"

        # 2. decision 枚举
        decision = data.get("decision")
        if decision not in DECISIONS:
            return f"decision 必须是 PROPOSE / HOLD / DATA_ERROR，收到 {decision!r}"

        # 3. objective 必须精确匹配
        if data.get("objective") != OBJECTIVE:
            return "objective 与配置的优化目标不一致"

        # 4. adjustments 与 decision 一致性
        adjustments = data.get("adjustments")
        if not isinstance(adjustments, dict):
            return "adjustments 必须是字典"
        if decision == "PROPOSE" and not adjustments:
            return "PROPOSE 必须包含至少 1 个 adjustment"
        if decision != "PROPOSE" and adjustments:
            return f"{decision} 不得携带 adjustments"

        # 5. confidence 范围（必须是数值，拒绝 bool）
        confidence = data.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return "confidence 必须是数值"
        if not 0 <= float(confidence) <= 1:
            return "confidence 必须在 [0, 1] 之间"

        # 6. 其他类型检查
        if not isinstance(data.get("diagnosis"), list):
            return "diagnosis 必须是列表"
        if not isinstance(data.get("target_metrics"), dict):
            return "target_metrics 必须是字典"
        if not isinstance(data.get("applicable_market_regimes"), list):
            return "applicable_market_regimes 必须是列表"
        if not isinstance(data.get("expected_impact"), dict):
            return "expected_impact 必须是字典"

        return None

    # ---------------------------------------------------------------
    # 旧契约 adjustments 格式兼容
    # ---------------------------------------------------------------

    def _normalize_legacy_adjustments(
        self, adjustments: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        旧契约宽松兼容：允许 adjustment 项为简单数值或 dict 缺 from

        返回规范化后的 adjustments；遇到无法解析的项返回 None。
        """
        normalized = {}
        for key, value in adjustments.items():
            if isinstance(value, (int, float)):
                normalized[key] = {"from": None, "to": value}
            elif isinstance(value, dict) and "to" in value:
                item = dict(value)
                if "from" not in item:
                    item["from"] = None
                normalized[key] = item
            else:
                logger.error(
                    "adjustment 项无法解析",
                    key=key,
                    value=value,
                )
                return None
        return normalized

    # ---------------------------------------------------------------
    # 参数校验入口（被 WeeklyTuningJob 调用）
    # ---------------------------------------------------------------

    def validate_adjustments(
        self,
        adjustments: Dict[str, Any],
        adapter,
        decision: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        调用 adapter 的 validate_params 做白名单/范围/manifest 校验

        Args:
            adjustments: LLM 建议的参数调整
            adapter: 策略适配器实例
            decision: 新契约的 decision（PROPOSE/HOLD/DATA_ERROR），
                      None 时旧契约默认 PROPOSE

        Returns:
            校验结果 {"valid": bool, "errors": list, "warnings": list, "validated": dict}
        """
        if not adjustments:
            return {"valid": True, "errors": [], "warnings": [], "validated": {}}

        try:
            # 先尝试传递 decision 参数（新契约路径）；旧适配器不接受时降级
            try:
                result = adapter.validate_params(
                    adjustments, decision=decision or "PROPOSE"
                )
            except TypeError:
                result = adapter.validate_params(adjustments)
            if result.get("errors"):
                logger.warning(
                    "参数校验发现问题",
                    strategy_id=getattr(adapter, "strategy_id", "unknown"),
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
            return {
                "valid": False,
                "errors": [f"校验异常: {str(e)}"],
                "warnings": [],
                "validated": {},
            }

    # ---------------------------------------------------------------
    # 内部工具方法（保留原有实现）
    # ---------------------------------------------------------------

    @staticmethod
    def _separate_reasoning(raw_text: str) -> tuple:
        """从思考模式响应中分离 reasoning_content 和 content"""
        reasoning = ""
        if raw_text.startswith("__REASONING__"):
            parts = raw_text.split("____END_REASONING____", 1)
            if len(parts) == 2:
                reasoning = parts[0].replace("__REASONING__", "", 1).strip()
                content_part = parts[1].replace("__CONTENT__", "", 1).strip()
                return reasoning, content_part
            # 格式异常，去标记后整体解析
            return reasoning, raw_text.replace("__REASONING__", "").strip()
        return reasoning, raw_text

    @staticmethod
    def _extract_first_json_object(text: str) -> str:
        """提取文本中第一个完整 JSON 对象（字符串感知）"""
        text = text.strip()
        brace_start = text.find("{")
        if brace_start == -1:
            return ""
        depth = 0
        in_string = False
        escaped = False
        for i in range(brace_start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[brace_start : i + 1]
        return ""

    @staticmethod
    def _extract_json(text: str) -> str:
        """从 markdown 代码块或全文中提取 JSON"""
        text = text.strip()
        pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
        for match in re.finditer(pattern, text, re.DOTALL):
            block = match.group(1).strip()
            if block.startswith("{"):
                json_obj = ResponseParser._extract_first_json_object(block)
                if json_obj:
                    return json_obj
        return ResponseParser._extract_first_json_object(text)

    @staticmethod
    def _parse_json_text(json_text: str) -> Any:
        """解析 JSON 文本，容忍尾随内容和常见格式问题"""
        try:
            return json.loads(json_text)
        except json.JSONDecodeError:
            # raw_decode 容忍尾随多余内容
            try:
                decoded, _ = json.JSONDecoder().raw_decode(json_text.strip())
                if isinstance(decoded, dict):
                    logger.info("JSON 经 raw_decode 容忍尾随内容后解析成功")
                    return decoded
            except json.JSONDecodeError:
                pass
            # 尝试修复后重试
            fixed = ResponseParser._fix_json(json_text)
            if fixed != json_text:
                try:
                    return json.loads(fixed)
                except json.JSONDecodeError as e2:
                    logger.error(
                        "JSON 修复后仍解析失败",
                        error=str(e2),
                        json_preview=json_text[:200],
                    )
            return {"error": f"JSON 解析失败: {json_text}"}

    @staticmethod
    def _fix_json(text: str) -> str:
        """修复未闭合的字符串和花括号"""
        text = text.strip()
        if not text:
            return text
        fixed = text
        in_string = False
        for i, ch in enumerate(fixed):
            if ch == '"' and (i == 0 or fixed[i - 1] != "\\"):
                in_string = not in_string
        if in_string:
            fixed += '"'
        open_braces = fixed.count("{") - fixed.count("}")
        if open_braces > 0:
            fixed += "}" * open_braces
        return fixed if fixed != text else text
