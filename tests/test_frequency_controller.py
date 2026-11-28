"""验证当前频控边界：控制器管日计数/亏损，策略管市场状态限额和冷却。

旧手工脚本引用从未存在于现有控制器的risk_config和双计数字段，且失败只打印。
本组沿真实调用边界断言，不再让失败条件作为pytest成功返回。
"""
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from strategies.btc_eth.strategy import BTCEthStrategy, FrequencyController

NOW = datetime(2026, 11, 26, 12)
CONFIG = {
    'max_daily_symbol_trades': 1, 'symbol_cooldown_hours': 72,
    'consecutive_loss_pause': 2, 'pause_duration_hours': 24,
    'max_daily_loss_usdt': 25, 'max_daily_loss_ratio': .05,
    'initial_capital_usdt': 500, 'weekly_loss_pause_enabled': False,
}


def controller():
    return FrequencyController(dict(CONFIG), strategy_name='离线测试策略')


async def eligible(ctrl, state, *, symbol='BTCUSDT', symbol_config=None):
    strategy = SimpleNamespace(frequency_controller=ctrl, symbol_config=symbol_config or {})
    return await BTCEthStrategy._check_stateful_execution_eligibility(
        strategy, symbol, NOW, market_state_name=state,
        market_behavior={'max_daily_trades': 2 if state == 'STRONG_TREND' else 3,
                         'ranging_symbol_cooldown_hours': 72 if state == 'STRONG_TREND' else 3},
    )


@pytest.mark.asyncio
async def test_market_state_applies_distinct_daily_limits():
    ctrl = controller(); ctrl.daily_trades[NOW.date().isoformat()] = 2
    assert not (await eligible(ctrl, 'STRONG_TREND'))[0]
    assert (await eligible(ctrl, 'RANGING'))[0]
    ctrl.daily_trades[NOW.date().isoformat()] = 3
    assert not (await eligible(ctrl, 'RANGING'))[0]


@pytest.mark.asyncio
async def test_symbol_ranging_limit_overrides_market_limit():
    ctrl = controller(); day = NOW.date().isoformat()
    config = {'SOLUSDT': {'ranging_max_daily_trades': .5}}
    assert (await eligible(ctrl, 'RANGING', symbol='SOLUSDT', symbol_config=config))[0]
    ctrl.symbol_daily_trades['SOLUSDT'][day] = 1
    allowed, reason = await eligible(ctrl, 'RANGING', symbol='SOLUSDT', symbol_config=config)
    assert not allowed and 'SOLUSDT' in reason
    assert (await eligible(ctrl, 'RANGING', symbol='ETHUSDT', symbol_config=config))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('hours,trend_allowed,ranging_allowed', [(2, False, False), (4, False, True), (72, True, True)])
async def test_market_state_cooldown(hours, trend_allowed, ranging_allowed):
    ctrl = controller(); ctrl.symbol_last_trade_time['BTCUSDT'] = NOW - timedelta(hours=hours)
    assert (await eligible(ctrl, 'STRONG_TREND'))[0] is trend_allowed
    assert (await eligible(ctrl, 'RANGING'))[0] is ranging_allowed


@pytest.mark.asyncio
async def test_real_loss_recording_triggers_pause():
    ctrl = controller()
    await ctrl.record_trade('BTCUSDT', NOW, Decimal('-1'))
    assert ctrl.pause_until is None
    await ctrl.record_trade('ETHUSDT', NOW, Decimal('-1'))
    assert ctrl.pause_until == NOW + timedelta(hours=24)
    allowed, reason = ctrl.can_trade('SOLUSDT', NOW)
    assert not allowed and '暂停' in reason


@pytest.mark.parametrize('pnl,allowed', [('-25', False), ('-10', True)])
def test_daily_loss_limit(pnl, allowed):
    ctrl = controller(); ctrl.daily_pnl[NOW.date().isoformat()] = Decimal(pnl)
    assert ctrl.can_trade('BTCUSDT', NOW)[0] is allowed


@pytest.mark.asyncio
async def test_record_trade_updates_total_symbol_and_time():
    ctrl = controller(); day = NOW.date().isoformat()
    await ctrl.record_trade('BTCUSDT', NOW)
    await ctrl.record_trade('ETHUSDT', NOW)
    assert ctrl.daily_trades[day] == 2
    assert ctrl.symbol_daily_trades['BTCUSDT'][day] == 1
    assert ctrl.symbol_daily_trades['ETHUSDT'][day] == 1
    assert ctrl.symbol_last_trade_time['BTCUSDT'] == NOW
    assert not ctrl.can_trade('BTCUSDT', NOW)[0]
