"""Verify a terminal entry order against complete, unique exchange trades."""
from decimal import Decimal


def verified_fill(record, order, trades):
    if not isinstance(order, dict) or order.get('status') != 'FILLED':
        raise ValueError('terminal fill unavailable')
    for key, source in [('symbol', 'symbol'), ('side', 'side'), ('order_id', 'orderId')]:
        if str(record[key]) != str(order.get(source)):
            raise ValueError('order identity mismatch')
    if not isinstance(trades, list) or not trades or len(trades) >= 1000:
        raise ValueError('complete fill page unavailable')
    seen, quantity, quote = set(), Decimal(0), Decimal(0)
    for trade in trades:
        for key, source in [('symbol', 'symbol'), ('side', 'side'), ('order_id', 'orderId')]:
            if str(record[key]) != str(trade.get(source)):
                raise ValueError('trade identity mismatch')
        identity = trade.get('id')
        if identity is None or identity in seen:
            raise ValueError('missing or duplicate trade')
        seen.add(identity)
        qty, price = Decimal(str(trade['qty'])), Decimal(str(trade['price']))
        if not qty.is_finite() or not price.is_finite() or qty <= 0 or price <= 0:
            raise ValueError('invalid fill amount')
        quantity += qty
        quote += qty * price
    if quantity != Decimal(str(order['executedQty'])) or quantity != Decimal(str(order['origQty'])):
        raise ValueError('incomplete executed quantity')
    return quantity, quote / quantity
