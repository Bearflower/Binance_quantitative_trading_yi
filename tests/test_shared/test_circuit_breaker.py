"""
组合级熔断器单测（需求 v1.1 第 4.6 节真值表全覆盖）

覆盖：
- S1/S2/S3 与 L1/L2/L3 六场景（含"等于阈值=放行"边界、组合级优先于单币级、不串扰）
- 冷却规则：命中 S2 后同币 LONG 也拦（跨方向）、到期自动恢复、不影响其它币
- 指数缺失 fail-open、单币涨幅缺失不误拦（组合级仍生效）
- load_index（DB 查询 + 同整点缓存）
- compute_1h_return / parse_cron / floor_index_hour
- _compute_pool_index（cap±10% 截断、MIN_SYMBOLS 下限、单币异常跳过）
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
# 项目根加入 sys.path，使 shared 包可导入
sys.path.insert(0, str(PROJECT_ROOT))

from shared.circuit_breaker import (
    BEIJING_TZ,
    CircuitBreaker,
    compute_1h_return,
    compute_cumulative_return,
    floor_index_hour,
    load_circuit_breaker_config,
    parse_cron,
)

# market_circuit_breaker_job 位于 dashboard/backend/services 下，
# 与项目根 services/ 命名空间包同名（全量收集时 kline 测试先 import
# services.kline_service 会缓存仅含项目根路径的 services 命名空间包，
# 后续再插 dashboard/backend 也不会重新扫描），故按文件路径显式加载，
# 绕开 services 包名解析，保证单独运行与全量运行行为一致。
_JOB_PATH = PROJECT_ROOT / "dashboard" / "backend" / "services" / "market_circuit_breaker_job.py"
_spec = importlib.util.spec_from_file_location("market_circuit_breaker_job", _JOB_PATH)
_job_module = importlib.util.module_from_spec(_spec)
sys.modules["market_circuit_breaker_job"] = _job_module
_spec.loader.exec_module(_job_module)

from market_circuit_breaker_job import (
    MIN_SYMBOLS,
    _compute_pool_index,
)


@pytest.fixture
def breaker():
    """熔断器实例（读真实共享配置，db=None 便于纯判定测试）"""
    cfg = load_circuit_breaker_config()
    assert cfg, "组合级熔断配置加载失败"
    return CircuitBreaker(cfg, db=None, pool="mtpcs")


# ==================== S 方向（做空）判定 ====================

class TestGuardShort:
    """SHORT 方向：S1/S2/S3 真值表"""

    def test_s1_pool_blocked(self, breaker):
        """S1：池指数 > +2%，单币涨幅任意值 → 拦（组合级 pool_short）"""
        for symbol_ret in (0.0, 0.03, -0.05):
            allow, level = breaker.guard("short", "BTCUSDT", symbol_ret, 0.03)
            assert (allow, level) == (False, "pool_short")

    def test_s1_pool_equal_threshold_allow(self, breaker):
        """边界：池指数 == +2%（等于阈值=放行），单币 ≤3% → 放行"""
        allow, level = breaker.guard("short", "BTCUSDT", 0.01, 0.02)
        assert (allow, level) == (True, "allow")

    def test_s2_coin_blocked(self, breaker):
        """S2：池指数 ≤2% 且单币涨幅 > +3% → 拦（单币级 coin_short，进冷却）"""
        allow, level = breaker.guard("short", "ETHUSDT", 0.05, 0.0)
        assert (allow, level) == (False, "coin_short")
        assert breaker.is_in_cooldown("ETHUSDT")

    def test_s2_coin_equal_threshold_allow(self, breaker):
        """边界：单币涨幅 == +3%（等于阈值=放行）"""
        allow, level = breaker.guard("short", "BTCUSDT", 0.03, 0.0)
        assert (allow, level) == (True, "allow")

    def test_s3_allow(self, breaker):
        """S3：池指数 ≤2% 且单币涨幅 ≤3% → 放行"""
        allow, level = breaker.guard("short", "BTCUSDT", 0.02, 0.01)
        assert (allow, level) == (True, "allow")

    def test_combined_precedence_over_coin(self, breaker):
        """组合级优先于单币级：S1 命中时不进冷却、返回 pool_short"""
        allow, level = breaker.guard("short", "SOLUSDT", 0.05, 0.03)
        assert (allow, level) == (False, "pool_short")
        assert not breaker.is_in_cooldown("SOLUSDT")

    def test_pool_up_does_not_block_long(self, breaker):
        """不串扰：池上涨（>+2%）时做多放行（L 方向不受 S1 影响）"""
        allow, level = breaker.guard("long", "BTCUSDT", 0.01, 0.03)
        assert (allow, level) == (True, "allow")


# ==================== L 方向（做多）判定 ====================

class TestGuardLong:
    """LONG 方向：L1/L2/L3 真值表"""

    def test_l1_pool_blocked(self, breaker):
        """L1：池指数 < -2%，单币涨幅任意值 → 拦（组合级 pool_long）"""
        for symbol_ret in (0.0, 0.03, -0.05):
            allow, level = breaker.guard("long", "BTCUSDT", symbol_ret, -0.03)
            assert (allow, level) == (False, "pool_long")

    def test_l1_pool_equal_threshold_allow(self, breaker):
        """边界：池指数 == -2%（等于阈值=放行），单币跌幅 ≤3% → 放行"""
        allow, level = breaker.guard("long", "BTCUSDT", -0.01, -0.02)
        assert (allow, level) == (True, "allow")

    def test_l2_coin_blocked(self, breaker):
        """L2：池指数 ≥-2% 且单币跌幅 > +3% → 拦（单币级 coin_long，进冷却）"""
        allow, level = breaker.guard("long", "ETHUSDT", -0.05, 0.0)
        assert (allow, level) == (False, "coin_long")
        assert breaker.is_in_cooldown("ETHUSDT")

    def test_l2_coin_equal_threshold_allow(self, breaker):
        """边界：单币跌幅 == 3%（等于阈值=放行）"""
        allow, level = breaker.guard("long", "BTCUSDT", -0.03, 0.0)
        assert (allow, level) == (True, "allow")

    def test_l3_allow(self, breaker):
        """L3：池指数 ≥-2% 且单币跌幅 ≤3% → 放行"""
        allow, level = breaker.guard("long", "BTCUSDT", -0.02, -0.01)
        assert (allow, level) == (True, "allow")

    def test_pool_down_does_not_block_short(self, breaker):
        """不串扰：池下跌（<-2%）时做空放行（S 方向不受 L1 影响）"""
        allow, level = breaker.guard("short", "BTCUSDT", -0.01, -0.03)
        assert (allow, level) == (True, "allow")


# ==================== 冷却规则 ====================

class TestCooldown:
    """冷却：跨方向拦截、到期自动恢复、不影响其它币"""

    def test_cooldown_cross_direction(self, breaker):
        """命中 S2 后，同币 LONG 也被拦（冷却不分方向）"""
        allow, level = breaker.guard("short", "ETHUSDT", 0.05, 0.0)
        assert (allow, level) == (False, "coin_short")
        allow, level = breaker.guard("long", "ETHUSDT", -0.01, 0.0)
        assert (allow, level) == (False, "cooldown")

    def test_cooldown_other_symbol_unaffected(self, breaker):
        """A 币冷却不影响 B 币开仓"""
        breaker.guard("short", "ETHUSDT", 0.05, 0.0)
        allow, level = breaker.guard("short", "BTCUSDT", 0.01, 0.0)
        assert (allow, level) == (True, "allow")

    def test_cooldown_expiry_auto_recover(self, breaker):
        """冷却到期自动恢复（无需额外解除动作）"""
        breaker.guard("short", "ETHUSDT", 0.05, 0.0)
        assert breaker.is_in_cooldown("ETHUSDT")
        # 模拟冷却到期：把截止时间拨回过去
        breaker._cooldown_until["ETHUSDT"] = datetime.now() - timedelta(seconds=1)
        assert not breaker.is_in_cooldown("ETHUSDT")
        allow, level = breaker.guard("short", "ETHUSDT", 0.01, 0.0)
        assert (allow, level) == (True, "allow")


# ==================== fail-open（缺失放行） ====================

class TestFailOpen:
    """指数/单币涨幅缺失均按放行处理"""

    def test_index_missing_short_allow(self, breaker):
        """指数缺失（None）时做空放行"""
        allow, level = breaker.guard("short", "BTCUSDT", 0.01, None)
        assert (allow, level) == (True, "allow")

    def test_index_missing_long_allow(self, breaker):
        """指数缺失（None）时做多放行"""
        allow, level = breaker.guard("long", "BTCUSDT", -0.01, None)
        assert (allow, level) == (True, "allow")

    def test_symbol_ret_missing_no_block(self, breaker):
        """单币涨幅缺失（None）不误拦；组合级仍独立生效"""
        allow, level = breaker.guard("short", "BTCUSDT", None, 0.0)
        assert (allow, level) == (True, "allow")
        # 组合级不依赖单币涨幅：池指数 > 阈值时即使单币涨幅缺失也拦
        allow, level = breaker.guard("short", "BTCUSDT", None, 0.03)
        assert (allow, level) == (False, "pool_short")


# ==================== load_index（DB 查询 + 缓存） ====================

class TestLoadIndex:
    """指数读取：查库 + 同整点缓存"""

    @pytest.fixture
    def index_hour(self):
        return floor_index_hour()

    async def test_load_index_found(self, index_hour):
        """命中返回 equal_weight"""
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value={"equal_weight": 0.03})
        cb = CircuitBreaker(load_circuit_breaker_config(), db=db, pool="mtpcs")
        value = await cb.load_index(index_hour)
        assert value == 0.03
        db.fetch_one.assert_awaited_once()

    async def test_load_index_missing(self, index_hour):
        """缺失返回 None（fail-open），并缓存避免重复查库"""
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value=None)
        cb = CircuitBreaker(load_circuit_breaker_config(), db=db, pool="mtpcs")
        assert await cb.load_index(index_hour) is None
        assert await cb.load_index(index_hour) is None
        # 同整点只查一次库（缓存生效）
        db.fetch_one.assert_awaited_once()

    async def test_load_index_db_error_fail_open(self, index_hour):
        """DB 异常返回 None（fail-open）"""
        db = MagicMock()
        db.fetch_one = AsyncMock(side_effect=RuntimeError("连接断开"))
        cb = CircuitBreaker(load_circuit_breaker_config(), db=db, pool="mtpcs")
        assert await cb.load_index(index_hour) is None

    async def test_load_index_db_none_fail_open(self, index_hour):
        """未绑定 DB（db=None）返回 None"""
        cb = CircuitBreaker(load_circuit_breaker_config(), db=None, pool="mtpcs")
        assert await cb.load_index(index_hour) is None


# ==================== 工具函数 ====================

class TestCompute1hReturn:
    """compute_1h_return"""

    def test_two_klines(self):
        """最近两根 1h K 线：(102-100)/100 = 0.02"""
        klines = [{"close": 100.0}, {"close": 102.0}]
        assert compute_1h_return(klines) == pytest.approx(0.02)

    def test_less_than_two(self):
        """不足 2 根返回 None"""
        assert compute_1h_return([]) is None
        assert compute_1h_return([{"close": 100.0}]) is None

    def test_prev_close_non_positive(self):
        """前根收盘价非正返回 None"""
        assert compute_1h_return([{"close": 0.0}, {"close": 102.0}]) is None


class TestParseCron:
    """parse_cron：5 字段 cron（分 时 日 月 周），返回 (hour, minute)"""

    def test_index_cron(self):
        """'3 * * * *' = 每小时 03 分 → hour='*', minute=3"""
        assert parse_cron("3 * * * *") == ("*", 3)

    def test_all_wildcard(self):
        """'* * * * *' → ('*', '*')"""
        assert parse_cron("* * * * *") == ("*", "*")

    def test_fixed_time(self):
        """'0 7 * * *' = 每天 07:00 → hour=7, minute=0"""
        assert parse_cron("0 7 * * *") == (7, 0)


class TestFloorIndexHour:
    """floor_index_hour：北京整点对齐"""

    def test_floor_to_hour(self):
        """非整点时刻对齐到北京整点"""
        dt = datetime(2026, 9, 21, 3, 45, 30)
        result = floor_index_hour(dt)
        assert result.hour == 3 and result.minute == 0 and result.second == 0
        assert result.tzinfo == BEIJING_TZ

    def test_utc_input_converted_to_beijing(self):
        """UTC 输入先转北京时区再取整点（UTC+8）"""
        utc_dt = datetime(2026, 9, 21, 20, 10, 0, tzinfo=__import__("datetime").timezone.utc)
        result = floor_index_hour(utc_dt)
        assert result.hour == 4 and result.minute == 0  # UTC 20:10 → 北京次日 04:10 → 整点 04:00


# ==================== 池指数计算 ====================

class TestComputePoolIndex:
    """_compute_pool_index：cap 截断、MIN_SYMBOLS 下限、单币异常跳过（1h 与 12h 双输出）"""

    @staticmethod
    def _klines(cur_close: float, base: float = 100.0, bars: int = 13) -> list:
        """构造 13 根 1h K 线：前 12 根 close=base，最后一根=cur_close。
        1h 涨幅与 12h 累计涨幅同源：均等于 (cur_close-base)/base。"""
        return [
            {"close": base, "open_time": i} for i in range(bars - 1)
        ] + [{"close": cur_close, "open_time": bars - 1}]

    class _FakeBinance:
        """可配置结果的假 Binance 客户端"""

        def __init__(self, results):
            self.results = results  # {symbol: klines 或 Exception}

        async def get_klines(self, symbol, interval="1h", limit=2):
            res = self.results[symbol]
            if isinstance(res, Exception):
                raise res
            return res

    @staticmethod
    def _unpack(result):
        """把 4 元组拆成 (1h 段, 12h 段)，便于按窗口断言"""
        equal_1h, valid_1h, equal_12h, valid_12h = result
        return (equal_1h, valid_1h), (equal_12h, valid_12h)

    def test_equal_weight_average(self):
        """等权均值：三币 ret 2% / 2% / -4% → 0.0（1h 与 12h 同步）"""
        client = self._FakeBinance({
            "A": self._klines(102),
            "B": self._klines(102),
            "C": self._klines(96),
        })
        (e1, s1), (e12, s12) = self._unpack(self._sync_compute(client, ["A", "B", "C"], cap=0.10))
        for eq in (e1, e12):
            assert eq == pytest.approx(0.0)
        assert s1 == ["A", "B", "C"]
        assert s12 == ["A", "B", "C"]

    def test_cap_truncation(self):
        """单币 ret 超出 ±10% 被截断：+50%/-50%/0% → mean(0.1,-0.1,0)=0.0（1h 与 12h 同步）"""
        client = self._FakeBinance({
            "A": self._klines(150),
            "B": self._klines(50),
            "C": self._klines(100),
        })
        (e1, _), (e12, _) = self._unpack(self._sync_compute(client, ["A", "B", "C"], cap=0.10))
        assert e1 == pytest.approx(0.0)
        assert e12 == pytest.approx(0.0)

    def test_min_symbols_not_met(self):
        """有效标的不足 MIN_SYMBOLS(3) 返回 None（1 个异常跳过 + 2 个有效）"""
        client = self._FakeBinance({
            "A": self._klines(102),
            "B": self._klines(102),
            "C": RuntimeError("网络错误"),
        })
        assert self._sync_compute(client, ["A", "B", "C"], cap=0.10) is None

    def test_symbol_exception_skipped(self):
        """单币异常跳过，其余正常参与均值"""
        client = self._FakeBinance({
            "A": self._klines(110),
            "B": RuntimeError("网络错误"),
            "C": self._klines(90),
            "D": self._klines(104),
        })
        _, (e12, s12) = self._unpack(self._sync_compute(client, ["A", "B", "C", "D"], cap=0.10))
        assert s12 == ["A", "C", "D"]
        # mean(0.1, -0.1, 0.04) = 0.01333...
        assert e12 == pytest.approx((0.10 - 0.10 + 0.04) / 3)

    def test_12h_span_requires_13_bars(self):
        """12h 累计需要 13 根 K 线：仅构造 2 根时 12h 无效、池整体视为不可用"""
        client = self._FakeBinance({
            "A": [{"close": 100, "open_time": 0}, {"close": 102, "open_time": 1}],
            "B": [{"close": 100, "open_time": 0}, {"close": 102, "open_time": 1}],
            "C": [{"close": 100, "open_time": 0}, {"close": 100, "open_time": 1}],
        })
        # 1h 有效 3 币但 12h 全部无效 → 整体 None（1h 与 12h 任一窗口不足即不可用）
        assert self._sync_compute(client, ["A", "B", "C"], cap=0.10) is None

    @staticmethod
    def _sync_compute(client, symbols, cap):
        """同步封装异步 _compute_pool_index（pytest asyncio auto 模式下直接 await 亦可）"""
        import asyncio
        return asyncio.run(_compute_pool_index(client, symbols, cap))


# ==================== 二期：12h 累计维度判定 ====================

class TestGuardShort12h:
    """SHORT 方向 12h 累计：pool_short_12h（慢牛拦空）"""

    def test_12h_pool_blocked(self, breaker):
        """12h 累计涨幅 > +2%，1h 瞬时 ≤2% → 拦（pool_short_12h）"""
        allow, level = breaker.guard("short", "BTCUSDT", 0.01, 0.01, 0.03)
        assert (allow, level) == (False, "pool_short_12h")

    def test_12h_equal_threshold_allow(self, breaker):
        """12h 累计涨幅 == +2%（等于阈值=放行）"""
        allow, level = breaker.guard("short", "BTCUSDT", 0.01, 0.01, 0.02)
        assert (allow, level) == (True, "allow")

    def test_1h_still_precedence(self):
        """1h 瞬时命中优先于 12h（同一组合级，先判 1h）"""
        cfg = load_circuit_breaker_config()
        cb = CircuitBreaker(cfg, db=None, pool="mtpcs")
        allow, level = cb.guard("short", "BTCUSDT", 0.01, 0.03, 0.03)
        assert (allow, level) == (False, "pool_short")

    def test_12h_missing_fail_open(self, breaker):
        """12h 指数缺失（None）不误拦；1h 不命中时放行"""
        allow, level = breaker.guard("short", "BTCUSDT", 0.01, 0.01, None)
        assert (allow, level) == (True, "allow")

    def test_include_12h_off_ignored(self):
        """include_12h=False 时回到一期行为：12h 命中不拦"""
        cfg = dict(load_circuit_breaker_config())
        cfg["include_12h"] = False
        cb = CircuitBreaker(cfg, db=None, pool="mtpcs")
        allow, level = cb.guard("short", "BTCUSDT", 0.01, 0.01, 0.05)
        assert (allow, level) == (True, "allow")


class TestGuardLong12h:
    """LONG 方向 12h 累计：pool_long_12h（阴跌拦多）"""

    def test_12h_pool_blocked(self, breaker):
        """12h 累计跌幅 > +2%（12h 指数 < -0.02），1h 瞬时 ≥-2% → 拦（pool_long_12h）"""
        allow, level = breaker.guard("long", "BTCUSDT", -0.01, -0.01, -0.03)
        assert (allow, level) == (False, "pool_long_12h")

    def test_12h_equal_threshold_allow(self, breaker):
        """12h 累计 == -2%（等于阈值=放行）"""
        allow, level = breaker.guard("long", "BTCUSDT", -0.01, -0.01, -0.02)
        assert (allow, level) == (True, "allow")

    def test_pool_12h_down_does_not_block_short(self, breaker):
        """不串扰：12h 阴跌拦多但不拦空"""
        allow, level = breaker.guard("short", "BTCUSDT", -0.01, 0.0, -0.03)
        assert (allow, level) == (True, "allow")

    def test_12h_missing_fail_open(self, breaker):
        allow, level = breaker.guard("long", "BTCUSDT", -0.01, 0.0, None)
        assert (allow, level) == (True, "allow")


class TestComputeCumulativeReturn:
    """compute_cumulative_return：12h 累计涨跌幅"""

    def test_12h_positive(self):
        """12 根后收盘 +3%：(103-100)/100 = 0.03"""
        klines = [{"close": 100.0}] * 12 + [{"close": 103.0}]
        assert compute_cumulative_return(klines, hours=12) == pytest.approx(0.03)

    def test_12h_negative(self):
        """12 根后收盘 -2%：(98-100)/100 = -0.02"""
        klines = [{"close": 100.0}] * 12 + [{"close": 98.0}]
        assert compute_cumulative_return(klines, hours=12) == pytest.approx(-0.02)

    def test_insufficient_bars(self):
        """不足 hours+1 根返回 None"""
        assert compute_cumulative_return([]) is None
        assert compute_cumulative_return([{"close": 100.0}] * 12, hours=12) is None

    def test_start_close_non_positive(self):
        """窗口起收盘价非正返回 None"""
        klines = [{"close": 0.0}] * 12 + [{"close": 100.0}]
        assert compute_cumulative_return(klines, hours=12) is None


class TestLoadIndex12h:
    """load_index_12h：查 equal_weight_12h 列 + 独立缓存键"""

    @pytest.fixture
    def index_hour(self):
        return floor_index_hour()

    async def test_load_12h_found(self, index_hour):
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value={"equal_weight_12h": 0.03})
        cb = CircuitBreaker(load_circuit_breaker_config(), db=db, pool="mtpcs")
        assert await cb.load_index_12h(index_hour) == 0.03
        db.fetch_one.assert_awaited_once()

    async def test_load_12h_null_returns_none(self, index_hour):
        """equal_weight_12h 为 NULL（旧数据未回填）返回 None（fail-open）"""
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value={"equal_weight_12h": None})
        cb = CircuitBreaker(load_circuit_breaker_config(), db=db, pool="mtpcs")
        assert await cb.load_index_12h(index_hour) is None

    async def test_load_12h_missing_row(self, index_hour):
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value=None)
        cb = CircuitBreaker(load_circuit_breaker_config(), db=db, pool="mtpcs")
        assert await cb.load_index_12h(index_hour) is None

    async def test_12h_and_1h_distinct_cache(self, index_hour):
        """1h 与 12h 各自缓存互不串扰（两次独立查询）"""
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value={"equal_weight": 0.01, "equal_weight_12h": 0.03})
        cb = CircuitBreaker(load_circuit_breaker_config(), db=db, pool="mtpcs")
        assert await cb.load_index(index_hour) == 0.01
        assert await cb.load_index_12h(index_hour) == 0.03
        assert db.fetch_one.await_count == 2


# ==================== 配置加载 ====================

class TestConfigLoading:
    """共享配置唯一事实源校验（与需求 4.3 阈值一致）"""

    def test_config_keys_and_values(self):
        """关键阈值键存在且与需求一致，无硬编码偏离"""
        cfg = load_circuit_breaker_config()
        assert cfg["enabled"] is True
        assert cfg["trigger_short"] == 0.02
        assert cfg["release_short"] == 0.01
        assert cfg["trigger_long"] == 0.02
        assert cfg["release_long"] == 0.01
        assert cfg["single_coin_short"] == 0.03
        assert cfg["single_coin_long"] == 0.03
        assert cfg["single_coin_cooldown_hours"] == 4
        assert cfg["per_symbol_ret_cap"] == 0.10
        assert cfg["min_symbols"] == 3
        assert cfg["index_cron"] == "3 * * * *"
        assert "BTCUSDT" in cfg["fixed_pool"]
        assert cfg["include_hrs_pool"] is True
        # 二期：12h 累计维度
        assert cfg["include_12h"] is True
        assert cfg["trigger_short_12h"] == 0.02
        assert cfg["trigger_long_12h"] == 0.02
        assert MIN_SYMBOLS == 3
