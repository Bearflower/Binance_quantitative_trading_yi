"""
测试定时任务调度器 TaskScheduler
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from datetime import datetime, timedelta
from apscheduler.triggers.cron import CronTrigger


class TestTaskScheduler:
    """测试 TaskScheduler"""

    @pytest.fixture
    def scheduler(self):
        """创建 TaskScheduler 实例"""
        from services.kline_service.core.scheduler import TaskScheduler

        mock_collector = AsyncMock()
        mock_collector.collect_recent = AsyncMock(return_value=5)
        mock_collector.validate_registered_symbols = AsyncMock(return_value=0)

        return TaskScheduler(collector=mock_collector)

    # ==================== add_job ====================

    def test_add_job_1m(self, scheduler):
        """测试添加 1m 周期任务"""
        scheduler.add_job("BTCUSDT", "1m")

        task_id = "BTCUSDT_1m"
        assert task_id in scheduler.tasks
        task = scheduler.tasks[task_id]
        assert task["cron"] == "* * * * *"
        assert task["minutes"] == 2

    def test_add_job_5m(self, scheduler):
        """测试添加 5m 周期任务"""
        scheduler.add_job("BTCUSDT", "5m")

        task = scheduler.tasks["BTCUSDT_5m"]
        assert task["cron"] == "*/5 * * * *"
        assert task["minutes"] == 10

    def test_add_job_15m(self, scheduler):
        """测试添加 15m 周期任务"""
        scheduler.add_job("BTCUSDT", "15m")

        task = scheduler.tasks["BTCUSDT_15m"]
        assert task["cron"] == "*/15 * * * *"
        assert task["minutes"] == 30

    def test_add_job_30m(self, scheduler):
        """测试添加 30m 周期任务"""
        scheduler.add_job("BTCUSDT", "30m")

        task = scheduler.tasks["BTCUSDT_30m"]
        assert task["cron"] == "*/30 * * * *"
        assert task["minutes"] == 60

    def test_add_job_1h(self, scheduler):
        """测试添加 1h 周期任务"""
        scheduler.add_job("BTCUSDT", "1h")

        task = scheduler.tasks["BTCUSDT_1h"]
        assert task["cron"] == "1 * * * *"
        assert task["minutes"] == 120

    def test_add_job_4h(self, scheduler):
        """测试添加 4h 周期任务"""
        scheduler.add_job("BTCUSDT", "4h")

        task = scheduler.tasks["BTCUSDT_4h"]
        assert task["cron"] == "1 0,4,8,12,16,20 * * *"
        assert task["minutes"] == 480

    def test_add_job_1d(self, scheduler):
        """测试添加 1d 周期任务"""
        scheduler.add_job("BTCUSDT", "1d")

        task = scheduler.tasks["BTCUSDT_1d"]
        assert task["cron"] == "1 0 * * *"
        assert task["minutes"] == 2880

    def test_add_job_custom_cron(self, scheduler):
        """测试自定义 cron 表达式"""
        scheduler.add_job("BTCUSDT", "15m", cron_expression="5 0 * * *", minutes=10)

        task = scheduler.tasks["BTCUSDT_15m"]
        assert task["cron"] == "5 0 * * *"
        assert task["minutes"] == 10

    def test_add_job_replace_existing(self, scheduler):
        """测试替换已存在的任务"""
        scheduler.add_job("BTCUSDT", "15m", minutes=30)
        scheduler.add_job("BTCUSDT", "15m", minutes=60)  # 替换

        # 任务 ID 相同，但配置已更新
        task = scheduler.tasks["BTCUSDT_15m"]
        assert task["minutes"] == 60

    def test_add_job_triggers_created(self, scheduler):
        """测试 add_job 创建了 APScheduler 任务"""
        scheduler.add_job("BTCUSDT", "15m")

        job = scheduler.scheduler.get_job("BTCUSDT_15m")
        assert job is not None
        assert job.id == "BTCUSDT_15m"
        assert job.name == "Collect BTCUSDT 15m"

    # ==================== add_jobs_from_config ====================

    def test_add_jobs_from_config(self, scheduler):
        """测试从配置批量添加任务"""
        config = {
            "BTCUSDT": {
                "15m": "*/15 * * * *",
                "1h": "1 * * * *",
            },
            "ETHUSDT": {
                "15m": "*/15 * * * *",
            },
        }

        scheduler.add_jobs_from_config(config)

        assert "BTCUSDT_15m" in scheduler.tasks
        assert "BTCUSDT_1h" in scheduler.tasks
        assert "ETHUSDT_15m" in scheduler.tasks
        assert len(scheduler.tasks) == 3

    def test_add_jobs_from_config_empty(self, scheduler):
        """测试空配置"""
        scheduler.add_jobs_from_config({})
        assert len(scheduler.tasks) == 0

    # ==================== start ====================

    def test_start_adds_cleanup_and_validate_jobs(self, scheduler):
        """测试 start 添加清理任务和验证任务"""
        with patch("services.kline_service.core.scheduler.registry") as mock_registry:
            mock_registry.get_active_symbols.return_value = []

            scheduler.start()

            # 检查清理任务和验证任务是否存在
            assert scheduler.scheduler.get_job("cleanup_expired_symbols") is not None
            assert scheduler.scheduler.get_job("validate_registered_symbols") is not None

    def test_start_loads_from_registry(self, scheduler, mock_registered_symbol_config):
        """测试 start 从注册表加载活跃标的"""
        with patch("services.kline_service.core.scheduler.registry") as mock_registry:
            mock_registry.get_active_symbols.return_value = [mock_registered_symbol_config]

            scheduler.start()

            # 应为 BTCUSDT 添加 3 个周期任务（15m, 1h, 4h）
            assert "BTCUSDT_15m" in scheduler.tasks
            assert "BTCUSDT_1h" in scheduler.tasks
            assert "BTCUSDT_4h" in scheduler.tasks

    def test_start_triggers_initial_collection(self, scheduler):
        """测试 start 触发首次采集"""
        with patch("services.kline_service.core.scheduler.registry") as mock_registry:
            mock_registry.get_active_symbols.return_value = []

            with patch.object(scheduler, "_trigger_initial_collection") as mock_trigger:
                scheduler.start()
                mock_trigger.assert_called_once()

    # ==================== 任务执行 ====================

    @pytest.mark.asyncio
    async def test_collect_task_success(self, scheduler):
        """测试采集任务正常执行"""
        scheduler.add_job("BTCUSDT", "15m")

        # 获取任务函数并执行
        job = scheduler.scheduler.get_job("BTCUSDT_15m")
        assert job is not None

        # 执行任务函数
        await job.func()

        scheduler.collector.collect_recent.assert_called_once_with("BTCUSDT", "15m", 30)

    @pytest.mark.asyncio
    async def test_collect_task_exception(self, scheduler):
        """测试采集任务异常时记录日志"""
        scheduler.collector.collect_recent.side_effect = Exception("采集失败")

        scheduler.add_job("BTCUSDT", "15m")

        job = scheduler.scheduler.get_job("BTCUSDT_15m")
        # 不应抛出异常
        await job.func()

    # ==================== _load_from_registry ====================

    def test_load_from_registry(self, scheduler, mock_registered_symbol_config):
        """测试从注册表加载活跃标的"""
        with patch("services.kline_service.core.scheduler.registry") as mock_registry:
            mock_registry.get_active_symbols.return_value = [mock_registered_symbol_config]

            scheduler._load_from_registry()

            assert "BTCUSDT_15m" in scheduler.tasks
            assert "BTCUSDT_1h" in scheduler.tasks
            assert "BTCUSDT_4h" in scheduler.tasks

    def test_load_from_registry_empty(self, scheduler):
        """测试注册表为空时"""
        with patch("services.kline_service.core.scheduler.registry") as mock_registry:
            mock_registry.get_active_symbols.return_value = []

            scheduler._load_from_registry()

            assert len(scheduler.tasks) == 0

    # ==================== shutdown ====================

    def test_shutdown(self, scheduler):
        """测试关闭调度器"""
        with patch.object(scheduler.scheduler, "shutdown") as mock_shutdown:
            scheduler.shutdown(wait=True)
            mock_shutdown.assert_called_once_with(wait=True)

    def test_shutdown_no_wait(self, scheduler):
        """测试关闭调度器（不等待）"""
        with patch.object(scheduler.scheduler, "shutdown") as mock_shutdown:
            scheduler.shutdown(wait=False)
            mock_shutdown.assert_called_once_with(wait=False)

    # ==================== 任务管理 ====================

    def test_pause_task(self, scheduler):
        """测试暂停任务"""
        scheduler.add_job("BTCUSDT", "15m")

        with patch.object(scheduler.scheduler, "pause_job") as mock_pause:
            scheduler.pause_task("BTCUSDT_15m")
            mock_pause.assert_called_once_with("BTCUSDT_15m")

    def test_resume_task(self, scheduler):
        """测试恢复任务"""
        scheduler.add_job("BTCUSDT", "15m")

        with patch.object(scheduler.scheduler, "resume_job") as mock_resume:
            scheduler.resume_task("BTCUSDT_15m")
            mock_resume.assert_called_once_with("BTCUSDT_15m")

    def test_remove_task(self, scheduler):
        """测试移除任务"""
        scheduler.add_job("BTCUSDT", "15m")
        assert "BTCUSDT_15m" in scheduler.tasks

        with patch.object(scheduler.scheduler, "remove_job") as mock_remove:
            scheduler.remove_task("BTCUSDT_15m")
            mock_remove.assert_called_once_with("BTCUSDT_15m")
            assert "BTCUSDT_15m" not in scheduler.tasks

    # ==================== get_tasks / get_next_run_time ====================

    def test_get_tasks(self, scheduler):
        """测试获取所有任务"""
        scheduler.add_job("BTCUSDT", "15m")
        scheduler.add_job("ETHUSDT", "1h")

        tasks = scheduler.get_tasks()
        assert len(tasks) == 2
        assert "BTCUSDT_15m" in tasks
        assert "ETHUSDT_1h" in tasks

    def test_get_next_run_time(self, scheduler):
        """测试获取任务下次运行时间"""
        scheduler.add_job("BTCUSDT", "15m")

        # 启动调度器后才能计算出 next_run_time
        with patch("services.kline_service.core.scheduler.registry") as mock_registry:
            mock_registry.get_active_symbols.return_value = []
            scheduler.start()

        next_run = scheduler.get_next_run_time("BTCUSDT_15m")
        assert next_run is not None
        assert isinstance(next_run, datetime)

    def test_get_next_run_time_not_found(self, scheduler):
        """测试获取不存在的任务的下次运行时间"""
        next_run = scheduler.get_next_run_time("NONEXISTENT")
        assert next_run is None

    # ==================== 清理过期和验证任务 ====================

    @pytest.mark.asyncio
    async def test_cleanup_expired_symbols(self, scheduler):
        """测试清理过期标的任务"""
        with patch("services.kline_service.core.scheduler.registry") as mock_registry:
            mock_registry.cleanup_expired = AsyncMock(return_value=2)

            await scheduler._cleanup_expired_symbols()

            mock_registry.cleanup_expired.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_expired_symbols_error(self, scheduler):
        """测试清理过期标的任务异常"""
        with patch("services.kline_service.core.scheduler.registry") as mock_registry:
            mock_registry.cleanup_expired = AsyncMock(side_effect=Exception("清理失败"))

            # 不应抛出异常
            await scheduler._cleanup_expired_symbols()

    @pytest.mark.asyncio
    async def test_validate_registered_symbols_task(self, scheduler):
        """测试验证注册标的任务"""
        await scheduler._validate_registered_symbols()

        scheduler.collector.validate_registered_symbols.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_registered_symbols_task_error(self, scheduler):
        """测试验证注册标的任务异常"""
        scheduler.collector.validate_registered_symbols.side_effect = Exception("验证失败")

        # 不应抛出异常
        await scheduler._validate_registered_symbols()

    # ==================== 首次采集 ====================

    def test_trigger_initial_collection_no_active(self, scheduler):
        """测试首次采集无任务"""
        # self.tasks 为空，应直接返回
        scheduler._trigger_initial_collection()

    def test_trigger_initial_collection_with_active(self, scheduler):
        """测试首次采集有任务"""
        scheduler.add_job("BTCUSDT", "15m")

        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop_instance = MagicMock()
            mock_loop_instance.is_running.return_value = True
            mock_loop.return_value = mock_loop_instance

            scheduler._trigger_initial_collection()

            mock_loop_instance.create_task.assert_called_once()
            args, _ = mock_loop_instance.create_task.call_args
            assert args[0] is not None  # 验证传入了 coroutine

    @pytest.mark.asyncio
    async def test_do_initial_collection(self, scheduler):
        """测试执行首次采集"""
        scheduler.collector.collect_recent.return_value = 10
        # 使用 tasks dict 格式
        tasks = {
            "BTCUSDT_15m": {"symbol": "BTCUSDT", "interval": "15m"},
            "BTCUSDT_1h": {"symbol": "BTCUSDT", "interval": "1h"},
        }

        await scheduler._do_initial_collection(tasks)

        assert scheduler.collector.collect_recent.call_count == 2
        scheduler.collector.collect_recent.assert_any_call(
            "BTCUSDT", "15m", minutes=1000
        )
        scheduler.collector.collect_recent.assert_any_call(
            "BTCUSDT", "1h", minutes=1000
        )

    @pytest.mark.asyncio
    async def test_do_initial_collection_error(self, scheduler):
        """测试首次采集异常"""
        scheduler.collector.collect_recent.side_effect = Exception("采集失败")
        tasks = {
            "BTCUSDT_15m": {"symbol": "BTCUSDT", "interval": "15m"},
        }

        # 不应抛出异常
        await scheduler._do_initial_collection(tasks)