"""
StratTuneAI 多策略AI调优系统入口

启动流程：
1. 加载 config.yaml 配置
2. 初始化数据库连接
3. 初始化飞书通知客户端
4. 创建 strategy_memory 表
5. 初始化 LLM 客户端
6. 初始化 Token 用量跟踪器
7. 初始化配置管理模块
8. 初始化消息发送器
9. 初始化周度调优任务
9.5. 初始化月度分配任务
10. 初始化 APScheduler 调度器（周度调优 + 月度分配）
11. 初始化 HTTP 服务器
12. 启动时补偿检查（上次调优距今超过7天则立即执行）
13. 启动调度器运行
14. 优雅关闭：捕获 SIGTERM/SIGINT，关闭调度器和数据库连接
"""

import asyncio
import calendar
import os
import signal
import sys
from datetime import datetime

import structlog
import yaml
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 确保项目根目录在 sys.path 中，以便导入 shared 模块
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from shared.database import DatabaseManager  # noqa: E402
from shared.notification import NotificationClient  # noqa: E402

from ai_tuner.deploy.config_operator import ConfigOperator  # noqa: E402
from ai_tuner.deploy.rollback_manager import RollbackManager  # noqa: E402
from ai_tuner.engine.cost_tracker import CostTracker  # noqa: E402
from ai_tuner.engine.llm_client import LLMClient  # noqa: E402
from ai_tuner.memory.db_handler import MemoryDBHandler  # noqa: E402
from ai_tuner.notifier.messenger import Messenger  # noqa: E402
from ai_tuner.scheduler.weekly_job import WeeklyTuningJob  # noqa: E402
from ai_tuner.allocation.monthly_job import MonthlyAllocationJob  # noqa: E402
from ai_tuner.allocation.profit_extraction_job import ProfitExtractionJob  # noqa: E402
from ai_tuner.cleanup.orphan_cleanup import OrphanCleanupJob  # noqa: E402
from ai_tuner.monitor.daily_health_check import DailyHealthCheck  # noqa: E402
from shared.binance_api import BinanceClient  # noqa: E402

# 配置 structlog
import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


class StratTuneAI:
    """
    StratTuneAI 多策略AI调优系统

    负责协调所有模块，管理系统的生命周期。
    """

    def __init__(self):
        """初始化系统"""
        self.config: dict = {}
        self.db_manager: DatabaseManager = None
        self.notification_client: NotificationClient = None
        self.scheduler: AsyncIOScheduler = None
        self.llm_client: LLMClient = None
        self.cost_tracker: CostTracker = None
        self.db_handler: MemoryDBHandler = None
        self.weekly_job: WeeklyTuningJob = None
        self.monthly_job: MonthlyAllocationJob = None
        self.profit_extraction_job: ProfitExtractionJob = None
        self.orphan_cleanup_job: OrphanCleanupJob = None
        self.health_checker: DailyHealthCheck = None
        self._running = False
        self.app: web.Application = None
        self.runner: web.AppRunner = None

    def load_config(self) -> dict:
        """
        加载系统配置文件

        从 ai_tuner/config.yaml 读取配置，并解析环境变量占位符。

        Returns:
            配置字典
        """
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            raw_config = yaml.safe_load(f) or {}

        # 解析环境变量占位符
        config = self._resolve_env_vars(raw_config)

        logger.info("系统配置加载完成", config_path=config_path)
        return config

    def _resolve_env_vars(self, obj):
        """
        递归解析配置中的环境变量占位符 ${VAR_NAME} 或 ${VAR_NAME:default}

        Args:
            obj: 配置对象（字典、列表、字符串等）

        Returns:
            解析后的配置对象
        """
        import re

        if isinstance(obj, dict):
            return {k: self._resolve_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._resolve_env_vars(item) for item in obj]
        elif isinstance(obj, str):
            def replace_env(match):
                var_expr = match.group(1)
                if ":" in var_expr:
                    var_name, default = var_expr.split(":", 1)
                    return os.getenv(var_name, default)
                return os.getenv(var_expr, "")
            return re.sub(r"\$\{([^}]+)\}", replace_env, obj)
        else:
            return obj

    async def initialize(self) -> None:
        """
        初始化所有子系统

        按顺序初始化：
        1. 数据库连接
        2. 通知客户端
        3. 记忆库表
        4. LLM 客户端
        5. 各业务模块
        6. 调度器
        """
        logger.info("开始初始化 StratTuneAI 系统")

        # 1. 加载配置
        self.config = self.load_config()

        # 2. 初始化数据库
        db_cfg = self.config.get("database", {})
        self.db_manager = DatabaseManager(
            host=db_cfg.get("host", "localhost"),
            port=int(db_cfg.get("port", 5432)),
            database=db_cfg.get("database", ""),
            user=db_cfg.get("user", ""),
            password=db_cfg.get("password", ""),
        )
        await self.db_manager.connect()
        logger.info("数据库连接已建立")

        # 3. 初始化通知客户端
        feishu_webhook = os.getenv(
            self.config.get("approval", {}).get("feishu_webhook_env", "FEISHU_WEBHOOK_TUNER"),
            ""
        )
        self.notification_client = NotificationClient(
            service_url=os.getenv("NOTIFICATION_SERVICE_URL", ""),
            use_direct_webhook=True,
        )
        # 通过公开方法注册 tuner 项目的 webhook
        if feishu_webhook and not self.notification_client.has_webhook("tuner"):
            self.notification_client.register_webhook("tuner", feishu_webhook)

        logger.info("通知客户端初始化完成")

        # 4. 创建记忆库表
        schema = self.config.get("database", {}).get("schema", "trading")
        self.db_handler = MemoryDBHandler(self.db_manager)
        await self.db_handler.ensure_table_exists(schema)
        logger.info("记忆库表就绪")

        # 5. 初始化 LLM 客户端
        self.llm_client = LLMClient(self.config.get("deepseek", {}))

        # 6. 初始化 Token 用量跟踪器
        pricing = self.config.get("deepseek", {}).get("pricing", {})
        self.cost_tracker = CostTracker(
            input_price_per_m=float(pricing.get("input_per_million", 1.74)),
            output_price_per_m=float(pricing.get("output_per_million", 3.48)),
            input_cache_hit_price=float(pricing.get("input_cache_hit", 0.174)),
        )
        self.llm_client.set_usage_callback(self._on_llm_usage)

        # 7. 初始化配置管理模块
        self.rollback_manager = RollbackManager(
            max_backups=self.config.get("rollback", {}).get("max_backups", 10)
        )
        self.config_operator = ConfigOperator(
            rollback_manager=self.rollback_manager
        )

        # 8. 初始化消息发送器
        self.messenger = Messenger(self.notification_client)

        # 8.5. 初始化每日健康检查
        self.health_checker = DailyHealthCheck(
            config=self.config,
            db_manager=self.db_manager,
            messenger=self.messenger,
        )
        logger.info("每日健康检查已初始化")

        # 9. 初始化周度调优任务
        self.weekly_job = WeeklyTuningJob(
            config=self.config,
            db_manager=self.db_manager,
            notification_client=self.notification_client,
            llm_client=self.llm_client,
            db_handler=self.db_handler,
            cost_tracker=self.cost_tracker,
            messenger=self.messenger,
            config_operator=self.config_operator,
            rollback_manager=self.rollback_manager,
        )

        # 9.5. 初始化月度分配任务
        self.monthly_job = MonthlyAllocationJob(
            config=self.config,
            db_manager=self.db_manager,
            notification_client=self.notification_client,
            messenger=self.messenger,
            config_operator=self.config_operator,
            rollback_manager=self.rollback_manager,
        )

        # 9.6. 初始化利润提取任务
        binance_api_key = os.getenv("BINANCE_API_KEY", "")
        binance_api_secret = os.getenv("BINANCE_API_SECRET", "")
        binance_testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
        binance_client = None
        if binance_api_key and binance_api_secret:
            binance_client = BinanceClient(
                api_key=binance_api_key,
                api_secret=binance_api_secret,
                testnet=binance_testnet,
                use_unified_account=True,
            )
            self.profit_extraction_job = ProfitExtractionJob(
                db_manager=self.db_manager,
                notification_client=self.notification_client,
                binance=binance_client,
                config=self.config,
            )
            logger.info("利润提取任务已初始化")
        else:
            logger.warning("BINANCE_API_KEY 未配置，利润提取任务将跳过")

        # 9.7. 初始化孤儿条件单清理任务（阶段二）
        cleanup_cfg = self.config.get("orphan_cleanup", {})
        if cleanup_cfg.get("enabled", True):
            if binance_client:
                self.orphan_cleanup_job = OrphanCleanupJob(
                    db=self.db_manager,
                    binance_client=binance_client,
                    notification_client=self.notification_client,
                    stale_hours_threshold=float(cleanup_cfg.get("stale_hours_threshold", 2.0)),
                )
                logger.info("孤儿条件单清理任务（阶段二）已初始化，支持自动取消条件单")
            else:
                logger.warning("BINANCE_API_KEY 未配置，孤儿条件单清理任务将跳过（无法取消订单）")
        else:
            logger.info("孤儿条件单清理任务已禁用")

        # 10. 初始化调度器
        scheduler_cfg = self.config.get("scheduler", {})
        self.scheduler = AsyncIOScheduler(
            timezone=scheduler_cfg.get("timezone", "Asia/Shanghai")
        )
        cron_expr = scheduler_cfg.get("cron_expression", "55 23 * * 0")
        # 解析并校验 cron 表达式
        try:
            from apscheduler.triggers.cron.fields import BaseField

            parts = cron_expr.strip().split()
            if len(parts) != 5:
                raise ValueError(
                    f"cron 表达式格式错误，需要5个字段（分 时 日 月 周），"
                    f"当前为 {len(parts)} 个: {cron_expr!r}"
                )
            # 通过 APScheduler 内置校验验证每个字段的合法性
            for i, (value, field_name) in enumerate(zip(
                parts, ["minute", "hour", "day", "month", "day_of_week"]
            )):
                BaseField(field_name, value)
        except Exception as e:
            raise ValueError(
                f"cron 表达式解析失败: {cron_expr!r}，错误: {e}"
            ) from e

        self.scheduler.add_job(
            self._scheduled_tuning,
            "cron",
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
            id="weekly_tuning",
            name="周度AI调优",
            replace_existing=True,
        )

        # 月度分配调度（每天执行，代码内判断是否为月末最后一天）
        monthly_cron = scheduler_cfg.get("monthly_cron_expression", "55 23 * * *")
        try:
            monthly_parts = monthly_cron.strip().split()
            if len(monthly_parts) != 5:
                raise ValueError(
                    f"月度 cron 表达式格式错误，需要5个字段（分 时 日 月 周），"
                    f"当前为 {len(monthly_parts)} 个: {monthly_cron!r}"
                )
            for i, (value, field_name) in enumerate(zip(
                monthly_parts, ["minute", "hour", "day", "month", "day_of_week"]
            )):
                BaseField(field_name, value)
        except Exception as e:
            raise ValueError(
                f"月度 cron 表达式解析失败: {monthly_cron!r}，错误: {e}"
            ) from e

        self.scheduler.add_job(
            self._scheduled_allocation,
            "cron",
            minute=monthly_parts[0],
            hour=monthly_parts[1],
            day=monthly_parts[2],
            month=monthly_parts[3],
            day_of_week=monthly_parts[4],
            id="monthly_allocation",
            name="月度资金分配",
            replace_existing=True,
        )

        logger.info("调度器初始化完成", cron_expression=cron_expr, monthly_cron_expression=monthly_cron)

        # 利润提取调度（每天 07:35 CST）
        if self.profit_extraction_job:
            self.scheduler.add_job(
                self._scheduled_profit_extraction,
                trigger=self.profit_extraction_job.get_cron_trigger(),
                id="profit_extraction",
                name="利润提取提醒",
                replace_existing=True,
            )
            logger.info("利润提取提醒任务已注册")

        # 孤儿条件单清理调度（每30分钟执行一次）
        if self.orphan_cleanup_job:
            cleanup_interval = cleanup_cfg.get("interval_minutes", 30)
            self.scheduler.add_job(
                self._scheduled_orphan_cleanup,
                "interval",
                minutes=cleanup_interval,
                id="orphan_cleanup",
                name="孤儿条件单清理检查",
                replace_existing=True,
                coalesce=True,
                misfire_grace_time=60,
            )
            logger.info("孤儿条件单清理任务已注册", interval_minutes=cleanup_interval)

        # 10.5. 注册每日健康检查
        health_check_cron = scheduler_cfg.get("health_check_cron", "0 10 * * *")
        try:
            health_parts = health_check_cron.strip().split()
            if len(health_parts) != 5:
                raise ValueError(
                    f"健康检查 cron 表达式格式错误，需要5个字段（分 时 日 月 周），"
                    f"当前为 {len(health_parts)} 个: {health_check_cron!r}"
                )
            for i, (value, field_name) in enumerate(zip(
                health_parts, ["minute", "hour", "day", "month", "day_of_week"]
            )):
                BaseField(field_name, value)
        except Exception as e:
            raise ValueError(
                f"健康检查 cron 表达式解析失败: {health_check_cron!r}，错误: {e}"
            ) from e

        self.scheduler.add_job(
            self._scheduled_health_check,
            "cron",
            minute=health_parts[0],
            hour=health_parts[1],
            day=health_parts[2],
            month=health_parts[3],
            day_of_week=health_parts[4],
            id="daily_health_check",
            name="每日健康检查",
            replace_existing=True,
        )
        logger.info("每日健康检查任务已注册", cron_expression=health_check_cron)

        # 11. 初始化 HTTP 服务器（飞书审批回调 + 管理 API）
        self._setup_http_server()

        # 12. 启动时补偿检查
        await self._check_catch_up()

        logger.info("StratTuneAI 系统初始化完成")

    async def start(self) -> None:
        """启动调度器和 HTTP 服务器"""
        # 启动 HTTP 服务器
        if self.runner:
            port = int(os.getenv("AI_TUNER_PORT", "8777"))
            await self.runner.setup()
            site = web.TCPSite(self.runner, "0.0.0.0", port)
            await site.start()
            logger.info("HTTP 服务器已启动", port=port)

        if self.scheduler:
            self.scheduler.start()
            self._running = True
            logger.info("调度器已启动")

            # 保持运行
            try:
                while self._running:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass

    async def shutdown(self) -> None:
        """优雅关闭系统"""
        logger.info("开始优雅关闭 StratTuneAI 系统")
        self._running = False

        # 关闭 HTTP 服务器
        if self.runner:
            await self.runner.cleanup()
            logger.info("HTTP 服务器已关闭")

        # 关闭调度器
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
            logger.info("调度器已关闭")

        # 关闭通知客户端
        if self.notification_client:
            await self.notification_client.close()

        # 关闭数据库连接
        if self.db_manager:
            await self.db_manager.disconnect()
            logger.info("数据库连接已关闭")

        logger.info("StratTuneAI 系统已关闭")

    async def _scheduled_tuning(self) -> None:
        """调度器触发的周度调优"""
        logger.info("定时调优触发")
        await self.weekly_job.run_weekly_tuning()

    async def _scheduled_allocation(self) -> None:
        """调度器触发的月度资金分配（每天执行，仅月末最后一天真正执行）"""
        now = datetime.now()
        last_day = calendar.monthrange(now.year, now.month)[1]
        if now.day != last_day:
            logger.debug("非月末最后一天，跳过月度资金分配", day=now.day, last_day=last_day)
            return

        logger.info("月末最后一天，开始月度资金分配")
        if self.monthly_job:
            await self.monthly_job.run_monthly_allocation()

    async def _scheduled_profit_extraction(self) -> None:
        """调度器触发的利润提取检查（每天 07:35 CST）"""
        if self.profit_extraction_job:
            result = await self.profit_extraction_job.run_daily_check()
            logger.info("利润提取检查完成", result=result)

    async def _scheduled_orphan_cleanup(self) -> None:
        """调度器触发的孤儿条件单清理检查（每30分钟执行一次）"""
        if self.orphan_cleanup_job:
            logger.info("触发孤儿条件单清理检查")
            await self.orphan_cleanup_job.execute()

    async def _scheduled_health_check(self) -> None:
        """调度器触发的每日健康检查（每天 10:00 CST）"""
        if self.health_checker:
            logger.info("触发每日健康检查")
            await self.health_checker.run_check()

    async def _check_catch_up(self) -> None:
        """
        启动时补偿检查

        如果上次调优距今超过 7 天，立即执行一次补偿调优。
        """
        try:
            # 查询最近一次调优时间
            schema = self.config.get("database", {}).get("schema", "trading")
            query = f"""
                SELECT MAX(created_at) as last_tuning
                FROM {schema}.strategy_memory
            """
            result = await self.db_manager.fetch_one(query)
            last_tuning = result.get("last_tuning") if result else None

            if last_tuning and isinstance(last_tuning, datetime):
                catch_up_days = self.config.get("scheduler", {}).get("catch_up_days", 7)
                days_since = (datetime.now() - last_tuning).days
                if days_since >= catch_up_days:
                    logger.info(
                        "检测到上次调优距今超过阈值，执行补偿调优",
                        last_tuning=last_tuning.isoformat(),
                        days_since=days_since,
                        catch_up_days=catch_up_days,
                    )
                    await self.weekly_job.run_weekly_tuning()
                else:
                    logger.info(
                        "上次调优在阈值内，无需补偿",
                        last_tuning=last_tuning.isoformat(),
                        days_since=days_since,
                        catch_up_days=catch_up_days,
                    )
            else:
                logger.info("未发现历史调优记录，执行首次调优")
                await self.weekly_job.run_weekly_tuning()

        except Exception as e:
            logger.error("补偿检查异常", error=str(e))

    def _on_llm_usage(self, model: str, prompt_tokens: int, completion_tokens: int, total_tokens: int) -> None:
        """
        LLM Token 用量回调

        Args:
            model: 模型名称
            prompt_tokens: 输入 Token 数
            completion_tokens: 输出 Token 数
            total_tokens: 总 Token 数
        """
        self.cost_tracker.record_usage(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    # ================================================================
    # HTTP API 端点
    # ================================================================

    def _setup_http_server(self) -> None:
        """初始化 HTTP 服务器，注册路由"""
        self.app = web.Application()
        self.app.router.add_get("/api/v1/health", self._handle_health)
        self.app.router.add_post("/api/v1/approval", self._handle_approval)
        self.app.router.add_post("/api/v1/trigger", self._handle_trigger)
        self.app.router.add_post("/api/v1/rollback", self._handle_rollback)
        self.runner = web.AppRunner(self.app)
        logger.info("HTTP 路由注册完成")

    async def _handle_health(self, request: web.Request) -> web.Response:
        """健康检查接口"""
        enabled_strategies = [
            s["strategy_id"] for s in self.config.get("strategies", [])
            if s.get("enabled", True)
        ]
        return web.json_response({
            "status": "healthy",
            "scheduler": "running" if self._running else "stopped",
            "strategies": enabled_strategies,
        })

    def _find_strategy_cfg(self, strategy_id: str) -> dict:
        """
        根据 strategy_id 查找策略配置

        Args:
            strategy_id: 策略唯一标识

        Returns:
            策略配置字典，未找到返回 None
        """
        for s in self.config.get("strategies", []):
            if s["strategy_id"] == strategy_id:
                return s
        return None

    async def _find_pending_approval(self, strategy_id: str, date: str) -> dict:
        """
        查找指定策略和日期的待审批记忆记录

        Args:
            strategy_id: 策略唯一标识
            date: 日期字符串，格式 "YYYY-MM-DD"

        Returns:
            匹配的记忆记录，未找到返回 None
        """
        pending = await self.db_handler.get_pending_approvals()
        for p in pending:
            if p["strategy_id"] == strategy_id and p.get("created_at"):
                mem_date = p["created_at"].strftime("%Y-%m-%d") if isinstance(p["created_at"], datetime) else ""
                if mem_date == date:
                    return p
        return None

    @staticmethod
    def _flatten_dict(d: dict, prefix: str = "") -> dict:
        """将嵌套字典展平为点号分隔路径的扁平字典

        Args:
            d: 嵌套字典
            prefix: 当前路径前缀

        Returns:
            展平后的字典，如 {"scoring.min_score": 75}
        """
        result = {}
        for key, value in d.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                result.update(StratTuneAI._flatten_dict(value, path))
            else:
                result[path] = value
        return result

    async def _handle_approval(self, request: web.Request) -> web.Response:
        """飞书审批回调接口

        POST /api/v1/approval
        Body: {"strategy_id": "btc_eth", "date": "2026-06-21", "action": "confirm|reject"}
        """
        try:
            data = await request.json()
            strategy_id = data.get("strategy_id", "")
            date = data.get("date", "")
            action = data.get("action", "")

            if action not in ("confirm", "reject"):
                return web.json_response(
                    {"status": "error", "message": "action 必须为 confirm 或 reject"},
                    status=400,
                )

            # 查找策略配置（公共方法）
            strategy_cfg = self._find_strategy_cfg(strategy_id)
            if not strategy_cfg:
                return web.json_response(
                    {"status": "error", "message": f"未找到策略: {strategy_id}"},
                    status=404,
                )
            strategy_name = strategy_cfg.get("name", strategy_id)

            if action == "confirm":
                # 查找待审批的记忆记录（公共方法）
                matched = await self._find_pending_approval(strategy_id, date)
                if not matched:
                    return web.json_response(
                        {"status": "error", "message": f"未找到 {strategy_id} {date} 的待审批记录"},
                        status=404,
                    )

                # 应用变更
                adjustments = matched.get("ai_suggestions", {}).get("adjustments", {})
                config_path = strategy_cfg["config_path"]
                from ai_tuner.deploy.diff_generator import DiffGenerator
                diff_gen = DiffGenerator()
                # 读取当前配置值，用于 diff 中显示旧值
                current_params = {}
                try:
                    import yaml
                    with open(config_path, "r") as f:
                        strategy_config = yaml.safe_load(f)
                    # 展平 YAML 为嵌套点号路径
                    current_params = self._flatten_dict(strategy_config)
                except Exception as e:
                    logger.warning("读取当前配置失败，diff 中旧值将显示为 ?",
                                   config_path=config_path, error=str(e))
                diff_text = diff_gen.generate_diff(strategy_name, adjustments, current_params)

                # 先应用配置变更到覆盖层，成功后再标记数据库状态（保证一致性）
                # 使用 apply_overrides 写入 tuning_overrides 目录，不修改 config.yaml
                success = await self.config_operator.apply_overrides(config_path, adjustments)
                if not success:
                    return web.json_response({
                        "status": "error",
                        "message": "配置变更应用失败，请查看服务端日志",
                    }, status=500)

                await self.db_handler.mark_applied(matched["id"], approved_by="feishu_callback")
                await self.messenger.send_applied_notification(strategy_name, strategy_id, diff_text)

                logger.info("审批通过并应用", strategy_id=strategy_id, date=date)
                return web.json_response({
                    "status": "ok",
                    "message": f"已确认应用 {strategy_id} 的调优建议",
                })

            elif action == "reject":
                # 查找所有匹配的待审批记忆记录（不再只处理第一条）
                pending = await self.db_handler.get_pending_approvals()
                rejected_count = 0
                for p in pending:
                    if p["strategy_id"] == strategy_id:
                        # 如果指定了日期，只拒绝匹配日期的记录
                        if date and p.get("created_at"):
                            mem_date = p["created_at"].strftime("%Y-%m-%d") if isinstance(p["created_at"], datetime) else ""
                            if mem_date != date:
                                continue
                        await self.db_handler.mark_rejected(p["id"])
                        rejected_count += 1

                if rejected_count == 0:
                    return web.json_response(
                        {"status": "error", "message": f"未找到 {strategy_id} {date} 的待审批记录"},
                        status=404,
                    )

                await self.messenger.send_rejected_notification(strategy_name, strategy_id)
                logger.info(
                    "审批拒绝",
                    strategy_id=strategy_id,
                    date=date,
                    rejected_count=rejected_count,
                )
                return web.json_response({
                    "status": "ok",
                    "message": f"已拒绝 {strategy_id} 的调优建议",
                })

        except Exception as e:
            logger.error("审批回调处理异常", error=str(e), exc_info=True)
            return web.json_response(
                {"status": "error", "message": "审批处理失败，请查看服务端日志"},
                status=500,
            )

    async def _handle_trigger(self, request: web.Request) -> web.Response:
        """手动触发调优接口

        POST /api/v1/trigger
        Body: {"strategy_ids": ["btc_eth"], "force": false}

        支持按 strategy_ids 过滤策略，force=true 时跳过"本周已调优"检查。
        """
        try:
            data = await request.json()
            strategy_ids = data.get("strategy_ids", None)
            force = data.get("force", False)

            # 校验 strategy_ids 合法性
            if strategy_ids is not None:
                if not isinstance(strategy_ids, list):
                    return web.json_response(
                        {"status": "error", "message": "strategy_ids 必须是数组"},
                        status=400,
                    )
                valid_ids = {s["strategy_id"] for s in self.config.get("strategies", [])}
                invalid_ids = [sid for sid in strategy_ids if sid not in valid_ids]
                if invalid_ids:
                    return web.json_response(
                        {"status": "error", "message": f"无效的策略ID: {invalid_ids}"},
                        status=400,
                    )

            await self.weekly_job.run_weekly_tuning(
                strategy_ids=strategy_ids,
                force=force,
            )
            scope = f"策略 {strategy_ids}" if strategy_ids else "所有策略"
            return web.json_response({
                "status": "accepted",
                "message": f"调优任务已触发（{scope}, force={force}）",
            })
        except Exception as e:
            logger.error("手动触发调优异常", error=str(e), exc_info=True)
            return web.json_response(
                {"status": "error", "message": "调优任务执行失败，请查看服务端日志"},
                status=500,
            )

    async def _handle_rollback(self, request: web.Request) -> web.Response:
        """手动回滚接口

        POST /api/v1/rollback
        Body: {"strategy_id": "btc_eth", "backup_file": "config.yaml.backup.xxx"}
        """
        try:
            data = await request.json()
            strategy_id = data.get("strategy_id", "")
            backup_file = data.get("backup_file", "")

            # 查找策略配置路径
            strategy_cfg = None
            for s in self.config.get("strategies", []):
                if s["strategy_id"] == strategy_id:
                    strategy_cfg = s
                    break

            if not strategy_cfg:
                return web.json_response(
                    {"status": "error", "message": f"未找到策略: {strategy_id}"},
                    status=404,
                )

            config_path = strategy_cfg["config_path"]
            if backup_file:
                # 防止路径穿越攻击：仅保留文件名部分
                safe_backup_file = os.path.basename(backup_file)
                backup_path = os.path.join(os.path.dirname(config_path), safe_backup_file)
                logger.info(
                    "回滚路径已构建",
                    original=backup_file,
                    safe=safe_backup_file,
                    backup_path=backup_path,
                )
            else:
                # 使用最新备份
                backups = self.rollback_manager.list_backups(config_path)
                if not backups:
                    return web.json_response(
                        {"status": "error", "message": f"未找到 {strategy_id} 的备份文件"},
                        status=404,
                    )
                backup_path = backups[-1]

            success = self.rollback_manager.rollback(config_path, backup_path)
            if success:
                strategy_name = strategy_cfg.get("name", strategy_id)
                await self.messenger.send_rollback_notification(
                    strategy_name, strategy_id, f"手动回滚到 {backup_path}"
                )
                return web.json_response({
                    "status": "ok",
                    "message": f"已回滚 {strategy_id} 配置到 {backup_path}",
                })
            else:
                return web.json_response(
                    {"status": "error", "message": "回滚失败"},
                    status=500,
                )

        except Exception as e:
            logger.error("手动回滚异常", error=str(e), exc_info=True)
            return web.json_response(
                {"status": "error", "message": "回滚操作失败，请查看服务端日志"},
                status=500,
            )


async def main():
    """主函数"""
    app = StratTuneAI()

    # 注册信号处理
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.create_task(app.shutdown())
        )

    try:
        await app.initialize()
        await app.start()
    except Exception as e:
        logger.error("系统运行异常", error=str(e))
        await app.shutdown()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())