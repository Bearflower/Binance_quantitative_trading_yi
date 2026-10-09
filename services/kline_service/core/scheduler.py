"""定时任务调度器"""

import asyncio
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from shared.utils.logger import get_logger
from .collector import KlineCollector
from .registry import registry
from .table_name_guard import read_non_negative_int

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

        # 创建任务函数（闭包内捕获 symbol / interval / minutes / self）
        async def collect_task():
            # 防御性自删：如果 scheduler ↔ registry 同步漏掉了，标的已过期/下架后
            # 本 job 仍会被 cron 触发 → 在 collect_recent 之前自检并自删，
            # 避免 stale job 持续抛「symbol 不在白名单」错误（PHAUSDT 事故）
            if not self._symbol_is_active(symbol):
                logger.warning(
                    f"定时任务 {symbol} {interval} 触发时标的已不在 active 列表，自删任务"
                )
                self.remove_task(f"{symbol}_{interval}")
                return
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

    def _symbol_is_active(self, symbol: str) -> bool:
        """判断 symbol 是否仍应被采集：固定标的 or registry active。

        用于 collect_task 闭包的防御性自删，避免 registry.cleanup_expired /
        validate_registered_symbols 只删 registry 不删 scheduler 的漂移期间
        持续触发 store_klines 白名单校验失败。
        """
        from shared.core.config import settings as _settings

        upper = symbol.upper()
        fixed = {s.upper() for s in (_settings.fixed_symbols_list or [])}
        if upper in fixed:
            return True
        active = {
            cfg.symbol.upper() for cfg in registry.get_active_symbols()
        }
        return upper in active

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

        # P0-1：按配置周期性重载注册表内存缓存，消除与 DB 的漂移（0 = 禁用）
        refresh_seconds = self._registry_refresh_seconds()
        if refresh_seconds > 0:
            self.scheduler.add_job(
                self._refresh_registry_cache,
                trigger='interval',
                seconds=refresh_seconds,
                id='refresh_registry_cache',
                name='Refresh Registry Cache',
            )
            logger.info(f"🔄 已添加注册表缓存刷新任务（每 {refresh_seconds} 秒）")

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
                            task_minutes = task_info.get("minutes", 1000)
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

    def _registry_refresh_seconds(self) -> int:
        """读取注册表缓存刷新间隔（秒）；缺失/非法（含 MagicMock）一律回退 0（禁用）"""
        from shared.core.config import settings as _settings

        return read_non_negative_int(
            getattr(_settings, "REGISTRY_CACHE_REFRESH_SECONDS", None), 0
        )

    async def _refresh_registry_cache(self):
        """周期重载注册表内存缓存（refresh_active 内部已做异常兜底并保留旧缓存）"""
        await registry.refresh_active()
        # 同步 scheduler job：DB 直改导致 registry 缓存漂移时，刷新后也要清理 stale job
        await self._sync_jobs_to_registry()

    async def _cleanup_expired_symbols(self):
        """清理过期的标的配置，并同步移除对应的 scheduler job"""
        try:
            cleaned = await registry.cleanup_expired()
            if cleaned > 0:
                logger.info(f"🧹 清理了 {cleaned} 个过期的标的配置")
            # 同步 scheduler job：过期标的的采集任务必须一并移除，避免 stale job
            # 持续触发并在 store_klines 白名单校验时抛错（PHAUSDT 事故根因）
            await self._sync_jobs_to_registry()
        except Exception as e:
            logger.error(f"清理过期配置失败：{e}")

    async def _validate_registered_symbols(self):
        """验证所有注册的标的在币安上是否有效，并同步移除被下架标的的 scheduler job"""
        try:
            cleaned = await self.collector.validate_registered_symbols()
            if cleaned > 0:
                logger.info(f"🧹 定期验证完成，清理了 {cleaned} 个无效的标的")
            # 同步 scheduler job：被自动下架清理的标的，其采集任务必须一并移除
            await self._sync_jobs_to_registry()
        except Exception as e:
            logger.error(f"定期验证标的失败：{e}")

    async def _sync_jobs_to_registry(self) -> None:
        """将 scheduler 任务列表与 registry active 列表对齐。

        仅处理「注册标的」（symbol 不在 settings.fixed_symbols_list）：
        - scheduler 中有、registry 中无 active 记录 → 移除任务（标的已过期/下架/DB 直改）
        - 固定标的（BTCUSDT 等）始终保留，不受 registry 变化影响

        这是对 registry.cleanup_expired / validate_registered_symbols / refresh_active
        只动 registry 不动 scheduler 的补偿，避免 stale job 持续触发 store_klines 白名单
        校验失败（PHAUSDT 事故根因）。
        """
        from shared.core.config import settings as _settings

        fixed = {s.upper() for s in (_settings.fixed_symbols_list or [])}
        active_registry_symbols = {
            cfg.symbol.upper() for cfg in registry.get_active_symbols()
        }

        stale_ids = []
        for task_id, task_info in list(self.tasks.items()):
            symbol = task_info["symbol"].upper()
            if symbol in fixed:
                # 固定标的：跳过，不受 registry 影响
                continue
            if symbol not in active_registry_symbols:
                stale_ids.append(task_id)

        for task_id in stale_ids:
            logger.warning(
                f"🧹 移除 stale 采集任务（标的已不在 registry active 列表）：{task_id}"
            )
            self.remove_task(task_id)

        if stale_ids:
            logger.info(f"registry ↔ scheduler 同步完成：移除 {len(stale_ids)} 个 stale 任务")

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
