"""
策略资金限额强制执行 —— 单元测试（方案 D：DB 主来源 + config 兜底 + 静态兜底）

覆盖范围（对应需求文档 R2/R3/R4/R6/R7 与决策 D1~D4）：

1. `shared/position_baseline.py`
   - 统一保证金口径 `calc_position_margin`：正常值 / 做空负值取绝对值 / None / 0 / 负杠杆 /
     contractSize 缺失与非法（按 1 估算）
   - `calc_occupied_margin`：多仓累加、同币种不丢数、非法记录跳过
   - `build_short_positions`：PM 空列表 = 零持仓、多头忽略、entryPrice 缺失走 markPrice、
     同币种加权合并
   - `rebuild_from_exchange`：成功 / 交易所异常 / exchangeInfo 正常与失败

2. `shared/capital_manager.py`
   - 限额优先级链：DB → config.monthly_limit → trading.total_position_margin_limit
   - 月度额优先于静态兜底（不取 min，避免月度分配被固定兜底值压制）
   - DB 记录缺失 / 无本策略条目 / entries 为 JSON 字符串 / entries 损坏 / 分配额非法 → 降级
   - DB 抛异常 / 超时 → 沿用最近一次成功值（db_stale）或降级
   - TTL 缓存命中 / 过期重查；bind_database 延迟绑定（hrs 场景）
   - 三级全不可用 → fail-open（限额 None，放行）
   - `can_open_within_limit`：等于限额放行 / 超出拒绝（含数值文案）
   - 废弃同步方法仍有明确语义（兼容未改造调用方）

3. `strategies/new_coin/executor.py`
   - 开仓校验链路：基线未就绪拒开 / 同币种判重拒开（三来源并集）/ 限额缩仓 / 缩仓后低于
     min_position_margin 拒开 / 限额兜底分支拒开 / fail-open 放行
   - 占用统计与上报口径（仅统计自有币种、交易所不可用降级、标记价缺失跳过）

4. `strategies/new_coin/strategy.py`
   - 启动基线重建：PM 空列表清空本地残留 / 数量以交易所为准、本地入场价优先 /
     entryPrice 缺失用 markPrice / 交易所异常保持基线未就绪
   - 策略归属过滤：他策略币种剔除且不写 position_tracking / DB 不可用降级本地 /
     本地亦无记录时跳过过滤 / DB 为空集合时清空
   - 持仓上报：交易所不可用跳过上报 / 空持仓上报空集合 / 使用统一口径

5. `btc_eth`（含 aggressive）与 `hrs` 的保证金口径统计与限额入口接入
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

import strategies.new_coin.strategy as new_coin_strategy_module
from shared.capital_manager import CapitalManager
from shared.position_baseline import (
    build_contract_size_map,
    build_short_positions,
    calc_graded_positions_margin,
    calc_occupied_margin,
    calc_position_margin,
    check_entry_within_limit,
    rebuild_from_exchange,
)
from strategies.btc_eth.strategy import BTCEthStrategy, PositionState
from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as BTCEthAggressiveStrategy
from strategies.hrs.strategy import HRSStrategy
from strategies.new_coin.executor import TradingExecutor
from strategies.new_coin.strategy import NewCoinStrategy

# 中国标准时间（与 CapitalManager 判定「当月」的时区保持一致）
CST = timezone(timedelta(hours=8))
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


# ============================================================================
# 通用工具
# ============================================================================

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> None:
    """深度合并字典（就地修改 base）"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _base_config(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造测试用策略配置（含限额三级来源与 DB 缓存/超时配置）"""
    config: Dict[str, Any] = {
        'strategy': {'name': 'new_coin'},
        'trading': {
            'leverage': 2,
            'single_position_margin': 50.0,
            'total_position_margin_limit': 150.0,
            'min_position_margin': 25.0,
        },
        'capital_limits': {
            'db_cache_ttl_seconds': 300,
            'db_query_timeout_seconds': 3.0,
        },
    }
    if overrides:
        _deep_merge(config, overrides)
    return config


def _write_config(tmp_path, config: Dict[str, Any]) -> str:
    """将配置写入临时 YAML，返回绝对路径（CapitalManager 每次调用都重新读取文件）"""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return str(path)


def _mock_db(row: Any = None, side_effect: Any = None) -> MagicMock:
    """构造带 fetch_one 的 DB mock（side_effect 优先）"""
    db = MagicMock()
    db.fetch_one = AsyncMock(return_value=row, side_effect=side_effect)
    return db


def _entries(amount: Any = 156.33, strategy_id: str = 'new_coin') -> List[Dict[str, Any]]:
    """构造 capital_allocation.entries 中的策略条目"""
    return [{'strategy_id': strategy_id, 'allocated_amount': amount}]


# ============================================================================
# 1. 统一保证金口径（shared/position_baseline.py）
# ============================================================================

class TestCalcPositionMargin:
    """单仓占用保证金口径：|positionAmt| × markPrice × contractSize / leverage"""

    @pytest.mark.parametrize("args", [
        (None, 100, 1, 2),      # 数量缺失
        (2, None, 1, 2),        # 标记价缺失
        (2, 0, 1, 2),           # 标记价为 0
        (2, -100, 1, 2),        # 标记价为负
        (2, 100, 1, None),      # 杠杆缺失
        (2, 100, 1, 0),         # 杠杆为 0（配置错误不猜测）
        (2, 100, 1, -2),        # 杠杆为负
        (0, 100, 1, 2),         # 数量为 0
        ('abc', 100, 1, 2),     # 非数值数量
    ])
    def test_invalid_inputs_return_zero(self, args):
        assert calc_position_margin(*args) == 0.0

    def test_normal_and_negative_amount(self):
        """做空为负值：取绝对值，结果与正数量一致"""
        assert calc_position_margin(2, 100, 1, 2) == pytest.approx(100.0)
        assert calc_position_margin(-2, 100, 1, 2) == pytest.approx(100.0)

    def test_contract_size_applied(self):
        assert calc_position_margin(-2, 100, 0.1, 2) == pytest.approx(10.0)

    def test_contract_size_missing_or_invalid_estimated_as_one(self):
        """contractSize 缺失/非法 → 按 1 估算（并记估算告警），不得返回 0 低估占用"""
        assert calc_position_margin(-2, 100, None, 2) == pytest.approx(100.0)
        assert calc_position_margin(-2, 100, 0, 2) == pytest.approx(100.0)
        assert calc_position_margin(-2, 100, 'abc', 2) == pytest.approx(100.0)


class TestCalcOccupiedMargin:
    """策略总占用保证金：Σ 单仓保证金，同币种累加不丢数"""

    def test_multiple_positions_summed(self):
        positions = [
            {'positionAmt': -2, 'markPrice': 100, 'contractSize': 1},
            {'positionAmt': -1, 'markPrice': 200, 'contractSize': 1},
        ]
        assert calc_occupied_margin(positions, 2) == pytest.approx(200.0)

    def test_same_symbol_records_accumulate(self):
        positions = [
            {'positionAmt': -1, 'markPrice': 100, 'contractSize': 1},
            {'positionAmt': -1, 'markPrice': 100, 'contractSize': 1},
        ]
        assert calc_occupied_margin(positions, 2) == pytest.approx(100.0)

    def test_invalid_records_skipped(self):
        positions = [None, 'x', {'positionAmt': -2, 'markPrice': 100, 'contractSize': 1}]
        assert calc_occupied_margin(positions, 2) == pytest.approx(100.0)

    def test_empty_positions_and_bad_leverage(self):
        assert calc_occupied_margin([], 2) == 0.0
        assert calc_occupied_margin([{'positionAmt': -2, 'markPrice': 100}], 0) == 0.0


class TestCalcGradedPositionsMargin:
    """按「持仓等级 → 杠杆」累加占用（MTPCS 两策略共用，E 组提取）"""

    class _Pos:
        """最小持仓桩：仅提供统一口径所需的三个属性"""

        def __init__(self, quantity, entry_price, grade):
            self.current_quantity = quantity
            self.entry_price = entry_price
            self.grade = grade

    def test_uses_grade_leverage(self):
        positions = [self._Pos(10, 10, 'C'), self._Pos(10, 10, 'S')]
        assert calc_graded_positions_margin(
            positions, {'S': 5, 'A': 4, 'B': 3, 'C': 2}, 2
        ) == pytest.approx(70.0)   # 10×10/2 + 10×10/5

    def test_unknown_grade_uses_min_leverage(self):
        positions = [self._Pos(10, 10, 'Z')]
        assert calc_graded_positions_margin(positions, {'S': 5, 'C': 2}, 3) == pytest.approx(50.0)

    def test_empty_config_uses_default_leverage(self):
        positions = [self._Pos(10, 10, None)]
        assert calc_graded_positions_margin(positions, {}, 2) == pytest.approx(50.0)

    def test_contract_size_applied(self):
        positions = [self._Pos(10, 10, 'C')]
        assert calc_graded_positions_margin(
            positions, {'C': 2}, 2, contract_size=0.1
        ) == pytest.approx(5.0)

    def test_invalid_positions_skipped(self):
        positions = [self._Pos(0, 10, 'C'), self._Pos(10, 0, 'C')]
        assert calc_graded_positions_margin(positions, {'C': 2}, 2) == 0.0


class TestBuildContractSizeMap:
    """从 exchangeInfo 构建 {symbol: contractSize}"""

    def test_skips_missing_symbol_and_invalid_size(self):
        info = {'symbols': [
            {'symbol': 'BTCUSDT', 'contractSize': '0.1'},
            {'symbol': 'ETHUSDT'},                      # 缺失 contractSize
            {'symbol': 'XRPUSDT', 'contractSize': '0'},  # 非正值
            {'symbol': 'SOLUSDT', 'contractSize': 'abc'},
            {'contractSize': '1'},                       # 缺 symbol
        ]}
        assert build_contract_size_map(info) == {'BTCUSDT': 0.1}

    def test_non_dict_returns_empty(self):
        assert build_contract_size_map(None) == {}
        assert build_contract_size_map('exchangeInfo') == {}


class TestBuildShortPositions:
    """交易所 positionRisk → 做空持仓基线"""

    def test_empty_list_means_no_positions(self):
        """PM 账户零持仓返回空列表（已知坑）→ 视为零持仓"""
        assert build_short_positions([], 2) == {}

    def test_long_position_and_invalid_records_ignored(self):
        positions = [
            None,
            {'symbol': 'BTCUSDT', 'positionAmt': 2, 'markPrice': 100},          # 多头
            {'positionAmt': -2, 'markPrice': 100},                             # 缺 symbol
            {'symbol': 'ETHUSDT', 'positionAmt': None, 'markPrice': 100},       # 数量非法
        ]
        assert build_short_positions(positions, 2, {'BTCUSDT': 1}) == {}

    def test_entry_price_missing_uses_mark_price(self):
        result = build_short_positions(
            [{'symbol': 'BTCUSDT', 'positionAmt': -2, 'markPrice': 100}], 2
        )
        pos = result['BTCUSDT']
        assert pos.quantity == pytest.approx(2.0)
        assert pos.entry_price == pytest.approx(100.0)
        assert pos.estimated_entry_price is True
        assert pos.estimated_contract_size is True
        assert pos.margin == pytest.approx(100.0)

    def test_exchange_entry_price_and_contract_size_used(self):
        result = build_short_positions(
            [{'symbol': 'BTCUSDT', 'positionAmt': -2, 'markPrice': 120, 'entryPrice': 100}],
            2, {'BTCUSDT': 1},
        )
        pos = result['BTCUSDT']
        assert pos.entry_price == pytest.approx(100.0)
        assert pos.estimated_entry_price is False
        assert pos.estimated_contract_size is False
        assert pos.margin == pytest.approx(120.0)   # 保证金按标记价计算

    def test_same_symbol_merged_with_weighted_entry_price(self):
        result = build_short_positions(
            [
                {'symbol': 'BTCUSDT', 'positionAmt': -1, 'markPrice': 100, 'entryPrice': 100},
                {'symbol': 'BTCUSDT', 'positionAmt': -3, 'markPrice': 100, 'entryPrice': 200},
            ],
            2, {'BTCUSDT': 1},
        )
        pos = result['BTCUSDT']
        assert pos.quantity == pytest.approx(4.0)
        assert pos.entry_price == pytest.approx((100 * 1 + 200 * 3) / 4)
        assert pos.margin == pytest.approx(200.0)


class TestRebuildFromExchange:
    """启动基线重建（R4）"""

    @pytest.mark.asyncio
    async def test_success_with_short_positions(self):
        client = MagicMock()
        client.get_position = AsyncMock(return_value=[
            {'symbol': 'BTCUSDT', 'positionAmt': -2, 'markPrice': 100, 'entryPrice': 90},
            {'symbol': 'ETHUSDT', 'positionAmt': 1, 'markPrice': 10, 'entryPrice': 10},
        ])
        result = await rebuild_from_exchange(client, 2, {'BTCUSDT': 1})
        assert result.success is True
        assert list(result.positions.keys()) == ['BTCUSDT']
        assert result.total_margin == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_exchange_failure_returns_unsuccessful(self):
        client = MagicMock()
        client.get_position = AsyncMock(side_effect=RuntimeError("接口失败"))
        result = await rebuild_from_exchange(client, 2, {})
        assert result.success is False
        assert result.positions == {}
        assert result.total_margin == 0.0

    @pytest.mark.asyncio
    async def test_contract_sizes_fetched_when_not_provided(self):
        client = MagicMock()
        client.get_position = AsyncMock(return_value=[
            {'symbol': 'XRPUSDT', 'positionAmt': -10, 'markPrice': 1, 'entryPrice': 1}
        ])
        client.get_exchange_info = AsyncMock(
            return_value={'symbols': [{'symbol': 'XRPUSDT', 'contractSize': '0.1'}]}
        )
        result = await rebuild_from_exchange(client, 2)
        client.get_exchange_info.assert_awaited_once()
        assert result.positions['XRPUSDT'].margin == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_contract_size_fetch_failure_estimates_one(self):
        client = MagicMock()
        client.get_position = AsyncMock(return_value=[
            {'symbol': 'XRPUSDT', 'positionAmt': -10, 'markPrice': 1, 'entryPrice': 1}
        ])
        client.get_exchange_info = AsyncMock(side_effect=RuntimeError("exchangeInfo 失败"))
        result = await rebuild_from_exchange(client, 2)
        assert result.positions['XRPUSDT'].estimated_contract_size is True
        assert result.positions['XRPUSDT'].margin == pytest.approx(5.0)


# ============================================================================
# 2. 限额优先级链（shared/capital_manager.py）
# ============================================================================

class TestCapitalManagerLimitChain:
    """R2/决策 D1/D2：DB 主来源 → config 月度额 → 静态兜底 → fail-open"""

    @pytest.mark.asyncio
    async def test_db_active_record_used(self, tmp_path):
        """DB 当月 active 记录的分配额生效（静态兜底放宽以便断言 DB 原值）"""
        config = _base_config({'trading': {'total_position_margin_limit': 1000.0}})
        mgr = CapitalManager(
            _write_config(tmp_path, config),
            db=_mock_db({'entries': _entries(156.33)}),
            strategy_id='new_coin',
        )
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(156.33)
        assert source == 'db'

    @pytest.mark.asyncio
    async def test_db_entries_json_string_parsed(self, tmp_path):
        """entries 以 JSON 字符串存储（已知坑）也必须正确解析"""
        config = _base_config({'trading': {'total_position_margin_limit': 1000.0}})
        mgr = CapitalManager(
            _write_config(tmp_path, config),
            db=_mock_db({'entries': json.dumps(_entries(88.8))}),
            strategy_id='new_coin',
        )
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(88.8)
        assert source == 'db'

    @pytest.mark.asyncio
    async def test_db_queried_with_current_month(self, tmp_path):
        """按 CST 当月查询（格式 YYYY-MM）"""
        db = _mock_db({'entries': _entries()})
        mgr = CapitalManager(_write_config(tmp_path, _base_config()), db=db, strategy_id='new_coin')
        await mgr.get_effective_margin_limit()
        month = db.fetch_one.await_args.args[1]
        assert MONTH_RE.match(month)
        assert month == datetime.now(CST).strftime('%Y-%m')

    @pytest.mark.asyncio
    async def test_monthly_limit_priority_over_static(self, tmp_path):
        """月度额优先于静态兜底：DB 300 与静态 150 同时存在时生效 300（不取 min）"""
        mgr = CapitalManager(
            _write_config(tmp_path, _base_config()),
            db=_mock_db({'entries': _entries(300.0)}),
            strategy_id='new_coin',
        )
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(300.0)
        assert source == 'db'

    @pytest.mark.asyncio
    async def test_incident_monthly_limit_not_suppressed_by_static(self, tmp_path):
        """事故回归：月度额 156.33 > 静态 150 时，生效值必须是 156.33（旧 min 语义会误压成 150）"""
        mgr = CapitalManager(
            _write_config(tmp_path, _base_config()),   # 静态兜底 150
            db=_mock_db({'entries': _entries(156.33)}),
            strategy_id='new_coin',
        )
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(156.33)
        assert source == 'db'

    @pytest.mark.asyncio
    async def test_config_monthly_limit_priority_over_static(self, tmp_path):
        """config.capital_limits.monthly_limit 同样优先于静态兜底"""
        config = _base_config({
            'capital_limits': {'monthly_limit': 180.0},
            'trading': {'total_position_margin_limit': 150.0},
        })
        mgr = CapitalManager(_write_config(tmp_path, config), db=_mock_db(None), strategy_id='new_coin')
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(180.0)
        assert source == 'config'

    @pytest.mark.asyncio
    async def test_no_db_row_falls_back_to_config_monthly(self, tmp_path):
        """DB 无当月记录 → 不回退上月，转 config.capital_limits.monthly_limit"""
        config = _base_config({
            'capital_limits': {'monthly_limit': 200.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        mgr = CapitalManager(_write_config(tmp_path, config), db=_mock_db(None), strategy_id='new_coin')
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(200.0)
        assert source == 'config'

    @pytest.mark.asyncio
    async def test_no_db_row_and_no_monthly_falls_back_to_static(self, tmp_path):
        mgr = CapitalManager(
            _write_config(tmp_path, _base_config()), db=_mock_db(None), strategy_id='new_coin'
        )
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(150.0)
        assert source == 'static'

    @pytest.mark.asyncio
    async def test_strategy_not_in_entries_falls_back(self, tmp_path):
        """DB 记录存在但无本策略条目 → 视为无分配，走降级链路"""
        # 静态兜底放宽，聚焦断言「降级到 config.monthly_limit」
        config = _base_config({
            'capital_limits': {'monthly_limit': 210.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        db = _mock_db({'entries': _entries(156.33, strategy_id='hrs')})
        mgr = CapitalManager(_write_config(tmp_path, config), db=db, strategy_id='new_coin')
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(210.0)
        assert source == 'config'

    @pytest.mark.asyncio
    async def test_invalid_allocation_amount_falls_back(self, tmp_path):
        # 静态兜底放宽，聚焦断言「分配额非法时降级到 config.monthly_limit」
        config = _base_config({
            'capital_limits': {'monthly_limit': 210.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        db = _mock_db({'entries': [{'strategy_id': 'new_coin', 'allocated_amount': 'abc'}]})
        mgr = CapitalManager(_write_config(tmp_path, config), db=db, strategy_id='new_coin')
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(210.0)
        assert source == 'config'

    @pytest.mark.asyncio
    async def test_broken_entries_json_falls_back(self, tmp_path):
        # 静态兜底放宽，聚焦断言「entries 损坏时降级到 config.monthly_limit」
        config = _base_config({
            'capital_limits': {'monthly_limit': 210.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        mgr = CapitalManager(
            _write_config(tmp_path, config), db=_mock_db({'entries': '{not-json'}), strategy_id='new_coin'
        )
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(210.0)
        assert source == 'config'

    @pytest.mark.asyncio
    async def test_all_sources_unavailable_is_fail_open(self, tmp_path):
        """决策 D2：三级全不可用 → fail-open（限额 None，调用方放行）"""
        config = {'strategy': {'name': 'new_coin'}, 'trading': {'leverage': 2}}
        mgr = CapitalManager(_write_config(tmp_path, config), db=None, strategy_id='new_coin')
        limit, source = await mgr.get_effective_margin_limit()
        assert limit is None
        assert source == 'none'

    @pytest.mark.asyncio
    async def test_without_db_uses_config_monthly(self, tmp_path):
        config = _base_config({
            'capital_limits': {'monthly_limit': 180.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        mgr = CapitalManager(_write_config(tmp_path, config), db=None, strategy_id='new_coin')
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(180.0)
        assert source == 'config'

    @pytest.mark.asyncio
    async def test_db_without_strategy_id_skips_db(self, tmp_path):
        config = _base_config({
            'capital_limits': {'monthly_limit': 180.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        db = _mock_db({'entries': _entries(156.33)})
        mgr = CapitalManager(_write_config(tmp_path, config), db=db, strategy_id=None)
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(180.0)
        assert source == 'config'
        db.fetch_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_ttl_hit_avoids_second_query(self, tmp_path):
        """TTL 内命中缓存，不再查库，来源标记为 db_cache"""
        config = _base_config({'trading': {'total_position_margin_limit': 1000.0}})
        db = _mock_db({'entries': _entries(156.33)})
        mgr = CapitalManager(_write_config(tmp_path, config), db=db, strategy_id='new_coin')
        first_limit, first_source = await mgr.get_effective_margin_limit()
        second_limit, second_source = await mgr.get_effective_margin_limit()
        db.fetch_one.assert_awaited_once()
        assert first_limit == pytest.approx(second_limit)
        assert (first_source, second_source) == ('db', 'db_cache')

    @pytest.mark.asyncio
    async def test_db_exception_uses_last_success_value(self, tmp_path):
        """DB 异常 → 沿用最近一次成功值（db_stale），限额不失效"""
        config = _base_config({
            'trading': {'total_position_margin_limit': 1000.0},
            'capital_limits': {'db_cache_ttl_seconds': 0.01},
        })
        db = _mock_db({'entries': _entries(156.33)})
        mgr = CapitalManager(_write_config(tmp_path, config), db=db, strategy_id='new_coin')
        first_limit, first_source = await mgr.get_effective_margin_limit()
        assert first_source == 'db'

        await asyncio.sleep(0.02)                     # 令缓存过期
        db.fetch_one = AsyncMock(side_effect=RuntimeError("连接失败"))
        second_limit, second_source = await mgr.get_effective_margin_limit()
        assert second_limit == pytest.approx(first_limit)
        assert second_source == 'db_stale'

    @pytest.mark.asyncio
    async def test_db_exception_without_cache_falls_back(self, tmp_path):
        config = _base_config({
            'capital_limits': {'monthly_limit': 200.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        mgr = CapitalManager(
            _write_config(tmp_path, config), db=_mock_db(side_effect=RuntimeError("连接失败")),
            strategy_id='new_coin',
        )
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(200.0)
        assert source == 'config'

    @pytest.mark.asyncio
    async def test_db_timeout_falls_back(self, tmp_path):
        """DB 查询超时（asyncio.wait_for）→ 降级链路继续，不中断策略主循环"""
        config = _base_config({
            'capital_limits': {'monthly_limit': 200.0, 'db_query_timeout_seconds': 0.01},
            'trading': {'total_position_margin_limit': 1000.0},
        })

        async def _slow_query(*_args, **_kwargs):
            await asyncio.sleep(0.1)
            return {'entries': _entries(156.33)}

        mgr = CapitalManager(
            _write_config(tmp_path, config), db=_mock_db(side_effect=_slow_query),
            strategy_id='new_coin',
        )
        limit, source = await mgr.get_effective_margin_limit()
        assert limit == pytest.approx(200.0)
        assert source == 'config'

    @pytest.mark.asyncio
    async def test_bind_database_enables_db_and_clears_cache(self, tmp_path):
        """hrs 场景：db 在 __init__ 之后注入，bind_database 后 DB 生效且旧缓存被清空"""
        config = _base_config({
            'capital_limits': {'monthly_limit': 200.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        mgr = CapitalManager(_write_config(tmp_path, config), strategy_id='hrs')
        limit, source = await mgr.get_effective_margin_limit()
        assert (limit, source) == (pytest.approx(200.0), 'config')

        mgr.bind_database(_mock_db({'entries': _entries(120.0, strategy_id='hrs')}), 'hrs')
        limit, source = await mgr.get_effective_margin_limit()
        assert (limit, source) == (pytest.approx(120.0), 'db')


class TestCanOpenWithinLimit:
    """唯一限额入口的放行/拒绝判定与最小开仓门槛"""

    @pytest.mark.asyncio
    async def test_allowed_when_total_equals_limit(self, tmp_path):
        config = _base_config({
            'capital_limits': {'monthly_limit': 150.0},
            'trading': {'total_position_margin_limit': 150.0},
        })
        mgr = CapitalManager(_write_config(tmp_path, config), strategy_id='new_coin')
        allowed, reason = await mgr.can_open_within_limit(100.0, 50.0)
        assert allowed is True
        assert reason == ""

    @pytest.mark.asyncio
    async def test_rejected_reason_contains_numbers(self, tmp_path):
        mgr = CapitalManager(_write_config(tmp_path, _base_config()), strategy_id='new_coin')
        allowed, reason = await mgr.can_open_within_limit(249.30, 50.0)
        assert allowed is False
        assert reason == "总持仓保证金超限(299.30/150.00)"

    @pytest.mark.asyncio
    async def test_fail_open_when_no_limit(self, tmp_path):
        config = {'strategy': {'name': 'new_coin'}, 'trading': {'leverage': 2}}
        mgr = CapitalManager(_write_config(tmp_path, config), strategy_id='new_coin')
        allowed, reason = await mgr.can_open_within_limit(999.0, 999.0)
        assert (allowed, reason) == (True, "")

    def test_min_position_margin_read(self, tmp_path):
        mgr = CapitalManager(_write_config(tmp_path, _base_config()), strategy_id='new_coin')
        assert mgr.get_min_position_margin() == pytest.approx(25.0)

    def test_min_position_margin_missing_returns_none(self, tmp_path):
        config = {'strategy': {'name': 'new_coin'}, 'trading': {'leverage': 2}}
        mgr = CapitalManager(_write_config(tmp_path, config), strategy_id='new_coin')
        assert mgr.get_min_position_margin() is None

    def test_deprecated_methods_keep_meaning(self, tmp_path):
        """兼容保留：废弃方法仍可返回配置值，且每方法仅告警一次"""
        config = _base_config({'capital_limits': {'monthly_limit': 100.0}})
        mgr = CapitalManager(_write_config(tmp_path, config), strategy_id='new_coin')
        # 名义口径旧语义：90 + 20 > 100 → 拒绝
        assert mgr.can_open_position(90.0, 20.0) is False
        assert mgr.is_allocated() is True
        assert mgr.get_allocated_capital() == pytest.approx(100.0)
        assert mgr.get_total_margin_limit() == pytest.approx(100.0)
        assert 'can_open_position' in mgr._deprecation_warned

    def test_deprecated_can_open_position_without_allocation(self, tmp_path):
        """未配置月度额时旧方法不做限制（保持向后兼容语义）"""
        mgr = CapitalManager(_write_config(tmp_path, _base_config()), strategy_id='new_coin')
        assert mgr.get_allocated_capital() is None
        assert mgr.can_open_position(0.0, 10000.0) is True
        assert mgr.is_allocated() is False


# ============================================================================
# 3. new_coin 开仓校验链路（strategies/new_coin/executor.py）
# ============================================================================

def _build_executor(
    tmp_path,
    overrides: Optional[Dict[str, Any]] = None,
    db: Any = None,
) -> TradingExecutor:
    """构建 new_coin 交易执行器（配置写入临时 YAML，供 CapitalManager 读取）"""
    config = _base_config(overrides)
    binance_api = MagicMock()
    binance_api.place_order = AsyncMock()
    binance_api.get_position = AsyncMock(return_value=[])
    binance_api.get_exchange_info = AsyncMock(return_value={'symbols': []})
    notification = MagicMock()
    notification.send = AsyncMock()
    executor = TradingExecutor(
        binance_api=binance_api,
        db=db if db is not None else _mock_db(),
        notification=notification,
        config=config,
        kline_service=None,
        config_path=_write_config(tmp_path, config),
    )
    # 生产路径由 _restore_state() → _rebuild_position_baseline() 保证基线就绪；
    # 此处默认模拟「启动完成」状态，需要验证未就绪行为的用例自行改回 False
    executor.baseline_ready = True
    return executor


def _stub_entry_checks(executor: TradingExecutor, occupied_margin: float = 0.0) -> None:
    """打桩开仓前置依赖，仅保留限额链路真实执行"""
    executor._get_account_balance = AsyncMock(return_value=Decimal('1000'))
    executor.baseline_ready = True
    executor._is_symbol_occupied = AsyncMock(return_value=False)
    executor.calc_current_occupied_margin = AsyncMock(return_value=occupied_margin)


def _stub_order_path(executor: TradingExecutor) -> None:
    """打桩下单及后续链路（前置校验通过后才会走到这里）"""
    executor._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))
    executor._set_leverage = AsyncMock()
    executor._place_short_order = AsyncMock(return_value={'orderId': 1, 'status': 'FILLED'})
    executor._wait_for_order_fill = AsyncMock(return_value={'orderId': 1, 'status': 'FILLED'})
    executor._save_order = AsyncMock()
    executor._calculate_atr = AsyncMock(return_value=Decimal('0'))
    executor._insert_short_position = AsyncMock()
    executor._set_stop_loss_take_profit = AsyncMock()
    executor._send_notification = AsyncMock()


class TestNewCoinEntryLimit:
    """new_coin execute_short 的开仓校验链路（R3/R4/R6 + 决策 D2/D3）"""

    @pytest.mark.asyncio
    async def test_baseline_not_ready_rejects(self, tmp_path):
        """R4：基线重建完成前禁止开新仓"""
        executor = _build_executor(tmp_path)
        executor.baseline_ready = False
        executor._get_account_balance = AsyncMock(return_value=Decimal('1000'))

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert order is None
        assert reason == "持仓基线未就绪，暂禁止开仓"

    @pytest.mark.asyncio
    async def test_duplicate_symbol_rejects(self, tmp_path):
        """R6：同币种已有持仓 → 拒绝重复开仓"""
        executor = _build_executor(tmp_path)
        executor._get_account_balance = AsyncMock(return_value=Decimal('1000'))
        executor._is_symbol_occupied = AsyncMock(return_value=True)

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert order is None
        assert reason == "该币种已有持仓，禁止重复开仓"

    @pytest.mark.asyncio
    async def test_insufficient_balance_rejects(self, tmp_path):
        executor = _build_executor(tmp_path)
        executor._get_account_balance = AsyncMock(return_value=Decimal('0'))

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert order is None
        assert reason == "账户余额不足"

    @pytest.mark.asyncio
    async def test_db_limit_shrinks_position(self, tmp_path):
        """R2+R3+决策 D3：限额来自 DB；剩余额度 40 < 单笔 50 → 按额度缩仓到 40 保证金"""
        config = _base_config({'trading': {'total_position_margin_limit': 1000.0}})
        executor = _build_executor(tmp_path, overrides=config, db=_mock_db({"entries": _entries(200.0)}))
        _stub_entry_checks(executor, occupied_margin=160.0)
        _stub_order_path(executor)

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert reason == ""
        assert order == {'orderId': 1, 'status': 'FILLED'}
        # margin = min(50, 40) = 40 → 数量 = 40 × 2 / 100 = 0.8
        placed_quantity = executor._place_short_order.await_args.args[1]
        assert float(placed_quantity) == pytest.approx(0.8)

    @pytest.mark.asyncio
    async def test_static_fallback_limit_shrinks_position(self, tmp_path):
        """DB 无当月记录 → 静态兜底 150 生效；剩余额度 30 → 缩仓到 0.6 张"""
        executor = _build_executor(tmp_path, db=_mock_db(None))
        _stub_entry_checks(executor, occupied_margin=120.0)
        _stub_order_path(executor)

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert reason == ""
        placed_quantity = executor._place_short_order.await_args.args[1]
        assert float(placed_quantity) == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_incident_scenario_rejects_with_quota_reason(self, tmp_path):
        """事故场景：占用 249.30 > 限额 min(156.33, 150) → 可用额度 0 → 拒开（仅允许减仓）"""
        executor = _build_executor(tmp_path, db=_mock_db({"entries": _entries(156.33)}))
        _stub_entry_checks(executor, occupied_margin=249.30)

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert order is None
        assert reason == "可用额度不足(缩仓后保证金0.00 < 门槛25.00)"
        executor.binance_api.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_shrunk_below_min_margin_rejects(self, tmp_path):
        """决策 D3：缩仓后保证金低于门槛 → 拒开"""
        # 静态兜底 150，占用 130 → 可用额度 20 < 门槛 35 → 拒开
        config = _base_config({'trading': {'min_position_margin': 35.0}})
        executor = _build_executor(tmp_path, overrides=config, db=_mock_db(None))
        _stub_entry_checks(executor, occupied_margin=130.0)   # 可用额度 20 < 门槛 35

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert order is None
        assert reason == "可用额度不足(缩仓后保证金20.00 < 门槛35.00)"
        executor.binance_api.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_fail_open_allows_open_without_limit(self, tmp_path):
        """决策 D2：三级限额均不可用 → fail-open 放行，按单笔保证金全额开仓"""
        config = _base_config({'trading': {'total_position_margin_limit': None}})
        executor = _build_executor(tmp_path, overrides=config)
        _stub_entry_checks(executor)
        _stub_order_path(executor)

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert reason == ""
        assert order is not None
        placed_quantity = executor._place_short_order.await_args.args[1]
        assert float(placed_quantity) == pytest.approx(1.0)   # 50 × 2 / 100

    @pytest.mark.asyncio
    async def test_can_open_within_limit_reject_reason_passthrough(self, tmp_path):
        """限额兜底分支：拒绝原因原样透传，且不触发下单"""
        executor = _build_executor(tmp_path)
        _stub_entry_checks(executor)
        executor.capital_mgr.can_open_within_limit = AsyncMock(
            return_value=(False, "总持仓保证金超限(150.50/150.00)")
        )

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert order is None
        assert reason == "总持仓保证金超限(150.50/150.00)"
        executor.binance_api.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_position_size_rejects(self, tmp_path):
        """价格非法导致仓位算不出 → 拒开（非额度原因）"""
        config = _base_config({'trading': {'total_position_margin_limit': None, 'min_position_margin': None}})
        executor = _build_executor(tmp_path, overrides=config)
        _stub_entry_checks(executor)

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 0.0)

        assert order is None
        assert reason == "仓位大小计算失败"

    @pytest.mark.asyncio
    async def test_contract_size_applied_to_new_margin(self, tmp_path):
        """contractSize≠1：缩仓后保证金按统一口径（含 contractSize）计算，门槛判定随之生效"""
        executor = _build_executor(tmp_path)
        executor.binance_api.get_exchange_info = AsyncMock(
            return_value={'symbols': [{'symbol': 'HUTUSDT', 'contractSize': '0.1'}]}
        )
        _stub_entry_checks(executor)

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        # 单笔保证金 50 → 数量 = 50×2/100 = 1.0 → new_margin = 1.0×100×0.1/2 = 5.0 < 门槛 25
        assert order is None
        assert reason == "可用额度不足(缩仓后保证金5.00 < 门槛25.00)"


class TestDuplicateSymbolDetection:
    """R6：交易所真实持仓 ∪ 本地记录 ∪ short_positions 并集判定"""

    @pytest.mark.asyncio
    async def test_local_tracking_hit(self, tmp_path):
        executor = _build_executor(tmp_path)
        executor.position_tracking = {'HUTUSDT': {'entry_quantity': 1.0}}
        executor.db.fetch_one = AsyncMock(return_value=None)
        assert await executor._is_symbol_occupied('HUTUSDT') is True

    @pytest.mark.asyncio
    async def test_exchange_position_hit(self, tmp_path):
        executor = _build_executor(tmp_path)
        executor.binance_api.get_position = AsyncMock(return_value=[
            {'symbol': 'HUTUSDT', 'positionAmt': -3, 'markPrice': 10}
        ])
        executor.db.fetch_one = AsyncMock(return_value=None)
        assert await executor._is_symbol_occupied('HUTUSDT') is True

    @pytest.mark.asyncio
    async def test_short_positions_table_hit(self, tmp_path):
        executor = _build_executor(tmp_path)
        executor.db.fetch_one = AsyncMock(return_value={'exists': 1})
        assert await executor._is_symbol_occupied('HUTUSDT') is True

    @pytest.mark.asyncio
    async def test_not_occupied_passes(self, tmp_path):
        executor = _build_executor(tmp_path)
        executor.db.fetch_one = AsyncMock(return_value=None)
        assert await executor._is_symbol_occupied('HUTUSDT') is False

    @pytest.mark.asyncio
    async def test_exchange_unavailable_without_local_record_rejects(self, tmp_path):
        """交易所不可用且本地无记录 → 保守拒绝（宁可漏开不可重开）"""
        executor = _build_executor(tmp_path)
        executor.binance_api.get_position = AsyncMock(side_effect=RuntimeError("接口失败"))
        executor.db.fetch_one = AsyncMock(return_value=None)

        assert await executor._is_symbol_occupied('HUTUSDT') is True
        executor.notification.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_short_positions_query_failure_does_not_block(self, tmp_path):
        """short_positions 查询异常 → 不阻断主流程（按无记录处理）"""
        executor = _build_executor(tmp_path)
        executor.db.fetch_one = AsyncMock(side_effect=RuntimeError("表不存在"))
        assert await executor._is_symbol_occupied('HUTUSDT') is False

    @pytest.mark.asyncio
    async def test_duplicate_notification_throttled(self, tmp_path):
        """同币种重复告警按窗口降频，避免刷屏"""
        executor = _build_executor(tmp_path)
        await executor._notify_duplicate_symbol('HUTUSDT', 3.0, ['exchange'])
        await executor._notify_duplicate_symbol('HUTUSDT', 3.0, ['exchange'])
        executor.notification.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_notification_failure_swallowed(self, tmp_path):
        executor = _build_executor(tmp_path)
        executor.notification.send = AsyncMock(side_effect=RuntimeError("通知失败"))
        await executor._notify_duplicate_symbol('HUTUSDT', None, ['position_tracking'])


class TestCurrentOccupiedMargin:
    """占用统计：仅统计策略自有币种，交易所不可用时降级本地估算"""

    @pytest.mark.asyncio
    async def test_only_own_symbols_counted(self, tmp_path):
        """同一 PM 账户被多策略共用 → 其他策略持仓不得计入本策略占用"""
        executor = _build_executor(tmp_path)
        executor.position_tracking = {'HUTUSDT': {'entry_quantity': 1.0}}
        executor.binance_api.get_position = AsyncMock(return_value=[
            {'symbol': 'HUTUSDT', 'positionAmt': -4, 'markPrice': 25},
            {'symbol': 'BTCUSDT', 'positionAmt': -1, 'markPrice': 100},
        ])
        assert await executor.calc_current_occupied_margin() == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_no_tracking_returns_zero(self, tmp_path):
        executor = _build_executor(tmp_path)
        assert await executor.calc_current_occupied_margin() == 0.0

    @pytest.mark.asyncio
    async def test_exchange_unavailable_uses_local_estimate(self, tmp_path):
        executor = _build_executor(tmp_path)
        executor.position_tracking = {'HUTUSDT': {'entry_quantity': 4.0, 'entry_price': 25.0}}
        executor.binance_api.get_position = AsyncMock(side_effect=RuntimeError("接口失败"))
        assert await executor.calc_current_occupied_margin() == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_contract_size_map_cached(self, tmp_path):
        executor = _build_executor(tmp_path)
        await executor.get_contract_size_map()
        await executor.get_contract_size_map()
        executor.binance_api.get_exchange_info.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_contract_size_map_failure_returns_empty(self, tmp_path):
        executor = _build_executor(tmp_path)
        executor.binance_api.get_exchange_info = AsyncMock(side_effect=RuntimeError("接口失败"))
        assert await executor.get_contract_size_map() == {}


class TestOccupancyReport:
    """R7：上报口径与限额口径统一（AC-6）"""

    @pytest.mark.asyncio
    async def test_margin_matches_unified_formula(self, tmp_path):
        executor = _build_executor(tmp_path)
        executor.position_tracking = {'HUTUSDT': {'entry_quantity': 1.0}}
        executor.binance_api.get_position = AsyncMock(return_value=[
            {'symbol': 'HUTUSDT', 'positionAmt': -4, 'markPrice': 25, 'entryPrice': 30}
        ])
        executor.binance_api.get_exchange_info = AsyncMock(
            return_value={'symbols': [{'symbol': 'HUTUSDT', 'contractSize': '1'}]}
        )

        margin_dict, qty_dict = await executor.build_occupancy_report({'HUTUSDT'})

        assert qty_dict == {'HUTUSDT': pytest.approx(4.0)}
        assert margin_dict['HUTUSDT'] == pytest.approx(50.0)
        # 与统一口径函数结果完全一致（误差 0，满足 AC-6）
        assert margin_dict['HUTUSDT'] == pytest.approx(calc_position_margin(-4, 25, 1, 2))

    @pytest.mark.asyncio
    async def test_exchange_unavailable_returns_none(self, tmp_path):
        """交易所不可用 → None：调用方必须保留上次上报值，不得用固定单笔保证金填充"""
        executor = _build_executor(tmp_path)
        executor.binance_api.get_position = AsyncMock(side_effect=RuntimeError("接口失败"))
        assert await executor.build_occupancy_report({'HUTUSDT'}) is None

    @pytest.mark.asyncio
    async def test_empty_symbols_returns_empty_dicts(self, tmp_path):
        executor = _build_executor(tmp_path)
        assert await executor.build_occupancy_report(set()) == ({}, {})

    @pytest.mark.asyncio
    async def test_zero_margin_symbol_skipped(self, tmp_path):
        """标记价缺失导致保证金算不出 → 跳过（避免上报 0 低估占用）"""
        executor = _build_executor(tmp_path)
        executor.position_tracking = {'HUTUSDT': {'entry_quantity': 1.0}}
        executor.binance_api.get_position = AsyncMock(return_value=[
            {'symbol': 'HUTUSDT', 'positionAmt': -4, 'markPrice': 0}
        ])
        assert await executor.build_occupancy_report({'HUTUSDT'}) == ({}, {})

    @pytest.mark.asyncio
    async def test_unowned_symbol_excluded(self, tmp_path):
        executor = _build_executor(tmp_path)
        executor.binance_api.get_position = AsyncMock(return_value=[
            {'symbol': 'HUTUSDT', 'positionAmt': -4, 'markPrice': 25},
            {'symbol': 'BTCUSDT', 'positionAmt': -1, 'markPrice': 100},
        ])
        margin_dict, _ = await executor.build_occupancy_report({'HUTUSDT'})
        assert set(margin_dict.keys()) == {'HUTUSDT'}


# ============================================================================
# 4. new_coin 启动基线重建与持仓上报（strategies/new_coin/strategy.py）
# ============================================================================

def _build_new_coin_strategy() -> NewCoinStrategy:
    """构建仅含基线重建/上报所需属性的 new_coin 策略实例"""
    strategy = NewCoinStrategy({'strategy': {'name': 'new_coin'}})
    strategy.db = MagicMock()
    return strategy


def _build_baseline_executor(own_symbols: Optional[set] = None) -> MagicMock:
    """
    构造基线重建用的 executor mock

    Args:
        own_symbols: 模拟 new_coin.short_positions 中的自有币种集合；
            None 表示模拟 DB 查询失败（调用方需降级处理）
    """
    executor = MagicMock()
    executor.leverage = 2
    executor.baseline_ready = True
    executor.position_tracking = {}
    executor.get_contract_size_map = AsyncMock(return_value={})
    executor.get_open_short_symbols = AsyncMock(
        return_value={'HUTUSDT'} if own_symbols is None else own_symbols
    )
    return executor


class TestBaselineRebuild:
    """R4：启动时从交易所重建持仓基线（AC-3）"""

    @pytest.mark.asyncio
    async def test_empty_exchange_positions_clears_local_record(self):
        """PM 空列表 = 本策略零持仓 → 清空本地残留并允许开仓"""
        strategy = _build_new_coin_strategy()
        strategy.positions = {'HUTUSDT': {'order_id': '1', 'entry_price': 10.0}}
        strategy.trading_executor = _build_baseline_executor()
        strategy.binance_client = MagicMock()
        strategy.binance_client.get_position = AsyncMock(return_value=[])

        await strategy._rebuild_position_baseline()

        assert strategy.positions == {}
        assert strategy.trading_executor.baseline_ready is True
        strategy.trading_executor.clear_position_tracking.assert_called_once_with('HUTUSDT')

    @pytest.mark.asyncio
    async def test_quantity_from_exchange_and_local_entry_price_kept(self):
        """数量与保证金以交易所为准，本地入场价优先保留"""
        strategy = _build_new_coin_strategy()
        strategy.positions = {
            'HUTUSDT': {'order_id': '9', 'entry_price': 33.0, 'entry_time': '2026-09-01T00:00:00+00:00'}
        }
        strategy.trading_executor = _build_baseline_executor()
        strategy.trading_executor.get_contract_size_map = AsyncMock(return_value={'HUTUSDT': 1.0})
        strategy.binance_client = MagicMock()
        strategy.binance_client.get_position = AsyncMock(return_value=[
            {'symbol': 'HUTUSDT', 'positionAmt': -4, 'markPrice': 25, 'entryPrice': 30}
        ])

        await strategy._rebuild_position_baseline()

        pos = strategy.positions['HUTUSDT']
        assert pos['quantity'] == pytest.approx(4.0)
        assert pos['entry_price'] == pytest.approx(33.0)
        assert pos['margin'] == pytest.approx(50.0)
        assert pos['order_id'] == '9'
        strategy.trading_executor.clear_position_tracking.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_entry_price_missing_uses_exchange_price(self):
        strategy = _build_new_coin_strategy()
        strategy.positions = {'HUTUSDT': {'order_id': None, 'entry_price': 0}}
        strategy.trading_executor = _build_baseline_executor()
        strategy.binance_client = MagicMock()
        strategy.binance_client.get_position = AsyncMock(return_value=[
            {'symbol': 'HUTUSDT', 'positionAmt': -2, 'markPrice': 40}
        ])

        await strategy._rebuild_position_baseline()

        assert strategy.positions['HUTUSDT']['entry_price'] == pytest.approx(40.0)

    @pytest.mark.asyncio
    async def test_exchange_failure_keeps_baseline_not_ready(self):
        """交易所异常 → 不改动本地记录，保持未就绪，由后续周期重试"""
        strategy = _build_new_coin_strategy()
        strategy.positions = {'HUTUSDT': {'order_id': '1', 'entry_price': 10.0}}
        strategy.trading_executor = _build_baseline_executor()
        strategy.binance_client = MagicMock()
        strategy.binance_client.get_position = AsyncMock(side_effect=RuntimeError("接口失败"))

        await strategy._rebuild_position_baseline()

        assert strategy.trading_executor.baseline_ready is False
        assert list(strategy.positions.keys()) == ['HUTUSDT']

    @pytest.mark.asyncio
    async def test_no_executor_returns_early(self):
        strategy = _build_new_coin_strategy()
        strategy.trading_executor = None

        await strategy._rebuild_position_baseline()   # 不应抛异常


class TestOwnPositionFilter:
    """R4：基线重建必须过滤策略归属（PM 账户 positionRisk 返回全账户空头）"""

    @pytest.mark.asyncio
    async def test_foreign_symbols_filtered_out(self):
        """他策略币种必须被剔除，且不得写入 position_tracking（防止越权管理）"""
        strategy = _build_new_coin_strategy()
        strategy.trading_executor = _build_baseline_executor(own_symbols={'HUTUSDT'})
        strategy.trading_executor.position_tracking = {}
        strategy.binance_client = MagicMock()
        strategy.binance_client.get_position = AsyncMock(return_value=[
            {'symbol': 'HUTUSDT', 'positionAmt': -4, 'markPrice': 25, 'entryPrice': 30},
            {'symbol': 'BTCUSDT', 'positionAmt': -1, 'markPrice': 100, 'entryPrice': 100},
        ])

        await strategy._rebuild_position_baseline()

        assert list(strategy.positions.keys()) == ['HUTUSDT']
        assert set(strategy.trading_executor.position_tracking.keys()) == {'HUTUSDT'}

    @pytest.mark.asyncio
    async def test_db_unavailable_falls_back_to_local_symbols(self):
        """DB 查询失败 → 降级为本地记录集合，仍过滤他策略币种"""
        strategy = _build_new_coin_strategy()
        strategy.positions = {'HUTUSDT': {'order_id': '9', 'entry_price': 33.0}}
        strategy.trading_executor = _build_baseline_executor()
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value=None)
        strategy.trading_executor.position_tracking = {}
        strategy.binance_client = MagicMock()
        strategy.binance_client.get_position = AsyncMock(return_value=[
            {'symbol': 'HUTUSDT', 'positionAmt': -4, 'markPrice': 25, 'entryPrice': 30},
            {'symbol': 'BTCUSDT', 'positionAmt': -1, 'markPrice': 100, 'entryPrice': 100},
        ])

        await strategy._rebuild_position_baseline()

        assert list(strategy.positions.keys()) == ['HUTUSDT']

    @pytest.mark.asyncio
    async def test_db_unavailable_without_local_records_skips_filter(self):
        """DB 与本地均无法判定归属 → 跳过过滤（宁可保留，不丢自有持仓）"""
        strategy = _build_new_coin_strategy()
        strategy.positions = {}
        strategy.trading_executor = _build_baseline_executor()
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value=None)
        strategy.trading_executor.position_tracking = {}
        strategy.binance_client = MagicMock()
        strategy.binance_client.get_position = AsyncMock(return_value=[
            {'symbol': 'HUTUSDT', 'positionAmt': -4, 'markPrice': 25, 'entryPrice': 30},
        ])

        await strategy._rebuild_position_baseline()

        assert list(strategy.positions.keys()) == ['HUTUSDT']

    @pytest.mark.asyncio
    async def test_db_empty_set_clears_all_positions(self):
        """DB 无 open 记录 = 本策略零持仓 → 清空交易所全账户空头，占用归零"""
        strategy = _build_new_coin_strategy()
        strategy.trading_executor = _build_baseline_executor(own_symbols=set())
        strategy.trading_executor.position_tracking = {}
        strategy.binance_client = MagicMock()
        strategy.binance_client.get_position = AsyncMock(return_value=[
            {'symbol': 'BTCUSDT', 'positionAmt': -1, 'markPrice': 100, 'entryPrice': 100},
        ])

        await strategy._rebuild_position_baseline()

        assert strategy.positions == {}
        assert strategy.trading_executor.position_tracking == {}


def _spy_sync_open_positions(monkeypatch) -> List[tuple]:
    """替换 strategy 模块内的 sync_open_positions，并记录调用参数"""
    calls: List[tuple] = []

    async def _fake(db, strategy_id, positions_margin=None, positions_qty=None):
        calls.append((strategy_id, positions_margin, positions_qty))

    monkeypatch.setattr(new_coin_strategy_module, 'sync_open_positions', _fake)
    return calls


class TestOpenPositionsReport:
    """R7：持仓上报口径（交易所不可用时保留上次值）"""

    @pytest.mark.asyncio
    async def test_exchange_unavailable_skips_upload(self, monkeypatch):
        """交易所不可用 → 跳过上报（保留上次值），避免占用被低估"""
        calls = _spy_sync_open_positions(monkeypatch)
        strategy = _build_new_coin_strategy()
        strategy.positions = {'HUTUSDT': {}}
        strategy.trading_executor = MagicMock()
        strategy.trading_executor.build_occupancy_report = AsyncMock(return_value=None)

        await strategy._sync_open_positions_to_db()

        assert calls == []

    @pytest.mark.asyncio
    async def test_empty_positions_upload_empty_sets(self, monkeypatch):
        calls = _spy_sync_open_positions(monkeypatch)
        strategy = _build_new_coin_strategy()
        strategy.positions = {}
        strategy.trading_executor = MagicMock()
        strategy.trading_executor.build_occupancy_report = AsyncMock(return_value=None)

        await strategy._sync_open_positions_to_db()

        assert calls == [('new_coin', {}, {})]

    @pytest.mark.asyncio
    async def test_upload_uses_unified_occupancy(self, monkeypatch):
        calls = _spy_sync_open_positions(monkeypatch)
        strategy = _build_new_coin_strategy()
        strategy.positions = {'HUTUSDT': {}}
        strategy.trading_executor = MagicMock()
        strategy.trading_executor.build_occupancy_report = AsyncMock(
            return_value=({'HUTUSDT': 50.0}, {'HUTUSDT': 4.0})
        )

        await strategy._sync_open_positions_to_db()

        assert calls == [('new_coin', {'HUTUSDT': 50.0}, {'HUTUSDT': 4.0})]

    @pytest.mark.asyncio
    async def test_no_executor_returns_early(self):
        strategy = _build_new_coin_strategy()
        strategy.trading_executor = None

        await strategy._sync_open_positions_to_db()   # 不应抛异常


# ============================================================================
# 5. btc_eth / btc_eth_aggressive / hrs 接入（统一保证金口径）
# ============================================================================

class TestMtpcsEntryLimits:
    """btc_eth（含 aggressive）：开仓前统一限额校验，口径为保证金"""

    @staticmethod
    def _build_strategy(strategy_cls, tmp_path, config: Dict[str, Any]):
        """构造仅含限额校验所需属性的 MTPCS 策略实例"""
        strategy = object.__new__(strategy_cls)
        strategy.positions = {}
        strategy.binance_config = {'leverage': {'S': 5, 'A': 4, 'B': 3, 'C': 2}}
        strategy.capital_mgr = CapitalManager(
            _write_config(tmp_path, config), strategy_id='btc_eth'
        )
        return strategy

    @staticmethod
    def _make_position(quantity: float, entry_price: float, grade: str) -> PositionState:
        pos = PositionState()
        pos.current_quantity = Decimal(str(quantity))
        pos.entry_price = Decimal(str(entry_price))
        pos.grade = grade
        return pos

    @pytest.mark.asyncio
    async def test_rejects_when_total_exceeds_limit(self, tmp_path):
        """占用 50 + 新增 100.5 > 限额 150 → 拒绝；恰好等于限额则放行"""
        strategy = self._build_strategy(BTCEthStrategy, tmp_path, _base_config())
        strategy.positions = {'BTCUSDT': self._make_position(10.0, 10.0, 'C')}   # 保证金 50
        signal = {'symbol': 'ETHUSDT', 'quantity': 0.2, 'entry_price': 1000.0, 'leverage': 2}

        assert await strategy._check_entry_limits('ETHUSDT', signal) is True       # 50 + 100 = 150

        signal['quantity'] = 0.201                                                # 50 + 100.5 > 150
        assert await strategy._check_entry_limits('ETHUSDT', signal) is False

    @pytest.mark.asyncio
    async def test_calc_current_total_margin_unified_formula(self, tmp_path):
        """占用口径：数量 × 入场价 / 该等级杠杆"""
        strategy = self._build_strategy(BTCEthStrategy, tmp_path, _base_config())
        strategy.positions = {
            'BTCUSDT': self._make_position(10.0, 10.0, 'C'),   # 10 × 10 / 2 = 50
            'ETHUSDT': self._make_position(10.0, 10.0, 'S'),   # 10 × 10 / 5 = 20
        }
        assert strategy._calc_current_total_margin() == pytest.approx(70.0)

    @pytest.mark.asyncio
    async def test_invalid_position_skipped(self, tmp_path):
        strategy = self._build_strategy(BTCEthStrategy, tmp_path, _base_config())
        strategy.positions = {'BTCUSDT': self._make_position(0.0, 10.0, 'C')}
        assert strategy._calc_current_total_margin() == 0.0

    @pytest.mark.asyncio
    async def test_aggressive_shares_btc_eth_allocation(self, tmp_path):
        """激进版 strategy_id 固定为 btc_eth：与原版共用同一份月度分配"""
        strategy = object.__new__(BTCEthAggressiveStrategy)
        strategy.positions = {}
        strategy.binance_config = {'leverage': {'S': 5, 'A': 4, 'B': 3, 'C': 2}}
        config = _base_config({'trading': {'total_position_margin_limit': 1000.0}})
        strategy.capital_mgr = CapitalManager(
            _write_config(tmp_path, config),
            db=_mock_db({'entries': _entries(156.33, strategy_id='btc_eth')}),
            strategy_id='btc_eth',
        )
        assert strategy.capital_mgr.strategy_id == 'btc_eth'

        limit, source = await strategy.capital_mgr.get_effective_margin_limit()
        assert limit == pytest.approx(156.33)
        assert source == 'db'

        strategy.positions = {'BTCUSDT': self._make_position(10.0, 10.0, 'C')}
        signal = {'symbol': 'BTCUSDT', 'quantity': 0.5, 'entry_price': 100.0, 'leverage': 2}
        # 占用 50 + 新增 25 = 75 ≤ 156.33 → 放行
        assert await strategy._check_entry_limits('BTCUSDT', signal) is True


class TestHrsEntryLimit:
    """hrs：保证金口径占用统计与限额入口接入"""

    @staticmethod
    def _build_strategy(leverage: float = 2) -> HRSStrategy:
        """构造仅含限额校验所需属性的 HRS 策略实例"""
        strategy = object.__new__(HRSStrategy)
        strategy.capital_mgr = CapitalManager('strategies/hrs/config.yaml', strategy_id='hrs')
        strategy.position_manager = MagicMock()
        strategy.trading_executor = MagicMock()
        strategy.trading_executor.leverage = leverage
        return strategy

    def test_calc_current_total_margin_sums_absolute_quantities(self):
        """多空仓位统一取绝对值，避免相互抵消导致占用被低估"""
        strategy = self._build_strategy(leverage=2)
        strategy.position_manager.get_all_positions.return_value = {
            'BTCUSDT': {'entry_quantity': -4, 'entry_price': 25},   # 4 × 25 / 2 = 50
            'ETHUSDT': {'quantity': 2, 'entry_price': 30},          # 2 × 30 / 2 = 30
        }
        assert strategy._calc_current_total_margin() == pytest.approx(80.0)

    def test_calc_current_total_margin_skips_invalid(self):
        strategy = self._build_strategy()
        strategy.position_manager.get_all_positions.return_value = {
            'BTCUSDT': {'entry_quantity': 0, 'entry_price': 25},
            'ETHUSDT': {'entry_quantity': 2, 'entry_price': 0},
        }
        assert strategy._calc_current_total_margin() == 0.0

    def test_calc_current_total_margin_empty(self):
        strategy = self._build_strategy()
        strategy.position_manager.get_all_positions.return_value = {}
        assert strategy._calc_current_total_margin() == 0.0

    @pytest.mark.asyncio
    async def test_bind_database_defers_db_injection(self, tmp_path):
        """hrs 的 db 在 initialize 阶段注入：bind 前走 config，bind 后走 DB"""
        config = {
            'strategy': {'name': 'hrs'},
            'capital_limits': {'monthly_limit': 200.0},
            'trading': {'total_position_margin_limit': 1000.0},
        }
        mgr = CapitalManager(_write_config(tmp_path, config), strategy_id='hrs')
        limit, source = await mgr.get_effective_margin_limit()
        assert (limit, source) == (pytest.approx(200.0), 'config')

        mgr.bind_database(_mock_db({'entries': _entries(120.0, strategy_id='hrs')}), 'hrs')
        limit, source = await mgr.get_effective_margin_limit()
        assert (limit, source) == (pytest.approx(120.0), 'db')


# ============================================================================
# 6. 补充覆盖（A~E 组中现有用例未覆盖的分支 / 边界）
# ============================================================================

class TestLimitChainGaps:
    """A 组补充：status 过滤契约、静态兜底缺失/相等、配置非法值与配置缺失"""

    @pytest.mark.asyncio
    async def test_db_query_filters_active_status(self, tmp_path):
        """A5：SQL 必须按 status='active' 过滤，非 active 记录不会被读到"""
        db = _mock_db(None)
        mgr = CapitalManager(_write_config(tmp_path, _base_config()), db=db, strategy_id='new_coin')
        await mgr.get_effective_margin_limit()
        sql = db.fetch_one.await_args.args[0]
        assert "status = 'active'" in sql

    @pytest.mark.asyncio
    async def test_inactive_status_equivalent_no_row_falls_back(self, tmp_path):
        """A5：status!=active 等价于查不到记录 → 降级到 config.capital_limits.monthly_limit"""
        config = _base_config({
            'capital_limits': {'monthly_limit': 210.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        mgr = CapitalManager(_write_config(tmp_path, config), db=_mock_db(None), strategy_id='new_coin')
        assert await mgr.get_effective_margin_limit() == (pytest.approx(210.0), 'config')

    @pytest.mark.asyncio
    async def test_monthly_without_static_uses_monthly(self, tmp_path):
        """A1/A3：静态兜底缺失（None）时月度额仍直接生效（不触发不一致告警分支）"""
        config = _base_config({
            'capital_limits': {'monthly_limit': 180.0},
            'trading': {'total_position_margin_limit': None},
        })
        mgr = CapitalManager(_write_config(tmp_path, config), db=_mock_db(None), strategy_id='new_coin')
        assert await mgr.get_effective_margin_limit() == (pytest.approx(180.0), 'config')

    @pytest.mark.asyncio
    async def test_monthly_equal_to_static_uses_monthly(self, tmp_path):
        """A1：月度额与静态兜底相等时不报错，取月度额来源"""
        config = _base_config({
            'capital_limits': {'monthly_limit': 150.0},
            'trading': {'total_position_margin_limit': 150.0},
        })
        mgr = CapitalManager(_write_config(tmp_path, config), db=_mock_db(None), strategy_id='new_coin')
        assert await mgr.get_effective_margin_limit() == (pytest.approx(150.0), 'config')

    @pytest.mark.asyncio
    async def test_invalid_monthly_config_value_falls_back_to_static(self, tmp_path):
        """A2/A3：config.monthly_limit 为非数值 → 视为不可用，回退静态兜底"""
        config = _base_config({'capital_limits': {'monthly_limit': 'abc'}})
        mgr = CapitalManager(_write_config(tmp_path, config), db=_mock_db(None), strategy_id='new_coin')
        assert await mgr.get_effective_margin_limit() == (pytest.approx(150.0), 'static')

    @pytest.mark.asyncio
    async def test_missing_config_path_is_fail_open(self):
        """A4：配置路径为空（读不到任何配置）→ fail-open，不误判超限"""
        mgr = CapitalManager('', db=None, strategy_id='new_coin')
        assert await mgr.get_effective_margin_limit() == (None, 'none')
        assert await mgr.can_open_within_limit(999.0, 999.0) == (True, "")


class TestCanOpenBoundaries:
    """B 组补充：超出 0.01、限额为 0、零/负输入边界"""

    @pytest.mark.asyncio
    async def test_exceed_by_one_cent_rejects(self, tmp_path):
        """B7：超出限额 0.01 → 拒绝"""
        config = _base_config({
            'capital_limits': {'monthly_limit': 150.0},
            'trading': {'total_position_margin_limit': 150.0},
        })
        mgr = CapitalManager(_write_config(tmp_path, config), strategy_id='new_coin')
        allowed, reason = await mgr.can_open_within_limit(100.0, 50.01)
        assert allowed is False
        assert reason == "总持仓保证金超限(150.01/150.00)"

    @pytest.mark.asyncio
    async def test_zero_limit_rejects_any_new_position(self, tmp_path):
        """B8：限额为 0（config 月度额）→ 任何正保证金新仓都拒绝"""
        config = _base_config({
            'capital_limits': {'monthly_limit': 0.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        mgr = CapitalManager(_write_config(tmp_path, config), strategy_id='new_coin')
        assert await mgr.get_effective_margin_limit() == (pytest.approx(0.0), 'config')
        allowed, reason = await mgr.can_open_within_limit(0.0, 50.0)
        assert allowed is False
        assert reason == "总持仓保证金超限(50.00/0.00)"

    @pytest.mark.asyncio
    async def test_db_zero_allocation_rejects_any_open(self, tmp_path):
        """B8：DB 月度分配额为 0（合法值，非缺失）→ 拒绝任何新仓"""
        config = _base_config({'trading': {'total_position_margin_limit': 1000.0}})
        mgr = CapitalManager(
            _write_config(tmp_path, config),
            db=_mock_db({'entries': _entries(0.0)}),
            strategy_id='new_coin',
        )
        assert await mgr.get_effective_margin_limit() == (pytest.approx(0.0), 'db')
        assert (await mgr.can_open_within_limit(0.0, 25.0))[0] is False

    @pytest.mark.asyncio
    async def test_zero_inputs_are_allowed(self, tmp_path):
        """B9：occupied=0 且 new=0 → 0 ≤ 限额，放行（零仓位不构成超限）"""
        mgr = CapitalManager(_write_config(tmp_path, _base_config()), strategy_id='new_coin')
        assert await mgr.can_open_within_limit(0.0, 0.0) == (True, "")

    @pytest.mark.asyncio
    async def test_zero_new_margin_at_exact_limit_allowed(self, tmp_path):
        """B9：占用恰好等于限额且不新增 → 放行"""
        mgr = CapitalManager(_write_config(tmp_path, _base_config()), strategy_id='new_coin')
        assert await mgr.can_open_within_limit(150.0, 0.0) == (True, "")

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=True,
        reason="已知防御缺口：负 new_margin 会抵消已超限占用而误放行（实际调用方由 calc_position_margin 保证非负）",
    )
    async def test_negative_new_margin_does_not_bypass_limit(self, tmp_path):
        """B9：占用已超限（249.30 > 150）时，负数 new_margin 不得使其误放行"""
        mgr = CapitalManager(_write_config(tmp_path, _base_config()), strategy_id='new_coin')
        allowed, _ = await mgr.can_open_within_limit(249.30, -100.0)
        assert allowed is False


class TestShrinkAndThresholdGaps:
    """C 组补充：min_position_margin 未配置时限额缩仓仍生效"""

    @pytest.mark.asyncio
    async def test_min_margin_none_still_shrinks_by_limit(self, tmp_path):
        """C13：未配置门槛时不做门槛拦截，但限额缩仓仍生效（剩余 20 → 缩仓到 0.4 张）"""
        config = _base_config({
            'trading': {'min_position_margin': None, 'total_position_margin_limit': 150.0},
        })
        executor = _build_executor(tmp_path, overrides=config, db=_mock_db(None))
        _stub_entry_checks(executor, occupied_margin=130.0)
        _stub_order_path(executor)

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert reason == ""
        assert order is not None
        placed_quantity = executor._place_short_order.await_args.args[1]
        assert float(placed_quantity) == pytest.approx(0.4)

    @pytest.mark.asyncio
    async def test_min_margin_none_quota_exhausted_still_rejects(self, tmp_path):
        """C12+C13：门槛未配置但额度耗尽 → 仍拒开（走仓位大小为 0 分支）"""
        config = _base_config({'trading': {'min_position_margin': None}})
        executor = _build_executor(tmp_path, overrides=config, db=_mock_db(None))
        _stub_entry_checks(executor, occupied_margin=200.0)   # 限额 150，已超限

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert order is None
        assert reason == "仓位大小计算失败"
        executor.binance_api.place_order.assert_not_called()


class TestPositionBaselineGaps:
    """D 组补充：contractSize 负值估算 + check_entry_within_limit 直接覆盖"""

    def test_contract_size_negative_estimated_as_one(self):
        """D15：contractSize 为负值（非法）→ 按 1 估算，不得返回 0 低估占用"""
        assert calc_position_margin(-2, 100, -1, 2) == pytest.approx(100.0)

    @staticmethod
    def _build_mtcs_strategy(tmp_path, total_margin: float) -> BTCEthStrategy:
        strategy = object.__new__(BTCEthStrategy)
        strategy.positions = {}
        strategy.binance_config = {'leverage': {'S': 5, 'A': 4, 'B': 3, 'C': 2}}
        strategy.capital_mgr = CapitalManager(
            _write_config(tmp_path, _base_config()), strategy_id='btc_eth'
        )
        strategy._calc_current_total_margin = lambda: total_margin
        return strategy

    @pytest.mark.asyncio
    async def test_check_entry_within_limit_allows_and_rejects(self, tmp_path):
        """D17 关联：共享入口在同一函数内放行/拒绝（占用 50 + 新增 100 = 150 放行，超出拒绝）"""
        strategy = self._build_mtcs_strategy(tmp_path, total_margin=50.0)
        signal = {'symbol': 'ETHUSDT', 'quantity': 0.1, 'entry_price': 1000.0, 'leverage': 2}

        assert await check_entry_within_limit(strategy, 'ETHUSDT', signal, 1) is True

        signal['quantity'] = 0.3            # 新增 150 → 50 + 150 = 200 > 150
        assert await check_entry_within_limit(strategy, 'ETHUSDT', signal, 1) is False

    @pytest.mark.asyncio
    async def test_check_entry_within_limit_clamps_signal_leverage(self, tmp_path):
        """D17 关联：信号杠杆低于下限时按 min_leverage 兜底（防除零）"""
        strategy = self._build_mtcs_strategy(tmp_path, total_margin=0.0)
        # leverage=1 < min_leverage=2 → 新增保证金 = 0.3 × 1000 / 2 = 150 ≤ 150 → 放行
        signal = {'symbol': 'ETHUSDT', 'quantity': 0.3, 'entry_price': 1000.0, 'leverage': 1}
        assert await check_entry_within_limit(strategy, 'ETHUSDT', signal, 2) is True


class TestDbFallbackGaps:
    """E 组补充：超时沿用最近成功值 + entries 解析边界"""

    @pytest.mark.asyncio
    async def test_db_timeout_uses_last_success_value(self, tmp_path):
        """E19：DB 超时（asyncio.TimeoutError）→ 沿用最近一次成功值（db_stale），限额不失效"""
        config = _base_config({
            'trading': {'total_position_margin_limit': 1000.0},
            'capital_limits': {'db_cache_ttl_seconds': 0.01, 'db_query_timeout_seconds': 0.01},
        })
        db = _mock_db({'entries': _entries(156.33)})
        mgr = CapitalManager(_write_config(tmp_path, config), db=db, strategy_id='new_coin')
        first_limit, first_source = await mgr.get_effective_margin_limit()
        assert (first_limit, first_source) == (pytest.approx(156.33), 'db')

        await asyncio.sleep(0.02)                     # 令缓存过期

        async def _slow_query(*_args, **_kwargs):
            await asyncio.sleep(0.1)                  # 超过 0.01s 超时阈值
            return {'entries': _entries(999.0)}

        db.fetch_one = AsyncMock(side_effect=_slow_query)
        limit, source = await mgr.get_effective_margin_limit()
        assert (limit, source) == (pytest.approx(156.33), 'db_stale')

    @pytest.mark.parametrize("raw,expected", [
        (None, None),                 # 字段缺失
        (123, None),                  # 非法类型（非 list/str）
        ('{"a": 1}', None),           # JSON 对象（非列表）
        ('123', None),                # JSON 标量（非列表）
        ('[not-json', None),          # 非法 JSON
        ('[]', []),                   # 合法空列表
        ([{'a': 1}], [{'a': 1}]),     # 已解析列表
    ])
    def test_parse_entries_handles_all_types(self, tmp_path, raw, expected):
        """E21：entries 解析覆盖 JSON 字符串 / list / 非法类型"""
        mgr = CapitalManager(_write_config(tmp_path, _base_config()), strategy_id='new_coin')
        assert mgr._parse_entries(raw) == expected

    @pytest.mark.asyncio
    async def test_entries_json_object_string_falls_back(self, tmp_path):
        """E21：entries 为合法 JSON 但不是列表 → 解析失败 → 降级"""
        config = _base_config({
            'capital_limits': {'monthly_limit': 210.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        mgr = CapitalManager(
            _write_config(tmp_path, config), db=_mock_db({'entries': '{"a": 1}'}), strategy_id='new_coin'
        )
        assert await mgr.get_effective_margin_limit() == (pytest.approx(210.0), 'config')

    @pytest.mark.asyncio
    async def test_entries_non_list_type_falls_back(self, tmp_path):
        """E21：entries 为非法类型（整数）→ 降级"""
        config = _base_config({
            'capital_limits': {'monthly_limit': 210.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        mgr = CapitalManager(
            _write_config(tmp_path, config), db=_mock_db({'entries': 123}), strategy_id='new_coin'
        )
        assert await mgr.get_effective_margin_limit() == (pytest.approx(210.0), 'config')

    @pytest.mark.asyncio
    async def test_entries_with_non_dict_items_skipped(self, tmp_path):
        """E21：entries 列表含非 dict 元素 → 跳过，仍能匹配到本策略条目"""
        config = _base_config({'trading': {'total_position_margin_limit': 1000.0}})
        db = _mock_db({'entries': [None, 'x', {'strategy_id': 'new_coin', 'allocated_amount': 77.7}]})
        mgr = CapitalManager(_write_config(tmp_path, config), db=db, strategy_id='new_coin')
        assert await mgr.get_effective_margin_limit() == (pytest.approx(77.7), 'db')

    @pytest.mark.asyncio
    async def test_matched_entry_null_amount_falls_back(self, tmp_path):
        """E22：本策略条目 allocated_amount 为 null → 视为不可用 → 降级，不崩溃"""
        config = _base_config({
            'capital_limits': {'monthly_limit': 210.0},
            'trading': {'total_position_margin_limit': 1000.0},
        })
        db = _mock_db({'entries': [{'strategy_id': 'new_coin', 'allocated_amount': None}]})
        mgr = CapitalManager(_write_config(tmp_path, config), db=db, strategy_id='new_coin')
        assert await mgr.get_effective_margin_limit() == (pytest.approx(210.0), 'config')

    @pytest.mark.asyncio
    async def test_numeric_string_amount_parsed(self, tmp_path):
        """E22：allocated_amount 为数值字符串 → 正常解析"""
        config = _base_config({'trading': {'total_position_margin_limit': 1000.0}})
        db = _mock_db({'entries': [{'strategy_id': 'new_coin', 'allocated_amount': '156.33'}]})
        mgr = CapitalManager(_write_config(tmp_path, config), db=db, strategy_id='new_coin')
        assert await mgr.get_effective_margin_limit() == (pytest.approx(156.33), 'db')


class TestIncidentRegressionExecutorLevel:
    """事故验收目标：执行器层确认 DB 月度额 156.33（而非静态 150）主导缩仓"""

    @pytest.mark.asyncio
    async def test_db_monthly_156_33_governs_shrink_over_static_150(self, tmp_path):
        """事故回归：占用 120 时，可用额度 = 156.33 - 120 = 36.33（旧 min 语义会缩到 30）"""
        executor = _build_executor(tmp_path, db=_mock_db({'entries': _entries(156.33)}))
        _stub_entry_checks(executor, occupied_margin=120.0)
        _stub_order_path(executor)

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert reason == ""
        assert order is not None
        placed_quantity = executor._place_short_order.await_args.args[1]
        # 缩仓后数量 = 36.33 × 2 / 100 = 0.7266，按 step_size 0.001 向下取整为 0.726
        # （若被静态 150 压制则缩到 30 → 0.6，两者可区分）
        assert float(placed_quantity) == pytest.approx(0.726, abs=1e-4)


# ============================================================================
# 遗留缺陷回归：基线同步到 position_tracking（P0 / AC-3）
# ============================================================================

class TestBaselineTrackingSync:
    """P0：基线重建后必须把持仓同步进 executor.position_tracking，占用不得被记为 0"""

    @pytest.mark.asyncio
    async def test_rebuild_populates_tracking_and_occupied_margin(self, tmp_path):
        """重启后交易所已有持仓、position_tracking 为空 → 重建后占用真实（非 0）"""
        exchange_positions = [
            {'symbol': 'PATHUSDT', 'positionAmt': -749, 'markPrice': 0.4, 'entryPrice': 0.5}
        ]
        executor = _build_executor(tmp_path)
        executor.binance_api.get_position = AsyncMock(return_value=exchange_positions)
        executor.position_tracking = {}
        executor.baseline_ready = False

        strategy = _build_new_coin_strategy()
        strategy.positions = {}
        strategy.trading_executor = executor
        strategy.binance_client = MagicMock()
        strategy.binance_client.get_position = AsyncMock(return_value=exchange_positions)

        await strategy._rebuild_position_baseline()

        track = executor.position_tracking['PATHUSDT']
        assert track['entry_quantity'] == pytest.approx(749.0)
        assert track['entry_price'] == pytest.approx(0.5)   # 本地缺失 → 用交易所 entryPrice
        assert 'algo_ids' in track

        # 核心：占用保证金按交易所真实持仓计算，不得再被记为 0
        # 749 × 0.4 × 1（contractSize 缺失按 1 估算）/ 2 = 149.8
        occupied = await executor.calc_current_occupied_margin()
        assert occupied == pytest.approx(149.8)
        assert occupied > 0

    @pytest.mark.asyncio
    async def test_rebuild_keeps_existing_algo_ids_and_atr(self):
        """基线同步不得覆盖已恢复的 algo_ids（条件单）与 atr 字段"""
        exchange_positions = [
            {'symbol': 'HUTUSDT', 'positionAmt': -4, 'markPrice': 25, 'entryPrice': 30}
        ]
        executor = _build_baseline_executor()
        executor.position_tracking = {'HUTUSDT': {'algo_ids': {'sl': 111}, 'atr': 1.23}}

        strategy = _build_new_coin_strategy()
        strategy.positions = {}
        strategy.trading_executor = executor
        strategy.binance_client = MagicMock()
        strategy.binance_client.get_position = AsyncMock(return_value=exchange_positions)

        await strategy._rebuild_position_baseline()

        track = executor.position_tracking['HUTUSDT']
        assert track['algo_ids'] == {'sl': 111}     # 保留，不被覆盖
        assert track['atr'] == pytest.approx(1.23)  # 保留，不被覆盖
        assert track['entry_quantity'] == pytest.approx(4.0)
        assert track['entry_price'] == pytest.approx(30.0)


# ============================================================================
# 遗留缺陷回归：杠杆配置非法拒绝开仓（P0 / R3）
# ============================================================================

class TestInvalidLeverageRejection:
    """P0：leverage 缺失 / ≤ 0 属配置错误，禁止开仓并告警（不得猜测默认值）"""

    @pytest.mark.parametrize("leverage", [None, 0, -2])
    @pytest.mark.asyncio
    async def test_invalid_leverage_rejects_open(self, tmp_path, leverage):
        config = _base_config({'trading': {'leverage': leverage}})
        executor = _build_executor(tmp_path, overrides=config)
        _stub_entry_checks(executor)
        _stub_order_path(executor)

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert order is None
        assert reason == "杠杆配置缺失或非法，禁止开仓"
        assert executor._leverage_valid is False
        assert executor.leverage == Decimal('0')
        executor.binance_api.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_leverage_marks_valid_and_allows_open(self, tmp_path):
        """回归：合法杠杆时标志为 True，正常放行"""
        executor = _build_executor(tmp_path)
        _stub_entry_checks(executor)
        _stub_order_path(executor)

        order, reason = await executor.execute_short('HUTUSDT', {'total_score': 7.4}, 100.0)

        assert executor._leverage_valid is True
        assert reason == ""
        assert order is not None


# ============================================================================
# P1：_resolve_open_margin_budget 拆分后子函数行为一致性
# ============================================================================

class TestOpenMarginBudgetDecomposition:
    """P1：5 步额度核算拆分为子函数后，各步骤行为与判定顺序保持不变"""

    @pytest.mark.asyncio
    async def test_read_available_budget_returns_occupied_limit_and_avail(self, tmp_path):
        executor = _build_executor(tmp_path, db=_mock_db({'entries': _entries(156.33)}))
        executor.calc_current_occupied_margin = AsyncMock(return_value=100.0)

        occupied, limit, source, avail = await executor._read_available_budget('HUTUSDT')

        assert occupied == pytest.approx(100.0)
        assert limit == pytest.approx(156.33)
        assert source == "db"
        assert avail == pytest.approx(56.33)

    @pytest.mark.asyncio
    async def test_read_available_budget_fail_open_returns_none_avail(self, tmp_path):
        config = _base_config({'trading': {'total_position_margin_limit': None}})
        executor = _build_executor(tmp_path, overrides=config)
        executor.calc_current_occupied_margin = AsyncMock(return_value=10.0)

        _, limit, source, avail = await executor._read_available_budget('HUTUSDT')

        assert limit is None
        assert avail is None
        assert source == "none"

    def test_reject_if_below_min_margin_boundaries(self, tmp_path):
        """门槛判定：等于门槛放行，低于门槛拒开（min_position_margin=25）"""
        executor = _build_executor(tmp_path)
        assert executor._reject_if_below_min_margin(
            'HUTUSDT', 25.0, 0.0, 150.0, 'static', 125.0
        ) is None
        assert executor._reject_if_below_min_margin(
            'HUTUSDT', 24.99, 0.0, 150.0, 'static', 125.0
        ) == "可用额度不足(缩仓后保证金24.99 < 门槛25.00)"

    def test_reject_if_below_min_margin_skips_without_threshold(self, tmp_path):
        config = _base_config({'trading': {'min_position_margin': None}})
        executor = _build_executor(tmp_path, overrides=config)
        assert executor._reject_if_below_min_margin(
            'HUTUSDT', 0.0, 0.0, None, 'none', None
        ) is None

    @pytest.mark.asyncio
    async def test_enforce_limit_check_passes_and_rejects(self, tmp_path):
        executor = _build_executor(tmp_path)
        assert await executor._enforce_limit_check(
            'HUTUSDT', 50.0, 50.0, 150.0, 'static'
        ) is None
        reject = await executor._enforce_limit_check('HUTUSDT', 150.0, 50.0, 150.0, 'static')
        assert reject == "总持仓保证金超限(200.00/150.00)"

    def test_match_exchange_short_qty_extracts_short_only(self):
        """拆出的交易所数量匹配：仅识别空头（positionAmt < 0）"""
        positions = [
            {'symbol': 'HUTUSDT', 'positionAmt': 3},     # 多头，忽略
            {'symbol': 'PATHUSDT', 'positionAmt': -7.33},
        ]
        assert TradingExecutor._match_exchange_short_qty('HUTUSDT', positions) is None
        assert TradingExecutor._match_exchange_short_qty('PATHUSDT', positions) == pytest.approx(7.33)
        assert TradingExecutor._match_exchange_short_qty('NONEUSDT', positions) is None