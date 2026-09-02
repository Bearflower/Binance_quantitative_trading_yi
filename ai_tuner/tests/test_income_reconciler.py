"""
统一 PnL 校准器（IncomeReconciler）单元测试

覆盖用例：
- 时区换算正确性：
  * _to_beijing_naive(0) 返回 1970-01-01 08:00:00（北京时间，无时区）
  * _record_utc_ms 与 _to_beijing_naive 往返一致（北京 naive 时间转回 UTC 毫秒 == 原值）
- 收入记录去重（_dedupe_income）：相同 (symbol, time, income) 只保留一条；不同 symbol 保留
- 出口单判定（_is_exit_order）：STOP/STOP_MARKET/TAKE_PROFIT/TAKE_PROFIT_MARKET 为 True，
  LIMIT/MARKET/PNL_SUMMARY 为 False
- 最佳匹配（_find_best_match）：
  * 窗口内选时间差最小者
  * 出口单获得优先级加成（diff 减去 exit_priority_bonus_ms）
  * 超出 match_window_ms 的不匹配
- run_once 主流程（mock db_manager 和 binance_client）：
  * enabled=False 时直接返回不调用 API
  * 有 income 有 pending 时成功回写并更新进度
  * income 为空时仅更新进度不报错
  * Binance API 抛异常时被捕获，任务不崩溃（记录日志）
- _fetch_pending_records 的 SQL 组装：excluded_strategies 为空/非空两种情况下
  调用 db.fetch_all 的参数正确（验证 NOT IN 占位符生成）

说明：所有用例均使用 mock，不连接真实数据库与 Binance API。
"""

import sys
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")

from ai_tuner.reconciler.income_reconciler import IncomeReconciler, _record_utc_ms, _to_beijing_naive


@pytest.fixture
def base_config():
    """基础配置：使用被测代码的默认参数兜底"""
    return {"reconciler": {}}


@pytest.fixture
def mock_db():
    """创建数据库管理器 mock（所有方法均为 AsyncMock）"""
    db = MagicMock()
    db.execute_ddl = AsyncMock()
    db.execute = AsyncMock()
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    return db


@pytest.fixture
def mock_binance():
    """创建 Binance 客户端 mock（get_income_history 为 AsyncMock）"""
    client = MagicMock()
    client.get_income_history = AsyncMock(return_value=[])
    return client


@pytest.fixture
def reconciler(mock_db, mock_binance, base_config):
    """创建被测对象实例"""
    return IncomeReconciler(mock_db, mock_binance, base_config)


# ============================================================
# 测试 1：时区换算正确性
# ============================================================


class TestTimezone:
    """测试时间换算工具函数"""

    def test_to_beijing_naive_epoch(self):
        """_to_beijing_naive(0) 应返回 1970-01-01 08:00:00（北京时间，无时区）"""
        result = _to_beijing_naive(0)
        assert result == datetime(1970, 1, 1, 8, 0, 0)
        # 必须是无时区（naive）时间，与 trade_records.executed_at 一致
        assert result.tzinfo is None

    def test_record_utc_ms_roundtrip(self):
        """北京 naive 时间转回 UTC 毫秒应等于原值（整秒毫秒值精确往返）"""
        for ms in (1609459200000, 1700000000000, 1755000000000):
            bj_naive = _to_beijing_naive(ms)
            assert _record_utc_ms(bj_naive) == ms

    def test_record_utc_ms_non_datetime_returns_zero(self):
        """非 datetime 输入应返回 0"""
        assert _record_utc_ms(None) == 0
        assert _record_utc_ms("2023-11-15") == 0
        assert _record_utc_ms(1700000000000) == 0


# ============================================================
# 测试 2：收入记录去重
# ============================================================


class TestDedupeIncome:
    """测试 _dedupe_income 收入记录去重"""

    def test_same_symbol_time_income_deduped(self, reconciler):
        """相同 (symbol, time, income) 的重复记录只保留第一条"""
        records = [
            {"symbol": "BTCUSDT", "time": 1700000000000, "income": "1.5"},
            {"symbol": "BTCUSDT", "time": 1700000000000, "income": "1.5"},
            {"symbol": "BTCUSDT", "time": 1700000000000, "income": "1.5"},
        ]
        result = reconciler._dedupe_income(records)
        assert len(result) == 1
        assert result[0]["income"] == "1.5"

    def test_different_symbol_kept(self, reconciler):
        """不同 symbol 的记录应全部保留"""
        records = [
            {"symbol": "BTCUSDT", "time": 1700000000000, "income": "1.5"},
            {"symbol": "ETHUSDT", "time": 1700000000000, "income": "1.5"},
        ]
        result = reconciler._dedupe_income(records)
        assert len(result) == 2

    def test_same_symbol_different_time_kept(self, reconciler):
        """相同 symbol 但 time 不同的记录应保留"""
        records = [
            {"symbol": "BTCUSDT", "time": 1700000000000, "income": "1.5"},
            {"symbol": "BTCUSDT", "time": 1700000000001, "income": "1.5"},
        ]
        result = reconciler._dedupe_income(records)
        assert len(result) == 2


# ============================================================
# 测试 3：出口单判定
# ============================================================


class TestIsExitOrder:
    """测试 _is_exit_order 出口单判定"""

    def test_exit_order_types_true(self):
        """STOP/STOP_MARKET/TAKE_PROFIT/TAKE_PROFIT_MARKET 应判定为出口单"""
        for order_type in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
            assert IncomeReconciler._is_exit_order(order_type) is True

    def test_non_exit_order_types_false(self):
        """LIMIT/MARKET/PNL_SUMMARY 不应判定为出口单"""
        for order_type in ("LIMIT", "MARKET", "PNL_SUMMARY"):
            assert IncomeReconciler._is_exit_order(order_type) is False

    def test_case_insensitive_and_empty(self):
        """大小写不敏感；None/空串返回 False"""
        assert IncomeReconciler._is_exit_order("stop") is True
        assert IncomeReconciler._is_exit_order("Take_Profit_Market") is True
        assert IncomeReconciler._is_exit_order(None) is False
        assert IncomeReconciler._is_exit_order("") is False


# ============================================================
# 测试 4：最佳匹配（_find_best_match）
# ============================================================


class TestFindBestMatch:
    """测试 _find_best_match 匹配算法"""

    @pytest.fixture
    def small_window_reconciler(self, mock_db, mock_binance):
        """使用小窗口参数（10 秒窗口、1 秒出口加成），便于构造匹配场景"""
        config = {
            "reconciler": {
                "match_window_ms": 10000,
                "exit_priority_bonus_ms": 1000,
            }
        }
        return IncomeReconciler(mock_db, mock_binance, config)

    def test_picks_smallest_diff(self, small_window_reconciler):
        """窗口内应选择时间差最小者"""
        inc_time = 1700000000000  # 对应北京时间 2023-11-15 06:13:20
        candidates = [
            # 与收入时间完全对齐，diff=0
            {"id": 1, "order_type": "LIMIT", "executed_at": datetime(2023, 11, 15, 6, 13, 20)},
            # 落后 5 秒，diff=5000
            {"id": 2, "order_type": "LIMIT", "executed_at": datetime(2023, 11, 15, 6, 13, 25)},
        ]
        match = small_window_reconciler._find_best_match(candidates, inc_time)
        assert match["id"] == 1

    def test_exit_order_priority_bonus(self, small_window_reconciler):
        """出口单应获得优先级加成（diff 减去 bonus 后反超普通单）"""
        inc_time = 1700000000000
        candidates = [
            # LIMIT 单，diff=2000，无加成
            {"id": 1, "order_type": "LIMIT", "executed_at": datetime(2023, 11, 15, 6, 13, 22)},
            # STOP 单，diff=2500，加成后 2500-1000=1500，应反超 id1
            {"id": 2, "order_type": "STOP", "executed_at": datetime(2023, 11, 15, 6, 13, 22, 500000)},
        ]
        match = small_window_reconciler._find_best_match(candidates, inc_time)
        assert match["id"] == 2

    def test_outside_window_no_match(self, small_window_reconciler):
        """diff 达到 match_window_ms（10 秒）时不应匹配"""
        inc_time = 1700000000000
        candidates = [
            # 恰好落后 10 秒，diff=10000 >= 窗口
            {"id": 1, "order_type": "LIMIT", "executed_at": datetime(2023, 11, 15, 6, 13, 30)},
        ]
        match = small_window_reconciler._find_best_match(candidates, inc_time)
        assert match is None

    def test_within_window_matches(self, small_window_reconciler):
        """diff 小于 match_window_ms 时应匹配"""
        inc_time = 1700000000000
        candidates = [
            # 落后 9.99 秒，diff=9990 < 10000
            {"id": 1, "order_type": "LIMIT", "executed_at": datetime(2023, 11, 15, 6, 13, 29, 990000)},
        ]
        match = small_window_reconciler._find_best_match(candidates, inc_time)
        assert match["id"] == 1


# ============================================================
# 测试 5：run_once 主流程
# ============================================================


class TestRunOnce:
    """测试 run_once 主流程（mock db_manager 与 binance_client）"""

    @pytest.mark.asyncio
    async def test_disabled_skips_api(self, mock_db, mock_binance):
        """enabled=False 时直接返回，不调用 Binance API 与数据库"""
        rec = IncomeReconciler(mock_db, mock_binance, {"reconciler": {"enabled": False}})
        await rec.run_once()
        mock_binance.get_income_history.assert_not_awaited()
        mock_db.execute_ddl.assert_not_awaited()
        mock_db.execute.assert_not_awaited()
        mock_db.fetch_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_full_flow_writes_back_and_updates_progress(self, mock_db, mock_binance, base_config):
        """有 income 有 pending 时成功回写 realized_pnl 并更新进度"""
        rec = IncomeReconciler(mock_db, mock_binance, base_config)
        fixed_now = 1700000000000
        # Binance 返回一条权威盈亏记录
        mock_binance.get_income_history.return_value = [
            {"symbol": "BTCUSDT", "time": fixed_now, "income": "12.34"},
        ]
        # 无历史校准进度
        mock_db.fetch_one.return_value = {"last_reconciled_ts": 0}
        # 待校准记录：与收入时间完全对齐（北京 naive）
        mock_db.fetch_all.return_value = [
            {"id": 1, "symbol": "BTCUSDT", "order_type": "STOP",
             "executed_at": datetime(2023, 11, 15, 6, 13, 20)},
        ]
        with patch.object(rec, "_update_reconciled_ts", new=AsyncMock()) as mock_update:
            with patch("ai_tuner.reconciler.income_reconciler._utc_now_ms", return_value=fixed_now):
                await rec.run_once()

        # 进度更新被调用
        mock_update.assert_awaited_once()
        # 成功回写 realized_pnl：找到 UPDATE 语句并核对参数（pnl=12.34, id=1）
        update_calls = [
            c for c in mock_db.execute.await_args_list
            if "UPDATE trading.trade_records SET realized_pnl" in c.args[0]
        ]
        assert len(update_calls) == 1
        assert update_calls[0].args[1] == Decimal("12.34")
        assert update_calls[0].args[2] == 1

    @pytest.mark.asyncio
    async def test_no_income_updates_progress_only(self, mock_db, mock_binance, base_config):
        """income 为空时仅更新进度，不报错"""
        rec = IncomeReconciler(mock_db, mock_binance, base_config)
        mock_binance.get_income_history.return_value = []  # 无收入记录
        mock_db.fetch_one.return_value = {"last_reconciled_ts": 0}
        with patch.object(rec, "_update_reconciled_ts", new=AsyncMock()) as mock_update:
            with patch("ai_tuner.reconciler.income_reconciler._utc_now_ms", return_value=1700000000000):
                await rec.run_once()

        # 进度被更新，且不发起待校准查询（fetch_all 未被调用）
        mock_update.assert_awaited_once()
        mock_db.fetch_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_pending_updates_progress_only(self, mock_db, mock_binance, base_config):
        """有 income 但无待校准记录时，仅更新进度，不执行回写"""
        rec = IncomeReconciler(mock_db, mock_binance, base_config)
        mock_binance.get_income_history.return_value = [
            {"symbol": "BTCUSDT", "time": 1700000000000, "income": "1.0"},
        ]
        mock_db.fetch_one.return_value = {"last_reconciled_ts": 0}
        mock_db.fetch_all.return_value = []  # 无待校准记录
        with patch.object(rec, "_update_reconciled_ts", new=AsyncMock()) as mock_update:
            with patch("ai_tuner.reconciler.income_reconciler._utc_now_ms", return_value=1700000000000):
                await rec.run_once()

        mock_update.assert_awaited_once()
        # 不应存在 UPDATE realized_pnl 的回写调用
        update_calls = [
            c for c in mock_db.execute.await_args_list
            if "UPDATE trading.trade_records SET realized_pnl" in c.args[0]
        ]
        assert update_calls == []

    @pytest.mark.asyncio
    async def test_binance_api_exception_caught(self, mock_db, mock_binance, base_config):
        """Binance API 抛异常时被捕获，任务不崩溃（记录日志并降级更新进度）"""
        rec = IncomeReconciler(mock_db, mock_binance, base_config)
        # get_income_history 抛出运行时异常
        mock_binance.get_income_history = AsyncMock(side_effect=RuntimeError("Binance 接口超时"))
        mock_db.fetch_one.return_value = {"last_reconciled_ts": 0}
        with patch.object(rec, "_update_reconciled_ts", new=AsyncMock()) as mock_update:
            with patch("ai_tuner.reconciler.income_reconciler._utc_now_ms", return_value=1700000000000):
                with patch("ai_tuner.reconciler.income_reconciler.logger", new=MagicMock()) as mock_logger:
                    await rec.run_once()  # 不应抛异常

        # 记录警告日志、进度仍被更新（优雅降级）
        mock_logger.warning.assert_called()
        mock_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_top_level_exception_caught(self, mock_db, mock_binance, base_config):
        """内部方法抛出异常时被 run_once 顶层 except 捕获，任务不崩溃（记录错误日志）"""
        rec = IncomeReconciler(mock_db, mock_binance, base_config)
        # 建表阶段抛异常，触发顶层 except
        mock_db.execute_ddl = AsyncMock(side_effect=RuntimeError("建表失败"))
        with patch("ai_tuner.reconciler.income_reconciler.logger", new=MagicMock()) as mock_logger:
            await rec.run_once()  # 不应抛异常

        mock_logger.error.assert_called()


# ============================================================
# 测试 6：_fetch_pending_records SQL 组装
# ============================================================


class TestFetchPendingRecords:
    """测试 _fetch_pending_records 的 SQL 组装与查询参数"""

    @pytest.mark.asyncio
    async def test_sql_without_excluded_strategies(self, mock_db, mock_binance):
        """excluded_strategies 为空时不生成 NOT IN 子句，仅 2 个查询参数"""
        config = {"reconciler": {"match_window_ms": 60000}}
        rec = IncomeReconciler(mock_db, mock_binance, config)
        start_ms, end_ms = 1700000000000, 1700000060000
        await rec._fetch_pending_records(start_ms, end_ms)

        call = mock_db.fetch_all.await_args
        sql, bj_start, bj_end = call.args
        # 不包含 NOT IN 子句，且查询条件完整
        assert "NOT IN" not in sql
        assert "realized_pnl IS NULL" in sql
        # 北京时间窗口换算正确（start 前移 / end 后移各一个 match_window_ms）
        assert bj_start == _to_beijing_naive(start_ms - 60000)
        assert bj_end == _to_beijing_naive(end_ms + 60000)

    @pytest.mark.asyncio
    async def test_sql_with_excluded_strategies(self, mock_db, mock_binance):
        """excluded_strategies 非空时生成 NOT IN 占位符并追加策略参数"""
        config = {
            "reconciler": {
                "match_window_ms": 60000,
                "excluded_strategies": ["网格交易策略", "高频策略"],
            }
        }
        rec = IncomeReconciler(mock_db, mock_binance, config)
        start_ms, end_ms = 1700000000000, 1700000060000
        await rec._fetch_pending_records(start_ms, end_ms)

        call = mock_db.fetch_all.await_args
        sql = call.args[0]
        # 占位符从 $3 开始（前两个参数是时间窗口），数量与策略数一致
        assert "AND strategy NOT IN ($3, $4)" in sql
        # 参数顺序：bj_start, bj_end, 各策略名
        assert call.args[1] == _to_beijing_naive(start_ms - 60000)
        assert call.args[2] == _to_beijing_naive(end_ms + 60000)
        assert call.args[3] == "网格交易策略"
        assert call.args[4] == "高频策略"
