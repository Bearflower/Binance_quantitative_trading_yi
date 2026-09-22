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
    """_compute_pool_index：cap 截断、MIN_SYMBOLS 下限、单币异常跳过"""

    @staticmethod
    def _klines(prev_close: float, cur_close: float) -> list:
        """构造两根 1h K 线（含 close 字段）"""
        return [{"close": prev_close, "open_time": 0}, {"close": cur_close, "open_time": 1}]

    class _FakeBinance:
        """可配置结果的假 Binance 客户端"""

        def __init__(self, results):
            self.results = results  # {symbol: klines 或 Exception}

        async def get_klines(self, symbol, interval="1h", limit=2):
            res = self.results[symbol]
            if isinstance(res, Exception):
                raise res
            return res

    def test_equal_weight_average(self):
        """等权均值：三币 ret 2% / 2% / -4% → 0.0"""
        client = self._FakeBinance({
            "A": self._klines(100, 102),
            "B": self._klines(100, 102),
            "C": self._klines(100, 96),
        })
        equal_weight, symbols = self._sync_compute(client, ["A", "B", "C"], cap=0.10)
        assert equal_weight == pytest.approx(0.0)
        assert symbols == ["A", "B", "C"]

    def test_cap_truncation(self):
        """单币 ret 超出 ±10% 被截断：+50%/-50%/0% → mean(0.1,-0.1,0)=0.0"""
        client = self._FakeBinance({
            "A": self._klines(100, 150),
            "B": self._klines(100, 50),
            "C": self._klines(100, 100),
        })
        equal_weight, _ = self._sync_compute(client, ["A", "B", "C"], cap=0.10)
        assert equal_weight == pytest.approx(0.0)

    def test_min_symbols_not_met(self):
        """有效标的不足 MIN_SYMBOLS(3) 返回 None（1 个异常跳过 + 2 个有效）"""
        client = self._FakeBinance({
            "A": self._klines(100, 102),
            "B": self._klines(100, 102),
            "C": RuntimeError("网络错误"),
        })
        assert self._sync_compute(client, ["A", "B", "C"], cap=0.10) is None

    def test_symbol_exception_skipped(self):
        """单币异常跳过，其余正常参与均值"""
        client = self._FakeBinance({
            "A": self._klines(100, 110),
            "B": RuntimeError("网络错误"),
            "C": self._klines(100, 90),
            "D": self._klines(100, 104),
        })
        equal_weight, symbols = self._sync_compute(client, ["A", "B", "C", "D"], cap=0.10)
        assert symbols == ["A", "C", "D"]
        # mean(0.1, -0.1, 0.04) = 0.01333...
        assert equal_weight == pytest.approx((0.10 - 0.10 + 0.04) / 3)

    @staticmethod
    def _sync_compute(client, symbols, cap):
        """同步封装异步 _compute_pool_index（pytest asyncio auto 模式下直接 await 亦可）"""
        import asyncio
        return asyncio.run(_compute_pool_index(client, symbols, cap))


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
        assert MIN_SYMBOLS == 3
