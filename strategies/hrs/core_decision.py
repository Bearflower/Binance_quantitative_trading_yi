"""HRS live/replay decision calculations; callers supply point-in-time facts.

Network/state collection stays in live orchestration and the replay DataPortal.
No recorded score, should_entry flag or chosen action is accepted by this API.
"""
from datetime import datetime
import math
from typing import Optional

from .pattern import PatternRecognizer
from .scoring_engine import ScoringEngine


class DecisionInputsUnavailable(ValueError):
    """Raw decision data cannot establish a historical decision."""


def validate_klines(klines, decision_at, *, interval_ms=3600000, minimum=1):
    if not isinstance(decision_at, datetime) or decision_at.tzinfo is None:
        raise DecisionInputsUnavailable('decision_time_invalid')
    if len(klines) < minimum:
        raise DecisionInputsUnavailable('kline_warmup_insufficient')
    times = [bar['open_time'] for bar in klines]
    if any(right-left != interval_ms for left,right in zip(times,times[1:])):
        raise DecisionInputsUnavailable('kline_history_discontinuous')
    if times[-1]+interval_ms > int(decision_at.timestamp()*1000):
        raise DecisionInputsUnavailable('kline_not_closed')
    for bar in klines:
        try:
            prices={key:float(bar[key]) for key in ('open','high','low','close')}
            volume=float(bar['volume'])
            if (any(not math.isfinite(value) or value<=0 for value in prices.values())
                    or not math.isfinite(volume) or volume<0
                    or not prices['low']<=prices['open']<=prices['high']
                    or not prices['low']<=prices['close']<=prices['high']):
                raise DecisionInputsUnavailable('kline_ohlcv_invalid')
        except (KeyError,TypeError,OverflowError) as exc:
            raise DecisionInputsUnavailable('kline_ohlcv_missing') from exc


def price_change_24h(klines, decision_at, *, interval_ms=3600000):
    required = int(round(86400000/interval_ms))+1
    if len(klines) < required:
        return None
    validate_klines(klines[-required:],decision_at,interval_ms=interval_ms,minimum=required)
    return (float(klines[-1]['close'])/float(klines[-required]['close'])-1)*100


def price_change_1h(klines):
    if len(klines) < 2 or float(klines[-2]['close']) <= 0:
        return None
    return (float(klines[-1]['close'])-float(klines[-2]['close']))/float(klines[-2]['close'])


def pattern_expired(direction, current_price, klines, lookback):
    if len(klines) < lookback:
        return False
    window = klines[-lookback:]
    return (current_price > max(float(k['high']) for k in window) if direction == 'short'
            else current_price < min(float(k['low']) for k in window))


def is_shadow_entry_mode(config, entry_mode, signal_origin=None):
    if entry_mode not in ('emm','semi_emm','lv_rm'):
        return False
    if entry_mode == 'lv_rm':
        if signal_origin is None:
            return True
        if signal_origin == 'nonempty_pool':
            return False
    return (config.get(entry_mode,{}) or {}).get('execution_mode') == 'shadow'


def analyze_standard(*, symbol, klines_1h, klines_4h, oi_market_cap_ratio,
                     funding_rate, has_market_cap, directions, decision_at, config,
                     scoring_engine=None, pattern_recognizer=None):
    validate_klines(klines_1h,decision_at,
        minimum=config.get('kline',{}).get('min_klines_for_analysis',24))
    validate_klines(klines_4h,decision_at,interval_ms=14400000,minimum=1)
    scorer = scoring_engine or ScoringEngine(config)
    recognizer = pattern_recognizer or PatternRecognizer(config)
    result = {'symbol':symbol,'short':None,'long':None}
    price = float(klines_1h[-1]['close'])
    change = price_change_24h(klines_1h,decision_at)
    lookback = config.get('trading',{}).get('abandon',{}).get('extreme_lookback',20)
    for direction in directions:
        if direction not in ('short','long'):
            raise ValueError('invalid_direction')
        if pattern_expired(direction,price,klines_1h,lookback):
            result[direction] = dict(score_result={},patterns={},current_price=price,
                funding_rate=funding_rate,should_entry=False,veto_reason='形态失效：价格突破反向极值')
            continue
        patterns = getattr(recognizer,'detect_'+direction+'_patterns')(klines_1h)
        scored = scorer.score(symbol=symbol,direction=direction,
            oi_market_cap_ratio=oi_market_cap_ratio,patterns=patterns,funding_rate=funding_rate,
            has_market_cap=has_market_cap,price_change_24h=change,klines_4h=klines_4h)
        result[direction] = dict(score_result=scored.to_dict(),_scoring_result=scored,
            patterns=patterns,current_price=price,funding_rate=funding_rate,
            should_entry=scorer.should_entry(scored))
    return result


def analyze_lv_rm(*, symbol, klines_1h, klines_4h, oi_market_cap_ratio,
                  funding_rate, has_market_cap, decision_at, config,
                  directions=('short','long'),scoring_engine=None):
    validate_klines(klines_1h,decision_at,
        minimum=config.get('kline',{}).get('min_klines_for_analysis',24))
    validate_klines(klines_4h,decision_at,interval_ms=14400000,minimum=1)
    scorer = scoring_engine or ScoringEngine(config)
    result = {'symbol':symbol,'short':None,'long':None}
    for direction in directions:
        scored = scorer.score_lv_rm_sync(symbol=symbol,direction=direction,
            oi_market_cap_ratio=oi_market_cap_ratio,funding_rate=funding_rate,
            has_market_cap=has_market_cap,klines_1h=klines_1h,klines_4h=klines_4h)
        result[direction] = dict(score_result=scored.to_dict(),_scoring_result=scored,
            patterns={},current_price=float(klines_1h[-1]['close']),funding_rate=funding_rate,
            should_entry=scorer.should_entry(scored))
    return result


def resolve_signal_conflicts(short_signals,long_signals):
    short = {s['symbol']:s for s in short_signals}
    long = {s['symbol']:s for s in long_signals}
    rejected = []
    def rank(signal):
        return tuple(signal.get(key,0) for key in ('score','technical_score','sentiment_score'))
    for symbol in sorted(short.keys() & long.keys()):
        if rank(short[symbol]) >= rank(long[symbol]):
            rejected.append(long.pop(symbol))
            if rank(short[symbol]) == rank(rejected[-1]):
                rejected.append(short.pop(symbol))
        else:
            rejected.append(short.pop(symbol))
    return tuple(short.values())+tuple(long.values()),tuple(rejected)


def evaluate_market_guard(config, direction, symbol, symbol_return, pool_index,
                          pool_index_12h, decision_at, cooldowns):
    """Use the shared guard with an explicit historical clock and cooldown map."""
    from datetime import timedelta
    from shared.circuit_breaker import CircuitBreaker

    class HistoricalGuard(CircuitBreaker):
        def is_in_cooldown(self, candidate):
            deadline = self._cooldown_until.get(candidate)
            return deadline is not None and decision_at < deadline
        def start_cooldown(self, candidate):
            self._cooldown_until[candidate] = decision_at + timedelta(hours=self._cooldown_hours)

    guard = HistoricalGuard(config,None,'hrs')
    guard._cooldown_until = dict(cooldowns)
    allow,level = guard.guard(direction,symbol,symbol_return,pool_index,pool_index_12h)
    return allow,level,dict(guard._cooldown_until)


def score_holding(*, symbol, direction, klines_1h, klines_4h, oi_market_cap_ratio,
                  funding_rate, has_market_cap, decision_at, config,
                  scoring_engine=None, pattern_recognizer=None):
    """Time-stop reanalysis always runs the standard chain outside candidate gates."""
    validate_klines(klines_1h,decision_at,
        minimum=config.get('kline',{}).get('min_klines_for_analysis',24))
    validate_klines(klines_4h,decision_at,interval_ms=14400000,minimum=1)
    scorer=scoring_engine or ScoringEngine(config)
    recognizer=pattern_recognizer or PatternRecognizer(config)
    patterns=getattr(recognizer,'detect_'+direction+'_patterns')(klines_1h)
    return scorer.score(symbol=symbol,direction=direction,patterns=patterns,
        oi_market_cap_ratio=oi_market_cap_ratio,funding_rate=funding_rate,
        has_market_cap=has_market_cap,klines_4h=klines_4h)
