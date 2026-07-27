"""
realized_pnl 修复代码单元测试

测试目标：验证 realized_pnl 回写相关修复代码的正确性

覆盖场景：
1. TradeLogger.update_realized_pnl 方法（回写平仓盈亏到交易记录）
2. TradeLogger 表结构 DDL 和 ALTER 语句（兼容已存在的生产表）
3. NewCoinStrategy._weekly_review 周报 SQL 修正（created_at → executed_at）
4. NewCoinStrategy._monitor_positions 平仓后回写逻辑

配置参数（来自 config.yaml）：
- strategy.db_strategy_name: '新币做空策略'

关键代码位置：
- shared/trade_logger.py 的 update_realized_pnl: 第221-272行
- shared/trade_logger.py 的 _CREATE_TABLE_DDL: 第68-83行
- shared/trade_logger.py 的 _ALTER_ADD_REALIZED_PNL_DDL: 第95-99行
- shared/trade_logger.py 的 ensure_table_exists: 第120-144行
- strategies/new_coin/strategy.py 的周报SQL: 第767-778行
- strategies/new_coin/strategy.py 的平仓后回写: 第887-937行
"""
from unittest.mock import AsyncMock, MagicMock
from decimal import Decimal
from datetime import datetime, timezone

import pytest

from shared.trade_logger import TradeLogger
from strategies.new_coin.strategy import NewCoinStrategy


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def trade_logger():
    """构造 TradeLogger 实例，mock 数据库依赖

    被 mock 的依赖：
    - db_manager: 数据库管理器（AsyncMock）
    """
    db = AsyncMock()
    return TradeLogger(db_manager=db, strategy_name="新币做空策略")


@pytest.fixture
def strategy():
    """构造 NewCoinStrategy 实例，并 mock 所有外部依赖

    被 mock 的依赖：
    - notification_client: 飞书通知客户端（AsyncMock）
    - db: 数据库管理器（AsyncMock）
    - binance_client: 币安客户端（AsyncMock）
    - kline_service: K线服务（AsyncMock）
    - trading_executor: 交易执行器（MagicMock + AsyncMock 方法）
    """
    config = {
        "strategy": {
            "name": "new_coin",
            "db_strategy_name": "新币做空策略",
        },
        "detector": {"check_interval": 300},
        "trading": {
            "max_drawdown": {
                "threshold": 0.15,
                "pause_days": 7,
            }
        },
    }

    s = NewCoinStrategy(config)

    # mock 外部客户端
    s.notification_client = AsyncMock()
    s.db = AsyncMock()
    s.binance_client = AsyncMock()
    s.kline_service = AsyncMock()

    # mock 交易执行器
    s.trading_executor = MagicMock()
    s.trading_executor.position_tracking = {}
    s.trading_executor.check_position_management = AsyncMock(return_value=None)
    s.trading_executor.cancel_all_algo_orders = AsyncMock(
        return_value={"failed": 0, "cancelled": 0}
    )

    return s


# ---------------------------------------------------------------------------
# 测试套件
# ---------------------------------------------------------------------------


class TestRealizedPnlFix:
    """realized_pnl 修复测试套件"""

    # ==================== 1. update_realized_pnl 方法测试 ====================

    async def test_update_realized_pnl_success(self, trade_logger):
        """正常回写成功：order_id 非空、数据库正常时返回 True

        验证：
        - 返回值为 True
        - db.execute 被调用一次
        - SQL 语句包含 UPDATE ... SET realized_pnl ... WHERE order_id ... side='BUY'
        - 参数顺序：realized_pnl、order_id、strategy_name
        """
        trade_logger.db.execute = AsyncMock(return_value=None)

        result = await trade_logger.update_realized_pnl(
            order_id="123456",
            realized_pnl=Decimal("100.50")
        )

        assert result is True
        trade_logger.db.execute.assert_called_once()

        # 验证传入的 SQL 和参数
        call_args = trade_logger.db.execute.call_args
        sql = call_args.args[0]
        assert "UPDATE trading.trade_records" in sql
        assert "SET realized_pnl = $1" in sql
        assert "WHERE order_id = $2 AND strategy = $3 AND side = 'BUY'" in sql
        # 验证参数顺序：str(realized_pnl)、order_id、strategy_name
        assert call_args.args[1] == "100.50"
        assert call_args.args[2] == "123456"
        assert call_args.args[3] == "新币做空策略"

    async def test_update_realized_pnl_empty_order_id(self, trade_logger):
        """order_id 为空时返回 False，不执行 SQL

        修复点：order_id 为空时短路返回，避免无意义的 UPDATE。
        """
        trade_logger.db.execute = AsyncMock(return_value=None)

        result = await trade_logger.update_realized_pnl(
            order_id="",
            realized_pnl=Decimal("100")
        )

        assert result is False
        trade_logger.db.execute.assert_not_called()

    async def test_update_realized_pnl_exception(self, trade_logger):
        """数据库异常时返回 False，不影响主流程

        修复点：异常被内部捕获，不向上抛出，保证平仓主流程不被影响。
        """
        trade_logger.db.execute = AsyncMock(
            side_effect=Exception("DB connection error")
        )

        # 不应抛出异常
        result = await trade_logger.update_realized_pnl(
            order_id="123456",
            realized_pnl=Decimal("100")
        )

        assert result is False

    async def test_update_realized_pnl_strategy_default(self, trade_logger):
        """strategy 参数默认使用 self.strategy_name

        验证：不传 strategy 参数时，SQL 参数中的策略名称使用
        初始化时设置的 self.strategy_name（'新币做空策略'）。
        """
        trade_logger.db.execute = AsyncMock(return_value=None)

        await trade_logger.update_realized_pnl(
            order_id="123456",
            realized_pnl=Decimal("100")
            # 不传 strategy 参数
        )

        call_args = trade_logger.db.execute.call_args
        # 第三个参数（strategy_name）应该是 self.strategy_name
        assert call_args.args[3] == trade_logger.strategy_name
        assert call_args.args[3] == "新币做空策略"

    # ==================== 2. 表结构测试 ====================

    def test_create_table_ddl_has_realized_pnl(self):
        """_CREATE_TABLE_DDL 包含 realized_pnl 列定义

        修复点：新建表时直接包含 realized_pnl 列，新部署环境无需额外 ALTER。
        """
        ddl = TradeLogger._CREATE_TABLE_DDL
        assert "realized_pnl" in ddl
        assert "DECIMAL(20,8)" in ddl

    def test_alter_ddl_has_if_not_exists(self):
        """_ALTER_ADD_REALIZED_PNL_DDL 使用 IF NOT EXISTS

        修复点：兼容已存在的生产表，重复执行 ALTER 不会报错。
        """
        alter_ddl = TradeLogger._ALTER_ADD_REALIZED_PNL_DDL
        assert "ADD COLUMN IF NOT EXISTS" in alter_ddl
        assert "realized_pnl" in alter_ddl

    async def test_ensure_table_exists_executes_alter(self, trade_logger):
        """ensure_table_exists 会执行 ALTER 语句补齐 realized_pnl 列

        验证：ensure_table_exists 调用 execute_ddl 时，
        传入的 DDL 列表中包含 ALTER TABLE ... ADD COLUMN realized_pnl 语句。
        """
        trade_logger.db.execute_ddl = AsyncMock()

        await trade_logger.ensure_table_exists()

        # 验证 execute_ddl 被调用，且包含 ALTER DDL
        executed_ddls = [
            call.args[0] for call in trade_logger.db.execute_ddl.call_args_list
        ]
        assert any(
            "ALTER TABLE" in ddl and "realized_pnl" in ddl
            for ddl in executed_ddls
        ), "ensure_table_exists 未执行 ALTER 语句补齐 realized_pnl 列"

    # ==================== 3. 周报SQL修正测试 ====================

    async def test_weekly_report_sql_uses_executed_at(self, strategy):
        """周报 SQL 使用 executed_at 而非 created_at

        修复点：trade_records 表的时间字段是 executed_at（成交时间），
        周报查询应基于 executed_at 过滤本周记录。
        """
        # mock db.fetch_all 返回空（避免后续通知处理）
        strategy.db.fetch_all = AsyncMock(return_value=[])

        await strategy._weekly_review()

        # 检查传给 fetch_all 的 SQL
        call_args = strategy.db.fetch_all.call_args
        sql = call_args.args[0]
        assert "executed_at" in sql, "周报 SQL 应使用 executed_at 字段"

    async def test_weekly_report_sql_not_uses_created_at(self, strategy):
        """周报 SQL 不包含错误的 created_at 字段

        修复点：created_at 是 orders 表的字段，trade_records 表无此字段，
        使用 created_at 会导致 SQL 执行报错。
        """
        strategy.db.fetch_all = AsyncMock(return_value=[])

        await strategy._weekly_review()

        call_args = strategy.db.fetch_all.call_args
        sql = call_args.args[0]
        assert "created_at" not in sql, "周报 SQL 不应包含 created_at 字段"

    # ==================== 4. 平仓后回写逻辑测试 ====================

    async def test_post_close_update_realized_pnl(self, strategy):
        """pnl 获取成功后调用 update_realized_pnl 回写盈亏

        验证：
        - 平仓后查询到 close_order（含 order_id）
        - trade_logger 存在时调用 update_realized_pnl
        - 传入的 order_id 来自 close_order，realized_pnl 由 pnl 转换为 Decimal
        """
        # 准备持仓（带 entry_time，确保能解析成功进入回写分支）
        strategy.positions = {
            "BTCUSDT": {
                "entry_price": 100.0,
                "entry_time": "2026-06-01T00:00:00",
            }
        }

        # mock 币安返回无持仓（已平仓）
        strategy.binance_client.get_position = AsyncMock(return_value=[])
        # mock pnl 获取成功
        strategy._get_position_pnl = AsyncMock(return_value=50.0)
        # mock 查询平仓订单（返回含 order_id 的记录）
        strategy.db.fetch_one = AsyncMock(return_value={"order_id": "999888"})
        # mock trade_logger
        mock_trade_logger = MagicMock()
        mock_trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy.binance_client.trade_logger = mock_trade_logger
        # mock 其他副作用方法
        strategy._save_state = AsyncMock()
        strategy._check_max_drawdown = AsyncMock()
        strategy._check_consecutive_loss_pause = AsyncMock()
        strategy._add_to_stop_loss_monitor = AsyncMock()

        await strategy._monitor_positions()

        # 验证 update_realized_pnl 被调用
        mock_trade_logger.update_realized_pnl.assert_called_once()
        call_kwargs = mock_trade_logger.update_realized_pnl.call_args.kwargs
        assert call_kwargs["order_id"] == "999888"
        assert call_kwargs["realized_pnl"] == Decimal(str(50.0))

    async def test_post_close_skip_when_pnl_none(self, strategy):
        """pnl 为 None 时跳过回写

        验证：
        - _get_position_pnl 返回 None 时（查不到平仓记录）
        - 不查询 close_order（db.fetch_one 不被调用）
        - 不调用 update_realized_pnl
        - 持仓被删除，主流程 continue
        """
        strategy.positions = {
            "BTCUSDT": {
                "entry_price": 100.0,
                "entry_time": "2026-06-01T00:00:00",
            }
        }

        strategy.binance_client.get_position = AsyncMock(return_value=[])
        # pnl 为 None
        strategy._get_position_pnl = AsyncMock(return_value=None)
        strategy._save_state = AsyncMock()
        # 监视 trade_logger 和 db.fetch_one
        mock_trade_logger = MagicMock()
        mock_trade_logger.update_realized_pnl = AsyncMock()
        strategy.binance_client.trade_logger = mock_trade_logger
        strategy.db.fetch_one = AsyncMock()

        await strategy._monitor_positions()

        # pnl 为 None 时不应查询 close_order，不应调用 update_realized_pnl
        strategy.db.fetch_one.assert_not_called()
        mock_trade_logger.update_realized_pnl.assert_not_called()
        # 持仓应被删除
        assert "BTCUSDT" not in strategy.positions

    async def test_post_close_skip_when_trade_logger_none(self, strategy):
        """trade_logger 为 None 时跳过回写（不抛异常）

        验证：
        - binance_client.trade_logger 为 None 时
        - 不抛出异常（隐式跳过 update_realized_pnl 调用）
        - 主流程继续执行（持仓被删除）
        """
        strategy.positions = {
            "BTCUSDT": {
                "entry_price": 100.0,
                "entry_time": "2026-06-01T00:00:00",
            }
        }

        strategy.binance_client.get_position = AsyncMock(return_value=[])
        strategy._get_position_pnl = AsyncMock(return_value=50.0)
        strategy.db.fetch_one = AsyncMock(return_value={"order_id": "999888"})
        # trade_logger 为 None
        strategy.binance_client.trade_logger = None
        # mock 其他副作用方法
        strategy._save_state = AsyncMock()
        strategy._check_max_drawdown = AsyncMock()
        strategy._check_consecutive_loss_pause = AsyncMock()
        strategy._add_to_stop_loss_monitor = AsyncMock()

        # 不应抛出异常
        await strategy._monitor_positions()

        # 主流程继续，持仓应被删除
        assert "BTCUSDT" not in strategy.positions

    async def test_post_close_log_when_entry_time_missing(self, strategy):
        """entry_time 缺失时记录 warning 日志，不查询 close_order

        验证：
        - entry_time 为 None 时（解析失败）
        - 进入 else 分支记录 warning 日志
        - 不查询 close_order（db.fetch_one 不被调用）
        - 主流程继续执行（持仓被删除）
        """
        strategy.positions = {
            "BTCUSDT": {
                "entry_price": 100.0,
                "entry_time": None,  # 缺失
            }
        }

        strategy.binance_client.get_position = AsyncMock(return_value=[])
        strategy._get_position_pnl = AsyncMock(return_value=50.0)
        strategy.db.fetch_one = AsyncMock()
        # mock 其他副作用方法
        strategy._save_state = AsyncMock()
        strategy._check_max_drawdown = AsyncMock()
        strategy._check_consecutive_loss_pause = AsyncMock()
        strategy._add_to_stop_loss_monitor = AsyncMock()

        await strategy._monitor_positions()

        # entry_time 缺失时不应查询 close_order（进入 else 分支记录 warning）
        strategy.db.fetch_one.assert_not_called()
        # 主流程继续，持仓应被删除
        assert "BTCUSDT" not in strategy.positions
