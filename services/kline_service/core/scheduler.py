"""定时任务调度器"""

import asyncio
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from shared.utils.logger import get_logger
from .collector import KlineCollector
from .registry import registry

logger = get_logger(__name__)


class TaskScheduler:
    """定时任务调度器"""

    def __init__(self, collector: KlineCollector):
        """
        初始化调度器

        Args:
            collector: K 线采集器
        """
        self.collector = collector
        self.scheduler = AsyncIOScheduler()
        self.tasks = {}

    def add_job(
        self,
        symbol: str,
        interval: str,
        cron_expression: Optional[str] = None,
        minutes: Optional[int] = None,  # 改为可选，自动根据周期计算
    ):
        """
        添加定时采集任务

        Args:
            symbol: 交易对
            interval: 时间间隔
            cron_expression: Cron 表达式（可选）
            minutes: 采集最近 N 分钟的数据（可选，默认根据周期自动计算）
        """
        # 根据时间间隔设置采集频率和采集窗口
        if cron_expression is None:
            # 默认策略：在每个周期结束后 1-2 分钟采集
            # ⚠️ 注意：minutes 必须 > 周期长度，否则 start_time 会落在目标蜡烛 open_time 之后，
            # 导致上一个已收盘的 K 线被遗漏。使用 2 倍周期长度确保可靠性。
            if interval == "1m":
                cron_expression = "* * * * *"  # 每分钟
                if minutes is None:
                    minutes = 2
            elif interval == "5m":
                cron_expression = "*/5 * * * *"  # 每 5 分钟
                if minutes is None:
                    minutes = 10
            elif interval == "15m":
                cron_expression = "*/15 * * * *"  # 每 15 分钟
                if minutes is None:
                    minutes = 30
            elif interval == "30m":
                cron_expression = "*/30 * * * *"  # 每 30 分钟
                if minutes is None:
                    minutes = 60
            elif interval == "1h":
                cron_expression = "0 * * * *"  # 每小时第 0 分钟（与策略运行时间对齐）
                if minutes is None:
                    minutes = 120  # 2小时，确保始终覆盖到已收盘的 1h K 线
            elif interval == "4h":
                cron_expression = "0 0,4,8,12,16,20 * * *"  # 每 4 小时（与策略运行时间对齐）
                if minutes is None:
                    minutes = 480  # 8小时（2个周期），确保始终覆盖到已收盘的 4h K 线
            elif interval == "1d":
                cron_expression = "0 0 * * *"  # 每天 0:00（与策略运行时间对齐）
                if minutes is None:
                    minutes = 2880  # 2天，确保始终覆盖到已收盘的日K线
            else:
                cron_expression = "*/15 * * * *"  # 默认 15 分钟
                if minutes is None:
                    minutes = 30
        else:
            # 如果提供了自定义 cron 表达式，使用默认 minutes
            if minutes is None:
                minutes = 5

        # 解析 cron 表达式
        parts = cron_expression.split()
        if len(parts) == 5:
            minute, hour, day, month, day_of_week = parts
            trigger = CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
            )
        else:
            logger.warning(f"无效的 Cron 表达式：{cron_expression}，使用默认 15 分钟")
            trigger = CronTrigger(minute="*/15")

        # 创建任务函数
        async def collect_task():
            try:
                logger.info(f"定时任务：采集 {symbol} {interval}")
                stored = await self.collector.collect_recent(
                    symbol, interval, minutes
                )
                logger.info(f"定时任务完成：存储 {stored} 条数据")
            except Exception as e:
                logger.error(f"定时任务失败：{symbol} {interval} - {e}")

        # 添加任务
        task_id = f"{symbol}_{interval}"
        self.scheduler.add_job(
            collect_task,
            trigger=trigger,
            id=task_id,
            name=f"Collect {symbol} {interval}",
            replace_existing=True,
            misfire_grace_time=60,  # 允许任务延迟 60 秒执行
        )

        self.tasks[task_id] = {
            "symbol": symbol,
            "interval": interval,
            "cron": cron_expression,
            "minutes": minutes,
        }

        logger.info(f"添加定时任务：{task_id} - {cron_expression}")

    def add_jobs_from_config(
        self, config: Dict[str, Dict[str, Optional[str]]]
    ):
        """
        从配置批量添加任务

        Args:
            config: 配置字典
                {
                    "BTCUSDT": {
                        "15m": "*/15 * * * *",
                        "1h": "5 * * * *"
                    },
                    "ETHUSDT": {
                        "15m": "*/15 * * * *",
                        "4h": "5 0,4,8,12,16,20 * * *"
                    }
                }
        """
        for symbol, intervals in config.items():
            for interval, cron in intervals.items():
                self.add_job(symbol, interval, cron_expression=cron)

    def start(self):
        """启动调度器"""
        # 添加定时清理过期配置的任务（每小时执行一次）
        self.scheduler.add_job(
            self._cleanup_expired_symbols,
            trigger='cron',
            minute=0,  # 每小时整点执行
            id='cleanup_expired_symbols',
            name='Cleanup Expired Symbols',
        )
        logger.info("⏰ 已添加清理过期配置任务（每小时执行）")
        
        # 添加定期验证标的有效性的任务（每6小时执行一次）
        self.scheduler.add_job(
            self._validate_registered_symbols,
            trigger='cron',
            hour='0,6,12,18',  # 每天 0:00, 6:00, 12:00, 18:00
            id='validate_registered_symbols',
            name='Validate Registered Symbols',
        )
        logger.info("🔍 已添加定期验证标的有效性任务（每6小时执行）")
        
        # 从注册表加载所有活跃的标的，恢复采集任务（重启后恢复）
        self._load_from_registry()
        
        self.scheduler.start()
        logger.info("✅ 定时任务调度器已启动")
        
        # 触发启动后的首次采集，避免重启后数据为空（策略查询时无数据）
        self._trigger_initial_collection()
    
    def _trigger_initial_collection(self):
        """触发启动后的首次采集（异步执行，不阻塞启动流程）
        
        覆盖所有已注册的任务（包括固定标的和注册表标的），
        避免重启后数据为空（策略查询时无数据）。
        """
        if not self.tasks:
            return
        
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._do_initial_collection(self.tasks))
                logger.info(f"已调度启动后首次采集：{len(self.tasks)} 个任务")
            else:
                logger.warning("事件循环未运行，跳过启动后首次采集")
        except RuntimeError as e:
            logger.warning(f"获取事件循环失败，跳过启动后首次采集：{e}")
    
    async def _do_initial_collection(self, tasks: dict):
        """执行启动后的首次采集，覆盖所有任务
        
        使用每个任务自身的 minutes 配置，确保各周期能获取足量历史数据：
        - 15m: 30分钟 → 约2根
        - 1h: 120分钟 → 约2根
        - 4h: 480分钟 → 约2根
        - 1d: 43200分钟(30天) → 约30根（网格策略需要至少30根日线计算ATR基线）
        """
        # 按 symbol 分组，避免重复采集
        symbol_intervals = {}
        for task_id, task_info in tasks.items():
            symbol = task_info["symbol"]
            interval = task_info["interval"]
            if symbol not in symbol_intervals:
                symbol_intervals[symbol] = set()
            symbol_intervals[symbol].add(interval)
        
        for symbol, intervals in symbol_intervals.items():
            for interval in intervals:
                try:
                    # 查找该任务的 minutes 配置，覆盖所有间隔
                    task_minutes = 1000  # 默认值
                    for task_id, task_info in tasks.items():
                        if task_info["symbol"] == symbol and task_info["interval"] == interval:
                            task_minutes = task_info["minutes"]
                            break
                    # 日线周期使用更大窗口（30天），确保网格策略有足够日线数据计算ATR基线
                    if interval == '1d' and task_minutes < 43200:
                        task_minutes = 43200
                    stored = await self.collector.collect_recent(
                        symbol, interval, minutes=task_minutes
                    )
                    if stored > 0:
                        logger.info(f"启动后首次采集成功：{symbol} {interval}，存储{stored}条")
                except Exception as e:
                    logger.warning(f"启动后首次采集失败：{symbol} {interval} - {e}")
    
    def _load_from_registry(self):
        """从注册表加载所有活跃的标的，添加采集任务"""
        active_symbols = registry.get_active_symbols()
        count = 0
        for config in active_symbols:
            for interval in config.intervals:
                self.add_job(config.symbol, interval)
                count += 1
        logger.info(f"从注册表加载 {len(active_symbols)} 个活跃标的，添加 {count} 个采集任务")
    
    async def _cleanup_expired_symbols(self):
        """清理过期的标的配置"""
        try:
            cleaned = await registry.cleanup_expired()
            if cleaned > 0:
                logger.info(f"🧹 清理了 {cleaned} 个过期的标的配置")
        except Exception as e:
            logger.error(f"清理过期配置失败：{e}")

    async def _validate_registered_symbols(self):
        """验证所有注册的标的在币安上是否有效"""
        try:
            cleaned = await self.collector.validate_registered_symbols()
            if cleaned > 0:
                logger.info(f"🧹 定期验证完成，清理了 {cleaned} 个无效的标的")
        except Exception as e:
            logger.error(f"定期验证标的失败：{e}")

    def shutdown(self, wait: bool = True):
        """
        关闭调度器

        Args:
            wait: 是否等待任务完成
        """
        self.scheduler.shutdown(wait=wait)
        logger.info("🛑 定时任务调度器已关闭")

    def get_tasks(self) -> Dict:
        """获取所有任务"""
        return self.tasks.copy()

    def get_next_run_time(self, task_id: str) -> Optional[datetime]:
        """
        获取任务下次运行时间

        Args:
            task_id: 任务 ID

        Returns:
            下次运行时间
        """
        job = self.scheduler.get_job(task_id)
        if job:
            return job.next_run_time
        return None

    def pause_task(self, task_id: str):
        """暂停任务"""
        self.scheduler.pause_job(task_id)
        logger.info(f"暂停任务：{task_id}")

    def resume_task(self, task_id: str):
        """恢复任务"""
        self.scheduler.resume_job(task_id)
        logger.info(f"恢复任务：{task_id}")

    def remove_task(self, task_id: str):
        """移除任务"""
        self.scheduler.remove_job(task_id)
        if task_id in self.tasks:
            del self.tasks[task_id]
        logger.info(f"移除任务：{task_id}")
