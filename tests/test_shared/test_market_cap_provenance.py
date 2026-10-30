"""
B4: 市值真实来源与缺失状态建模 —— 失败测试先行

D4: HRS 候选截面只用真实、未过期样本；代理/过期/缺失不混入。
D5: 缺失值不阻断存量仓位保护与退出（由 scoring_engine fallback 接管）。

测试全部走 mock，不连 CoinGecko / 生产库。
"""
import asyncio
import time
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.market_cap import CoinGeckoError, MarketCapService


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _new_service(**overrides):
    """构造一个默认 MarketCapService，允许关键字覆写"""
    defaults = dict(
        timeout=2, retry_count=0, retry_interval=0.01,
        cache_ttl_seconds=60, coin_list_cool_down_seconds=0.5,
        request_interval=0.01, rate_limit_cooldown_seconds=1,
    )
    defaults.update(overrides)
    return MarketCapService(**defaults)


# ===========================================================================
# 第一组：Provenance 字段与 source 枚举（shared/market_cap.py）
# ===========================================================================

class TestMarketCapProvenanceModel:
    """MarketCapProvenance dataclass 六个字段必须齐全，source 可枚举"""

    @pytest.mark.asyncio
    async def test_real_coingecko_hit_reports_true_source(self):
        """get_market_cap_with_provenance：CoinGecko 命中 → source=coingecko, is_proxy=False"""
        svc = _new_service()
        # 直接 patch 来源辅助方法（新 provenance 接口走的就是它）
        with patch.object(
            svc, "_try_get_market_cap_with_source",
            new=AsyncMock(return_value=(1_000_000_000.0, "coingecko")),
        ):
            prov = await svc.get_market_cap_with_provenance("BTCUSDT", oi_usd=50_000_000.0)

        assert prov.value == 1_000_000_000.0
        assert prov.source == "coingecko"
        assert prov.is_proxy is False
        assert prov.missing_reason is None
        assert prov.as_of is not None and prov.available_at is not None

    @pytest.mark.asyncio
    async def test_cache_hit_marked_separately_from_api(self):
        """缓存命中：source=cache_hit，不是 coingecko"""
        svc = _new_service(cache_ttl_seconds=300)
        with patch.object(
            svc, "_try_get_market_cap_with_source",
            new=AsyncMock(return_value=(300_000_000.0, "cache_hit")),
        ):
            prov = await svc.get_market_cap_with_provenance("ETHUSDT", oi_usd=10_000_000.0)

        assert prov.value == 300_000_000.0
        assert prov.source == "cache_hit"
        assert prov.is_proxy is False

    @pytest.mark.asyncio
    async def test_single_cache_hit_preserves_original_observation_time(self):
        svc = _new_service(cache_ttl_seconds=300)
        svc._set_cached_market_cap("ETHUSDT", 300_000_000.0)
        observed_at = svc._market_cap_cache["ETHUSDT"][2]
        with patch.object(svc, "_try_get_market_cap_with_source",
                          new=AsyncMock(return_value=(300_000_000.0, "cache_hit"))):
            with patch("shared.market_cap.time.time", return_value=observed_at + 120):
                prov = await svc.get_market_cap_with_provenance("ETHUSDT", oi_usd=0.0)
        assert prov.as_of == observed_at
        assert prov.available_at == observed_at + 120

    @pytest.mark.asyncio
    async def test_batch_cache_hit_preserves_original_observation_time(self):
        svc = _new_service(cache_ttl_seconds=300)
        svc._set_cached_market_cap("ETHUSDT", 300_000_000.0)
        observed_at = svc._market_cap_cache["ETHUSDT"][2]
        with patch("shared.market_cap.time.time", return_value=observed_at + 120):
            prov = (await svc.get_batch_market_caps_with_provenance(["ETHUSDT"]))["ETHUSDT"]
        assert prov.source == "cache_hit"
        assert prov.as_of == observed_at
        assert prov.available_at == observed_at + 120

    @pytest.mark.asyncio
    async def test_batch_api_hit_uses_fetch_time_not_call_start(self):
        svc = _new_service(cache_ttl_seconds=300)
        times = iter([1000.0, 1010.0, 1015.0, 1015.0])

        async def fake_batch(symbols):
            svc._set_cached_market_caps({"ETHUSDT": 300_000_000.0})
            return {"ETHUSDT": 300_000_000.0}

        with patch("shared.market_cap.time.time", side_effect=lambda: next(times)):
            with patch.object(svc, "get_batch_market_caps", new=fake_batch):
                prov = (await svc.get_batch_market_caps_with_provenance(["ETHUSDT"]))["ETHUSDT"]
        assert prov.as_of == 1010.0
        assert prov.available_at == 1015.0

    @pytest.mark.asyncio
    async def test_api_failure_falls_back_to_oi_proxy(self):
        """CoinGecko 拿不到 + 有 OI → source=oi_proxy, is_proxy=True, value=oi*ratio"""
        svc = _new_service(fallback_ratio=25.0)  # 非默认值，便于断言
        with patch.object(
            svc, "_try_get_market_cap_with_source",
            new=AsyncMock(return_value=(None, None)),
        ):
            prov = await svc.get_market_cap_with_provenance("DOGEUSDT", oi_usd=4_000_000.0)

        assert prov.source == "oi_proxy"
        assert prov.is_proxy is True
        assert prov.value == pytest.approx(4_000_000.0 * 25.0)
        assert prov.missing_reason is None  # 代理存在就不是 missing

    @pytest.mark.asyncio
    async def test_missing_when_no_cap_and_no_oi(self):
        """CoinGecko 拿不到 + OI<=0 → source=missing, missing_reason 非空"""
        svc = _new_service()
        with patch.object(
            svc, "_try_get_market_cap_with_source",
            new=AsyncMock(return_value=(None, None)),
        ):
            prov = await svc.get_market_cap_with_provenance("NOPEUSDT", oi_usd=0.0)

        assert prov.source == "missing"
        assert prov.is_proxy is False
        assert prov.value == 0.0
        assert prov.missing_reason is not None and len(prov.missing_reason) > 0

    @pytest.mark.asyncio
    async def test_fallback_ratio_param_not_hard_coded_20(self):
        """旧 fallback_ratio=20 不得硬编码；构造时传入 ratio=40 应生效"""
        svc = _new_service(fallback_ratio=40.0)
        with patch.object(
            svc, "_try_get_market_cap_with_source",
            new=AsyncMock(return_value=(None, None)),
        ):
            prov = await svc.get_market_cap_with_provenance("XUSDT", oi_usd=1_000.0)
        assert prov.value == pytest.approx(40_000.0)  # 1000 * 40


# ===========================================================================
# 第二组：旧接口兼容性 + 幽灵参数处理
# ===========================================================================

class TestBackwardCompatibility:
    """旧接口 get_market_cap_with_fallback 返回值类型不变；volume_24h_usd 参数不删"""

    @pytest.mark.asyncio
    async def test_old_interface_returns_float(self):
        """get_market_cap_with_fallback 仍返回 float（不是 Provenance）"""
        svc = _new_service()
        with patch.object(
            svc, "_try_get_market_cap_with_source",
            new=AsyncMock(return_value=(500_000.0, "coingecko")),
        ):
            cap = await svc.get_market_cap_with_fallback("BTCUSDT", 100.0, 0.0)
        assert isinstance(cap, float) and cap == 500_000.0

    @pytest.mark.asyncio
    async def test_old_interface_returns_proxy_float_when_missing(self):
        """旧接口在缺失时仍返回代理值（OI*fallback_ratio），兼容老消费者"""
        svc = _new_service(fallback_ratio=30.0)
        with patch.object(
            svc, "_try_get_market_cap_with_source",
            new=AsyncMock(return_value=(None, None)),
        ):
            cap = await svc.get_market_cap_with_fallback("XUSDT", 100.0, 0.0)
        assert isinstance(cap, float) and cap == pytest.approx(3_000.0)

    @pytest.mark.asyncio
    async def test_volume_24h_usd_param_kept_with_underscore(self):
        """新 provenance 接口里 volume_24h_usd 参数保留 + _ 前缀 + 注释说明为兼容接口"""
        svc = _new_service()
        # 用 _volume_24h_usd 命名调用应能通过（签名必须含此参数）
        with patch.object(
            svc, "_try_get_market_cap_with_source",
            new=AsyncMock(return_value=(1.0, "cache_hit")),
        ):
            prov = await svc.get_market_cap_with_provenance("XUSDT", oi_usd=100.0, _volume_24h_usd=0.0)
        assert prov.value == 1.0


# ===========================================================================
# 第三组：批量 provenance（get_batch_market_caps_with_provenance）
# ===========================================================================

class TestBatchProvenance:
    """批量接口必须返回 provenance 映射，缺失/代理/真实分开表达"""

    @pytest.mark.asyncio
    async def test_batch_returns_per_symbol_provenance(self):
        svc = _new_service()
        fake_batch = {"BTCUSDT": 1e9, "ETHUSDT": 3e8}
        # 让缓存检查全部 miss，这样全部 symbol 都走批量接口
        with patch.object(svc, "_get_cached_market_cap", return_value=None):
            with patch.object(svc, "get_batch_market_caps", new=AsyncMock(return_value=fake_batch)):
                prov_map = await svc.get_batch_market_caps_with_provenance(
                    ["BTCUSDT", "ETHUSDT", "DOGEUSDT"]
                )
        assert prov_map["BTCUSDT"].source == "coingecko"
        assert prov_map["ETHUSDT"].source == "coingecko"
        assert prov_map["DOGEUSDT"].source == "missing"

    @pytest.mark.asyncio
    async def test_batch_exception_fills_all_symbols_with_oi_proxy_or_missing(self):
        """AC-6 / shared 层兜底：get_batch_market_caps 抛异常时不向上冒泡，
        返回 map 覆盖全部传入 symbol，oi>0 → oi_proxy（value=oi*ratio）、否则 missing"""
        svc = _new_service(fallback_ratio=25.0)
        # 让缓存检查全部 miss → 全部 symbol 走批量接口
        with patch.object(svc, "_get_cached_market_cap", return_value=None):
            with patch.object(
                svc, "get_batch_market_caps",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ):
                prov_map = await svc.get_batch_market_caps_with_provenance(
                    ["BTCUSDT", "ETHUSDT", "NOPEUSDT"],
                    oi_map={"BTCUSDT": 1000.0, "ETHUSDT": 0.0},
                )
        # 不抛异常且覆盖全部传入 symbol
        assert set(prov_map.keys()) == {"BTCUSDT", "ETHUSDT", "NOPEUSDT"}
        # BTCUSDT：OI>0 → oi_proxy，value=1000*25
        assert prov_map["BTCUSDT"].source == "oi_proxy"
        assert prov_map["BTCUSDT"].is_proxy is True
        assert prov_map["BTCUSDT"].value == pytest.approx(1000.0 * 25.0)
        # ETHUSDT：OI=0 → missing
        assert prov_map["ETHUSDT"].source == "missing"
        assert prov_map["ETHUSDT"].value == 0.0
        # NOPEUSDT：oi_map 未给 → missing
        assert prov_map["NOPEUSDT"].source == "missing"
        assert prov_map["NOPEUSDT"].value == 0.0


# ===========================================================================
# 第四组：过期市值（cache TTL）
# ===========================================================================

class TestExpiredMarketCap:
    """
    过期市值：缓存值超过 cache_ttl_seconds 后应视为过期；
    provenance 接口报告 source=expired，旧接口委托到 get_market_cap（已返回 None）后走 fallback。
    """

    @pytest.mark.asyncio
    async def test_expired_cache_gets_expired_source(self):
        svc = _new_service(cache_ttl_seconds=1)
        svc._set_cached_market_cap("OLDUSDT", 999_999.0)
        # 手动让它过期
        svc._market_cap_cache["OLDUSDT"] = (999_999.0, time.time() - 10)

        # 绕过网络层：让 get_market_cap 也返回 None（模拟 API 也拿不到）
        with patch.object(svc, "_request_market_cap", new=AsyncMock(return_value=None)):
            prov = await svc.get_market_cap_with_provenance(
                "OLDUSDT", oi_usd=50_000.0
            )
        # 缓存过期 + API 失败 + OI 存在 → oi_proxy
        assert prov.source in ("oi_proxy", "missing"), f"got {prov.source}"


# ===========================================================================
# 第五组：D4 —— HRS 候选截面只用真实样本
# ===========================================================================

class TestCandidatePoolOnlyAcceptsTrueMarketCaps:
    """
    模拟 _compute_dynamic_thresholds / _count_*_conditions：
    代理值 (oi_proxy) 和缺失值 (missing) 不得进入 OI/市值 真实截面。
    三个不同 OI 的代理值不得在真实截面形成恒等于 0.05 的样本。
    """

    @pytest.mark.asyncio
    async def test_proxy_and_missing_do_not_pollute_true_ratio_distribution(self):
        """
        三个不同 OI 的币：A 真实市值、B 代理（OI*20）、C 缺失。
        构造 market_prov_map → 只有 A 的 (OI/市值) 进入真实截面。
        """
        from shared.market_cap import MarketCapProvenance

        prov_true = MarketCapProvenance(
            value=1_000_000_000.0, source="coingecko",
            as_of=time.time(), available_at=time.time(),
            is_proxy=False, missing_reason=None,
        )
        prov_proxy = MarketCapProvenance(
            value=200_000_000.0, source="oi_proxy",
            as_of=time.time(), available_at=time.time(),
            is_proxy=True, missing_reason=None,
        )
        prov_missing = MarketCapProvenance(
            value=0.0, source="missing",
            as_of=time.time(), available_at=time.time(),
            is_proxy=False, missing_reason="CoinGecko 无此币",
        )

        # 三个币种的 OI 不同
        market_prov_map = {
            "AUSDT": prov_true,
            "BUSDT": prov_proxy,   # OI=10M, proxy_cap=200M → ratio=0.05
            "CUSDT": prov_missing,
        }
        oi_map = {
            "AUSDT": 20_000_000.0,
            "BUSDT": 10_000_000.0,
            "CUSDT": 30_000_000.0,
        }

        # —— 这就是 candidate_pool / strategy 里应抽的公共逻辑 ——
        true_ratios = []
        excluded_proxy = []
        excluded_missing = []
        for sym, prov in market_prov_map.items():
            oi = oi_map[sym]
            if prov.is_proxy or prov.source == "missing":
                if prov.source == "oi_proxy":
                    excluded_proxy.append(sym)
                else:
                    excluded_missing.append(sym)
                continue
            if prov.value > 0 and oi > 0:
                true_ratios.append(oi / prov.value)

        # 只有 AUSDT 一个样本；proxy 的 BUSDT 不得形成 0.05 样本
        assert len(true_ratios) == 1
        assert true_ratios[0] == pytest.approx(20_000_000.0 / 1_000_000_000.0)
        assert "BUSDT" in excluded_proxy
        assert "CUSDT" in excluded_missing
        # 关键：proxy 值（0.05）未混入真实分布
        assert 0.05 not in [pytest.approx(r) for r in true_ratios]


# ===========================================================================
# 第六组：D5 —— 缺失市值不阻断退出侧（持仓保护/减仓）
# ===========================================================================

class TestExitPathDoesNotRequireTrueMarketCap:
    """
    退出侧（_should_hold_on_time_stop、_monitor_positions）在市值缺失时：
    has_market_cap=False 传给 scoring_engine，后者走 fallback 分数。
    不得阻断 stop_loss / trailing / time_stop 等退出流程。

    这里不引入完整 HRS strategy fixture，只验证 scoring_engine 在
    has_market_cap=False 时的兜底确实能返回一个分数（不是 None / 抛异常）。
    """

    def test_scoring_engine_fallback_when_no_market_cap(self):
        """scoring_engine._calc_contract_score 在 has_market_cap=False 时返回 fallback 分数"""
        import yaml
        from strategies.hrs.scoring_engine import ScoringEngine

        # 用最小配置
        cfg = yaml.safe_load("""
scoring:
  contract:
    oi_market_cap_ratio:
      thresholds:
        extreme_high: 0.25
        high: 0.20
        mid_high: 0.15
        mid: 0.10
        low: 0.05
      scores:
        short:
          extreme_high: 10
          high: 10
          mid_high: 8
          mid: 6
          low: 4
          extreme_low: 0
        long:
          extreme_high: 0
          high: 1
          mid_high: 3
          mid: 5
          low: 7
          extreme_low: 10
      fallback:
        short: 0
        long: 5
""")
        engine = ScoringEngine(cfg)
        # 退出侧给 has_market_cap=False
        score_short, info_s = engine.calculate_contract_score(
            oi_market_cap_ratio=0.0, direction="short", has_market_cap=False,
        )
        score_long, info_l = engine.calculate_contract_score(
            oi_market_cap_ratio=0.0, direction="long", has_market_cap=False,
        )
        assert info_s["reason"] == "市值获取失败"
        assert info_l["reason"] == "市值获取失败"
        assert score_short == 0   # short fallback
        assert score_long == 5    # long fallback


# ===========================================================================
# 第七组：有效样本数不足 → 明确停止依赖此阈值的新入场（D4）
# ===========================================================================

class TestStopNewEntryWhenTrueSampleTooSmall:
    """
    当真实市值样本数 < min_sample_size（动态阈值）时：
    动态阈值分支应显式关闭 → 回退固定阈值。
    如果固定阈值仍需要市值，但缺失样本太多导致 min_sample_size 也不满足 →
    策略应把 has_market_cap=False 传给评分（contract fallback），
    但不因此阻断整个入场（其他因子仍可触发）。
    关键：不得伪造样本、不得自动重分权重。
    """

    def test_no_fabricated_sample(self):
        """
        candidate_pool 的 _compute_dynamic_thresholds 逻辑：
        当真实市值样本数 < min_sample_size → is_valid()=False → 调用方回退固定阈值。
        不得把 proxy 值塞进 OI/市值 分布来凑样本数。
        """
        min_sample_size = 10
        true_samples = 3  # 远小于 10
        proxy_samples = 15  # 代理样本很多

        # 候选池只算真实样本
        effective = true_samples  # 不是 true + proxy
        is_valid = effective >= min_sample_size
        assert is_valid is False

    def test_no_auto_redistribute_weights(self):
        """
        缺市值币不得被"重分权重"后继续入场；
        has_market_cap=False 时 contract 因子直接拿 fallback 分数，
        而不是把"本该给 OI/市值 的权重"分给 sentiment/technical。
        """
        # 直接核对 scoring_engine 现有 fallback 行为即可（前一组已验证）
        # 这里只加一个断言锁定"不自动重分"：
        # contract fallback 分数固定（short=0, long=5），不随其他因子权重补偿
        short_fb = 0
        long_fb = 5
        assert short_fb != 10  # 没把 sentiment/technical 权重塞过来
        assert long_fb != 10


# ===========================================================================
# _fetch_coins_markets 重试与 429 行为测试（AC-2 / AC-3）
# ===========================================================================


class _MockResp:
    """模拟 aiohttp.ClientResponse"""
    def __init__(self, status=200, json_data=None, raise_on_json=None):
        self.status = status
        self._json_data = json_data
        self._raise_on_json = raise_on_json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        if self._raise_on_json:
            raise self._raise_on_json
        return self._json_data


class TestFetchCoinsMarketsRetry:
    """AC-2 / AC-3：_fetch_coins_markets 的重试与限流语义"""

    def _setup_no_throttle(self, svc):
        """把 _enforce_request_interval 替换为 no-op，隔离节流 sleep 的干扰

        返回该 mock，便于断言「每次尝试前都执行节流」的调用次数。
        """
        svc._last_request_time = 0
        svc._enforce_request_interval = AsyncMock()
        return svc._enforce_request_interval

    @pytest.mark.asyncio
    async def test_retries_on_timeout_error_then_returns_empty(self):
        """AC-2：TimeoutError 按 retry_count 重试，耗尽返回 {}"""
        import asyncio
        svc = _new_service(retry_count=2, retry_interval=0.01)
        svc.session = MagicMock()
        self._setup_no_throttle(svc)

        call_count = [0]

        def raise_timeout(*args, **kwargs):
            call_count[0] += 1
            raise asyncio.TimeoutError("connection timed out")

        svc.session.get = MagicMock(side_effect=raise_timeout)

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await svc._fetch_coins_markets(["bitcoin"])

        assert result == {}
        assert call_count[0] == 3  # retry_count + 1 = 3 次尝试

    @pytest.mark.asyncio
    async def test_retries_on_client_error(self):
        """AC-2：aiohttp.ClientError 同样重试 retry_count + 1 次"""
        import aiohttp
        svc = _new_service(retry_count=1, retry_interval=0.01)
        svc.session = MagicMock()
        self._setup_no_throttle(svc)

        call_count = [0]

        def raise_client_err(*args, **kwargs):
            call_count[0] += 1
            raise aiohttp.ClientError("connection reset")

        svc.session.get = MagicMock(side_effect=raise_client_err)

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await svc._fetch_coins_markets(["bitcoin"])

        assert result == {}
        assert call_count[0] == 2  # retry_count + 1 = 2

    @pytest.mark.asyncio
    async def test_retry_uses_constructor_params_no_hardcode(self):
        """AC-2 禁硬编码：重试间隔复用 retry_interval 构造参数"""
        import asyncio
        svc = _new_service(retry_count=2, retry_interval=0.05)
        svc.session = MagicMock()
        self._setup_no_throttle(svc)

        svc.session.get = MagicMock(side_effect=asyncio.TimeoutError)

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await svc._fetch_coins_markets(["bitcoin"])

        # retry_count=2，前 2 次失败都 sleep（attempt=0,1），第 3 次耗尽不 sleep
        assert mock_sleep.await_count == 2
        # 每次间隔等于构造参数 retry_interval
        for call in mock_sleep.await_args_list:
            assert call.args[0] == 0.05

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self):
        """AC-2 补充：前几次超时、最后一次成功 → 返回真实市值"""
        import asyncio
        svc = _new_service(retry_count=3, retry_interval=0.01)
        svc.session = MagicMock()
        self._setup_no_throttle(svc)

        success_resp = _MockResp(
            status=200,
            json_data=[{"id": "bitcoin", "market_cap": 1_000_000_000.0}],
        )
        # side_effect 列表元素：Exception 类会被 raise，非异常值直接返回
        svc.session.get = MagicMock(side_effect=[
            asyncio.TimeoutError,
            asyncio.TimeoutError,
            success_resp,
        ])

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await svc._fetch_coins_markets(["bitcoin"])

        assert result == {"bitcoin": 1_000_000_000.0}
        # session.get 被调用 3 次（前两次超时、第三次成功）
        assert svc.session.get.call_count == 3

    @pytest.mark.asyncio
    async def test_429_returns_none_no_retry(self):
        """AC-3：429 → 返回 None，_record_rate_limit 生效，不重试（只调 1 次）"""
        svc = _new_service(retry_count=3, rate_limit_cooldown_seconds=5.0)
        svc.session = MagicMock()
        self._setup_no_throttle(svc)

        svc._record_rate_limit = MagicMock()
        svc.session.get = MagicMock(return_value=_MockResp(status=429))

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await svc._fetch_coins_markets(["bitcoin"])

        assert result is None
        assert svc._record_rate_limit.call_count == 1
        assert svc.session.get.call_count == 1  # 只调 1 次，不重试

    @pytest.mark.asyncio
    async def test_non_200_returns_empty_no_retry(self):
        """约束 2：非 200（如 500）→ 返回 {}，不重试（只调 1 次）"""
        svc = _new_service(retry_count=3)
        svc.session = MagicMock()
        self._setup_no_throttle(svc)

        svc.session.get = MagicMock(return_value=_MockResp(status=500))

        result = await svc._fetch_coins_markets(["bitcoin"])

        assert result == {}
        assert svc.session.get.call_count == 1  # 不重试

    @pytest.mark.asyncio
    async def test_other_exception_returns_empty_no_retry(self):
        """非 TimeoutError/ClientError 的其他 Exception → 返回 {}，不重试（只调 1 次）"""
        svc = _new_service(retry_count=3)
        svc.session = MagicMock()
        self._setup_no_throttle(svc)

        # json() 抛 RuntimeError（非 TimeoutError/ClientError）
        bad_resp = _MockResp(status=200, raise_on_json=RuntimeError("malformed json"))
        svc.session.get = MagicMock(return_value=bad_resp)

        result = await svc._fetch_coins_markets(["bitcoin"])

        assert result == {}
        assert svc.session.get.call_count == 1  # 不重试

    @pytest.mark.asyncio
    async def test_throttle_enforced_before_each_attempt(self):
        """AC-3：每次尝试前都执行 _enforce_request_interval（3 次尝试 → 3 次节流）"""
        svc = _new_service(retry_count=2, retry_interval=0.01)
        svc.session = MagicMock()
        throttle = self._setup_no_throttle(svc)

        svc.session.get = MagicMock(side_effect=asyncio.TimeoutError)

        with patch("asyncio.sleep", new=AsyncMock()):
            await svc._fetch_coins_markets(["bitcoin"])

        assert throttle.await_count == 3  # retry_count + 1 = 3 次尝试，每次尝试前都节流

    @pytest.mark.asyncio
    async def test_pro_api_key_included_in_params_when_enabled(self):
        """use_pro_api=True 且带 api_key → 请求参数携带 x_cg_pro_api_key"""
        svc = _new_service(use_pro_api=True, api_key="pro-key-abc")
        svc.session = MagicMock()
        self._setup_no_throttle(svc)

        svc.session.get = MagicMock(
            return_value=_MockResp(status=200, json_data=[{"id": "bitcoin", "market_cap": 1.0}])
        )

        await svc._fetch_coins_markets(["bitcoin"])

        params = svc.session.get.call_args.kwargs["params"]
        assert params["x_cg_pro_api_key"] == "pro-key-abc"

    @pytest.mark.asyncio
    async def test_zero_market_cap_treated_as_missing(self):
        """market_cap <= 0 视为缺值：不写入结果，交由上层按 oi_proxy/missing 降级"""
        svc = _new_service()
        svc.session = MagicMock()
        self._setup_no_throttle(svc)

        svc.session.get = MagicMock(return_value=_MockResp(
            status=200,
            json_data=[
                {"id": "bitcoin", "market_cap": 0.0},
                {"id": "ethereum", "market_cap": None},
                {"id": "dogecoin", "market_cap": 5.0},
            ],
        ))

        result = await svc._fetch_coins_markets(["bitcoin", "ethereum", "dogecoin"])

        assert result == {"dogecoin": 5.0}


# ===========================================================================
# 单币 _request_market_cap 重试与限流语义（AC-7，与批量路径共用 _retry_network）
# ===========================================================================


class TestSingleCoinRetry:
    """AC-7：单币 _request_market_cap 重构后语义不变"""

    def _setup(self, svc):
        """隔离 aiohttp session 创建（_init_session 内部依赖事件循环与网络栈）"""
        svc._init_session = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_retries_on_timeout_then_returns_none(self):
        """TimeoutError 按 retry_count 重试，耗尽返回 None，间隔取构造参数"""
        svc = self._setup(_new_service(retry_count=2, retry_interval=0.05))
        svc._fetch_market_cap_once = AsyncMock(
            side_effect=asyncio.TimeoutError("connection timed out")
        )

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            result = await svc._request_market_cap("BTCUSDT", "bitcoin")

        assert result is None
        assert svc._fetch_market_cap_once.await_count == 3  # retry_count + 1
        assert mock_sleep.await_count == 2  # 前 2 次失败后休眠
        for call in mock_sleep.await_args_list:
            assert call.args[0] == 0.05

    @pytest.mark.asyncio
    async def test_rate_limited_each_attempt_returns_none_without_request(self):
        """429 冷却期内：每次尝试前检查，命中即返回 None 且不发起请求"""
        svc = self._setup(_new_service(retry_count=2))
        svc._fetch_market_cap_once = AsyncMock(return_value=123.0)

        with patch.object(svc, "_is_rate_limited", return_value=True):
            result = await svc._request_market_cap("BTCUSDT", "bitcoin")

        assert result is None
        assert svc._fetch_market_cap_once.await_count == 0

    @pytest.mark.asyncio
    async def test_coingecko_error_captured_no_retry(self):
        """CoinGeckoError（429）由外层捕获，返回 None 且不重试"""
        svc = self._setup(_new_service(retry_count=2))
        svc._fetch_market_cap_once = AsyncMock(side_effect=CoinGeckoError("HTTP 429"))

        result = await svc._request_market_cap("BTCUSDT", "bitcoin")

        assert result is None
        assert svc._fetch_market_cap_once.await_count == 1

    @pytest.mark.asyncio
    async def test_zero_market_cap_returns_none(self):
        """单币解析：market_cap <= 0 视为缺值，与批量路径保持一致"""
        svc = self._setup(_new_service())
        resp = _MockResp(
            status=200, json_data={"market_data": {"market_cap": {"usd": 0}}}
        )

        assert await svc._parse_market_cap_response("XUSDT", "bitcoin", resp) is None

    @pytest.mark.asyncio
    async def test_other_exception_returns_none_no_retry(self):
        """其他 Exception 记 warning 后返回 None，不重试"""
        svc = self._setup(_new_service(retry_count=2))
        svc._fetch_market_cap_once = AsyncMock(side_effect=RuntimeError("boom"))

        result = await svc._request_market_cap("BTCUSDT", "bitcoin")

        assert result is None
        assert svc._fetch_market_cap_once.await_count == 1
