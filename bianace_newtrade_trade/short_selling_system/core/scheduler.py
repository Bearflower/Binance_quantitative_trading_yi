"""
定时任务调度模块

负责：
- 配置 APScheduler
- 管理不同频率的扫描任务
- 支持动态调整频率
"""

import time
from datetime import datetime
from typing import Callable, Optional, Dict, Any
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from utils.logger import logger
from config.settings import settings


class TaskScheduler:
    """任务调度器"""
    
    def __init__(self):
        """初始化任务调度器"""
        # 创建调度器
        self.scheduler = BlockingScheduler(
            timezone='Asia/Shanghai'  # 设置时区为北京时间
        )
        
        # 任务注册表
        self.registered_tasks: Dict[str, Any] = {}
        
        logger.info("✅ 任务调度器初始化完成")
    
    def add_interval_task(
        self,
        task_id: str,
        func: Callable,
        seconds: int = None,
        minutes: int = None,
        hours: int = None,
        days: int = None,
        **kwargs
    ):
        """
        添加定时任务 (按间隔)
        
        Args:
            task_id: 任务 ID
            func: 任务函数
            seconds/minutes/hours/days: 间隔时间
            **kwargs: 传递给任务函数的参数
        """
        # 构建有效的参数
        trigger_kwargs = {}
        if seconds:
            trigger_kwargs['seconds'] = seconds
        if minutes:
            trigger_kwargs['minutes'] = minutes
        if hours:
            trigger_kwargs['hours'] = hours
        if days:
            trigger_kwargs['days'] = days
        
        trigger = IntervalTrigger(**trigger_kwargs)
        
        self.scheduler.add_job(
            func=func,
            trigger=trigger,
            id=task_id,
            name=task_id,
            kwargs=kwargs,
            replace_existing=True
        )
        
        self.registered_tasks[task_id] = {
            'func': func,
            'trigger': 'interval',
            'interval': {
                'seconds': seconds,
                'minutes': minutes,
                'hours': hours,
                'days': days
            }
        }
        
        logger.info(
            f"✅ 添加定时任务：{task_id}, "
            f"间隔：{seconds or 0}秒 {minutes or 0}分 {hours or 0}时 {days or 0}天"
        )
    
    def add_cron_task(
        self,
        task_id: str,
        func: Callable,
        hour: int = None,
        minute: int = None,
        second: int = None,
        day_of_week: str = None,
        **kwargs
    ):
        """
        添加定时任务 (按 Cron 表达式)
        
        Args:
            task_id: 任务 ID
            func: 任务函数
            hour/minute/second: 执行时间
            day_of_week: 星期几 (0-6 或 mon-fri)
            **kwargs: 传递给任务函数的参数
        """
        trigger = CronTrigger(
            hour=hour,
            minute=minute,
            second=second,
            day_of_week=day_of_week,
            timezone='Asia/Shanghai'
        )
        
        self.scheduler.add_job(
            func=func,
            trigger=trigger,
            id=task_id,
            name=task_id,
            kwargs=kwargs,
            replace_existing=True
        )
        
        self.registered_tasks[task_id] = {
            'func': func,
            'trigger': 'cron',
            'cron': {
                'hour': hour,
                'minute': minute,
                'second': second,
                'day_of_week': day_of_week
            }
        }
        
        logger.info(
            f"✅ 添加定时任务：{task_id}, "
            f"Cron: {hour}:{minute}:{second}, 星期：{day_of_week}"
        )
    
    def remove_task(self, task_id: str) -> bool:
        """
        移除定时任务
        
        Args:
            task_id: 任务 ID
            
        Returns:
            是否移除成功
        """
        try:
            self.scheduler.remove_job(task_id)
            if task_id in self.registered_tasks:
                del self.registered_tasks[task_id]
            logger.info(f"✅ 移除任务：{task_id}")
            return True
        except Exception as e:
            logger.error(f"❌ 移除任务失败：{e}")
            return False
    
    def pause_task(self, task_id: str) -> bool:
        """
        暂停定时任务
        
        Args:
            task_id: 任务 ID
            
        Returns:
            是否暂停成功
        """
        try:
            job = self.scheduler.get_job(task_id)
            if job:
                job.pause()
                logger.info(f"⏸️ 暂停任务：{task_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ 暂停任务失败：{e}")
            return False
    
    def resume_task(self, task_id: str) -> bool:
        """
        恢复定时任务
        
        Args:
            task_id: 任务 ID
            
        Returns:
            是否恢复成功
        """
        try:
            job = self.scheduler.get_job(task_id)
            if job:
                job.resume()
                logger.info(f"▶️ 恢复任务：{task_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ 恢复任务失败：{e}")
            return False
    
    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        获取任务状态
        
        Args:
            task_id: 任务 ID
            
        Returns:
            任务状态信息
        """
        try:
            job = self.scheduler.get_job(task_id)
            if job:
                return {
                    'id': job.id,
                    'name': job.name,
                    'next_run_time': str(job.next_run_time),
                    'paused': job.paused,
                }
            return None
        except Exception as e:
            logger.error(f"❌ 获取任务状态失败：{e}")
            return None
    
    def get_all_tasks_status(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有任务状态
        
        Returns:
            任务状态字典
        """
        status = {}
        for task_id in self.registered_tasks.keys():
            task_status = self.get_task_status(task_id)
            if task_status:
                status[task_id] = task_status
        return status
    
    def start(self):
        """
        启动调度器
        
        此方法会阻塞主线程
        """
        logger.info("🚀 启动任务调度器...")
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("🛑 任务调度器已停止")
    
    def start_background(self):
        """
        后台启动调度器
        
        此方法不会阻塞主线程
        """
        logger.info("🚀 后台启动任务调度器...")
        self.scheduler.start(paused=True)
        logger.info("✅ 任务调度器已后台启动 (暂停状态)")


class MonitoringScheduler(TaskScheduler):
    """监控任务调度器 (针对新币监控优化)"""
    
    def __init__(self):
        """初始化监控调度器"""
        super().__init__()
        
        # 当前监控模式
        self.current_mode = 'no_new_coin'  # 'high_freq', 'normal', 'no_new_coin'
    
    def set_high_freq_mode(self, interval: int = 60):
        """
        设置高频监控模式 (新币上线 0-24 小时)
        
        Args:
            interval: 扫描间隔 (秒，默认 60 秒)
        """
        self._update_task_interval(
            'new_listing_scan',
            seconds=interval
        )
        self.current_mode = 'high_freq'
        logger.info(f"🔥 切换到高频监控模式：{interval}秒/次")
    
    def set_normal_mode(self, interval: int = 300):
        """
        设置普通监控模式 (新币上线 1-7 天)
        
        Args:
            interval: 扫描间隔 (秒，默认 300 秒)
        """
        self._update_task_interval(
            'new_listing_scan',
            seconds=interval
        )
        self.current_mode = 'normal'
        logger.info(f"📊 切换到普通监控模式：{interval}秒/次")
    
    def set_idle_mode(self, interval: int = 3600):
        """
        设置空闲监控模式 (无新币)
        
        Args:
            interval: 扫描间隔 (秒，默认 3600 秒)
        """
        self._update_task_interval(
            'new_listing_scan',
            seconds=interval
        )
        self.current_mode = 'no_new_coin'
        logger.info(f"💤 切换到空闲监控模式：{interval}秒/次")
    
    def _update_task_interval(self, task_id: str, seconds: int):
        """
        更新任务间隔
        
        Args:
            task_id: 任务 ID
            seconds: 新的间隔 (秒)
        """
        if task_id in self.registered_tasks:
            task_info = self.registered_tasks[task_id]
            func = task_info['func']
            kwargs = task_info.get('kwargs', {})
            
            # 移除旧任务
            self.remove_task(task_id)
            
            # 添加新任务
            self.add_interval_task(
                task_id=task_id,
                func=func,
                seconds=seconds,
                **kwargs
            )


# 全局调度器实例
monitoring_scheduler = MonitoringScheduler()
