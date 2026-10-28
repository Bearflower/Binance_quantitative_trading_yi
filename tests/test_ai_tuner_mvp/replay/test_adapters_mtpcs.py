"""MTPCS 激进版回放适配器的测试（T12）。

口径（D4）：K 线全部来自 ``tests/test_strategies/fixtures/btc_eth_aggressive_golden.py``
的合成夹具，只能证明「适配器决策链与 core 纯函数逐字段一致」，
**不得**记为「实盘金样对账通过」。
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import ai_tuner.replay.adapters.mtpcs_aggressive as adapter_module
from ai_tuner.replay.adapters.base import PreparedContext
from ai_tuner.replay.adapters.mtpcs_aggressive import MtpcsAggressiveAdapter
from ai_tuner.replay.broker import PortfolioView
from ai_tuner.replay.data_portal import DataPortal
from ai_tuner.replay.models import (
    Bar,
    ConfigHashes,
    DataBlocked,
    Position,
    QualificationBlocked,
    UniverseSlice,
)
from strategies.btc_eth_aggressive.core import common
from strategies.btc_eth_aggressive.core import direction as direction_mod
from strategies.btc_eth_aggressive.core import exits, filters, scoring
from tests.test_ai_tuner_mvp.replay.conftest import make_bars, write_snapshot
from tests.test_strategies.fixtures import btc_eth_aggressive_golden as golden

UTC = timezone.utc
SYMBOL = "BTCUSDT"
NUMERIC_FIELDS = ("open", "high", "low", "close", "volume", "quote_volume")
MODULE_PATH = Path(adapter_module.__file__)


# ---------------------------------------------------------------------------
# 夹具与轻量上下文
# ---------------------------------------------------------------------------


def _config() -> Dict[str, Any]:
    return golden.config_dict()


def _config_with_ranging_guards() -> Dict[str, Any]:
    """打开震荡开仓侧四护栏（冻结夹具默认 entry_filters.enabled=False）。"""
    config = golden.config_dict()
    config["strategy"]["risk"]["ranging_strategy"]["entry_filters"] = {"enabled": True}
    return config


def _make_adapter(config=None, quantity: Decimal = Decimal("0.5")) -> MtpcsAggressiveAdapter:
    return MtpcsAggressiveAdapter(
        config or _config(),
        quantity_for=lambda symbol, decision: quantity,
        market_state="TREND",
    )


def _parse_time(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _bar(row: Dict[str, Any], interval: str, symbol: str = SYMBOL) -> Bar:
    return Bar(
        symbol=symbol,
        interval=interval,
        open_time=_parse_time(row["open_time"]),
        close_time=_parse_time(row["close_time"]),
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=Decimal(str(row["volume"])),
        quote_volume=Decimal(str(row["quote_volume"])),
        trades=int(row["trades"]),
    )


def _lightweight_ctx(symbols: Tuple[str, ...], intervals: Tuple[str, ...]) -> PreparedContext:
    as_of = datetime(2026, 2, 1, tzinfo=UTC)
    return PreparedContext(
        strategy_id=MtpcsAggressiveAdapter.strategy_id,
        as_of=as_of,
        universe=UniverseSlice(
            strategy_id=MtpcsAggressiveAdapter.strategy_id,
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            valid_to=datetime(2027, 1, 1, tzinfo=UTC),
            symbols=symbols,
            intervals=intervals,
            source="fixture",
            config_hash="a" * 64,
        ),
        config_hashes=ConfigHashes(
            strategy_id=MtpcsAggressiveAdapter.strategy_id,
            as_of=as_of,
            base_config_hash="b" * 64,
            params_manifest_hash="c" * 64,
            overlay_hash="d" * 64,
        ),
        portfolio=PortfolioView(positions=(), last_prices={}, realized_equity=Decimal("0")),
        extras={"symbols": list(symbols), "intervals": list(intervals), "market_state": "TREND"},
    )


def _feed(
    adapter: MtpcsAggressiveAdapter,
    klines: Dict[str, List[Dict[str, Any]]],
    symbols: Tuple[str, ...] = (SYMBOL,),
) -> PreparedContext:
    """按真实 ``on_bar`` 逐根喂入 K 线（保证缓冲路径被覆盖）。"""
    ctx = _lightweight_ctx(symbols, tuple(klines.keys()))
    for symbol in symbols:
        for interval, rows in klines.items():
            for row in rows:
                adapter.on_bar(_bar(row, interval, symbol), ctx)
    return ctx


def _numeric_klines(klines: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    """把夹具行归一成与实盘一致的 Decimal 数值（作为 core 直接调用的输入）。"""
    return {
        interval: [
            dict(row, **{field: Decimal(str(row[field])) for field in NUMERIC_FIELDS})
            for row in rows
        ]
        for interval, rows in klines.items()
    }


# ---------------------------------------------------------------------------
# 期望值：直接用 core 纯函数算（不经过适配器）
# ---------------------------------------------------------------------------


def _expected_reject(
    market_state_name: str, reason: Any, score: float = 0.0
) -> Dict[str, Any]:
    return {
        "symbol": SYMBOL,
        "direction": None,
        "grade": "D",
        "score": float(score),
        "reason": str(reason),
        "market_state": market_state_name,
        "entry_price": None,
        "atr": None,
        "stop_loss_price": None,
        "tp1_price": None,
        "tp2_price": None,
    }


def _expected_pass(
    config: Dict[str, Any],
    indicators: Dict,
    klines: Dict[str, List[Dict[str, Any]]],
    market_state_name: str,
    direction: str,
    grade: str,
    score: float,
) -> Dict[str, Any]:
    strategy_config = config["strategy"]
    risk_config = strategy_config["risk"]
    price_interval = (
        "1h" if "1h" in klines else strategy_config["timeframes"][-1]
    )
    price = Decimal(str(klines[price_interval][-1]["close"]))
    atr = Decimal(str(indicators[price_interval]["ATR"].iloc[-1]))
    return {
        "symbol": SYMBOL,
        "direction": direction,
        "grade": grade,
        "score": score,
        "reason": "",
        "market_state": market_state_name,
        "entry_price": price,
        "atr": atr,
        "stop_loss_price": exits.calculate_stop_price(
            direction, price, atr, grade, risk_config
        ),
        "tp1_price": exits.calculate_tp_price(price, atr, direction, 1, grade, risk_config),
        "tp2_price": exits.calculate_tp_price(price, atr, direction, 2, grade, risk_config),
    }


def _expected_trend_chain(
    config: Dict[str, Any],
    klines: Dict[str, List[Dict[str, Any]]],
    vol_boost: float = 0.0,
) -> Dict[str, Any]:
    strategy_config = config["strategy"]
    risk_config = strategy_config["risk"]
    scoring_config = strategy_config["scoring"]
    symbol_config = strategy_config["symbol_config"]
    indicators = common.build_indicators(klines, risk_config)

    passed, trend_result = filters.check_trend_filter(indicators, klines, risk_config)
    if not passed:
        return _expected_reject("TREND", trend_result)
    direction = (
        trend_result
        if trend_result
        else direction_mod.determine_direction(indicators, strategy_config["timeframes"])
    )
    spike = filters.check_kline_spike(klines, risk_config.get("prohibition", {}))
    if spike is not None:
        return _expected_reject("TREND", spike)

    score = float(
        scoring.calculate_score(indicators, klines, scoring_config, risk_config, "TREND")
    )
    grade = scoring.determine_grade(score, scoring_config, symbol_config, SYMBOL)
    vol_ok, vol_reason = filters.check_volume_filter(
        SYMBOL, grade, klines, risk_config, symbol_config, vol_boost
    )
    if not vol_ok:
        return _expected_reject("TREND", vol_reason, score)
    return _expected_pass(config, indicators, klines, "TREND", direction, grade, score)


def _expected_ranging_chain(
    config: Dict[str, Any],
    klines: Dict[str, List[Dict[str, Any]]],
    vol_boost: float = 0.0,
) -> Dict[str, Any]:
    strategy_config = config["strategy"]
    risk_config = strategy_config["risk"]
    scoring_config = strategy_config["scoring"]
    symbol_config = strategy_config["symbol_config"]
    ranging_config = risk_config.get("ranging_strategy", {})
    indicators = common.build_indicators(klines, risk_config)

    direction, vote_reason, _conditions = direction_mod.vote_ranging_direction(
        indicators, klines, ranging_config.get("entry_conditions", {})
    )
    if direction is None:
        return _expected_reject("RANGING", vote_reason)

    if ranging_config.get("entry_filters", {}).get("enabled", True):
        ok, reason, _ratio = filters.evaluate_volatility_regime(indicators, ranging_config)
        if not ok:
            return _expected_reject("RANGING", reason)
        ok, reason = direction_mod.check_direction_alignment(
            direction, indicators, ranging_config
        )
        if not ok:
            return _expected_reject("RANGING", reason)
        ok, reason = filters.check_volume_confirm(
            direction, indicators, klines, ranging_config
        )
        if not ok:
            return _expected_reject("RANGING", reason)
        ok, reason = filters.check_overheat_ban(
            direction, indicators, klines, ranging_config
        )
        if not ok:
            return _expected_reject("RANGING", reason)

    spike = filters.check_kline_spike(klines, risk_config.get("prohibition", {}))
    if spike is not None:
        return _expected_reject("RANGING", spike)

    score = float(
        scoring.calculate_score(indicators, klines, scoring_config, risk_config, "RANGING")
    )
    grade = scoring.determine_grade(score, scoring_config, symbol_config, SYMBOL)

    downgraded = filters.apply_overheat_downgrade(
        grade, direction, indicators, klines, ranging_config
    )
    if downgraded is None:
        return _expected_reject("RANGING", "过热降级后低于最低允许等级，禁开", score)
    grade = downgraded

    vol_ok, vol_reason = filters.check_volume_filter(
        SYMBOL, grade, klines, risk_config, symbol_config, vol_boost
    )
    if not vol_ok:
        return _expected_reject("RANGING", vol_reason, score)
    return _expected_pass(config, indicators, klines, "RANGING", direction, grade, score)


def _assert_same_decision(actual: Optional[Dict[str, Any]], expected: Dict[str, Any]) -> None:
    """逐字段比对（任何字段不等都直接指出字段名与两侧取值）。"""
    assert actual is not None, "适配器返回 None，期望 {0}".format(expected)
    for field in (
        "symbol",
        "market_state",
        "direction",
        "grade",
        "score",
        "reason",
        "entry_price",
        "atr",
        "stop_loss_price",
        "tp1_price",
        "tp2_price",
    ):
        assert actual[field] == expected[field], "字段 {0} 不一致：适配器={1} 期望={2}".format(
            field, actual[field], expected[field]
        )


# ---------------------------------------------------------------------------
# 1. 5 场景一致性：适配器 vs core 直接调用
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", golden.SCENARIOS)
def test_trend_path_matches_core_chain(scenario: str) -> None:
    """趋势路径（入场方向 / 评分 / 等级 / 三个退出价）与 core 逐项相等。"""
    raw_klines = golden.scenario_data(scenario)["klines"]
    config = _config()
    adapter = _make_adapter(config)
    _feed(adapter, raw_klines)

    actual = adapter.decide(SYMBOL, "TREND")
    expected = _expected_trend_chain(config, _numeric_klines(raw_klines))
    _assert_same_decision(actual, expected)
    if expected["direction"] is None:
        assert actual["grade"] == "D"
        assert actual["stop_loss_price"] is None


@pytest.mark.parametrize("scenario", golden.SCENARIOS)
def test_ranging_path_matches_core_chain(scenario: str) -> None:
    """震荡路径（投票 / 护栏 / 降级 / 成交量 / 价格）与 core 逐项相等。"""
    raw_klines = golden.scenario_data(scenario)["klines"]
    config = _config()
    adapter = _make_adapter(config)
    _feed(adapter, raw_klines)

    actual = adapter.decide(SYMBOL, "RANGING")
    expected = _expected_ranging_chain(config, _numeric_klines(raw_klines))
    _assert_same_decision(actual, expected)
    if expected["direction"] is None:
        assert actual["grade"] == "D"
        assert actual["reason"] == expected["reason"]


@pytest.mark.parametrize("scenario", golden.SCENARIOS)
def test_ranging_guards_match_core_chain(scenario: str) -> None:
    """打开开仓侧四护栏后，拒绝顺序与 core 链一致（覆盖护栏分支）。"""
    raw_klines = golden.scenario_data(scenario)["klines"]
    config = _config_with_ranging_guards()
    adapter = _make_adapter(config)
    _feed(adapter, raw_klines)

    actual = adapter.decide(SYMBOL, "RANGING")
    expected = _expected_ranging_chain(config, _numeric_klines(raw_klines))
    _assert_same_decision(actual, expected)
    assert expected["direction"] is None or expected["reason"] == ""


def test_decide_returns_none_when_timeframe_missing() -> None:
    """缓冲缺任一配置周期 → decide 返回 None（不猜测、不补数据）。"""
    raw_klines = golden.scenario_data("trend_pullback")["klines"]
    adapter = _make_adapter()
    ctx = _lightweight_ctx((SYMBOL,), ("1h", "4h"))
    for row in raw_klines["1h"]:
        adapter.on_bar(_bar(row, "1h"), ctx)
    for row in raw_klines["4h"]:
        adapter.on_bar(_bar(row, "4h"), ctx)

    assert adapter.decide(SYMBOL, "TREND") is None
    assert adapter.decide(SYMBOL, "RANGING") is None


# ---------------------------------------------------------------------------
# 2. 数据缺失阻断
# ---------------------------------------------------------------------------


def test_prepare_raises_data_blocked_when_interval_empty(tmp_path) -> None:
    """某 symbol × interval 无 K 线 → prepare 抛 DataBlocked 且 gaps 含 kline_empty。"""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    as_of = start + timedelta(hours=4)
    bars_1h = make_bars(SYMBOL, "1h", start, [("100", "101", "99", "100")] * 4)
    bars_4h = make_bars(
        SYMBOL, "4h", start, [("100", "101", "99", "100")], step_minutes=240
    )
    directory = str(tmp_path / "snapshot")
    write_snapshot(
        directory,
        "btc_eth_aggressive",
        as_of,
        start,
        {"BTCUSDT|1h": bars_1h, "BTCUSDT|4h": bars_4h},
        [SYMBOL],
        ["1h", "4h", "1d"],
        extra_files={"klines/BTCUSDT_1d.jsonl": ""},
    )
    portal = DataPortal(directory, "btc_eth_aggressive", as_of, start)

    with pytest.raises(DataBlocked) as excinfo:
        _make_adapter().prepare(portal)

    assert any(gap == "kline_empty:BTCUSDT:1d" for gap in excinfo.value.gaps)


def test_prepare_builds_context_when_coverage_is_complete(tmp_path) -> None:
    """三周期齐全 → prepare 成功，extras 带 symbols / intervals / market_state。"""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    # 1d K 线收盘时间 = start + 1 天，as_of 必须晚于它才不被未来数据守卫过滤
    as_of = start + timedelta(days=2)
    bars_1h = make_bars(SYMBOL, "1h", start, [("100", "101", "99", "100")] * 4)
    bars_4h = make_bars(
        SYMBOL, "4h", start, [("100", "101", "99", "100")], step_minutes=240
    )
    bars_1d = make_bars(
        SYMBOL, "1d", start, [("100", "101", "99", "100")], step_minutes=1440
    )
    directory = str(tmp_path / "snapshot")
    write_snapshot(
        directory,
        "btc_eth_aggressive",
        as_of,
        start,
        {"BTCUSDT|1h": bars_1h, "BTCUSDT|4h": bars_4h, "BTCUSDT|1d": bars_1d},
        [SYMBOL],
        ["1h", "4h", "1d"],
    )
    portal = DataPortal(directory, "btc_eth_aggressive", as_of, start)

    ctx = _make_adapter().prepare(portal)

    assert ctx.strategy_id == "btc_eth_aggressive"
    assert ctx.as_of == as_of
    assert ctx.extras["symbols"] == [SYMBOL]
    assert ctx.extras["intervals"] == ["1h", "4h", "1d"]
    assert ctx.extras["market_state"] == "TREND"
    assert ctx.portfolio.positions == ()


# ---------------------------------------------------------------------------
# 3. protective_prices
# ---------------------------------------------------------------------------


def _open_position(
    symbol: str = SYMBOL,
    direction: str = "LONG",
    entry_price: str = "100",
    atr: str = "1.25",
    grade: str = "A",
) -> Position:
    return Position(
        position_id="pos.000001",
        symbol=symbol,
        direction=direction,
        entry_price=Decimal(entry_price),
        entry_quantity=Decimal("1"),
        current_quantity=Decimal("1"),
        atr=Decimal(atr),
        grade=grade,
        entry_time=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_protective_prices_match_core() -> None:
    """未平仓持仓 → 返回恰好三个 key，且价格与 core 退出公式相等。"""
    adapter = _make_adapter()
    position = _open_position()
    adapter._portfolio = PortfolioView(
        positions=(position,), last_prices={SYMBOL: Decimal("101")}, realized_equity=Decimal("0")
    )
    risk_config = _config()["strategy"]["risk"]

    protective = adapter.protective_prices(SYMBOL)

    assert protective is not None
    assert sorted(protective) == ["stop_loss_price", "tp1_price", "tp2_price"]
    assert protective["stop_loss_price"] == exits.calculate_stop_price(
        "LONG", position.entry_price, position.atr, "A", risk_config
    )
    assert protective["tp1_price"] == exits.calculate_tp_price(
        position.entry_price, position.atr, "LONG", 1, "A", risk_config
    )
    assert protective["tp2_price"] == exits.calculate_tp_price(
        position.entry_price, position.atr, "LONG", 2, "A", risk_config
    )


def test_protective_prices_none_without_open_position() -> None:
    """无持仓（或持仓已平完）→ 返回 None。"""
    adapter = _make_adapter()
    assert adapter.protective_prices(SYMBOL) is None

    closed = Position(
        position_id="pos.000002",
        symbol=SYMBOL,
        direction="LONG",
        entry_price=Decimal("100"),
        entry_quantity=Decimal("1"),
        current_quantity=Decimal("0"),
        atr=Decimal("1.25"),
        grade="A",
        entry_time=datetime(2026, 1, 1, tzinfo=UTC),
    )
    adapter._portfolio = PortfolioView(
        positions=(closed,), last_prices={}, realized_equity=Decimal("0")
    )
    assert adapter.protective_prices(SYMBOL) is None


# ---------------------------------------------------------------------------
# 4. intents：不加仓 / 数量过滤 / 确定性序
# ---------------------------------------------------------------------------


def test_intents_skip_open_position_and_non_positive_quantity() -> None:
    """已持仓 symbol 不加仓；数量为 0 的 symbol 跳过；产出按 symbol 排序。"""
    raw_klines = golden.scenario_data("trend_pullback")["klines"]
    quantities = {SYMBOL: Decimal("0.5"), "SOLUSDT": Decimal("0"), "XRPUSDT": Decimal("0.5")}
    adapter = MtpcsAggressiveAdapter(
        _config(),
        quantity_for=lambda symbol, decision: quantities.get(symbol),
        market_state="TREND",
    )
    ctx = _feed(adapter, raw_klines, symbols=(SYMBOL, "SOLUSDT", "XRPUSDT"))
    ctx.portfolio = PortfolioView(
        positions=(_open_position(symbol="XRPUSDT"),),
        last_prices={},
        realized_equity=Decimal("0"),
    )

    intents = adapter.intents(ctx)
    assert [intent.symbol for intent in intents] == [SYMBOL]

    ctx.portfolio = PortfolioView(positions=(), last_prices={}, realized_equity=Decimal("0"))
    intents = adapter.intents(ctx)
    assert [intent.symbol for intent in intents] == [SYMBOL, "XRPUSDT"]
    assert [intent.seq for intent in intents] == [1, 2]


def test_intents_fields_follow_decision() -> None:
    """意图字段（side / order_type / quantity / grade / 参考价 / reason）取自决策。"""
    raw_klines = golden.scenario_data("trend_pullback")["klines"]
    adapter = _make_adapter(quantity=Decimal("0.25"))
    ctx = _feed(adapter, raw_klines)
    decision = adapter.decide(SYMBOL, "TREND")
    assert decision is not None and decision["direction"] is not None

    intents = adapter.intents(ctx)

    assert len(intents) == 1
    intent = intents[0]
    assert intent.strategy_id == "btc_eth_aggressive"
    assert intent.order_type == "MARKET"
    assert intent.side == decision["direction"]
    assert intent.quantity == Decimal("0.25")
    assert intent.grade == decision["grade"]
    assert intent.reason == "{0}级信号".format(decision["grade"])
    assert intent.reference_price == decision["entry_price"]


def test_intents_raise_qualification_blocked_without_market_state() -> None:
    """构造未指定市场状态且 extras 也不带 → QualificationBlocked（不默认 TREND）。"""
    raw_klines = golden.scenario_data("trend_pullback")["klines"]
    adapter = MtpcsAggressiveAdapter(
        _config(), quantity_for=lambda symbol, decision: Decimal("0.5")
    )
    ctx = _feed(adapter, raw_klines)
    ctx.extras.pop("market_state")

    with pytest.raises(QualificationBlocked) as excinfo:
        adapter.intents(ctx)
    assert "market_state_unavailable" in excinfo.value.reasons


def test_decide_rejects_unknown_market_state() -> None:
    """decide 收到允许集合之外的状态名 → ValueError（不得静默按 TREND 处理）。"""
    raw_klines = golden.scenario_data("trend_pullback")["klines"]
    adapter = _make_adapter()
    _feed(adapter, raw_klines)

    with pytest.raises(ValueError) as excinfo:
        adapter.decide(SYMBOL, "SIDEWAYS")
    assert "SIDEWAYS" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5. import 白名单 / 不读系统时钟（S4 合约）
# ---------------------------------------------------------------------------


def _absolute_import_targets(path: Path) -> List[str]:
    """展开模块内全部绝对 import 为点号全名（含函数内的惰性 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            if node.names:
                targets.extend("{0}.{1}".format(module, alias.name) for alias in node.names)
            else:
                targets.append(module)
    return targets


def test_adapter_import_whitelist() -> None:
    """不得 import strategy / backtest / shared；strategies.* 只允许 core 子包。"""
    targets = _absolute_import_targets(MODULE_PATH)
    assert targets, "未解析到任何 import，扫描逻辑失效"

    forbidden = ("strategies.btc_eth_aggressive.strategy", "backtest", "shared")
    for target in targets:
        for prefix in forbidden:
            assert not target.startswith(prefix), "适配器不得 import {0}".format(target)
        if target.startswith("strategies"):
            assert target.startswith("strategies.btc_eth_aggressive.core."), (
                "strategies 只允许 core 子包：{0}".format(target)
            )


def test_adapter_does_not_read_system_clock() -> None:
    """适配器不得读系统时钟（S4：确定性回放只依赖快照时间）。"""
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "datetime.now(" not in source
    assert "utcnow(" not in source
