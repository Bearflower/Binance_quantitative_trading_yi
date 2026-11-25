"""Shared HRS exit arithmetic. No clients, clocks, database or configuration IO.

Keep the live stop calculation's float arithmetic before Decimal tick rounding;
changing it silently would change existing protection at tick boundaries.
"""
from decimal import Decimal


def format_price(price, tick_size, config):
    price, tick_size = Decimal(str(price)), Decimal(str(tick_size))
    if tick_size <= 0:
        return price
    settings = config.get('trading', {}).get('price', {})
    return (price / tick_size).quantize(
        Decimal(str(settings.get('quantize_precision', 1))),
        rounding=settings.get('quantize_rounding', 'ROUND_HALF_UP')) * tick_size


def mode_exit_parameters(config, entry_mode):
    trading = config.get('trading', {})
    if entry_mode == 'lv_rm':
        lv = config.get('lv_rm', {})
        sl, tp = lv.get('stop_loss', {}), lv.get('take_profit', {})
        defaults = (1.5, .01, .03, 1., 2., 1., 48)
        time = lv.get('time_stop', {})
    else:
        sl, tp = trading.get('stop_loss', {}), trading.get('batch_take_profit', {})
        defaults = (2.5, .015, .05, 1.5, 3.5, 1.5, 72)
        time = trading.get('time_stop', {})
    return {
        'atr_multiplier': sl.get('atr_multiplier', defaults[0]),
        'emergency_percent': sl.get('emergency_percent', defaults[1]),
        'min_absolute_percent': sl.get('min_absolute_percent', defaults[2]),
        'target1_atr_multiplier': tp.get('target1_atr_multiplier', defaults[3]),
        'target1_close_percent': tp.get('target1_close_percent', .30),
        'target2_atr_multiplier': tp.get('target2_atr_multiplier', defaults[4]),
        'target2_close_percent': tp.get('target2_close_percent', .40),
        'trailing_atr_multiplier': (tp.get('trailing_stop_atr_multiplier', defaults[5])
            if entry_mode == 'lv_rm' else trading.get('trailing', {}).get('atr_multiplier', defaults[5])),
        'max_holding_hours': time.get('max_holding_hours', defaults[6]),
    }


def stop_loss_price(direction, entry_price, atr, tick_size, params, config):
    if direction not in ('short', 'long'):
        raise ValueError('invalid_direction')
    price, atr = float(entry_price), float(atr)
    sign = 1 if direction == 'short' else -1
    prices = (price + sign * float(params['atr_multiplier']) * atr,
              price * (1 + sign * float(params['emergency_percent'])),
              price * (1 + sign * float(params['min_absolute_percent'])))
    selected = max(prices) if sign == 1 else min(prices)
    return format_price(Decimal(str(selected)), tick_size, config)


def take_profit_price(direction, target, entry_price, atr, tick_size, params, config):
    if direction not in ('short', 'long'):
        raise ValueError('invalid_direction')
    key = {'tp1':'target1_atr_multiplier','tp2':'target2_atr_multiplier'}[target]
    distance = Decimal(str(atr)) * Decimal(str(params[key]))
    return format_price(Decimal(str(entry_price)) + (-distance if direction == 'short' else distance),
                        tick_size, config)


def trailing_stop_price(direction, best_price, atr, tick_size, params, config):
    if direction not in ('short', 'long'):
        raise ValueError('invalid_direction')
    distance = float(atr) * float(params['trailing_atr_multiplier'])
    price = float(best_price) + (distance if direction == 'short' else -distance)
    return format_price(Decimal(str(max(price, .01))), tick_size, config)


def calculate_atr(klines, period=14):
    """Same rolling true-range mean as TechnicalIndicators.calculate_atr."""
    if not klines or len(klines) < period + 1:
        return 0.0
    ranges = []
    for index, bar in enumerate(klines):
        high, low = float(bar['high']), float(bar['low'])
        previous = float(klines[index-1]['close']) if index else None
        ranges.append(max(high-low, abs(high-previous), abs(low-previous))
                      if previous is not None else high-low)
    return sum(ranges[-int(period):]) / int(period)
