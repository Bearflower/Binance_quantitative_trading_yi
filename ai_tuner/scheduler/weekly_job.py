"""
周度调优主流程
每周日 23:55 触发，遍历所有已注册策略，执行完整的 AI 调优流水线

流程：
1. 遍历已注册策略列表
2. 对每个策略：采集数据 → 构建上下文 → 渲染 Prompt → 调用 LLM → 解析响应 → 校验参数 → 保存记忆 → 推送审批
3. 记录 Token 用量和成本
"""

import importlib
import os
from typing import Any, Dict

import structlog
from jinja2 import Template

from ai_tuner.engine.cost_tracker import CostTracker
from ai_tuner.engine.llm_client import LLMClient
from ai_tuner.engine.response_parser import ResponseParser
from ai_tuner.deploy.diff_generator import DiffGenerator
from ai_tuner.memory.context_builder import ContextBuilder

logger = structlog.get_logger()


# 动态导入白名单：只允许 ai_tuner.adapters 命名空间下的模块
_IMPORT_WHITELIST_PREFIX = "ai_tuner.adapters."


class WeeklyTuningJob:
    """
    周度调优任务

    执行完整的 AI 调优流水线：采集数据 → 调用 AI → 解析建议 → 推送审批
    """

    def __init__(
        self,
        config: Dict[str, Any],
        db_manager,
        notification_client,
        llm_client: LLMClient,
        db_handler,
        cost_tracker: CostTracker,
        messenger,
        config_operator,
        rollback_manager,
    ):
        """
        初始化周度调优任务

        Args:
            config: 完整系统配置
            db_manager: DatabaseManager 实例
            notification_client: NotificationClient 实例
            llm_client: LLMClient 实例
            db_handler: MemoryDBHandler 实例
            cost_tracker: CostTracker 实例
            messenger: Messenger 实例
            config_operator: ConfigOperator 实例
            rollback_manager: RollbackManager 实例
        """
        self.config = config
        self.db_manager = db_manager
        self.notification_client = notification_client
        self.llm_client = llm_client
        self.db_handler = db_handler
        self.cost_tracker = cost_tracker
        self.messenger = messenger
        self.config_operator = config_operator
        self.rollback_manager = rollback_manager

        self.response_parser = ResponseParser()
        self.diff_generator = DiffGenerator()
        self.context_builder = ContextBuilder(
            context_window_size=config.get("memory", {}).get("context_window_size", 3)
        )

        # 项目根目录
        self.project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    async def run_weekly_tuning(
        self,
        strategy_ids: list = None,
        force: bool = False,
    ) -> None:
        """
        执行周度调优主流程

        遍历所有已注册策略，对每个策略执行完整的 AI 调优流水线。
        单个策略失败不影响其他策略的执行。

        Args:
            strategy_ids: 可选，指定需要调优的策略ID列表；为 None 时遍历所有已注册策略
            force: 是否强制调优（跳过"本周已调优"检查）
        """
        strategies = self.config.get("strategies", [])
        if not strategies:
            logger.warning("没有注册任何策略，跳过调优")
            return

        # 按 strategy_ids 过滤
        if strategy_ids:
            strategy_ids_set = set(strategy_ids)
            strategies = [s for s in strategies if s.get("strategy_id") in strategy_ids_set]
            if not strategies:
                logger.warning("过滤后无匹配策略，跳过调优", strategy_ids=strategy_ids)
                return

        logger.info("开始周度调优", strategy_count=len(strategies), force=force)

        total_success = 0
        total_skip = 0
        total_error = 0

        for strategy_cfg in strategies:
            strategy_id = strategy_cfg.get("strategy_id", "")
            strategy_name = strategy_cfg.get("name", "")

            try:
                result = await self._tune_single_strategy(strategy_cfg, force=force)
                if result == "success":
                    total_success += 1
                elif result == "skip":
                    total_skip += 1
                else:
                    total_error += 1
            except Exception as e:
                total_error += 1
                logger.error(
                    "策略调优异常",
                    strategy_id=strategy_id,
                    strategy_name=strategy_name,
                    error=str(e),
                )
                await self.messenger.send_error_notification(
                    strategy_name=strategy_name,
                    strategy_id=strategy_id,
                    error_message=str(e),
                )

        # 输出成本汇总
        cost_summary = self.cost_tracker.get_summary()
        logger.info(
            "周度调优完成",
            total_strategies=len(strategies),
            success=total_success,
            skip=total_skip,
            error=total_error,
            total_cost_usd=cost_summary.get("total_cost_usd", 0),
        )

    async def _tune_single_strategy(
        self, strategy_cfg: Dict[str, Any], force: bool = False
    ) -> str:
        """
        对单个策略执行完整的调优流水线

        Args:
            strategy_cfg: 策略配置字典
            force: 是否强制调优（跳过"本周已调优"检查）

        Returns:
            "success" / "skip" / "error"
        """
        strategy_id = strategy_cfg["strategy_id"]
        strategy_name = strategy_cfg["name"]
        adapter_class_path = strategy_cfg["adapter_class"]
        config_path = strategy_cfg["config_path"]

        logger.info("开始调优策略", strategy_id=strategy_id, strategy_name=strategy_name)

        # 步骤1：动态导入并实例化适配器
        adapter = self._load_adapter(adapter_class_path, strategy_cfg)
        if not adapter:
            return "error"

        # 步骤2：采集数据
        report = await adapter.collect()
        if report.performance.total_trades == 0:
            logger.info("策略本周无交易，跳过调优", strategy_id=strategy_id)
            return "skip"

        # 步骤3：构建历史上下文
        report_dict = report.model_dump()
        context = await self.context_builder.build_context(
            strategy_id=strategy_id,
            db_handler=self.db_handler,
            current_report=report_dict,
        )

        # 步骤4：加载并渲染 Prompt 模板
        system_prompt, user_prompt = self._build_prompts(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            adapter=adapter,
            report_dict=report_dict,
            context=context,
        )

        if not system_prompt or not user_prompt:
            logger.error("Prompt 构建失败", strategy_id=strategy_id)
            return "error"

        # 步骤5：调用 LLM
        raw_response = await self.llm_client.call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

        if not raw_response:
            logger.error("LLM 响应为空", strategy_id=strategy_id)
            await self.messenger.send_error_notification(
                strategy_name=strategy_name,
                strategy_id=strategy_id,
                error_message="DeepSeek API 返回空响应",
            )
            return "error"

        # 步骤6：解析 JSON 响应
        parsed = self.response_parser.parse_response(raw_response)
        if "error" in parsed:
            logger.error("AI响应解析失败", strategy_id=strategy_id, error=parsed["error"])
            await self.messenger.send_error_notification(
                strategy_name=strategy_name,
                strategy_id=strategy_id,
                error_message=f"AI 响应解析失败: {parsed['error']}",
            )
            return "error"

        # 步骤7：校验参数
        adjustments = parsed.get("adjustments", {})
        validation = self.response_parser.validate_adjustments(adjustments, adapter)

        if validation["errors"]:
            logger.warning("参数校验有问题，使用校正后的值", errors=validation["errors"])
            adjustments = validation["validated"]

        # 步骤8：保存记忆到数据库
        summary = parsed.get("summary", "")
        ai_suggestions = {
            "reasons": parsed.get("reasons", ""),
            "summary": summary,
            "adjustments": adjustments,
            "expected_impact": parsed.get("expected_impact", ""),
            "confidence": parsed.get("confidence", 0),
        }

        memory_id = await self.db_handler.save_memory(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            report=report_dict,
            ai_suggestions=ai_suggestions,
            summary=summary,
        )

        # 步骤9：如果 AI 建议"维持不变"，跳过推送
        if not adjustments:
            logger.info("AI建议维持不变，记录到记忆库但不推送审批", strategy_id=strategy_id)
            return "skip"

        # 步骤10：生成变更清单
        current_params = adapter.get_current_params()
        diff_text = self.diff_generator.generate_diff(
            strategy_name=strategy_name,
            adjustments=adjustments,
            current_params=current_params,
        )

        # 步骤11：推送飞书审批
        await self.messenger.send_tuning_card(
            strategy_name=strategy_name,
            strategy_id=strategy_id,
            diff_text=diff_text,
            ai_reasons=parsed.get("reasons", ""),
            expected_impact=parsed.get("expected_impact", ""),
            memory_id=memory_id,
        )

        logger.info("策略调优完成", strategy_id=strategy_id, memory_id=memory_id)
        return "success"

    def _load_adapter(self, adapter_class_path: str, strategy_cfg: Dict[str, Any]):
        """
        动态加载适配器类

        仅允许加载 ai_tuner.adapters.* 命名空间下的模块，防止任意代码执行。

        Args:
            adapter_class_path: 适配器类路径，如 "ai_tuner.adapters.mtpcs_adapter.MTPCSAdapter"
            strategy_cfg: 策略配置

        Returns:
            适配器实例，加载失败返回 None
        """
        try:
            module_path, class_name = adapter_class_path.rsplit(".", 1)

            # 安全校验：只允许 ai_tuner.adapters.* 命名空间下的模块
            if not module_path.startswith(_IMPORT_WHITELIST_PREFIX):
                logger.error(
                    "适配器模块路径不在白名单中，拒绝加载",
                    adapter_class_path=adapter_class_path,
                    module_path=module_path,
                    whitelist_prefix=_IMPORT_WHITELIST_PREFIX,
                )
                return None

            module = importlib.import_module(module_path)
            adapter_class = getattr(module, class_name)
            adapter = adapter_class(self.db_manager)

            logger.info(
                "适配器加载成功",
                strategy_id=strategy_cfg.get("strategy_id"),
                adapter_class=class_name,
            )
            return adapter
        except Exception as e:
            logger.error(
                "适配器加载失败",
                adapter_class_path=adapter_class_path,
                error=str(e),
            )
            return None

    def _build_prompts(
        self,
        strategy_id: str,
        strategy_name: str,
        adapter,
        report_dict: Dict[str, Any],
        context: str,
    ) -> tuple:
        """
        加载 Prompt 模板并渲染

        Args:
            strategy_id: 策略唯一标识
            strategy_name: 策略显示名称
            adapter: 适配器实例
            report_dict: 策略报告字典
            context: 历史上下文文本

        Returns:
            (system_prompt, user_prompt) 元组
        """
        prompts_dir = os.path.join(self.project_root, "prompts")

        try:
            # 加载通用规则
            common_rules = self._load_template(
                os.path.join(prompts_dir, "common_rules.txt")
            )
            # 加载策略特定系统提示词
            strategy_system = self._load_template(
                os.path.join(prompts_dir, f"{strategy_id}_system.txt")
            )
            # 加载策略特定用户提示词模板
            strategy_user_template = self._load_template(
                os.path.join(prompts_dir, f"{strategy_id}_user.txt")
            )

            # 组装系统提示词
            system_prompt = f"{common_rules}\n\n{strategy_system}"

            # 渲染用户提示词
            current_params = adapter.get_current_params()
            import json

            user_prompt = Template(strategy_user_template).render(
                strategy_name=strategy_name,
                current_params=json.dumps(current_params, ensure_ascii=False, indent=2),
                report=json.dumps(report_dict, ensure_ascii=False, indent=2, default=str),
                memory_history=context,
            )

            return system_prompt, user_prompt

        except Exception as e:
            logger.error(
                "Prompt 模板加载/渲染失败",
                strategy_id=strategy_id,
                error=str(e),
            )
            return "", ""

    @staticmethod
    def _load_template(file_path: str) -> str:
        """
        加载 Prompt 模板文件

        Args:
            file_path: 模板文件路径

        Returns:
            模板内容字符串
        """
        if not os.path.exists(file_path):
            logger.warning("Prompt模板文件不存在", file_path=file_path)
            return ""

        with open(file_path, "r", encoding="utf-8") as f:
            return f.read().strip()