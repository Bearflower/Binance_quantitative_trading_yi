"""
realized_pnl 修复代码单元测试

测试目标：验证 realized_pnl 回写相关修复代码的正确性

覆盖场景：
1. TradeLogger.update_realized_pnl 方法增强（新增降级匹配模式）
2. TradeLogger 表结构 DDL 和 ALTER 语句（兼容已存在的生产表）
3. NewCoinStrategy._weekly_review 周报 SQL 修正（created_at → executed_at）
4. NewCoinStrategy._monitor_positions 平仓后回写逻辑
5. NewCoinStrategy._monitor_positions 平仓单查询 SQL 修复

配置参数（来自 config.yaml）：
- strategy.db_strategy_name: '新币做空策略'

关键代码位置：
- shared/trade_logger.py 的 update_realized_pnl: 第221-341行
- shared/trade_logger.py 的 _parse_update_count: 第343-363行
- shared/trade_logger.py 的 _CREATE_TABLE_DDL: 第68-83行
- shared/trade_logger.py 的 _ALTER_ADD_REALIZED_PNL_DDL: 第95-99行
- shared/trade_logger.py 的 ensure_table_exists: 第120-144行
- strategies/new_coin/strategy.py 的周报SQL: 第840-873行
- strategies/new_coin/strategy.py 的平仓后回写: 第982-1034行
"""
from unittest.mock import AsyncMock, MagicMock, call
from decimal import Decimal
from datetime import datetime, timezone, timedelta

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
        trade_logger.db.execute = AsyncMock(return_value="UPDATE 1")

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
        assert "WHERE order_id = $2 AND strategy = $3 AND side = $4" in sql
        # 验证参数顺序：str(realized_pnl)、order_id、strategy_name、match_side
        assert call_args.args[1] == "100.50"
        assert call_args.args[2] == "123456"
        assert call_args.args[3] == "新币做空策略"
        assert call_args.args[4] == "BUY"

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
        # 参数3（strategy_name）应该是 self.strategy_name
        assert call_args.args[3] == trade_logger.strategy_name
        assert call_args.args[3] == "新币做空策略"
        # 参数4（match_side）默认应为 'BUY'
        assert call_args.args[4] == "BUY"

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
        assert call_kwargs["side"] == "BUY", "做空平仓方向应为 BUY"
        assert call_kwargs["symbol"] == "BTCUSDT"
        assert "executed_at" in call_kwargs

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


# ===========================================================================
# F1: update_realized_pnl 增强测试
# ===========================================================================


class TestUpdateRealizedPnlEnhanced:
    """update_realized_pnl 增强功能测试（降级匹配模式）"""

    @pytest.fixture
    def trade_logger(self):
        """构造 TradeLogger 实例，mock 数据库依赖"""
        db = AsyncMock()
        return TradeLogger(db_manager=db, strategy_name="新币做空策略")

    # ------------------------------------------------------------------
    # 模式一：order_id 精确匹配
    # ------------------------------------------------------------------

    async def test_mode1_success(self, trade_logger):
        """模式一成功：order_id 非空，mock 返回 UPDATE 1，验证返回 True

        验证：
        - 返回值为 True
        - db.execute 仅被调用一次（模式一命中后直接返回，不执行模式二）
        """
        trade_logger.db.execute = AsyncMock(return_value="UPDATE 1")

        result = await trade_logger.update_realized_pnl(
            order_id="ORDER001",
            realized_pnl=Decimal("50.25")
        )

        assert result is True
        assert trade_logger.db.execute.call_count == 1

    async def test_mode1_success_strategy_override(self, trade_logger):
        """模式一成功：strategy 参数覆盖 self.strategy_name

        验证：传入 strategy 参数时，SQL 中的策略名称使用传入值而非默认值
        """
        trade_logger.db.execute = AsyncMock(return_value="UPDATE 1")

        result = await trade_logger.update_realized_pnl(
            order_id="ORDER001",
            realized_pnl=Decimal("50.25"),
            strategy="其他策略"
        )

        assert result is True
        call_args = trade_logger.db.execute.call_args
        assert call_args.args[3] == "其他策略"  # strategy_name
        assert call_args.args[4] == "BUY"  # match_side 默认值

    # ------------------------------------------------------------------
    # 模式一失败后降级匹配
    # ------------------------------------------------------------------

    async def test_mode1_fail_then_fallback(self, trade_logger):
        """模式一失败后降级：第一次返回 UPDATE 0，第二次返回 UPDATE 1

        验证：
        - 返回值为 True
        - db.execute 被调用两次（模式一未命中，降级匹配命中）
        """
        executed_at = datetime.now()
        trade_logger.db.execute = AsyncMock(side_effect=["UPDATE 0", "UPDATE 1"])

        result = await trade_logger.update_realized_pnl(
            order_id="ORDER001",
            realized_pnl=Decimal("50.25"),
            symbol="BTCUSDT",
            executed_at=executed_at
        )

        assert result is True
        assert trade_logger.db.execute.call_count == 2

    async def test_fallback_sql_correctness(self, trade_logger):
        """降级匹配 SQL 正确性

        验证降级匹配的 SQL 包含：
        - WHERE id = (SELECT id FROM ...) 子查询结构
        - symbol 条件
        - side 条件（动态传入）
        - executed_at BETWEEN 时间范围
        - order_id IS NULL
        - realized_pnl IS NULL
        - order_type NOT LIKE 'STOP%'
        - ORDER BY executed_at DESC LIMIT 1
        """
        executed_at = datetime(2026, 7, 27, 10, 0, 0)
        trade_logger.db.execute = AsyncMock(side_effect=["UPDATE 0", "UPDATE 1"])

        await trade_logger.update_realized_pnl(
            order_id="ORDER001",
            realized_pnl=Decimal("50.25"),
            symbol="BTCUSDT",
            executed_at=executed_at,
            time_window=300
        )

        # 第二次调用是降级匹配 SQL
        fallback_sql = trade_logger.db.execute.call_args_list[1].args[0]
        fallback_args = trade_logger.db.execute.call_args_list[1].args

        assert "UPDATE trading.trade_records" in fallback_sql
        assert "SET realized_pnl = $1" in fallback_sql
        assert "WHERE id = (" in fallback_sql
        assert "SELECT id FROM trading.trade_records" in fallback_sql
        assert "symbol = $3" in fallback_sql
        assert "side = $4" in fallback_sql
        assert "executed_at BETWEEN" in fallback_sql
        assert "order_id IS NULL" in fallback_sql
        assert "realized_pnl IS NULL" in fallback_sql
        assert "order_type NOT LIKE" in fallback_sql and "STOP" in fallback_sql or "STOP" in fallback_sql
        assert "ORDER BY executed_at DESC" in fallback_sql
        assert "LIMIT 1" in fallback_sql

        # 验证降级匹配参数：pnl, strategy, symbol, match_side, start_time, end_time
        assert fallback_args[1] == "50.25"
        assert fallback_args[2] == "新币做空策略"
        assert fallback_args[3] == "BTCUSDT"
        assert fallback_args[4] == "BUY"  # match_side 默认值

        # 验证时间窗口：前后各 300 秒
        start_time = fallback_args[5]
        end_time = fallback_args[6]
        assert isinstance(start_time, datetime)
        assert isinstance(end_time, datetime)
        # 开始时间 = 目标时间 - 300秒
        expected_start = executed_at - timedelta(seconds=300)
        expected_end = executed_at + timedelta(seconds=300)
        assert start_time == expected_start
        assert end_time == expected_end

    async def test_fallback_sql_uses_correct_time_window(self, trade_logger):
        """降级匹配使用自定义 time_window 参数

        验证：传入 time_window=600 时，时间窗口为前后各 600 秒
        """
        executed_at = datetime(2026, 7, 27, 10, 0, 0)
        trade_logger.db.execute = AsyncMock(side_effect=["UPDATE 0", "UPDATE 1"])

        await trade_logger.update_realized_pnl(
            order_id="ORDER001",
            realized_pnl=Decimal("50.25"),
            symbol="BTCUSDT",
            executed_at=executed_at,
            time_window=600
        )

        fallback_args = trade_logger.db.execute.call_args_list[1].args
        # 参数顺序：pnl, strategy, symbol, match_side, start_time, end_time
        start_time = fallback_args[5]
        end_time = fallback_args[6]
        expected_start = executed_at - timedelta(seconds=600)
        expected_end = executed_at + timedelta(seconds=600)
        assert start_time == expected_start
        assert end_time == expected_end

    # ------------------------------------------------------------------
    # order_id 为空直接降级
    # ------------------------------------------------------------------

    async def test_empty_order_id_fallback(self, trade_logger):
        """order_id 为空时跳过模式一，直接执行模式二

        验证：
        - order_id="" 时，不执行模式一 SQL
        - 如果有 symbol 和 executed_at，直接执行降级匹配
        - 返回 True
        """
        executed_at = datetime.now()
        trade_logger.db.execute = AsyncMock(return_value="UPDATE 1")

        result = await trade_logger.update_realized_pnl(
            order_id="",
            realized_pnl=Decimal("100"),
            symbol="BTCUSDT",
            executed_at=executed_at
        )

        assert result is True
        # 应仅调用一次（模式二），且 SQL 是降级匹配 SQL
        assert trade_logger.db.execute.call_count == 1
        fallback_sql = trade_logger.db.execute.call_args.args[0]
        assert "order_id IS NULL" in fallback_sql

    async def test_empty_order_id_no_params_no_call(self, trade_logger):
        """order_id 为空且无 symbol/executed_at 时，不执行任何 SQL

        验证：返回 False，db.execute 不被调用
        """
        trade_logger.db.execute = AsyncMock(return_value="UPDATE 1")

        result = await trade_logger.update_realized_pnl(
            order_id="",
            realized_pnl=Decimal("100")
        )

        assert result is False
        trade_logger.db.execute.assert_not_called()

    # ------------------------------------------------------------------
    # 降级匹配参数不完整
    # ------------------------------------------------------------------

    async def test_fallback_missing_symbol(self, trade_logger):
        """降级匹配缺少 symbol 参数时返回 False

        验证：order_id 非空但模式一未命中，缺少 symbol 时不执行降级匹配
        """
        executed_at = datetime.now()
        trade_logger.db.execute = AsyncMock(return_value="UPDATE 0")

        result = await trade_logger.update_realized_pnl(
            order_id="ORDER001",
            realized_pnl=Decimal("100"),
            symbol=None,
            executed_at=executed_at
        )

        assert result is False
        # 仅执行模式一（1次调用）
        assert trade_logger.db.execute.call_count == 1

    async def test_fallback_missing_executed_at(self, trade_logger):
        """降级匹配缺少 executed_at 参数时返回 False

        验证：order_id 非空但模式一未命中，缺少 executed_at 时不执行降级匹配
        """
        trade_logger.db.execute = AsyncMock(return_value="UPDATE 0")

        result = await trade_logger.update_realized_pnl(
            order_id="ORDER001",
            realized_pnl=Decimal("100"),
            symbol="BTCUSDT",
            executed_at=None
        )

        assert result is False
        assert trade_logger.db.execute.call_count == 1

    async def test_fallback_both_missing(self, trade_logger):
        """降级匹配缺少 symbol 和 executed_at 时返回 False

        验证：order_id 为空且无 symbol/executed_at，不执行任何 SQL
        """
        trade_logger.db.execute = AsyncMock()

        result = await trade_logger.update_realized_pnl(
            order_id="",
            realized_pnl=Decimal("100")
        )

        assert result is False
        trade_logger.db.execute.assert_not_called()

    # ------------------------------------------------------------------
    # 数据库异常
    # ------------------------------------------------------------------

    async def test_db_exception(self, trade_logger):
        """数据库异常时返回 False，不影响主流程

        验证：异常被内部捕获，不向上抛出
        """
        trade_logger.db.execute = AsyncMock(
            side_effect=Exception("DB connection error")
        )

        # 不应抛出异常
        result = await trade_logger.update_realized_pnl(
            order_id="ORDER001",
            realized_pnl=Decimal("100")
        )

        assert result is False

    async def test_fallback_db_exception(self, trade_logger):
        """降级匹配时数据库异常，返回 False

        验证：模式一返回 UPDATE 0，模式二抛异常，捕获后返回 False
        """
        executed_at = datetime.now()
        trade_logger.db.execute = AsyncMock(
            side_effect=["UPDATE 0", Exception("DB error on fallback")]
        )

        # 不应抛出异常
        result = await trade_logger.update_realized_pnl(
            order_id="ORDER001",
            realized_pnl=Decimal("100"),
            symbol="BTCUSDT",
            executed_at=executed_at
        )

        assert result is False

    # ------------------------------------------------------------------
    # _parse_update_count 辅助方法
    # ------------------------------------------------------------------

    def test_parse_update_count_normal(self):
        """_parse_update_count 正常解析 UPDATE N"""
        assert TradeLogger._parse_update_count("UPDATE 1") == 1
        assert TradeLogger._parse_update_count("UPDATE 0") == 0
        assert TradeLogger._parse_update_count("UPDATE 999") == 999

    def test_parse_update_count_invalid(self):
        """_parse_update_count 解析异常返回 0"""
        assert TradeLogger._parse_update_count(None) == 0
        assert TradeLogger._parse_update_count("") == 0
        assert TradeLogger._parse_update_count("NOT_UPDATE") == 0
        assert TradeLogger._parse_update_count("UPDATE") == 0
        assert TradeLogger._parse_update_count("UPDATE ABC") == 0
        assert TradeLogger._parse_update_count(123) == 0
        assert TradeLogger._parse_update_count("") == 0


# ===========================================================================
# F3: new_coin SQL 修复测试
# ===========================================================================


class TestNewCoinSQLFix:
    """new_coin 策略平仓单查询 SQL 修复测试"""

    @pytest.fixture
    def strategy(self):
        """构造 NewCoinStrategy 实例，mock 所有外部依赖"""
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

        s.notification_client = AsyncMock()
        s.db = AsyncMock()
        s.binance_client = AsyncMock()
        s.kline_service = AsyncMock()

        s.trading_executor = MagicMock()
        s.trading_executor.position_tracking = {}
        s.trading_executor.check_position_management = AsyncMock(return_value=None)
        s.trading_executor.cancel_all_algo_orders = AsyncMock(
            return_value={"failed": 0, "cancelled": 0}
        )

        return s

    async def test_close_order_sql_has_order_id_not_null(self, strategy):
        """平仓单查询 SQL 包含 order_id IS NOT NULL

        验证：修复后的 SQL 使用 order_id IS NOT NULL 过滤掉 buy 单（开仓单）
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
        strategy.binance_client.trade_logger = MagicMock()
        strategy.binance_client.trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy._save_state = AsyncMock()
        strategy._check_max_drawdown = AsyncMock()
        strategy._check_consecutive_loss_pause = AsyncMock()
        strategy._add_to_stop_loss_monitor = AsyncMock()

        await strategy._monitor_positions()

        # 验证 fetch_one SQL 包含 order_id IS NOT NULL
        call_args = strategy.db.fetch_one.call_args
        sql = call_args.args[0]
        assert "order_id IS NOT NULL" in sql, "SQL 应包含 order_id IS NOT NULL 过滤条件"

    async def test_close_order_sql_has_between_and_not_null(self, strategy):
        """平仓单查询 SQL 使用 BETWEEN 时间窗口和 order_id IS NOT NULL

        验证：SQL 使用 BETWEEN 限制时间窗口避免跨交易匹配，使用 order_id IS NOT NULL 排除无条件单 ID 的记录
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
        strategy.binance_client.trade_logger = MagicMock()
        strategy.binance_client.trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy._save_state = AsyncMock()
        strategy._check_max_drawdown = AsyncMock()
        strategy._check_consecutive_loss_pause = AsyncMock()
        strategy._add_to_stop_loss_monitor = AsyncMock()

        await strategy._monitor_positions()

        call_args = strategy.db.fetch_one.call_args
        sql = call_args.args[0]
        assert "BETWEEN" in sql, "SQL 应使用 BETWEEN 时间窗口限制跨交易匹配"
        assert "order_id IS NOT NULL" in sql, "SQL 应要求 order_id 不为空"
        assert "LIMIT 1" in sql, "SQL 应使用 LIMIT 1 取最近一条记录"

    async def test_close_order_sql_has_limit_1(self, strategy):
        """平仓单查询 SQL 包含 LIMIT 1

        验证：SQL 使用 LIMIT 1 确保多条记录时只取最近一条
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
        strategy.binance_client.trade_logger = MagicMock()
        strategy.binance_client.trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy._save_state = AsyncMock()
        strategy._check_max_drawdown = AsyncMock()
        strategy._check_consecutive_loss_pause = AsyncMock()
        strategy._add_to_stop_loss_monitor = AsyncMock()

        await strategy._monitor_positions()

        call_args = strategy.db.fetch_one.call_args
        sql = call_args.args[0]
        assert "LIMIT 1" in sql or "limit 1" in sql, "SQL 应包含 LIMIT 1 限制取最近一条记录"

    async def test_close_order_multiple_records_limit_1(self, strategy):
        """多条记录时 fetch_one 使用 LIMIT 1 确保只返回一条

        验证：fetch_one 的语义 + LIMIT 1 确保即使有多条记录也只处理最近一条
        """
        strategy.positions = {
            "BTCUSDT": {
                "entry_price": 100.0,
                "entry_time": "2026-06-01T00:00:00",
            }
        }
        strategy.binance_client.get_position = AsyncMock(return_value=[])
        strategy._get_position_pnl = AsyncMock(return_value=50.0)
        # mock 返回一条记录（fetch_one 本身只返回一条，但 SQL 有 LIMIT 1 确保）
        strategy.db.fetch_one = AsyncMock(return_value={"order_id": "CLOSE001"})
        strategy.binance_client.trade_logger = MagicMock()
        strategy.binance_client.trade_logger.update_realized_pnl = AsyncMock(return_value=True)
        strategy._save_state = AsyncMock()
        strategy._check_max_drawdown = AsyncMock()
        strategy._check_consecutive_loss_pause = AsyncMock()
        strategy._add_to_stop_loss_monitor = AsyncMock()

        await strategy._monitor_positions()

        # 验证 SQL 包含 ORDER BY executed_at DESC
        call_args = strategy.db.fetch_one.call_args
        sql = call_args.args[0]
        assert "ORDER BY" in sql and "executed_at" in sql and "DESC" in sql, \
            "SQL 应包含 ORDER BY executed_at DESC 确保取最新记录"

        # 验证传入的正确的 order_id
        strategy.binance_client.trade_logger.update_realized_pnl.assert_called_once()
        call_kwargs = strategy.binance_client.trade_logger.update_realized_pnl.call_args.kwargs
        assert call_kwargs["order_id"] == "CLOSE001"

    async def test_close_order_no_record_found(self, strategy):
        """未找到平仓订单记录时跳过回写

        验证：fetch_one 返回 None 时，不调用 update_realized_pnl，主流程继续
        """
        strategy.positions = {
            "BTCUSDT": {
                "entry_price": 100.0,
                "entry_time": "2026-06-01T00:00:00",
            }
        }
        strategy.binance_client.get_position = AsyncMock(return_value=[])
        strategy._get_position_pnl = AsyncMock(return_value=50.0)
        # fetch_one 返回 None（无记录）
        strategy.db.fetch_one = AsyncMock(return_value=None)
        strategy.binance_client.trade_logger = MagicMock()
        strategy.binance_client.trade_logger.update_realized_pnl = AsyncMock()
        strategy._save_state = AsyncMock()
        strategy._check_max_drawdown = AsyncMock()
        strategy._check_consecutive_loss_pause = AsyncMock()
        strategy._add_to_stop_loss_monitor = AsyncMock()

        await strategy._monitor_positions()

        # 不应调用 update_realized_pnl
        strategy.binance_client.trade_logger.update_realized_pnl.assert_not_called()
        # 持仓应被删除
        assert "BTCUSDT" not in strategy.positions

    async def test_close_order_has_no_order_id(self, strategy):
        """fetch_one 返回记录但 order_id 为空时跳过回写

        验证：返回记录中 order_id 为空时，不调用 update_realized_pnl
        """
        strategy.positions = {
            "BTCUSDT": {
                "entry_price": 100.0,
                "entry_time": "2026-06-01T00:00:00",
            }
        }
        strategy.binance_client.get_position = AsyncMock(return_value=[])
        strategy._get_position_pnl = AsyncMock(return_value=50.0)
        # fetch_one 返回记录但 order_id 为空
        strategy.db.fetch_one = AsyncMock(return_value={"order_id": None})
        strategy.binance_client.trade_logger = MagicMock()
        strategy.binance_client.trade_logger.update_realized_pnl = AsyncMock()
        strategy._save_state = AsyncMock()
        strategy._check_max_drawdown = AsyncMock()
        strategy._check_consecutive_loss_pause = AsyncMock()
        strategy._add_to_stop_loss_monitor = AsyncMock()

        await strategy._monitor_positions()

        # 不应调用 update_realized_pnl
        strategy.binance_client.trade_logger.update_realized_pnl.assert_not_called()
        # 持仓应被删除
        assert "BTCUSDT" not in strategy.positions


# ===========================================================================
# F4: calculate_pnl 静态方法测试
# ===========================================================================


class TestCalculatePnl:
    """TradeLogger.calculate_pnl 静态方法测试"""

    # ==================== LONG 方向 ====================

    def test_long_profit(self):
        """LONG 盈利：entry=100, exit=110, qty=1 => pnl=10"""
        pnl = TradeLogger.calculate_pnl('LONG', Decimal('100'), Decimal('110'), Decimal('1'))
        assert pnl == Decimal('10')

    def test_long_loss(self):
        """LONG 亏损：entry=100, exit=90, qty=1 => pnl=-10"""
        pnl = TradeLogger.calculate_pnl('LONG', Decimal('100'), Decimal('90'), Decimal('1'))
        assert pnl == Decimal('-10')

    # ==================== SHORT 方向 ====================

    def test_short_profit(self):
        """SHORT 盈利：entry=110, exit=100, qty=1 => pnl=10"""
        pnl = TradeLogger.calculate_pnl('SHORT', Decimal('110'), Decimal('100'), Decimal('1'))
        assert pnl == Decimal('10')

    def test_short_loss(self):
        """SHORT 亏损：entry=90, exit=100, qty=1 => pnl=-10"""
        pnl = TradeLogger.calculate_pnl('SHORT', Decimal('90'), Decimal('100'), Decimal('1'))
        assert pnl == Decimal('-10')

    # ==================== 边界情况 ====================

    def test_partial_quantity(self):
        """部分平仓：entry=100, exit=110, qty=0.5 => pnl=5"""
        pnl = TradeLogger.calculate_pnl('LONG', Decimal('100'), Decimal('110'), Decimal('0.5'))
        assert pnl == Decimal('5')

    def test_invalid_direction(self):
        """不支持的持仓方向抛出 ValueError"""
        with pytest.raises(ValueError, match="不支持的持仓方向"):
            TradeLogger.calculate_pnl('INVALID', Decimal('100'), Decimal('110'), Decimal('1'))
