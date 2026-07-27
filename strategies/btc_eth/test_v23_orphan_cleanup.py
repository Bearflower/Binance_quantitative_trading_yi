"""
v6.23 孤儿条件单修复专项测试
测试内容：
1. 配置契约测试 - cancel_retry 配置项完整性和类型正确性
2. 条件单取消重试机制测试 - _cleanup_position_orders / _retry_pending_cancellations
3. 启动时孤儿条件单检测测试 - _startup_orphan_cleanup / _do_startup_orphan_cleanup
4. 条件单成交记录测试 - _record_executed_conditional_order

所有测试使用 mock 数据，不依赖真实 API 调用。
"""
import sys
import os
import asyncio
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import yaml
import structlog

structlog.reset_defaults()
structlog.configure(
    processors=[
        structlog.processors.KeyValueRenderer(key_order=['event']),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=False,
)


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
# 辅助类：模拟数据库查询结果
# ============================================================================

class MockRow(dict):
    """模拟数据库行对象，支持属性访问"""
    def __getattr__(self, key):
        if key in self:
            return self[key]
        raise AttributeError(f"MockRow has no attribute {key}")


# ============================================================================
# 第一部分：配置契约测试
# ============================================================================

class TestCancelRetryConfigContract(unittest.TestCase):
    """验证 config.yaml 中 cancel_retry 配置项的完整性和类型正确性"""

    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        cls.risk_config = cls.config['strategy']['risk']

    def test_cancel_retry_section_exists(self):
        """测试：cancel_retry 配置项存在"""
        self.assertIn('cancel_retry', self.risk_config,
                      "缺少 cancel_retry 配置项")

    def test_max_retries_type(self):
        """测试：max_retries 为 int 类型且值合理"""
        cr = self.risk_config['cancel_retry']
        self.assertIn('max_retries', cr, "缺少 max_retries")
        self.assertIsInstance(cr['max_retries'], int,
                              "max_retries 应为 int 类型")
        self.assertGreater(cr['max_retries'], 0,
                           "max_retries 应大于 0")
        self.assertLessEqual(cr['max_retries'], 100,
                             "max_retries 不应超过 100")

    def test_retry_interval_cycles_type(self):
        """测试：retry_interval_cycles 为 int 类型且值合理"""
        cr = self.risk_config['cancel_retry']
        self.assertIn('retry_interval_cycles', cr,
                      "缺少 retry_interval_cycles")
        self.assertIsInstance(cr['retry_interval_cycles'], int,
                              "retry_interval_cycles 应为 int 类型")
        self.assertGreaterEqual(cr['retry_interval_cycles'], 1,
                                "retry_interval_cycles 应 >= 1")

    def test_notify_on_timeout_type(self):
        """测试：notify_on_timeout 为 bool 类型"""
        cr = self.risk_config['cancel_retry']
        self.assertIn('notify_on_timeout', cr,
                      "缺少 notify_on_timeout")
        self.assertIsInstance(cr['notify_on_timeout'], bool,
                              "notify_on_timeout 应为 bool 类型")

    def test_max_cleanup_hours_type(self):
        """测试：max_cleanup_hours 为 int 类型且值合理"""
        cr = self.risk_config['cancel_retry']
        self.assertIn('max_cleanup_hours', cr,
                      "缺少 max_cleanup_hours")
        self.assertIsInstance(cr['max_cleanup_hours'], int,
                              "max_cleanup_hours 应为 int 类型")
        self.assertGreater(cr['max_cleanup_hours'], 0,
                           "max_cleanup_hours 应大于 0")

    def test_cleanup_timeout_seconds_type(self):
        """测试：cleanup_timeout_seconds 为 int 类型且值合理"""
        cr = self.risk_config['cancel_retry']
        self.assertIn('cleanup_timeout_seconds', cr,
                      "缺少 cleanup_timeout_seconds")
        self.assertIsInstance(cr['cleanup_timeout_seconds'], int,
                              "cleanup_timeout_seconds 应为 int 类型")
        self.assertGreater(cr['cleanup_timeout_seconds'], 0,
                           "cleanup_timeout_seconds 应大于 0")

    def test_trade_lookup_window_minutes_type(self):
        """测试：trade_lookup_window_minutes 为 int 类型且值合理"""
        cr = self.risk_config['cancel_retry']
        self.assertIn('trade_lookup_window_minutes', cr,
                      "缺少 trade_lookup_window_minutes")
        self.assertIsInstance(cr['trade_lookup_window_minutes'], int,
                              "trade_lookup_window_minutes 应为 int 类型")
        self.assertGreater(cr['trade_lookup_window_minutes'], 0,
                           "trade_lookup_window_minutes 应大于 0")

    def test_all_cancel_retry_fields_present(self):
        """测试：cancel_retry 下所有必需字段都存在"""
        cr = self.risk_config['cancel_retry']
        required_fields = [
            'max_retries',
            'retry_interval_cycles',
            'notify_on_timeout',
            'max_cleanup_hours',
            'cleanup_timeout_seconds',
            'trade_lookup_window_minutes',
        ]
        for field in required_fields:
            self.assertIn(field, cr, f"缺少 cancel_retry.{field}")

    def test_cleanup_silent_error_codes_exists(self):
        """测试：cleanup_silent_error_codes 配置存在且类型正确"""
        self.assertIn('cleanup_silent_error_codes', self.risk_config,
                      "缺少 cleanup_silent_error_codes")
        codes = self.risk_config['cleanup_silent_error_codes']
        self.assertIsInstance(codes, list,
                              "cleanup_silent_error_codes 应为 list 类型")
        self.assertGreater(len(codes), 0,
                           "cleanup_silent_error_codes 不应为空")
        for code in codes:
            self.assertIsInstance(code, int,
                                  f"错误码 {code} 应为 int 类型")


# ============================================================================
# 第二部分：条件单取消重试机制测试
# ============================================================================

class TestCancelRetryMechanism(unittest.TestCase):
    """测试 _cleanup_position_orders 和 _retry_pending_cancellations 方法"""

    def setUp(self):
        """创建模拟的策略实例"""
        self.config = load_config()
        self.risk_config = self.config['strategy']['risk']

        # 创建 mock 客户端
        self.mock_binance = MagicMock()
        self.mock_binance.get_account_info = AsyncMock()
        self.mock_binance.cancel_algo_order = AsyncMock()
        self.mock_binance.cancel_order = AsyncMock()
        self.mock_kline = MagicMock()
        self.mock_notification = MagicMock()
        self.mock_notification.send = AsyncMock()
        self.mock_notification.send_error_notification = AsyncMock()
        self.mock_notification.send_trade_notification = AsyncMock()
        self.mock_db = MagicMock()
        self.mock_db.fetch_all = AsyncMock(return_value=[])
        self.mock_db.execute = AsyncMock()

        from strategies.btc_eth.strategy import BTCEthStrategy, PositionState

        self.strategy = BTCEthStrategy(
            config=self.config,
            binance_client=self.mock_binance,
            kline_service=self.mock_kline,
            notification_client=self.mock_notification,
            db_manager=self.mock_db
        )
        self.PositionState = PositionState

    def _create_position(self, symbol="BTCUSDT", **kwargs):
        """创建带有指定属性的持仓状态"""
        pos = self.PositionState()
        for key, value in kwargs.items():
            setattr(pos, key, value)
        return pos

    # ---------- 取消成功场景 ----------

    def test_cancel_success_clears_order_id(self):
        """测试：取消成功时 order_id 置为 None，重试计数清除"""
        async def _run():
            pos = self._create_position(
                stop_loss_order_id=12345,
                cancel_pending=True,
                cancel_retry_count={"stop_loss": 2},
            )
            self.strategy.positions["BTCUSDT"] = pos

            await self.strategy._cleanup_position_orders("BTCUSDT", pos)

            # 验证 API 被调用
            self.mock_binance.cancel_algo_order.assert_called_once_with("BTCUSDT", 12345)
            # 验证 order_id 被清除
            self.assertIsNone(pos.stop_loss_order_id)
            # 验证重试计数被清除
            self.assertNotIn("stop_loss", pos.cancel_retry_count)
            # 验证所有条件单都清理完毕时 cancel_pending 置为 False
            self.assertFalse(pos.cancel_pending)

        asyncio.run(_run())

    # ---------- 取消失败：可重试错误 ----------

    def test_cancel_failure_retains_order_id(self):
        """测试：取消失败时 order_id 保留，重试计数递增"""
        async def _run():
            from shared.binance_api import BinanceAPIError
            # 设置 cancel_algo_order 抛出可重试异常
            self.mock_binance.cancel_algo_order = AsyncMock(
                side_effect=BinanceAPIError(-1001, "Internal error")
            )

            pos = self._create_position(
                stop_loss_order_id=12345,
                cancel_pending=True,
            )
            self.strategy.positions["BTCUSDT"] = pos

            await self.strategy._cleanup_position_orders("BTCUSDT", pos)

            # 验证 order_id 保留
            self.assertIsNotNone(pos.stop_loss_order_id)
            self.assertEqual(pos.stop_loss_order_id, 12345)
            # 验证重试计数递增
            self.assertEqual(pos.cancel_retry_count.get("stop_loss"), 1)
            # 验证 cancel_pending 保持 True
            self.assertTrue(pos.cancel_pending)

        asyncio.run(_run())

    # ---------- 重试成功（第 N 次） ----------

    def test_retry_succeeds_on_nth_attempt(self):
        """测试：第 N 次重试成功时 order_id 置为 None"""
        async def _run():
            from shared.binance_api import BinanceAPIError
            call_count = [0]

            async def cancel_with_retry(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] < 3:
                    raise BinanceAPIError(-1001, "Internal error")
                # 第三次调用成功

            self.mock_binance.cancel_algo_order = AsyncMock(side_effect=cancel_with_retry)

            pos = self._create_position(
                stop_loss_order_id=12345,
                cancel_pending=True,
                cancel_retry_count={"stop_loss": 2},
                last_retry_cycle=0,
            )
            self.strategy.positions["BTCUSDT"] = pos

            # 第一次重试（call_count=1 < 3，失败）
            await self.strategy._cleanup_position_orders("BTCUSDT", pos)
            self.assertEqual(pos.cancel_retry_count.get("stop_loss"), 3)
            self.assertIsNotNone(pos.stop_loss_order_id)

            # 第二次重试（call_count=2 < 3，失败）
            await self.strategy._cleanup_position_orders("BTCUSDT", pos)
            self.assertEqual(pos.cancel_retry_count.get("stop_loss"), 4)
            self.assertIsNotNone(pos.stop_loss_order_id)

            # 第三次重试（call_count=3，成功）
            await self.strategy._cleanup_position_orders("BTCUSDT", pos)
            # 验证成功，重试计数已清除，order_id 已置为 None
            self.assertIsNone(pos.stop_loss_order_id)
            self.assertNotIn("stop_loss", pos.cancel_retry_count)

        asyncio.run(_run())

    # ---------- 达到最大重试次数 ----------

    def test_max_retries_triggers_timeout_notification(self):
        """测试：达到最大重试次数后发送通知"""
        async def _run():
            from shared.binance_api import BinanceAPIError
            # 设置每次取消都失败
            self.mock_binance.cancel_algo_order = AsyncMock(
                side_effect=BinanceAPIError(-1001, "Internal error")
            )

            pos = self._create_position(
                stop_loss_order_id=12345,
                cancel_pending=True,
                cancel_retry_count={"stop_loss": 9},  # 已经是第9次，再试一次就达到10次
            )
            self.strategy.positions["BTCUSDT"] = pos

            await self.strategy._cleanup_position_orders("BTCUSDT", pos)

            # 验证 order_id 被清除（放弃重试）
            self.assertIsNone(pos.stop_loss_order_id)
            # 验证重试计数被清除
            self.assertNotIn("stop_loss", pos.cancel_retry_count)
            # 验证通知发送
            self.mock_notification.send_error_notification.assert_called_once()

        asyncio.run(_run())

    # ---------- 条件单已执行（-2021） ----------

    def test_condition_executed_2021(self):
        """测试：条件单已执行（-2021）被视为已取消"""
        async def _run():
            from shared.binance_api import BinanceAPIError
            self.mock_binance.cancel_algo_order = AsyncMock(
                side_effect=BinanceAPIError(-2021, "Order already filled")
            )

            pos = self._create_position(
                stop_loss_order_id=12345,
                cancel_pending=True,
                cancel_retry_count={"stop_loss": 3},
            )
            self.strategy.positions["BTCUSDT"] = pos

            await self.strategy._cleanup_position_orders("BTCUSDT", pos)

            # 验证 order_id 被清除
            self.assertIsNone(pos.stop_loss_order_id)
            # 验证重试计数被清除
            self.assertNotIn("stop_loss", pos.cancel_retry_count)
            # 验证 _record_executed_conditional_order 被调用
            self.mock_db.execute.assert_any_call(
                unittest.mock.ANY,
                unittest.mock.ANY
            )

        asyncio.run(_run())

    # ---------- 订单已不存在（-2011） ----------

    def test_order_not_exists_2011(self):
        """测试：订单已不存在（-2011）被视为已取消"""
        async def _run():
            from shared.binance_api import BinanceAPIError
            self.mock_binance.cancel_algo_order = AsyncMock(
                side_effect=BinanceAPIError(-2011, "Order does not exist")
            )

            pos = self._create_position(
                stop_loss_order_id=12345,
                cancel_pending=True,
                cancel_retry_count={"stop_loss": 3},
            )
            self.strategy.positions["BTCUSDT"] = pos

            await self.strategy._cleanup_position_orders("BTCUSDT", pos)

            # 验证 order_id 被清除
            self.assertIsNone(pos.stop_loss_order_id)
            # 验证重试计数被清除
            self.assertNotIn("stop_loss", pos.cancel_retry_count)

        asyncio.run(_run())

    # ---------- 静默错误码（-2022） ----------

    def test_silent_error_code_2022(self):
        """测试：静默错误码（-2022）被视为已取消"""
        async def _run():
            from shared.binance_api import BinanceAPIError
            self.mock_binance.cancel_algo_order = AsyncMock(
                side_effect=BinanceAPIError(-2022, "Order already canceled")
            )

            pos = self._create_position(
                stop_loss_order_id=12345,
                cancel_pending=True,
                cancel_retry_count={"stop_loss": 3},
            )
            self.strategy.positions["BTCUSDT"] = pos

            await self.strategy._cleanup_position_orders("BTCUSDT", pos)

            # 验证 order_id 被清除
            self.assertIsNone(pos.stop_loss_order_id)
            # 验证重试计数被清除
            self.assertNotIn("stop_loss", pos.cancel_retry_count)

        asyncio.run(_run())

    # ---------- 重试间隔控制 ----------

    def test_retry_interval_control(self):
        """测试：重试间隔控制 - 未达到间隔周期时不执行重试"""
        async def _run():
            pos = self._create_position(
                stop_loss_order_id=12345,
                cancel_pending=True,
                cancel_retry_count={"stop_loss": 1},
                last_retry_cycle=5,
            )
            self.strategy.positions["BTCUSDT"] = pos

            # 设置当前周期为 5（与 last_retry_cycle 相同，间隔=1，未达到）
            self.strategy._cycle_count = 5
            self.mock_binance.cancel_algo_order = AsyncMock()

            await self.strategy._retry_pending_cancellations()

            # 验证未执行取消（间隔未到）
            self.mock_binance.cancel_algo_order.assert_not_called()

        asyncio.run(_run())

    def test_retry_interval_reached(self):
        """测试：重试间隔控制 - 达到间隔周期时执行重试"""
        async def _run():
            pos = self._create_position(
                stop_loss_order_id=12345,
                cancel_pending=True,
                cancel_retry_count={"stop_loss": 1},
                last_retry_cycle=4,
            )
            self.strategy.positions["BTCUSDT"] = pos

            # 设置当前周期为 6（与 last_retry_cycle 差2，间隔=1，已达到）
            self.strategy._cycle_count = 6
            self.mock_binance.cancel_algo_order = AsyncMock()

            await self.strategy._retry_pending_cancellations()

            # 验证执行了取消
            self.mock_binance.cancel_algo_order.assert_called_once()

        asyncio.run(_run())

    # ---------- 强制清理超时 ----------

    def test_force_cleanup_timeout(self):
        """测试：超过 max_cleanup_hours 强制放弃重试"""
        async def _run():
            pos = self._create_position(
                stop_loss_order_id=12345,
                cancel_pending=True,
                cancel_retry_count={"stop_loss": 3},
                last_retry_cycle=0,
                first_retry_time=datetime.now() - timedelta(hours=72),  # 超过48小时
            )
            self.strategy.positions["BTCUSDT"] = pos

            self.strategy._cycle_count = 10
            self.mock_binance.cancel_algo_order = AsyncMock()

            await self.strategy._retry_pending_cancellations()

            # 验证未执行取消（已超时）
            self.mock_binance.cancel_algo_order.assert_not_called()
            # 验证重试计数被清除
            self.assertFalse(pos.cancel_retry_count)
            # 验证 cancel_pending 被置为 False
            self.assertFalse(pos.cancel_pending)

        asyncio.run(_run())

    # ---------- 多个条件单类型 ----------

    def test_multiple_order_types_retry(self):
        """测试：多个条件单类型同时有重试计数"""
        async def _run():
            from shared.binance_api import BinanceAPIError
            self.mock_binance.cancel_algo_order = AsyncMock(
                side_effect=BinanceAPIError(-1001, "Internal error")
            )

            pos = self._create_position(
                stop_loss_order_id=111,
                tp1_order_id=222,
                tp2_order_id=333,
                cancel_pending=True,
                cancel_retry_count={"stop_loss": 1, "tp1": 2},
                last_retry_cycle=0,
            )
            self.strategy.positions["BTCUSDT"] = pos

            await self.strategy._cleanup_position_orders("BTCUSDT", pos)

            # 验证两个条件单都尝试了取消
            self.assertEqual(self.mock_binance.cancel_algo_order.call_count, 3)
            # 验证重试计数都递增了
            self.assertEqual(pos.cancel_retry_count.get("stop_loss"), 2)
            self.assertEqual(pos.cancel_retry_count.get("tp1"), 3)

        asyncio.run(_run())

    # ---------- _cleanup_residual_orders 保留持仓 ----------

    def test_residual_cleanup_keeps_position_with_retry(self):
        """测试：_cleanup_residual_orders 对待重试条件单的持仓保留记录"""
        async def _run():
            from shared.binance_api import BinanceAPIError
            # 设置 API 取消失败，确保重试计数保留
            self.mock_binance.cancel_algo_order = AsyncMock(
                side_effect=BinanceAPIError(-1001, "Internal error")
            )

            pos = self._create_position(
                current_quantity=Decimal('0'),
                stop_loss_order_id=12345,
                cancel_pending=False,
                cancel_retry_count={"stop_loss": 1},
                last_retry_cycle=0,
            )
            self.strategy.positions["BTCUSDT"] = pos

            await self.strategy._cleanup_residual_orders()

            # 验证持仓仍保留（有待重试项）
            self.assertIn("BTCUSDT", self.strategy.positions)
            # 验证 cancel_pending 被置为 True
            self.assertTrue(pos.cancel_pending)

        asyncio.run(_run())


# ============================================================================
# 第三部分：启动时孤儿条件单检测测试
# ============================================================================

class TestStartupOrphanCleanup(unittest.TestCase):
    """测试 _startup_orphan_cleanup 和 _do_startup_orphan_cleanup 方法"""

    def setUp(self):
        """创建模拟的策略实例"""
        self.config = load_config()
        self.risk_config = self.config['strategy']['risk']

        # 创建 mock 客户端
        self.mock_binance = MagicMock()
        self.mock_binance.get_account_info = AsyncMock()
        self.mock_binance.cancel_all_algo_orders = AsyncMock()
        self.mock_binance.cancel_algo_order = AsyncMock()
        self.mock_kline = MagicMock()
        self.mock_notification = MagicMock()
        self.mock_notification.send = AsyncMock()
        self.mock_notification.send_error_notification = AsyncMock()
        self.mock_notification.send_trade_notification = AsyncMock()
        self.mock_db = MagicMock()
        self.mock_db.fetch_all = AsyncMock(return_value=[])
        self.mock_db.execute = AsyncMock()
        self.mock_db.execute_ddl = AsyncMock()

        from strategies.btc_eth.strategy import BTCEthStrategy

        self.strategy = BTCEthStrategy(
            config=self.config,
            binance_client=self.mock_binance,
            kline_service=self.mock_kline,
            notification_client=self.mock_notification,
            db_manager=self.mock_db
        )

    # ---------- 无数据库管理器 ----------

    def test_no_db_manager(self):
        """测试：无数据库管理器时跳过"""
        async def _run():
            strategy_no_db = self.strategy.__class__(
                config=self.config,
                binance_client=self.mock_binance,
                kline_service=self.mock_kline,
                notification_client=self.mock_notification,
                db_manager=None
            )
            await strategy_no_db._startup_orphan_cleanup()
            # 不会调用任何 API
            self.mock_binance.get_position.assert_not_called()

        asyncio.run(_run())

    # ---------- 无 OPEN 条件单 ----------

    def test_no_open_orders(self):
        """测试：无 OPEN 条件单时跳过"""
        async def _run():
            get_open_orders_path = "shared.condition_orders.get_open_orders"
            with patch(get_open_orders_path, AsyncMock(return_value=[])):
                await self.strategy._do_startup_orphan_cleanup()
                # 不应查询交易所持仓
                self.mock_binance.get_position.assert_not_called()

        asyncio.run(_run())

    # ---------- 有孤儿条件单时执行取消 ----------

    def test_orphan_orders_canceled(self):
        """测试：有孤儿条件单时执行批量取消"""
        async def _run():
            # 模拟 OPEN 条件单
            open_orders = [
                {"symbol": "BTCUSDT", "algo_id": 1001, "strategy_name": "btc_eth"},
                {"symbol": "BTCUSDT", "algo_id": 1002, "strategy_name": "btc_eth"},
                {"symbol": "ETHUSDT", "algo_id": 2001, "strategy_name": "btc_eth"},
            ]
            # 模拟交易所持仓：只有 SOLUSDT（没有 BTCUSDT 和 ETHUSDT）
            exchange_positions = [
                {"symbol": "SOLUSDT", "positionAmt": "1.0"},
            ]
            self.mock_binance.get_position = AsyncMock(return_value=exchange_positions)

            get_open_orders_path = "shared.condition_orders.get_open_orders"
            with patch(get_open_orders_path, AsyncMock(return_value=open_orders)):
                await self.strategy._do_startup_orphan_cleanup()

                # 验证批量取消被调用（BTCUSDT 和 ETHUSDT）
                self.assertEqual(
                    self.mock_binance.cancel_all_algo_orders.call_count, 2
                )
                # 验证通知发送
                self.mock_notification.send.assert_called_once()

        asyncio.run(_run())

    # ---------- 无孤儿条件单 ----------

    def test_no_orphan_orders(self):
        """测试：所有条件单对应持仓都在时跳过"""
        async def _run():
            open_orders = [
                {"symbol": "BTCUSDT", "algo_id": 1001, "strategy_name": "btc_eth"},
                {"symbol": "ETHUSDT", "algo_id": 2001, "strategy_name": "btc_eth"},
            ]
            # 模拟交易所持仓：包含 BTCUSDT 和 ETHUSDT
            exchange_positions = [
                {"symbol": "BTCUSDT", "positionAmt": "0.5"},
                {"symbol": "ETHUSDT", "positionAmt": "2.0"},
            ]
            self.mock_binance.get_position = AsyncMock(return_value=exchange_positions)

            get_open_orders_path = "shared.condition_orders.get_open_orders"
            with patch(get_open_orders_path, AsyncMock(return_value=open_orders)):
                await self.strategy._do_startup_orphan_cleanup()

                # 验证未执行取消
                self.mock_binance.cancel_all_algo_orders.assert_not_called()
                # 验证未发送通知
                self.mock_notification.send.assert_not_called()

        asyncio.run(_run())

    # ---------- 批量取消失败，降级到逐个取消 ----------

    def test_batch_cancel_fallback_to_individual(self):
        """测试：批量取消失败时降级到逐个取消"""
        async def _run():
            open_orders = [
                {"symbol": "BTCUSDT", "algo_id": 1001, "strategy_name": "btc_eth"},
                {"symbol": "BTCUSDT", "algo_id": 1002, "strategy_name": "btc_eth"},
            ]
            exchange_positions = [
                {"symbol": "SOLUSDT", "positionAmt": "1.0"},
            ]
            self.mock_binance.get_position = AsyncMock(return_value=exchange_positions)
            # 批量取消抛出异常
            self.mock_binance.cancel_all_algo_orders = AsyncMock(
                side_effect=Exception("Batch cancel failed")
            )
            # 逐个取消成功
            self.mock_binance.cancel_algo_order = AsyncMock()

            get_open_orders_path = "shared.condition_orders.get_open_orders"
            with patch(get_open_orders_path, AsyncMock(return_value=open_orders)):
                await self.strategy._do_startup_orphan_cleanup()

                # 验证逐个取消被调用（每个孤儿条件单）
                self.assertEqual(
                    self.mock_binance.cancel_algo_order.call_count, 2
                )
                # 验证通知发送
                self.mock_notification.send.assert_called_once()

        asyncio.run(_run())

    # ---------- 微尘持仓忽略 ----------

    def test_ignore_dust_positions(self):
        """测试：微尘持仓不视为有效持仓，关联条件单仍视为孤儿"""
        async def _run():
            open_orders = [
                {"symbol": "BTCUSDT", "algo_id": 1001, "strategy_name": "btc_eth"},
            ]
            # BTCUSDT 只有微尘持仓（0.000001 < min_position_amt=0.00001）
            exchange_positions = [
                {"symbol": "BTCUSDT", "positionAmt": "0.000001"},
            ]
            self.mock_binance.get_position = AsyncMock(return_value=exchange_positions)

            get_open_orders_path = "shared.condition_orders.get_open_orders"
            with patch(get_open_orders_path, AsyncMock(return_value=open_orders)):
                await self.strategy._do_startup_orphan_cleanup()

                # 验证孤儿条件单被取消（微尘持仓被忽略）
                self.mock_binance.cancel_all_algo_orders.assert_called_once_with("BTCUSDT")

        asyncio.run(_run())

    # ---------- 清理结果发送通知 ----------

    def test_cleanup_notification_sent(self):
        """测试：清理结果发送通知"""
        async def _run():
            open_orders = [
                {"symbol": "BTCUSDT", "algo_id": 1001, "strategy_name": "btc_eth"},
            ]
            exchange_positions = [
                {"symbol": "SOLUSDT", "positionAmt": "1.0"},
            ]
            self.mock_binance.get_position = AsyncMock(return_value=exchange_positions)
            self.mock_binance.cancel_all_algo_orders = AsyncMock()

            get_open_orders_path = "shared.condition_orders.get_open_orders"
            with patch(get_open_orders_path, AsyncMock(return_value=open_orders)):
                await self.strategy._do_startup_orphan_cleanup()

                # 验证通知发送
                self.mock_notification.send.assert_called_once()
                call_args = self.mock_notification.send.call_args
                self.assertIn("孤儿条件单清理", str(call_args))

        asyncio.run(_run())

    # ---------- _startup_orphan_cleanup 超时保护 ----------

    def test_startup_cleanup_timeout(self):
        """测试：启动时孤儿条件单检测超时"""
        async def _run():
            # 模拟超时
            async def slow_cleanup():
                await asyncio.sleep(10)

            # 设置超时时间为 0.01 秒
            with patch.object(self.strategy, '_get_cancel_retry_config',
                              return_value=0.01):
                # 替换 _do_startup_orphan_cleanup 为慢方法
                original = self.strategy._do_startup_orphan_cleanup
                self.strategy._do_startup_orphan_cleanup = slow_cleanup

                # 不应抛出异常
                await self.strategy._startup_orphan_cleanup()

                # 恢复
                self.strategy._do_startup_orphan_cleanup = original

        asyncio.run(_run())


# ============================================================================
# 第四部分：条件单成交记录测试
# ============================================================================

class TestExecutedConditionalOrder(unittest.TestCase):
    """测试 _record_executed_conditional_order 方法"""

    def setUp(self):
        """创建模拟的策略实例"""
        self.config = load_config()
        self.risk_config = self.config['strategy']['risk']

        self.mock_binance = MagicMock()
        self.mock_binance.get_account_info = AsyncMock()
        self.mock_kline = MagicMock()
        self.mock_notification = MagicMock()
        self.mock_notification.send = AsyncMock()
        self.mock_notification.send_error_notification = AsyncMock()
        self.mock_notification.send_trade_notification = AsyncMock()
        self.mock_db = MagicMock()
        self.mock_db.fetch_all = AsyncMock(return_value=[])
        self.mock_db.execute = AsyncMock()

        from strategies.btc_eth.strategy import BTCEthStrategy

        self.strategy = BTCEthStrategy(
            config=self.config,
            binance_client=self.mock_binance,
            kline_service=self.mock_kline,
            notification_client=self.mock_notification,
            db_manager=self.mock_db
        )

    # ---------- 无数据库管理器 ----------

    def test_no_db_manager(self):
        """测试：无数据库管理器时跳过"""
        async def _run():
            strategy_no_db = self.strategy.__class__(
                config=self.config,
                binance_client=self.mock_binance,
                kline_service=self.mock_kline,
                notification_client=self.mock_notification,
                db_manager=None
            )
            await strategy_no_db._record_executed_conditional_order(
                "BTCUSDT", "stop_loss", 12345
            )
            # 不会调用数据库
            self.mock_db.execute.assert_not_called()

        asyncio.run(_run())

    # ---------- 条件单已执行（-2021）时更新状态为 EXECUTED ----------

    def test_mark_order_executed_on_2021(self):
        """测试：条件单已执行（-2021）时更新状态为 EXECUTED"""
        async def _run():
            with patch('shared.condition_orders.mark_order_executed', AsyncMock()) as mock_mark:
                await self.strategy._record_executed_conditional_order(
                    "BTCUSDT", "stop_loss", 12345
                )

                # 验证 mark_order_executed 被调用
                mock_mark.assert_called_once_with(
                    self.mock_db, algo_id=12345
                )

        asyncio.run(_run())

    # ---------- 查询 trade_records 表确认成交记录 ----------

    def test_query_trade_records(self):
        """测试：查询 trade_records 表确认成交记录"""
        async def _run():
            with patch('shared.condition_orders.mark_order_executed', AsyncMock()):
                self.mock_db.fetch_all = AsyncMock(return_value=[
                    {"id": 1, "order_id": 10001, "side": "SELL",
                     "quantity": "0.5", "price": "70000", "executed_at": datetime.now()}
                ])

                await self.strategy._record_executed_conditional_order(
                    "BTCUSDT", "stop_loss", 12345
                )

                # 验证查询了 trade_records
                self.mock_db.fetch_all.assert_called_once()
                call_args = self.mock_db.fetch_all.call_args
                sql = call_args[0][0]
                self.assertIn("trade_records", sql)
                self.assertIn("BTCUSDT", str(call_args))

        asyncio.run(_run())

    # ---------- 无成交记录时记录警告 ----------

    def test_no_trade_records_warning(self):
        """测试：条件单已执行但无成交记录时记录日志（不抛异常）"""
        async def _run():
            with patch('shared.condition_orders.mark_order_executed', AsyncMock()):
                self.mock_db.fetch_all = AsyncMock(return_value=[])

                # 不应抛出异常
                await self.strategy._record_executed_conditional_order(
                    "BTCUSDT", "stop_loss", 12345
                )

                # 验证查询被执行
                self.mock_db.fetch_all.assert_called_once()

        asyncio.run(_run())

    # ---------- 数据库异常不被传播 ----------

    def test_db_error_handled_gracefully(self):
        """测试：数据库异常被优雅处理，不传播"""
        async def _run():
            self.mock_db.fetch_all = AsyncMock(side_effect=Exception("DB connection lost"))
            with patch('shared.condition_orders.mark_order_executed', AsyncMock()):
                # 不应抛出异常
                await self.strategy._record_executed_conditional_order(
                    "BTCUSDT", "stop_loss", 12345
                )

        asyncio.run(_run())


# ============================================================================
# 第五部分：集成测试
# ============================================================================

class TestIntegration(unittest.TestCase):
    """集成测试：验证多个 v6.23 功能同时工作的场景"""

    def setUp(self):
        self.config = load_config()

        self.mock_binance = MagicMock()
        self.mock_binance.get_account_info = AsyncMock()
        self.mock_binance.cancel_algo_order = AsyncMock()
        self.mock_binance.cancel_order = AsyncMock()
        self.mock_binance.cancel_all_algo_orders = AsyncMock()
        self.mock_binance.get_position = AsyncMock(return_value=[])
        self.mock_kline = MagicMock()
        self.mock_notification = MagicMock()
        self.mock_notification.send = AsyncMock()
        self.mock_notification.send_error_notification = AsyncMock()
        self.mock_notification.send_trade_notification = AsyncMock()
        self.mock_db = MagicMock()
        self.mock_db.fetch_all = AsyncMock(return_value=[])
        self.mock_db.execute = AsyncMock()

        from strategies.btc_eth.strategy import BTCEthStrategy, PositionState
        self.strategy = BTCEthStrategy(
            config=self.config,
            binance_client=self.mock_binance,
            kline_service=self.mock_kline,
            notification_client=self.mock_notification,
            db_manager=self.mock_db
        )
        self.PositionState = PositionState

    def test_retry_then_cleanup_residual(self):
        """
        测试：重试机制 + 残余清理集成场景
        场景：条件单取消失败 -> 重试 -> 重试成功 -> 残余清理删除持仓
        """
        async def _run():
            from shared.binance_api import BinanceAPIError
            # 第一次：取消失败
            self.mock_binance.cancel_algo_order = AsyncMock(
                side_effect=BinanceAPIError(-1001, "Internal error")
            )

            pos = self.PositionState()
            pos.entry_price = Decimal('60000')
            pos.entry_time = datetime.now() - timedelta(hours=24)
            pos.direction = "LONG"
            pos.current_quantity = Decimal('0')  # 已平仓
            pos.stop_loss_order_id = 12345
            pos.cancel_pending = True  # 正常流程：第一次取消失败后 cancel_pending 设为 True
            pos.cancel_retry_count = {"stop_loss": 1}  # 已有一次重试
            pos.last_retry_cycle = 0
            pos.first_retry_time = None
            self.strategy.positions["BTCUSDT"] = pos

            # 执行重试（取消失败，重试计数递增）
            await self.strategy._cleanup_position_orders("BTCUSDT", pos)
            self.assertEqual(pos.cancel_retry_count.get("stop_loss"), 2)
            self.assertTrue(pos.cancel_pending)

            # 模拟第二个周期：重试成功
            self.strategy._cycle_count = 1
            self.mock_binance.cancel_algo_order = AsyncMock()

            await self.strategy._retry_pending_cancellations()
            self.assertIsNone(pos.stop_loss_order_id)
            self.assertNotIn("stop_loss", pos.cancel_retry_count)

            # 残余清理应删除持仓
            await self.strategy._cleanup_residual_orders()
            self.assertNotIn("BTCUSDT", self.strategy.positions)

        asyncio.run(_run())

    def test_max_retry_then_cleanup_residual(self):
        """
        测试：重试达到上限 + 残余清理集成场景
        场景：条件单一直取消失败 -> 达到最大重试次数 -> 发送通知 -> 清理删除持仓
        """
        async def _run():
            from shared.binance_api import BinanceAPIError
            self.mock_binance.cancel_algo_order = AsyncMock(
                side_effect=BinanceAPIError(-1001, "Internal error")
            )

            pos = self.PositionState()
            pos.entry_price = Decimal('60000')
            pos.entry_time = datetime.now() - timedelta(hours=24)
            pos.direction = "LONG"
            pos.current_quantity = Decimal('0')
            pos.stop_loss_order_id = 12345
            pos.cancel_pending = True
            pos.cancel_retry_count = {"stop_loss": 9}  # 第10次达到上限
            pos.last_retry_cycle = 0
            pos.first_retry_time = None
            self.strategy.positions["BTCUSDT"] = pos

            # 执行清理（达到最大重试次数）
            await self.strategy._cleanup_position_orders("BTCUSDT", pos)

            # 验证发送了超时通知
            self.mock_notification.send_error_notification.assert_called_once()
            # 验证 order_id 被清除
            self.assertIsNone(pos.stop_loss_order_id)
            # 验证重试计数被清除
            self.assertNotIn("stop_loss", pos.cancel_retry_count)

            # 残余清理应删除持仓
            await self.strategy._cleanup_residual_orders()
            self.assertNotIn("BTCUSDT", self.strategy.positions)

        asyncio.run(_run())

    def test_startup_cleanup_with_retry_context(self):
        """
        测试：启动时孤儿检测 + 重试机制集成场景
        场景：启动时清理孤儿条件单 -> 后续周期中正常取消重试
        """
        async def _run():
            # 启动时：无 OPEN 条件单，跳过
            get_open_orders_path = "shared.condition_orders.get_open_orders"
            with patch(get_open_orders_path, AsyncMock(return_value=[])):
                await self.strategy._do_startup_orphan_cleanup()
                self.mock_binance.get_position.assert_not_called()

            # 后续周期：正常取消重试
            from shared.binance_api import BinanceAPIError
            self.mock_binance.cancel_algo_order = AsyncMock(
                side_effect=BinanceAPIError(-1001, "Internal error")
            )

            pos = self.PositionState()
            pos.entry_price = Decimal('60000')
            pos.entry_time = datetime.now() - timedelta(hours=24)
            pos.direction = "LONG"
            pos.current_quantity = Decimal('0')
            pos.stop_loss_order_id = 12345
            pos.cancel_pending = True
            pos.cancel_retry_count = {"stop_loss": 1}
            pos.last_retry_cycle = 0
            pos.first_retry_time = None
            self.strategy.positions["BTCUSDT"] = pos

            await self.strategy._cleanup_position_orders("BTCUSDT", pos)
            self.assertEqual(pos.cancel_retry_count.get("stop_loss"), 2)

        asyncio.run(_run())


# ============================================================================
# 运行入口
# ============================================================================

if __name__ == '__main__':
    print("=" * 70)
    print("  v6.23 孤儿条件单修复专项测试套件")
    print("  测试内容：配置契约 | 取消重试 | 启动孤儿检测 | 成交记录 | 集成")
    print("=" * 70)

    unittest.main(verbosity=2, argv=['test_v23_orphan_cleanup.py'])