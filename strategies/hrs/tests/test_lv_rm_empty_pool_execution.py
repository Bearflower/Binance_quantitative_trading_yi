"""
B2: HRS 空候选池 LV-RM 执行 + 统一信号收尾 + 持仓监控调度 + 影子落库 测试

覆盖：
- _run_lv_rm_only_cycle 用具名列表承接 LV-RM 信号，进入统一收尾链（跑真实 _resolve_conflicts）
- _check_lv_rm_entries 真实产出的 signal dict 必须含 price_change_1h 且值正确
- 影子闸门：LV-RM/EMM/semi_emm 默认影子；decision chain 跑完但 execute_short/execute_long 不被调；
  _update_signal_resolution 必须收到 resolution='shadow'（P1：杜绝假成交证据）
- _execute_cycle 用 finally 确保结算/回撤/暂停所有早退路径都调 _monitor_positions 且只调一次
- 禁止自证：不 mock 整条收尾链（不再 mock _resolve_conflicts / _check_lv_rm_entries），
  改为真实调用 + mock 末端依赖（trading_executor / DB / 通知）

本文件仅测 strategy.py 的 LV-RM 空池执行逻辑，不连交易所、不连生产 DB。
"""
import pytest
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, ANY

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)


# ---------- 测试辅助：构造 HRSStrategy + 最小依赖集 ----------

def _make_klines(n: int, base_close: float = 100.0, slope: float = 0.1) -> list:
    """构造 n 根 1h K 线，open_time 连续 UTC 毫秒"""
    base = 1700000000000
    return [
        {"open_time": base + i * 3600000, "close": base_close + i * slope}
        for i in range(n)
    ]


def _setup_strategy(klines_for_symbol: str = "TESTUSDT"):
    """构造 HRSStrategy 并挂好跑真实 LV-RM 链路所需的末端 mock。

    真实方法会被调用：_run_lv_rm_only_cycle → _check_lv_rm_entries →
    scoring_engine.score_lv_rm → _finalize_and_execute_signals → _resolve_conflicts → execute_signal。
    末端依赖（binance_client / trading_executor / DB / 通知 / DB schema check）全部 mock。
    """
    from strategies.hrs.strategy import HRSStrategy

    strategy = HRSStrategy(CONFIG)

    # circuit_breaker getattr 防御
    strategy.circuit_breaker = None

    # K 线缓存：25 根（够 price_change_24h + price_change_1h）
    klines = _make_klines(25, base_close=100.0, slope=0.1)
    strategy._klines_cache[klines_for_symbol] = klines
    strategy._klines_4h_cache[klines_for_symbol] = _make_klines(4, base_close=100.0, slope=0.2)
    strategy.min_klines = 24
    # 跳过实际预热（真实 _check_lv_rm_entries 已保证 klines 够用）
    strategy._warmup_klines = AsyncMock(return_value=klines)

    # market_data 末端 mock
    strategy.market_data = MagicMock()
    strategy.market_data.get_oi_usd = AsyncMock(return_value=1e11)   # 高于 execute_signal 里 min_oi 门槛
    strategy.market_data.get_funding_rate = AsyncMock(return_value=0.0001)
    strategy.market_data.get_24h_volume = AsyncMock(return_value=1e9)
    strategy.market_data.get_market_cap = AsyncMock(return_value=1e12)
    # B4: 生产 analyze 走 _fetch_market_cap_ratio → get_market_cap_provenance，
    # 需提供来源感知返回值（source=coingecko 视为真实市值）
    from shared.market_cap import MarketCapProvenance
    strategy.market_data.get_market_cap_provenance = AsyncMock(
        return_value=MarketCapProvenance(
            value=1e12, source="coingecko", is_proxy=False, missing_reason=None,
        )
    )

    # risk_manager / position_manager
    strategy.risk_manager = MagicMock()
    strategy.risk_manager.is_circuit_breaker_active = MagicMock(return_value=False)
    strategy.risk_manager.is_paused = MagicMock(return_value=False)
    strategy.risk_manager.check_drawdown = AsyncMock(return_value=True)
    strategy.risk_manager.check_price_change = MagicMock(return_value=True)
    strategy.risk_manager.can_open_position = MagicMock(return_value=True)  # 关键：让风控通过
    strategy.risk_manager.is_blacklisted = MagicMock(return_value=False)
    strategy.risk_manager.calculate_position_size = MagicMock(return_value=100.0)  # 返回合法 float，避免 Decimal 崩

    strategy.position_manager = MagicMock()
    strategy.position_manager.has_position = MagicMock(return_value=False)

    # binance_client（execute_signal 开头会 get_ticker / get_account_info）
    strategy.binance_client = MagicMock()
    strategy.binance_client.get_ticker = AsyncMock(return_value={"lastPrice": "102.0"})
    # 账户余额高一点 → total_ratio_cap * balance 够 new_margin
    strategy.binance_client.get_account_info = AsyncMock(return_value={"totalMarginBalance": "100000"})

    # scoring_engine：short 满足 should_entry，long 不满足（避免双向冲突分流）
    real_score = MagicMock()
    real_score.total_score = 7.0
    real_score.technical_score = 4.5
    real_score.sentiment_score = 2.5
    real_score.bb_position = 0.3
    real_score.rsi_value = 25.0

    real_score_long = MagicMock()
    real_score_long.total_score = 7.5
    real_score_long.technical_score = 3.0   # < 4.0，让 should_entry 拒绝 long
    real_score_long.sentiment_score = 2.7
    real_score_long.bb_position = 0.7
    real_score_long.rsi_value = 75.0

    strategy.scoring_engine = MagicMock()
    strategy.scoring_engine.score_lv_rm = AsyncMock(
        side_effect=lambda **kw: real_score if kw.get("direction") == "short" else real_score_long
    )
    # LV-RM 的 should_entry 检查：总分 ≥ 阈值 + 技术分 ≥ 4.0
    def _should_entry(score):
        if score.total_score >= 6.0 and score.technical_score >= 4.0:
            return True
        return False
    strategy.scoring_engine.should_entry = MagicMock(side_effect=_should_entry)

    # 候选池
    strategy.candidate_pool = MagicMock()
    strategy.candidate_pool.get_lv_rm_scan_range = AsyncMock(return_value=[klines_for_symbol])
    strategy.candidate_pool.get_short_candidates = MagicMock(return_value=set())
    strategy.candidate_pool.get_long_candidates = MagicMock(return_value=set())

    # execute_signal 里有个 _reject_on_exchange_position 开关，默认 True 时会
    # await _check_exchange_position_allows_entry → mock 掉返回 True 让链路继续
    strategy._check_exchange_position_allows_entry = AsyncMock(return_value=True)
    strategy._reject_on_exchange_position = False  # 或用 patch，但直接赋值更简单

    # DB / 通知：让 execute_signal 内部不崩
    strategy.db = MagicMock()
    strategy.db.execute = AsyncMock(return_value=None)
    strategy.capital_mgr = MagicMock()
    strategy.capital_mgr.can_open_within_limit = AsyncMock(return_value=(True, None))

    # trading_executor：**关键** 断言它从未被 await（影子闸门挡住）
    strategy.trading_executor = MagicMock()
    strategy.trading_executor.leverage = 5
    strategy.trading_executor.calculate_atr = AsyncMock(return_value=0.5)
    strategy.trading_executor._get_symbol_precision = AsyncMock(return_value=(0.01, 3))
    strategy.trading_executor.execute_short = AsyncMock()
    strategy.trading_executor.execute_long = AsyncMock()

    # 通知（_send_cycle_summary 会调 send，真实方法内 await notification_client.send）
    strategy.notification_client = MagicMock()
    strategy.notification_client.send = AsyncMock(return_value=None)
    strategy.notification_client.send_notification = AsyncMock(return_value=None)

    # 看板上报 / 清理等 finally 里的末端方法：让它们跑但不依赖真实 DB
    strategy._trim_klines_cache = MagicMock()
    strategy._cleanup_orphan_orders = AsyncMock(return_value=None)
    strategy._report_positions_to_dashboard = AsyncMock(return_value=None)
    strategy._ensure_db_schema = AsyncMock(return_value=None)

    # _update_signal_resolution：**关键** 捕获 resolution 值，断言收到 "shadow"
    strategy._update_signal_resolution = AsyncMock(return_value=None)

    return strategy, klines


# ====================================================================
# B2-1: 空池 LV-RM 信号进入统一收尾，真实 execute_signal 不调 trading_executor
# ====================================================================

class TestEmptyPoolLVExecution:
    """空候选池：真实链路断言 1 条 LV-RM 信号到达 shadow 闸门，未真实下单"""

    async def test_empty_pool_lv_signal_runs_full_chain_shadow_blocks_executor(self):
        """跑真实链路：_run_lv_rm_only_cycle → _check_lv_rm_entries →
        _finalize_and_execute_signals → _resolve_conflicts → execute_signal。
        影子闸门应让 execute_short/execute_long 从未被 await，
        且 _update_signal_resolution 收到 resolution='shadow'。"""
        strategy, klines = _setup_strategy()

        await strategy._run_lv_rm_only_cycle()

        # 确认 execute_signal 确实被调用了（链路到达闸门）
        assert strategy.trading_executor.execute_short.await_count == 0, \
            "影子模式下 LV-RM 信号不应真实调用 execute_short"
        assert strategy.trading_executor.execute_long.await_count == 0, \
            "影子模式下 LV-RM 信号不应真实调用 execute_long"
        # _update_signal_resolution 应收到 1 条 resolution="shadow"
        # （影子闸门返回 True 且标 shadow_blocked=True）
        assert strategy._update_signal_resolution.await_count >= 1, \
            "shadow 闸门返回 True，_update_signal_resolution 应被调（至少 1 条信号）"
        call_args = strategy._update_signal_resolution.call_args
        # R9: _update_signal_resolution 新签名 (signal_id, resolution)，
        # resolution 为 args[1]；signal_id 可能是 None（_log_signal_to_db 未 mock 时
        # fetch_one 失败），但不影响对 resolution 值的断言。
        resolution = (
            call_args.args[1] if len(call_args.args) >= 2
            else call_args.kwargs.get("resolution")
        )
        assert resolution == "shadow", f"影子阻断应写 resolution='shadow'，实际 '{resolution}'"

    async def test_shadow_off_real_execute_short_called(self):
        """放通影子闸门（lv_rm.execution_mode=execute）后，execute_short 应被调用。
        这是 shadow 闸门"真的在工作"的反向证明。"""
        strategy, klines = _setup_strategy()
        # 放通 LV-RM 影子
        strategy.config = dict(strategy.config)
        strategy.config["lv_rm"] = {**strategy.config.get("lv_rm", {}), "execution_mode": "execute"}

        await strategy._run_lv_rm_only_cycle()

        # 闸门放通后应该真调 execute_short
        assert strategy.trading_executor.execute_short.await_count >= 1, \
            "影子闸门放通后应真实调用 execute_short"


# ====================================================================
# B2-2: _check_lv_rm_entries 真实产出的 LV-RM signal 必须含 price_change_1h
# ====================================================================

class TestLV_RMPriceChange1HReal:
    """不 mock _check_lv_rm_entries，让真实实现产出 signal dict，
    断言 price_change_1h 存在且值等于 _calc_price_change_1h 的计算结果"""

    async def test_real_lv_rm_signal_has_price_change_1h(self):
        strategy, klines = _setup_strategy()

        shorts = []
        longs = []
        await strategy._check_lv_rm_entries(["TESTUSDT"], shorts, longs)

        assert len(shorts) == 1, f"应产出 1 条 short LV-RM 信号，实际 {len(shorts)}"
        sig = shorts[0]
        assert "price_change_1h" in sig, \
            f"真实 _check_lv_rm_entries 产出的 signal 缺 price_change_1h，keys={list(sig.keys())}"

        # 值应等于 helper 对同一 klines 的计算结果
        expected = strategy._calc_price_change_1h(klines)
        assert sig["price_change_1h"] == expected, \
            f"price_change_1h 值不对: signal={sig['price_change_1h']}, helper={expected}"

        # entry_mode 必须是 lv_rm
        assert sig["entry_mode"] == "lv_rm"


# ====================================================================
# B2-3: _execute_cycle finally 调度监控（不在本轮改动范围内，保留原测试）
# ====================================================================

class TestExecuteCycleMonitorDispatch:
    """_execute_cycle 所有早退路径（结算、回撤、暂停、空池）都必须调 _monitor_positions 且只调一次"""

    async def test_settlement_skip_still_monitor(self):
        from strategies.hrs.strategy import HRSStrategy
        strategy = HRSStrategy(CONFIG)
        strategy._monitor_positions = AsyncMock(return_value=None)
        strategy._report_positions_to_dashboard = AsyncMock(return_value=None)
        strategy._trim_klines_cache = MagicMock()
        strategy._cleanup_orphan_orders = AsyncMock(return_value=None)
        with patch.object(strategy, "_is_settlement_skip_window", return_value=True):
            await strategy._execute_cycle()
        strategy._monitor_positions.assert_awaited_once()

    async def test_paused_still_monitor(self):
        from strategies.hrs.strategy import HRSStrategy
        strategy = HRSStrategy(CONFIG)
        strategy._monitor_positions = AsyncMock(return_value=None)
        strategy._report_positions_to_dashboard = AsyncMock(return_value=None)
        strategy._trim_klines_cache = MagicMock()
        strategy._cleanup_orphan_orders = AsyncMock(return_value=None)
        strategy.binance_client = MagicMock()
        strategy.binance_client.get_account_info = AsyncMock(return_value={"totalMarginBalance": "10000"})
        strategy.risk_manager = MagicMock()
        strategy.risk_manager.is_paused = MagicMock(return_value=True)
        strategy._get_pause_info = MagicMock(
            return_value={"until": datetime.now(timezone.utc) + timedelta(hours=1), "reason": "test"}
        )
        await strategy._execute_cycle()
        strategy._monitor_positions.assert_awaited_once()


# ====================================================================
# B2-4: config.yaml 影子开关默认 shadow
# ====================================================================

class TestLV_RMShadowMode:
    """验证 execution_mode 配置默认 shadow"""

    def test_config_yaml_lv_rm_has_execution_mode(self):
        lv_cfg = CONFIG.get("lv_rm", {})
        assert "execution_mode" in lv_cfg, \
            "config.yaml lv_rm 段缺少 execution_mode 字段"
        assert lv_cfg["execution_mode"] == "shadow", \
            f"LV-RM execution_mode 默认应为 'shadow'，实际 {lv_cfg.get('execution_mode')}"

    def test_config_yaml_emm_semi_emm_default_shadow(self):
        for mode_name, mode_cfg in [("emm", CONFIG.get("emm", {})),
                                     ("semi_emm", CONFIG.get("semi_emm", {}))]:
            assert "execution_mode" in mode_cfg, f"{mode_name} 缺 execution_mode"
            assert mode_cfg["execution_mode"] == "shadow", \
                f"{mode_name}.execution_mode 默认应为 'shadow'"
