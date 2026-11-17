"""Persist the exact entry-time exit geometry; legacy positions retain legacy rules."""
from decimal import Decimal
import math

PARAMETERS = frozenset({'atr_multiplier', 'emergency_percent', 'min_absolute_percent',
    'target1_atr_multiplier', 'target1_close_percent', 'target2_atr_multiplier',
    'target2_close_percent', 'trailing_atr_multiplier', 'max_holding_hours'})


def validate_exit_plan(value):
    try:
        if not isinstance(value, dict) or value['version'] != 1:
            raise ValueError()
        params = value['params']
        if not isinstance(params, dict) or not PARAMETERS <= params.keys():
            raise ValueError()
        result = {'version': 1, 'params': {}}
        for key, raw in [('reference_price', value['reference_price']), ('atr', value['atr'])]:
            number = Decimal(str(raw))
            if not number.is_finite() or number <= 0:
                raise ValueError()
            result[key] = float(number)
            if not math.isfinite(result[key]) or result[key] <= 0:
                raise ValueError()
        for key in PARAMETERS:
            number = Decimal(str(params[key]))
            if not number.is_finite() or number < 0 or (number == 0 and key != 'target2_close_percent'):
                raise ValueError()
            result['params'][key] = float(number)
            if not math.isfinite(result['params'][key]) or (number > 0 and result['params'][key] <= 0):
                raise ValueError()
        p = result['params']
        if p['emergency_percent'] >= 1 or p['min_absolute_percent'] >= 1:
            raise ValueError()
        if p['target1_close_percent'] + p['target2_close_percent'] > 1:
            raise ValueError()
        if not all(math.isfinite(value) for value in (result['reference_price'], result['atr'], *p.values())):
            raise ValueError()
        # Supported exit horizons must also fit runtime datetime arithmetic.
        if p['max_holding_hours'] > 24 * 366 * 10:
            raise ValueError()
        return result
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError('invalid_hrs_exit_plan') from exc
