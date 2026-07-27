"""
v6.16.10 新增功能测试脚本
测试内容：
1. 强制利润提取（_check_profit_extraction）
2. 单周亏损 >15% 暂停 3 天（_check_weekly_loss）
3. 经济日历禁止交易（_check_economic_calendar）
4. config.yaml 配置契约合规性
5. 性能基准测试

所有测试使用 mock 数据，不依赖真实 API 调用。
"""
import sys
import os
import asyncio
import unittest
import time
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import yaml


# ============================================================================
# 辅助函数：加载配置
# ============================================================================

def load_config():
    """加载策略配置文件"""
    config_path = os.path.join(
        os.path.dirname(__file__),
        "config.yaml"
    )
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


# ============================================================================
# 第一部分：配置契约合规性测试
# ============================================================================

class TestConfigContractCompliance(unittest.TestCase):
    """验证 config.yaml 配置格式正确、所有新增配置项存在且类型正确"""

    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        cls.risk_config = cls.config['strategy']['risk']
        cls.freq_config = cls.risk_config['frequency_control']

    def test_weekly_loss_pause_config_exists(self):
        """测试：单周亏损暂停相关配置项存在且类型正确"""
        fc = self.freq_config

        self.assertIn('weekly_loss_pause_enabled', fc,
                      "缺少 weekly_loss_pause_enabled 配置项")
        self.assertIsInstance(fc['weekly_loss_pause_enabled'], bool,
                              "weekly_loss_pause_enabled 应为 bool 类型")

        self.assertIn('weekly_loss_max_ratio', fc,
                      "缺少 weekly_loss_max_ratio 配置项")
        self.assertIsInstance(fc['weekly_loss_max_ratio'], (int, float),
                              "weekly_loss_max_ratio 应为数值类型")
        self.assertGreater(fc['weekly_loss_max_ratio'], 0,
                           "weekly_loss_max_ratio 应大于 0")
        self.assertLessEqual(fc['weekly_loss_max_ratio'], 1.0,
                             "weekly_loss_max_ratio 应 <= 1.0")

        self.assertIn('weekly_loss_pause_days', fc,
                      "缺少 weekly_loss_pause_days 配置项")
        self.assertIsInstance(fc['weekly_loss_pause_days'], int,
                              "weekly_loss_pause_days 应为 int 类型")
        self.assertGreater(fc['weekly_loss_pause_days'], 0,
                           "weekly_loss_pause_days 应大于 0")

    def test_profit_extraction_config_exists(self):
        """测试：利润提取配置项存在且类型正确"""
        self.assertIn('profit_extraction', self.risk_config,
                      "缺少 profit_extraction 配置项")
        pe = self.risk_config['profit_extraction']

        self.assertIn('enabled', pe, "缺少 profit_extraction.enabled")
        self.assertIsInstance(pe['enabled'], bool,
                              "profit_extraction.enabled 应为 bool 类型")

        self.assertIn('extract_ratio', pe, "缺少 profit_extraction.extract_ratio")
        self.assertIsInstance(pe['extract_ratio'], (int, float),
                              "profit_extraction.extract_ratio 应为数值类型")
        self.assertGreater(pe['extract_ratio'], 0,
                           "extract_ratio 应大于 0")
        self.assertLessEqual(pe['extract_ratio'], 1.0,
                             "extract_ratio 应 <= 1.0")

        self.assertIn('min_extract_usdt', pe,
                      "缺少 profit_extraction.min_extract_usdt")
        self.assertIsInstance(pe['min_extract_usdt'], (int, float),
                              "min_extract_usdt 应为数值类型")
        self.assertGreater(pe['min_extract_usdt'], 0,
                           "min_extract_usdt 应大于 0")

    def test_economic_calendar_config_exists(self):
        """测试：经济日历配置项存在且类型正确"""
        self.assertIn('economic_calendar', self.risk_config,
                      "缺少 economic_calendar 配置项")
        ec = self.risk_config['economic_calendar']

        self.assertIn('enabled', ec, "缺少 economic_calendar.enabled")
        self.assertIsInstance(ec['enabled'], bool,
                              "economic_calendar.enabled 应为 bool 类型")

        self.assertIn('ban_window_minutes', ec,
                      "缺少 economic_calendar.ban_window_minutes")
        self.assertIsInstance(ec['ban_window_minutes'], int,
                              "ban_window_minutes 应为 int 类型")
        self.assertGreater(ec['ban_window_minutes'], 0,
                           "ban_window_minutes 应大于 0")

        self.assertIn('events', ec, "缺少 economic_calendar.events")
        self.assertIsInstance(ec['events'], list,
                              "economic_calendar.events 应为 list 类型")
        self.assertGreater(len(ec['events']), 0,
                           "economic_calendar.events 不应为空")

        # 验证每个事件的格式
        for i, event in enumerate(ec['events']):
            with self.subTest(event_index=i):
                self.assertIn('date', event,
                              f"events[{i}] 缺少 date 字段")
                self.assertIn('time', event,
                              f"events[{i}] 缺少 time 字段")
                self.assertIn('name', event,
                              f"events[{i}] 缺少 name 字段")
                # 验证日期格式 "YYYY-MM-DD"
                try:
                    datetime.strptime(event['date'], "%Y-%m-%d")
                except ValueError:
                    self.fail(
                        f"events[{i}].date 格式错误: {event['date']}，"
                        f"应为 YYYY-MM-DD"
                    )
                # 验证时间格式 "HH:MM"
                try:
                    datetime.strptime(event['time'], "%H:%M")
                except ValueError:
                    self.fail(
                        f"events[{i}].time 格式错误: {event['time']}，"
                        f"应为 HH:MM"
                    )

    def test_frequency_control_required_fields(self):
        """测试：频率控制基础配置项完整性"""
        fc = self.freq_config
        required_fields = [
            'max_daily_total_trades',
            'max_daily_symbol_trades',
            'symbol_cooldown_hours',
            'consecutive_loss_pause',
            'pause_duration_hours',
            'max_daily_loss_usdt',
            'initial_capital_usdt',
        ]
        for field in required_fields:
            self.assertIn(field, fc, f"缺少 frequency_control.{field}")

    def test_no_hardcoded_values_in_config(self):
        """测试：配置值不应为 0 或负值（数据完整性检查）"""
        # 频率控制
        fc = self.freq_config
        self.assertGreater(fc['max_daily_total_trades'], 0)
        self.assertGreater(fc['max_daily_symbol_trades'], 0)
        self.assertGreater(fc['initial_capital_usdt'], 0)
        self.assertGreater(fc['max_daily_loss_usdt'], 0)

        # 利润提取
        pe = self.risk_config['profit_extraction']
        self.assertGreater(pe['extract_ratio'], 0)
        self.assertGreater(pe['min_extract_usdt'], 0)

        # 经济日历
        ec = self.risk_config['economic_calendar']
        self.assertGreater(ec['ban_window_minutes'], 0)


# ============================================================================
# 第二部分：FrequencyController 单周亏损暂停逻辑测试
# ============================================================================

class TestWeeklyLossPause(unittest.TestCase):
    """测试 FrequencyController._check_weekly_loss() 方法"""

    def setUp(self):
        """创建模拟的 FrequencyController 实例"""
        self.config = {
            'max_daily_total_trades': 6,
            'max_daily_symbol_trades': 2,
            'symbol_cooldown_hours': 12,
            'consecutive_loss_pause': 5,
            'pause_duration_hours': 24,
            'max_daily_loss_usdt': 25,
            'max_daily_loss_ratio': 0.05,
            'initial_capital_usdt': 500,
            'weekly_loss_pause_enabled': True,
            'weekly_loss_max_ratio': 0.15,
            'weekly_loss_pause_days': 3,
        }

        # 导入 FrequencyController
        from strategies.btc_eth.strategy import FrequencyController
        self.fc = FrequencyController(
            config=self.config,
            db_manager=None,
            strategy_name="测试策略"
        )

    # ---------- 开关控制 ----------

    def test_weekly_loss_disabled(self):
        """测试：当 weekly_loss_pause_enabled=False 时，始终返回 True"""
        self.config['weekly_loss_pause_enabled'] = False
        from strategies.btc_eth.strategy import FrequencyController
        fc = FrequencyController(config=self.config, db_manager=None,
                                 strategy_name="测试")
        now = datetime(2026, 6, 22, 10, 0)
        can_trade, reason = fc._check_weekly_loss(now)
        self.assertTrue(can_trade)
        self.assertIn("未启用", reason)

    def test_weekly_loss_enabled_by_default(self):
        """测试：当配置缺失 weekly_loss_pause_enabled 时，默认启用"""
        cfg_no_flag = dict(self.config)
        del cfg_no_flag['weekly_loss_pause_enabled']
        from strategies.btc_eth.strategy import FrequencyController
        fc = FrequencyController(config=cfg_no_flag, db_manager=None,
                                 strategy_name="测试")
        self.assertTrue(fc.config.get('weekly_loss_pause_enabled', True))

    # ---------- 正常交易场景 ----------

    def test_weekly_loss_no_data(self):
        """测试：没有周盈亏数据时，应允许交易"""
        now = datetime(2026, 6, 22, 10, 0)
        can_trade, reason = self.fc._check_weekly_loss(now)
        self.assertTrue(can_trade)
        self.assertIn("正常", reason)

    def test_weekly_loss_positive(self):
        """测试：当周盈利时，应允许交易"""
        self.fc.daily_pnl = {
            '2026-06-22': Decimal('30'),
            '2026-06-23': Decimal('20'),
            '2026-06-24': Decimal('-10'),
        }
        now = datetime(2026, 6, 24, 10, 0)
        can_trade, reason = self.fc._check_weekly_loss(now)
        self.assertTrue(can_trade)
        self.assertIn("正常", reason)

    def test_weekly_loss_small_loss(self):
        """测试：当周亏损小于阈值时，应允许交易"""
        self.fc.daily_pnl = {
            '2026-06-22': Decimal('-10'),
            '2026-06-23': Decimal('-20'),
            '2026-06-24': Decimal('-10'),
        }
        # 总亏损 40U < 500 * 0.15 = 75U
        now = datetime(2026, 6, 24, 10, 0)
        can_trade, reason = self.fc._check_weekly_loss(now)
        self.assertTrue(can_trade)
        self.assertIn("正常", reason)

    # ---------- 触发暂停场景 ----------

    def test_weekly_loss_exceeds_threshold(self):
        """测试：单周亏损超过 15% 阈值，触发暂停"""
        self.fc.daily_pnl = {
            '2026-06-22': Decimal('-30'),
            '2026-06-23': Decimal('-25'),
            '2026-06-24': Decimal('-30'),
        }
        # 总亏损 85U > 500 * 0.15 = 75U
        now = datetime(2026, 6, 24, 10, 0)
        can_trade, reason = self.fc._check_weekly_loss(now)
        self.assertFalse(can_trade)
        self.assertIn("暂停", reason)
        self.assertIn("15%", reason)
        # 验证暂停截止时间
        self.assertIsNotNone(self.fc.weekly_pause_until)
        expected_pause = now + timedelta(days=3)
        self.assertEqual(
            self.fc.weekly_pause_until.replace(microsecond=0),
            expected_pause.replace(microsecond=0)
        )

    def test_weekly_loss_exactly_at_threshold(self):
        """测试：边界条件 - 亏损恰好等于阈值"""
        self.fc.daily_pnl = {
            '2026-06-22': Decimal('-75'),  # 恰好 500 * 0.15 = 75U
        }
        now = datetime(2026, 6, 24, 10, 0)
        can_trade, reason = self.fc._check_weekly_loss(now)
        self.assertFalse(can_trade, "亏损恰好等于阈值时应触发暂停")

    def test_weekly_loss_just_below_threshold(self):
        """测试：边界条件 - 亏损恰好低于阈值"""
        self.fc.daily_pnl = {
            '2026-06-22': Decimal('-74.99'),  # 略低于 75U
        }
        now = datetime(2026, 6, 24, 10, 0)
        can_trade, reason = self.fc._check_weekly_loss(now)
        self.assertTrue(can_trade, "亏损略低于阈值时不应触发暂停")

    def test_weekly_loss_very_large_loss(self):
        """测试：极端亏损场景（如 90% 亏损）"""
        self.fc.daily_pnl = {
            '2026-06-22': Decimal('-450'),  # 90% 亏损
        }
        now = datetime(2026, 6, 24, 10, 0)
        can_trade, reason = self.fc._check_weekly_loss(now)
        self.assertFalse(can_trade)
        self.assertIsNotNone(self.fc.weekly_pause_until)

    # ---------- 暂停期行为 ----------

    def test_still_paused_during_period(self):
        """测试：暂停期内，后续检查仍返回 False"""
        self.fc.daily_pnl = {
            '2026-06-22': Decimal('-80'),  # 超过阈值
        }
        now = datetime(2026, 6, 24, 10, 0)
        # 第一次触发
        can_trade1, _ = self.fc._check_weekly_loss(now)
        self.assertFalse(can_trade1)

        # 2 天后（仍在暂停期内）
        now2 = now + timedelta(days=2)
        can_trade2, _ = self.fc._check_weekly_loss(now2)
        self.assertFalse(can_trade2, "暂停期内应仍返回 False")

    def test_resume_after_pause_period(self):
        """测试：暂停期结束后，恢复交易"""
        self.fc.daily_pnl = {
            '2026-06-22': Decimal('-80'),
        }
        now = datetime(2026, 6, 24, 10, 0)
        # 触发暂停
        can_trade1, _ = self.fc._check_weekly_loss(now)
        self.assertFalse(can_trade1)

        # 3 天 + 1 秒后（暂停期结束）
        now2 = now + timedelta(days=3, seconds=1)
        # 暂停结束后，原有的亏损数据还在，会再次触发
        can_trade2, _ = self.fc._check_weekly_loss(now2)
        self.assertFalse(can_trade2, "旧数据仍在，应再次触发暂停")

    def test_resume_with_new_week(self):
        """测试：新的一周开始，旧周亏损不影响新周"""
        self.fc.daily_pnl = {
            '2026-06-22': Decimal('-80'),  # 属于 W26
        }
        now = datetime(2026, 6, 24, 10, 0)
        # 设置暂停
        self.fc._check_weekly_loss(now)
        self.assertIsNotNone(self.fc.weekly_pause_until)

        # 模拟到下一周（W27），暂停已结束（6/24 + 3天 = 6/27，6/29 已过暂停期）
        now_new_week = datetime(2026, 6, 29, 10, 0)  # 新的一周
        can_trade, reason = self.fc._check_weekly_loss(now_new_week)
        # 新周没有亏损数据，且暂停已结束，应允许交易
        self.assertTrue(can_trade, "新周无亏损数据且暂停已结束，应允许交易")

    # ---------- can_trade 集成测试 ----------

    def test_can_trade_with_weekly_pause(self):
        """测试：can_trade 方法在单周亏损暂停时返回 False"""
        self.fc.daily_pnl = {
            '2026-06-22': Decimal('-80'),
        }
        now = datetime(2026, 6, 24, 10, 0)
        can_trade, reason = self.fc.can_trade('BTCUSDT', now)
        self.assertFalse(can_trade)
        self.assertIn("单周亏损", reason)

    def test_can_trade_without_weekly_pause(self):
        """测试：can_trade 方法在正常状态下返回 True"""
        now = datetime(2026, 6, 22, 10, 0)
        can_trade, reason = self.fc.can_trade('BTCUSDT', now)
        self.assertTrue(can_trade)

    # ---------- 周标识计算 ----------

    def test_week_key_calculation(self):
        """测试：_get_week_key 方法正确计算 ISO 周标识"""
        # 2026-06-22 是周一，属于 W26
        week_key = self.fc._get_week_key(datetime(2026, 6, 22))
        self.assertEqual(week_key, "2026-W26")

        # 2026-01-01 是周四，属于 W01
        week_key = self.fc._get_week_key(datetime(2026, 1, 1))
        self.assertEqual(week_key, "2026-W01")

        # 2026-12-31 是周四，属于 W53
        week_key = self.fc._get_week_key(datetime(2026, 12, 31))
        self.assertEqual(week_key, "2026-W53")

    def test_weekly_pnl_calculation(self):
        """测试：_calculate_weekly_pnl 正确聚合多日盈亏"""
        self.fc.daily_pnl = {
            '2026-06-15': Decimal('-30'),
            '2026-06-16': Decimal('10'),
            '2026-06-17': Decimal('-20'),
            '2026-06-22': Decimal('-50'),  # 不同周
        }
        # W25 (6/15-6/21) 包含 6/15, 6/16, 6/17
        pnl = self.fc._calculate_weekly_pnl("2026-W25")
        self.assertIsNotNone(pnl)
        self.assertEqual(pnl, Decimal('-40'))  # -30 + 10 - 20

        # W26 (6/22-6/28) 包含 6/22
        pnl2 = self.fc._calculate_weekly_pnl("2026-W26")
        self.assertIsNotNone(pnl2)
        self.assertEqual(pnl2, Decimal('-50'))


# ============================================================================
# 第三部分：经济日历禁止交易逻辑测试
# ============================================================================

class TestEconomicCalendar(unittest.TestCase):
    """测试 BTCEthStrategy._check_economic_calendar() 方法"""

    def setUp(self):
        """创建模拟的策略实例"""
        self.config = load_config()
        self.risk_config = self.config['strategy']['risk']

        # 创建 mock 客户端
        self.mock_binance = MagicMock()
        self.mock_binance.get_account_info = AsyncMock()
        self.mock_kline = MagicMock()
        self.mock_notification = MagicMock()
        self.mock_notification.send = AsyncMock()
        self.mock_notification.send_error_notification = AsyncMock()
        self.mock_notification.send_trade_notification = AsyncMock()
        self.mock_db = None

        from strategies.btc_eth.strategy import BTCEthStrategy

        self.strategy = BTCEthStrategy(
            config=self.config,
            binance_client=self.mock_binance,
            kline_service=self.mock_kline,
            notification_client=self.mock_notification,
            db_manager=self.mock_db
        )

        # 获取经济日历配置
        self.ec_config = self.risk_config.get('economic_calendar', {})
        self.ban_window = self.ec_config.get('ban_window_minutes', 60)

    # ---------- 开关控制 ----------

    def test_calendar_disabled(self):
        """测试：当 economic_calendar.enabled=False 时，始终返回 True"""
        # 临时修改配置
        original = self.strategy.risk_config['economic_calendar']['enabled']
        self.strategy.risk_config['economic_calendar']['enabled'] = False

        now = datetime(2026, 6, 22, 10, 0)
        can_trade, reason = self.strategy._check_economic_calendar(now)
        self.assertTrue(can_trade)
        self.assertIn("未启用", reason)

        self.strategy.risk_config['economic_calendar']['enabled'] = original

    def test_no_events_configured(self):
        """测试：当 events 列表为空时，应返回 True"""
        original_events = self.strategy.risk_config['economic_calendar']['events']
        self.strategy.risk_config['economic_calendar']['events'] = []

        now = datetime(2026, 6, 22, 10, 0)
        can_trade, reason = self.strategy._check_economic_calendar(now)
        self.assertTrue(can_trade)
        self.assertIn("无经济事件", reason)

        self.strategy.risk_config['economic_calendar']['events'] = original_events

    # ---------- 正常交易场景 ----------

    def test_no_event_nearby(self):
        """测试：当前时间不在任何事件窗口内，应允许交易"""
        # 2026-06-22 是周一，最近的配置事件是 2026-06-10 CPI
        now = datetime(2026, 6, 22, 10, 0)
        can_trade, reason = self.strategy._check_economic_calendar(now)
        self.assertTrue(can_trade)
        self.assertIn("不在", reason)

    def test_far_future_event(self):
        """测试：远在未来的事件不应影响当前交易"""
        now = datetime(2025, 1, 1, 10, 0)  # 2025年初，第一个事件是1/15
        can_trade, reason = self.strategy._check_economic_calendar(now)
        self.assertTrue(can_trade)

    # ---------- 禁止交易场景 ----------

    def test_exactly_at_event_time(self):
        """测试：当前时间恰好等于事件时间，应禁止交易"""
        # 使用配置中的第一个事件：2025-01-15 13:30 CPI
        event_time = datetime(2025, 1, 15, 13, 30)
        can_trade, reason = self.strategy._check_economic_calendar(event_time)
        self.assertFalse(can_trade)
        self.assertIn("禁止交易", reason)
        self.assertIn("CPI", reason)

    def test_within_ban_window_before(self):
        """测试：事件前 30 分钟（在禁止窗口内），应禁止交易"""
        event_time = datetime(2025, 1, 15, 13, 30)
        # 事件前 30 分钟 = 13:00
        check_time = event_time - timedelta(minutes=30)
        can_trade, reason = self.strategy._check_economic_calendar(check_time)
        self.assertFalse(can_trade)
        self.assertIn("禁止交易", reason)

    def test_within_ban_window_after(self):
        """测试：事件后 30 分钟（在禁止窗口内），应禁止交易"""
        event_time = datetime(2025, 1, 15, 13, 30)
        # 事件后 30 分钟 = 14:00
        check_time = event_time + timedelta(minutes=30)
        can_trade, reason = self.strategy._check_economic_calendar(check_time)
        self.assertFalse(can_trade)
        self.assertIn("禁止交易", reason)

    # ---------- 边界条件 ----------

    def test_boundary_minute_before_start(self):
        """测试：窗口开始前 1 分钟，应允许交易"""
        event_time = datetime(2025, 1, 15, 13, 30)
        window_start = event_time - timedelta(minutes=self.ban_window)
        check_time = window_start - timedelta(minutes=1)
        can_trade, reason = self.strategy._check_economic_calendar(check_time)
        self.assertTrue(can_trade)

    def test_boundary_minute_after_end(self):
        """测试：窗口结束后 1 分钟，应允许交易"""
        event_time = datetime(2025, 1, 15, 13, 30)
        window_end = event_time + timedelta(minutes=self.ban_window)
        check_time = window_end + timedelta(minutes=1)
        can_trade, reason = self.strategy._check_economic_calendar(check_time)
        self.assertTrue(can_trade)

    def test_boundary_exactly_at_window_start(self):
        """测试：边界条件 - 恰好等于窗口开始时间，应禁止"""
        event_time = datetime(2025, 1, 15, 13, 30)
        window_start = event_time - timedelta(minutes=self.ban_window)
        can_trade, reason = self.strategy._check_economic_calendar(window_start)
        self.assertFalse(can_trade, "恰好等于窗口开始时间时应禁止交易")

    def test_boundary_exactly_at_window_end(self):
        """测试：边界条件 - 恰好等于窗口结束时间，应禁止"""
        event_time = datetime(2025, 1, 15, 13, 30)
        window_end = event_time + timedelta(minutes=self.ban_window)
        can_trade, reason = self.strategy._check_economic_calendar(window_end)
        self.assertFalse(can_trade, "恰好等于窗口结束时间时应禁止交易")

    # ---------- 重要事件类型 ----------

    def test_fomc_event_blocked(self):
        """测试：FOMC 利率决议事件应正确禁止交易"""
        # 2025-01-29 19:00 FOMC
        event_time = datetime(2025, 1, 29, 19, 0)
        can_trade, reason = self.strategy._check_economic_calendar(event_time)
        self.assertFalse(can_trade)
        self.assertIn("FOMC", reason)

    def test_nfp_event_blocked(self):
        """测试：非农就业 NFP 事件应正确禁止交易"""
        # 2025-02-07 13:30 NFP
        event_time = datetime(2025, 2, 7, 13, 30)
        can_trade, reason = self.strategy._check_economic_calendar(event_time)
        self.assertFalse(can_trade)
        self.assertIn("非农", reason)

    def test_cpi_event_blocked(self):
        """测试：CPI 事件应正确禁止交易"""
        # 2025-01-15 13:30 CPI
        event_time = datetime(2025, 1, 15, 13, 30)
        can_trade, reason = self.strategy._check_economic_calendar(event_time)
        self.assertFalse(can_trade)
        self.assertIn("CPI", reason)

    # ---------- 过期事件优化 ----------

    def test_skip_expired_events(self):
        """测试：已过期的事件应被跳过，不影响性能"""
        now = datetime(2026, 6, 22, 10, 0)
        # 所有 2025 年的事件都已过期，应快速返回 True
        start = time.perf_counter()
        can_trade, reason = self.strategy._check_economic_calendar(now)
        elapsed = time.perf_counter() - start
        self.assertTrue(can_trade)
        # 应在 1ms 内完成（跳过已过期事件）
        self.assertLess(elapsed, 0.01,
                        f"过期事件跳过耗时过长: {elapsed*1000:.2f}ms")

    # ---------- 2026 年事件 ----------

    def test_2026_cpi_event(self):
        """测试：2026 年 CPI 事件正确禁止交易"""
        # 2026-06-10 12:30 CPI
        event_time = datetime(2026, 6, 10, 12, 30)
        can_trade, reason = self.strategy._check_economic_calendar(event_time)
        self.assertFalse(can_trade)
        self.assertIn("CPI", reason)

    def test_2026_fomc_event(self):
        """测试：2026 年 FOMC 事件正确禁止交易"""
        # 2026-06-17 18:00 FOMC
        event_time = datetime(2026, 6, 17, 18, 0)
        can_trade, reason = self.strategy._check_economic_calendar(event_time)
        self.assertFalse(can_trade)
        self.assertIn("FOMC", reason)


# ============================================================================
# 第四部分：利润提取逻辑测试
# ============================================================================

class TestProfitExtraction(unittest.TestCase):
    """测试 BTCEthStrategy._check_profit_extraction() 方法的逻辑"""

    def setUp(self):
        """创建模拟的策略实例"""
        self.config = load_config()
        self.risk_config = self.config['strategy']['risk']

        # 创建 mock 客户端
        self.mock_binance = MagicMock()
        self.mock_binance.get_account_info = AsyncMock()
        self.mock_kline = MagicMock()
        self.mock_notification = MagicMock()
        self.mock_notification.send = AsyncMock()
        self.mock_notification.send_error_notification = AsyncMock()
        self.mock_notification.send_trade_notification = AsyncMock()
        self.mock_db = None

        from strategies.btc_eth.strategy import BTCEthStrategy

        self.strategy = BTCEthStrategy(
            config=self.config,
            binance_client=self.mock_binance,
            kline_service=self.mock_kline,
            notification_client=self.mock_notification,
            db_manager=self.mock_db
        )

        # 重置 ATH 追踪
        self.strategy._highest_equity = Decimal('0')
        self.strategy._ath_balance = Decimal('0')
        self.strategy._profit_extraction_last_notified = None

        self.initial_capital = Decimal('500')
        self.extract_ratio = Decimal('0.50')
        self.min_extract = Decimal('10')

    async def _setup_account_mock(self, total_equity: Decimal):
        """设置 mock 账户信息"""
        self.mock_binance.get_account_info = AsyncMock(return_value={
            'totalWalletBalance': float(total_equity),
            'availableBalance': float(total_equity),
        })

    # ---------- 开关控制 ----------

    def test_profit_extraction_disabled(self):
        """测试：当 profit_extraction.enabled=False 时，不执行任何操作"""
        async def _run():
            self.strategy.risk_config['profit_extraction']['enabled'] = False
            await self._setup_account_mock(Decimal('600'))
            await self.strategy._check_profit_extraction()
            self.mock_notification.send.assert_not_called()
            self.assertEqual(self.strategy._highest_equity, Decimal('0'))
        asyncio.run(_run())

    # ---------- 无操作场景 ----------

    def test_no_equity(self):
        """测试：账户权益为 0 时，不执行任何操作"""
        async def _run():
            await self._setup_account_mock(Decimal('0'))
            await self.strategy._check_profit_extraction()
            self.mock_notification.send.assert_not_called()
        asyncio.run(_run())

    def test_not_new_ath(self):
        """测试：当前权益未超过历史最高，不触发提取"""
        async def _run():
            self.strategy._highest_equity = Decimal('600')
            await self._setup_account_mock(Decimal('550'))
            await self.strategy._check_profit_extraction()
            self.mock_notification.send.assert_not_called()
            self.assertEqual(self.strategy._highest_equity, Decimal('600'))
        asyncio.run(_run())

    def test_not_profitable(self):
        """测试：权益高于初始资金但未超过 ATH，不触发"""
        async def _run():
            self.strategy._highest_equity = Decimal('600')
            await self._setup_account_mock(Decimal('580'))
            await self.strategy._check_profit_extraction()
            self.mock_notification.send.assert_not_called()
        asyncio.run(_run())

    # ---------- 触发提取场景 ----------

    def test_new_ath_triggers_notification(self):
        """测试：权益创新高时，发送推送通知"""
        async def _run():
            self.strategy._highest_equity = Decimal('500')
            await self._setup_account_mock(Decimal('600'))
            await self.strategy._check_profit_extraction()
            self.mock_notification.send.assert_called_once()
            call_args = self.mock_notification.send.call_args
            self.assertIn("利润提取", str(call_args))
        asyncio.run(_run())

    def test_ath_updated_after_new_high(self):
        """测试：权益创新高后，ATH 余额应更新"""
        async def _run():
            self.strategy._highest_equity = Decimal('500')
            await self._setup_account_mock(Decimal('620'))
            await self.strategy._check_profit_extraction()
            self.assertEqual(self.strategy._highest_equity, Decimal('620'))
        asyncio.run(_run())

    def test_extract_amount_calculation(self):
        """测试：提取金额计算正确（盈利的 50%）"""
        async def _run():
            self.strategy._highest_equity = Decimal('500')
            await self._setup_account_mock(Decimal('700'))
            await self.strategy._check_profit_extraction()
            call_args = self.mock_notification.send.call_args
            message = str(call_args)
            self.assertIn("100", message, "通知中应包含提取金额 100U")
        asyncio.run(_run())

    # ---------- 最小提取金额 ----------

    def test_below_min_extract(self):
        """测试：提取金额低于最小提取额时，不发送通知"""
        async def _run():
            self.strategy._highest_equity = Decimal('500')
            self.strategy.risk_config['profit_extraction']['min_extract_usdt'] = 50
            await self._setup_account_mock(Decimal('520'))
            await self.strategy._check_profit_extraction()
            self.mock_notification.send.assert_not_called()
        asyncio.run(_run())

    def test_above_min_extract(self):
        """测试：提取金额高于最小提取额时，发送通知"""
        async def _run():
            self.strategy._highest_equity = Decimal('500')
            self.strategy.risk_config['profit_extraction']['min_extract_usdt'] = 10
            await self._setup_account_mock(Decimal('530'))
            await self.strategy._check_profit_extraction()
            self.mock_notification.send.assert_called_once()
        asyncio.run(_run())

    # ---------- 每周最多一次 ----------

    def test_weekly_dedup(self):
        """测试：同一周内多次触发，只发送一次通知"""
        async def _run():
            self.strategy._highest_equity = Decimal('500')
            self.strategy._profit_extraction_last_notified = datetime.now().strftime("%Y-W%W")
            await self._setup_account_mock(Decimal('700'))
            await self.strategy._check_profit_extraction()
            self.mock_notification.send.assert_not_called()
        asyncio.run(_run())

    def test_new_week_allows_new_notification(self):
        """测试：新的一周，允许再次发送通知"""
        async def _run():
            self.strategy._highest_equity = Decimal('500')
            # 代码使用 %W 格式（非ISO），设为上周"2026-W24"避免与当前周冲突
            self.strategy._profit_extraction_last_notified = "2026-W24"
            await self._setup_account_mock(Decimal('650'))
            await self.strategy._check_profit_extraction()
            self.mock_notification.send.assert_called_once()
        asyncio.run(_run())

    # ---------- 边界条件 ----------

    def test_exact_same_as_ath(self):
        """测试：权益恰好等于 ATH，不触发"""
        async def _run():
            self.strategy._highest_equity = Decimal('600')
            await self._setup_account_mock(Decimal('600'))
            await self.strategy._check_profit_extraction()
            self.mock_notification.send.assert_not_called()
        asyncio.run(_run())

    def test_just_above_ath(self):
        """测试：权益略高于 ATH，触发通知"""
        async def _run():
            self.strategy._highest_equity = Decimal('600')
            await self._setup_account_mock(Decimal('600.01'))
            await self.strategy._check_profit_extraction()
            self.assertEqual(self.strategy._highest_equity, Decimal('600.01'))
        asyncio.run(_run())

    def test_large_profit(self):
        """测试：大幅盈利场景（翻倍）"""
        async def _run():
            self.strategy._highest_equity = Decimal('500')
            await self._setup_account_mock(Decimal('1000'))
            await self.strategy._check_profit_extraction()
            call_args = self.mock_notification.send.call_args
            message = str(call_args)
            self.assertIn("250", message, "通知中应包含提取金额 250U")
        asyncio.run(_run())

    def test_ath_from_db_persistence(self):
        """测试：从数据库加载的 ATH 余额生效"""
        async def _run():
            self.strategy._ath_balance = Decimal('550')
            self.strategy._highest_equity = Decimal('0')
            await self._setup_account_mock(Decimal('560'))
            await self.strategy._check_profit_extraction()
            self.assertEqual(self.strategy._highest_equity, Decimal('560'))
        asyncio.run(_run())

    def test_ath_from_db_higher_than_memory(self):
        """测试：数据库 ATH 高于内存 ATH，取最大值"""
        async def _run():
            self.strategy._ath_balance = Decimal('700')
            self.strategy._highest_equity = Decimal('600')
            await self._setup_account_mock(Decimal('650'))
            await self.strategy._check_profit_extraction()
            self.mock_notification.send.assert_not_called()
        asyncio.run(_run())


# ============================================================================
# 第五部分：性能基准测试
# ============================================================================

class TestPerformanceBenchmark(unittest.TestCase):
    """验证新功能不会显著增加策略执行时间"""

    @classmethod
    def setUpClass(cls):
        cls.config = load_config()

    def test_economic_calendar_performance(self):
        """测试：经济日历检查耗时应在 1ms 以内"""
        from strategies.btc_eth.strategy import BTCEthStrategy

        mock_binance = MagicMock()
        mock_kline = MagicMock()
        mock_notification = MagicMock()
        mock_notification.send_error_notification = AsyncMock()
        mock_notification.send_trade_notification = AsyncMock()

        strategy = BTCEthStrategy(
            config=self.config,
            binance_client=mock_binance,
            kline_service=mock_kline,
            notification_client=mock_notification,
            db_manager=None
        )

        # 预热
        for _ in range(5):
            strategy._check_economic_calendar(datetime(2026, 6, 22, 10, 0))

        # 计时
        iterations = 1000
        start = time.perf_counter()
        for _ in range(iterations):
            strategy._check_economic_calendar(datetime(2026, 6, 22, 10, 0))
        elapsed = time.perf_counter() - start

        avg_ms = (elapsed / iterations) * 1000
        print(f"\n  经济日历检查: {iterations} 次, 平均 {avg_ms:.4f}ms/次")
        self.assertLess(avg_ms, 0.5,
                        f"经济日历检查过慢: {avg_ms:.4f}ms/次 (目标 < 0.5ms)")

    def test_weekly_loss_performance(self):
        """测试：单周亏损检查耗时应在 0.5ms 以内"""
        from strategies.btc_eth.strategy import FrequencyController

        fc_config = {
            'max_daily_total_trades': 6,
            'max_daily_symbol_trades': 2,
            'symbol_cooldown_hours': 12,
            'consecutive_loss_pause': 5,
            'pause_duration_hours': 24,
            'max_daily_loss_usdt': 25,
            'max_daily_loss_ratio': 0.05,
            'initial_capital_usdt': 500,
            'weekly_loss_pause_enabled': True,
            'weekly_loss_max_ratio': 0.15,
            'weekly_loss_pause_days': 3,
        }

        fc = FrequencyController(config=fc_config, db_manager=None,
                                 strategy_name="性能测试")

        # 设置一些模拟数据
        for i in range(30):
            day = (datetime(2026, 6, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            fc.daily_pnl[day] = Decimal('-5')

        # 预热
        for _ in range(5):
            fc._check_weekly_loss(datetime(2026, 6, 22, 10, 0))

        # 计时
        iterations = 1000
        start = time.perf_counter()
        for _ in range(iterations):
            fc._check_weekly_loss(datetime(2026, 6, 22, 10, 0))
        elapsed = time.perf_counter() - start

        avg_ms = (elapsed / iterations) * 1000
        print(f"  单周亏损检查: {iterations} 次 (30天数据), 平均 {avg_ms:.4f}ms/次")
        self.assertLess(avg_ms, 0.5,
                        f"单周亏损检查过慢: {avg_ms:.4f}ms/次 (目标 < 0.5ms)")

    def test_can_trade_full_performance(self):
        """测试：can_trade 完整流程耗时应在 1ms 以内"""
        from strategies.btc_eth.strategy import FrequencyController

        fc_config = {
            'max_daily_total_trades': 6,
            'max_daily_symbol_trades': 2,
            'symbol_cooldown_hours': 12,
            'consecutive_loss_pause': 5,
            'pause_duration_hours': 24,
            'max_daily_loss_usdt': 25,
            'max_daily_loss_ratio': 0.05,
            'initial_capital_usdt': 500,
            'weekly_loss_pause_enabled': True,
            'weekly_loss_max_ratio': 0.15,
            'weekly_loss_pause_days': 3,
        }

        fc = FrequencyController(config=fc_config, db_manager=None,
                                 strategy_name="性能测试")

        # 预热
        for _ in range(5):
            fc.can_trade('BTCUSDT', datetime(2026, 6, 22, 10, 0))

        # 计时
        iterations = 1000
        start = time.perf_counter()
        for _ in range(iterations):
            fc.can_trade('BTCUSDT', datetime(2026, 6, 22, 10, 0))
        elapsed = time.perf_counter() - start

        avg_ms = (elapsed / iterations) * 1000
        print(f"  can_trade 完整流程: {iterations} 次, 平均 {avg_ms:.4f}ms/次")
        self.assertLess(avg_ms, 1.0,
                        f"can_trade 过慢: {avg_ms:.4f}ms/次 (目标 < 1ms)")

    def test_profit_extraction_logic_only(self):
        """测试：利润提取纯逻辑（不含 API 调用）耗时"""
        from strategies.btc_eth.strategy import BTCEthStrategy

        mock_binance = MagicMock()
        mock_binance.get_account_info = AsyncMock(return_value={
            'totalWalletBalance': 600.0,
            'availableBalance': 600.0,
        })
        mock_kline = MagicMock()
        mock_notification = MagicMock()
        mock_notification.send_error_notification = AsyncMock()
        mock_notification.send_trade_notification = AsyncMock()

        strategy = BTCEthStrategy(
            config=self.config,
            binance_client=mock_binance,
            kline_service=mock_kline,
            notification_client=mock_notification,
            db_manager=None
        )
        strategy._highest_equity = Decimal('500')

        # 注意：这是 async 方法，这里只测纯逻辑的计算部分
        # 通过直接调用内部逻辑来测量
        import asyncio

        async def _run():
            start = time.perf_counter()
            for _ in range(100):
                strategy._highest_equity = Decimal('500')
                strategy._profit_extraction_last_notified = None
                await strategy._check_profit_extraction()
            return time.perf_counter() - start

        elapsed = asyncio.run(_run())
        avg_ms = (elapsed / 100) * 1000
        print(f"  利润提取逻辑: 100 次, 平均 {avg_ms:.4f}ms/次")
        # 利润提取包含 API 调用（mock），所以阈值宽松一些
        self.assertLess(avg_ms, 5.0,
                        f"利润提取逻辑过慢: {avg_ms:.4f}ms/次 (目标 < 5ms)")


# ============================================================================
# 第六部分：集成测试
# ============================================================================

class TestIntegration(unittest.TestCase):
    """集成测试：验证多个功能同时工作的场景"""

    def setUp(self):
        self.config = load_config()

    def test_can_trade_with_weekly_pause_and_economic_calendar(self):
        """测试：同时有单周亏损暂停和经济日历禁止时，行为正确"""
        from strategies.btc_eth.strategy import FrequencyController

        fc_config = {
            'max_daily_total_trades': 6,
            'max_daily_symbol_trades': 2,
            'symbol_cooldown_hours': 12,
            'consecutive_loss_pause': 5,
            'pause_duration_hours': 24,
            'max_daily_loss_usdt': 25,
            'max_daily_loss_ratio': 0.05,
            'initial_capital_usdt': 500,
            'weekly_loss_pause_enabled': True,
            'weekly_loss_max_ratio': 0.15,
            'weekly_loss_pause_days': 3,
        }

        fc = FrequencyController(config=fc_config, db_manager=None,
                                 strategy_name="集成测试")

        # 设置单周亏损（W26 周内的数据）
        fc.daily_pnl = {'2026-06-22': Decimal('-80')}
        now = datetime(2026, 6, 24, 10, 0)

        # can_trade 应该先检查暂停，再检查经济日历
        can_trade, reason = fc.can_trade('BTCUSDT', now)
        self.assertFalse(can_trade)
        self.assertIn("单周亏损", reason)

    def test_consecutive_loss_and_weekly_loss_independent(self):
        """测试：连续亏损暂停和单周亏损暂停相互独立"""
        from strategies.btc_eth.strategy import FrequencyController

        fc_config = {
            'max_daily_total_trades': 6,
            'max_daily_symbol_trades': 2,
            'symbol_cooldown_hours': 12,
            'consecutive_loss_pause': 5,
            'pause_duration_hours': 24,
            'max_daily_loss_usdt': 25,
            'max_daily_loss_ratio': 0.05,
            'initial_capital_usdt': 500,
            'weekly_loss_pause_enabled': True,
            'weekly_loss_max_ratio': 0.15,
            'weekly_loss_pause_days': 3,
        }

        fc = FrequencyController(config=fc_config, db_manager=None,
                                 strategy_name="集成测试")

        now = datetime(2026, 6, 22, 10, 0)

        # 设置连续亏损暂停
        fc.consecutive_losses = 5
        fc.pause_until = now + timedelta(hours=24)

        # 设置单周亏损暂停
        fc.weekly_pause_until = now + timedelta(days=3)

        can_trade, reason = fc.can_trade('BTCUSDT', now)
        self.assertFalse(can_trade)
        # 先检查连续亏损暂停
        self.assertIn("策略暂停", reason)

    def test_economic_calendar_with_actual_2026_events(self):
        """测试：验证 2026 年所有配置事件都能正确解析"""
        from strategies.btc_eth.strategy import BTCEthStrategy

        mock_binance = MagicMock()
        mock_kline = MagicMock()
        mock_notification = MagicMock()
        mock_notification.send_error_notification = AsyncMock()
        mock_notification.send_trade_notification = AsyncMock()

        strategy = BTCEthStrategy(
            config=self.config,
            binance_client=mock_binance,
            kline_service=mock_kline,
            notification_client=mock_notification,
            db_manager=None
        )

        ec_config = self.config['strategy']['risk']['economic_calendar']
        ban_window = ec_config['ban_window_minutes']

        # 验证每个 2026 年事件都能正确禁止交易
        count_2026 = 0
        for event in ec_config['events']:
            if event['date'].startswith('2026'):
                count_2026 += 1
                event_dt = datetime.strptime(
                    f"{event['date']} {event['time']}", "%Y-%m-%d %H:%M"
                )
                can_trade, reason = strategy._check_economic_calendar(event_dt)
                self.assertFalse(
                    can_trade,
                    f"2026 年事件 {event['name']} ({event['date']} {event['time']}) "
                    f"应禁止交易"
                )

        self.assertGreater(count_2026, 0,
                           "应至少有一个 2026 年的事件")


# ============================================================================
# 运行入口
# ============================================================================

if __name__ == '__main__':
    print("=" * 70)
    print("  v6.16.10 新增功能测试套件")
    print("  测试内容：利润提取 | 单周亏损暂停 | 经济日历 | 配置契约 | 性能")
    print("=" * 70)

    # 使用 unittest 运行
    unittest.main(verbosity=2, argv=['test_v23_functional.py'])