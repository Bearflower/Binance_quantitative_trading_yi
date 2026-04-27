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
            if interval == "1m":
                cron_expression = "* * * * *"  # 每分钟
                if minutes is None:
                    minutes = 1
            elif interval == "5m":
                cron_expression = "*/5 * * * *"  # 每 5 分钟
                if minutes is None:
                    minutes = 5
            elif interval == "15m":
                cron_expression = "*/15 * * * *"  # 每 15 分钟
                if minutes is None:
                    minutes = 15
            elif interval == "30m":
                cron_expression = "*/30 * * * *"  # 每 30 分钟
                if minutes is None:
                    minutes = 30
            elif interval == "1h":
                cron_expression = "1 * * * *"  # 每小时第 1 分钟（03:01 采集 02:00-03:00）
                if minutes is None:
                    minutes = 60
            elif interval == "4h":
                cron_expression = "1 0,4,8,12,16,20 * * *"  # 每 4 小时
                if minutes is None:
                    minutes = 240
            elif interval == "1d":
                cron_expression = "1 0 * * *"  # 每天 0:01
                if minutes is None:
                    minutes = 1440
            else:
                cron_expression = "*/15 * * * *"  # 默认 15 分钟
                if minutes is None:
                    minutes = 15
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
        
        self.scheduler.start()
        logger.info("✅ 定时任务调度器已启动")
    
    async def _cleanup_expired_symbols(self):
        """清理过期的标的配置"""
        try:
            cleaned = await registry.cleanup_expired()
            if cleaned > 0:
                logger.info(f"🧹 清理了 {cleaned} 个过期的标的配置")
        except Exception as e:
            logger.error(f"清理过期配置失败：{e}")

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
