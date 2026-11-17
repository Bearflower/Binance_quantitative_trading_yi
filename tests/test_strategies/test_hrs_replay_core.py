"""Live/replay calculation contracts; synthetic cases do not qualify performance."""
import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

UTC = timezone.utc
AT = datetime(2026, 11, 17, 1, tzinfo=UTC)


def _module(name):
    assert importlib.util.find_spec(name) is not None, f'{name} calculation boundary missing'
    return __import__(name, fromlist=['*'])


def _bars(count=30):
    return [dict(open_time=int((AT-timedelta(hours=count-i)).timestamp()*1000),
                 open=100+i*.1, high=102+i*.1, low=98+i*.1, close=100+i*.1,
                 volume=1000) for i in range(count)]


def test_shared_standard_runs_actual_scorer_and_keeps_shadows():
    core = _module('strategies.hrs.core_decision')
    from strategies.hrs.scoring_engine import ScoringEngine
    from strategies.hrs.pattern import PatternRecognizer
    config = {'emm': {'enabled': True}, 'semi_emm': {'enabled': True}}
    scorer, recognizer = ScoringEngine(config), PatternRecognizer(config)
    bars = _bars()
    trend = _bars_4h()
    actual = core.analyze_standard(symbol='TESTUSDT', klines_1h=bars, klines_4h=trend,
        oi_market_cap_ratio=.2, funding_rate=.001, has_market_cap=True,
        directions=('short','long'), decision_at=AT, config=config,
        scoring_engine=scorer, pattern_recognizer=recognizer)
    for direction in ('short','long'):
        expected = scorer.score('TESTUSDT',direction,.2,
            getattr(recognizer,f'detect_{direction}_patterns')(bars),.001,True,
            (bars[-1]['close']/bars[-25]['close']-1)*100,trend)
        assert actual[direction]['score_result'] == expected.to_dict()
        assert actual[direction]['_scoring_result'].to_dict() == expected.to_dict()


def test_shared_lv_rm_uses_same_sync_and_async_scorer():
    from strategies.hrs.scoring_engine import ScoringEngine
    scorer = ScoringEngine({})
    assert hasattr(scorer,'score_lv_rm_sync'), 'pure LV scoring requires sync replay boundary'
    kwargs = dict(symbol='TESTUSDT',direction='short',oi_market_cap_ratio=.1,
                  funding_rate=.001,has_market_cap=True,klines_1h=_bars(),klines_4h=_bars_4h())
    assert scorer.score_lv_rm_sync(**kwargs).to_dict() == asyncio.run(scorer.score_lv_rm(**kwargs)).to_dict()
    core = _module('strategies.hrs.core_decision')
    kwargs.pop('direction')
    kwargs.update(config={}, decision_at=AT, directions=('short','long'), scoring_engine=scorer)
    result = core.analyze_lv_rm(**kwargs)
    assert result['short']['_scoring_result'].entry_mode == 'lv_rm'
    assert result['long']['_scoring_result'].entry_mode == 'lv_rm'


def test_shadow_origin_and_conflict_ties_preserve_live_policy():
    core = _module('strategies.hrs.core_decision')
    config = {'lv_rm': {'execution_mode':'shadow'},'emm':{'execution_mode':'shadow'}}
    assert core.is_shadow_entry_mode(config,'lv_rm','empty_pool')
    assert not core.is_shadow_entry_mode(config,'lv_rm','nonempty_pool')
    assert core.is_shadow_entry_mode(config,'lv_rm',None)
    assert core.is_shadow_entry_mode(config,'emm','shadow_scoring')
    short = dict(symbol='A',score=7,technical_score=6,sentiment_score=5,direction='short')
    long = dict(short,direction='long')
    accepted,rejected=core.resolve_signal_conflicts([short],[long])
    assert accepted == () and len(rejected)==2
    long['sentiment_score']=6
    accepted,rejected=core.resolve_signal_conflicts([short],[long])
    assert accepted == (long,) and rejected == (short,)


def test_geometry_matches_live_distance_floor_rounding_and_modes():
    exits = _module('strategies.hrs.exit_geometry')
    config={'trading':{'stop_loss':{'atr_multiplier':2.5,'emergency_percent':.015,
                                 'min_absolute_percent':.05}},
            'lv_rm':{'stop_loss':{'min_absolute_percent':.03}}}
    standard=exits.mode_exit_parameters(config,'standard')
    lv=exits.mode_exit_parameters(config,'lv_rm')
    assert exits.stop_loss_price('short',100,.2,Decimal('.01'),standard,config)==Decimal('105')
    assert exits.stop_loss_price('long',100,.2,Decimal('.01'),standard,config)==Decimal('95')
    assert exits.stop_loss_price('short',100,.2,Decimal('.01'),lv,config)==Decimal('103')
    assert exits.take_profit_price('short','tp2',100,2,Decimal('.01'),standard,config)==Decimal('93')
    assert exits.calculate_atr(_bars(),14)==4
    assert exits.trailing_stop_price('long',110,2,Decimal('.01'),standard,config)==Decimal('107')


def test_core_rejects_future_and_discontinuous_bars():
    core = _module('strategies.hrs.core_decision')
    bars=_bars()
    from ai_tuner.replay.models import DataBlocked
    for broken in (bars[:-1]+[dict(bars[-1],open_time=int(AT.timestamp()*1000))],
                   bars[:10]+bars[11:]):
        with pytest.raises((DataBlocked,ValueError)):
            core.analyze_standard(symbol='TESTUSDT',klines_1h=broken,klines_4h=_bars_4h(),
                oi_market_cap_ratio=.2,funding_rate=.001,has_market_cap=True,
                directions=('short',),decision_at=AT,config={})


def test_market_guard_uses_shared_thresholds_with_explicit_clock():
    core=_module('strategies.hrs.core_decision')
    assert hasattr(core,'evaluate_market_guard'), 'replay must run the live market guard'
    config=dict(trigger_short=.02,trigger_long=.02,single_coin_short=.03,
                single_coin_long=.03,single_coin_cooldown_hours=4,include_12h=False)
    allow,level,cooldowns=core.evaluate_market_guard(config,'short','A',.04,0,None,AT,{})
    assert not allow and level=='coin_short'
    assert cooldowns['A']==AT+timedelta(hours=4)
    allow,level,_=core.evaluate_market_guard(config,'long','A',0,0,None,AT,cooldowns)
    assert not allow and level=='cooldown'
    allow,level,_=core.evaluate_market_guard(config,'short','B',.03,.02,None,AT,{})
    assert allow and level=='allow'


@pytest.mark.parametrize('field,value',[('close',float('nan')),('high',float('inf')),('volume',-1),('low',999)])
def test_shared_core_rejects_nonfinite_or_inconsistent_ohlcv(field,value):
    core=_module('strategies.hrs.core_decision')
    bars=_bars();bars[-1]=dict(bars[-1],**{field:value})
    with pytest.raises(ValueError):
        core.analyze_standard(symbol='TESTUSDT',klines_1h=bars,klines_4h=_bars_4h(),
            oi_market_cap_ratio=.2,funding_rate=.001,has_market_cap=True,
            directions=('short',),decision_at=AT,config={})


def _bars_4h():
    bars=_bars()
    for index,bar in enumerate(bars):
        bar['open_time']=int((AT-timedelta(hours=4*(len(bars)-index))).timestamp()*1000)
    return bars


def test_shared_core_rejects_future_4h_trend_input():
    core=_module('strategies.hrs.core_decision')
    trend=_bars_4h();trend[-1]['open_time']=int(AT.timestamp()*1000)
    with pytest.raises(ValueError):
        core.analyze_standard(symbol='TESTUSDT',klines_1h=_bars(),klines_4h=trend,
            oi_market_cap_ratio=.2,funding_rate=.001,has_market_cap=True,
            directions=('short',),decision_at=AT,config={})


def test_shared_hrs_risk_core_allows_only_legal_explicit_full_tp1():
    from strategies.hrs.risk_budget import assess_entry
    D=Decimal
    arguments=dict(allocation=D(1000),entry_price=D(100),stop_price=D(95),direction='long',
        existing_open_risk=D(0),leverage=D(2),remaining_margin=D(500),qty_step=D('.001'),
        min_qty=D('.001'),min_notional=D(5),fee_rate=D('.0005'),pressure_slippage=D('.02'),
        take_profit_prices=(D(103),D(107)))
    result=assess_entry(**arguments,take_profit_ratios=(D(1),D(0)))
    assert result.allowed and result.exit_ratios==(D(1),D(0))
    for ratios in ((0,0),('.3',0),(-1,'.4'),(1,'.1'),('.5','.5')):
        assert not assess_entry(**arguments,take_profit_ratios=tuple(D(str(value)) for value in ratios)).allowed
    arguments['take_profit_prices']=(D(1),D(107))
    assert not assess_entry(**arguments,take_profit_ratios=(D(1),D(0))).allowed
