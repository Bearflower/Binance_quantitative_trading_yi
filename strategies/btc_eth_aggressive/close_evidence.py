"""两套 MTPCS 使用同一套普通平仓单的成交与盈亏核验。"""
from decimal import Decimal

from shared.entry_fill_evidence import verified_fill


async def confirmed_close_order(client, symbol, side, order_id, quantity):
    order = await client.get_order(symbol, int(order_id))
    if (not isinstance(order, dict) or str(order.get("orderId")) != str(order_id)
            or order.get("symbol") != symbol or order.get("side") != side
            or order.get("reduceOnly") is not True or order.get("status") != "FILLED"
            or Decimal(str(order["executedQty"])) != quantity
            or Decimal(str(order["origQty"])) != quantity):
        return None
    return order


async def record_close_fills(client, order, is_stop):
    """仅写精确的下单回执，成交缺失时留待交易所事实对账。"""
    owner = client.trade_logger
    if owner is None:
        return False
    symbol, order_id, side = order["symbol"], str(order["orderId"]), order["side"]
    fills = await client.get_user_trades(symbol, order_id=order_id, limit=1000)
    record = dict(symbol=symbol, order_id=order_id, side=side)
    quantity, price = verified_fill(record, order, fills)
    epoch = owner._record_epoch(client)
    if not owner._receipt_in_epoch(order, epoch) or len(owner._epoch_trades(fills, epoch)) != len(fills):
        raise ValueError("平仓成交不在本账户交易时段内")
    pnl = sum((Decimal(str(fill["realizedPnl"])) for fill in fills), Decimal(0))
    if not pnl.is_finite():
        raise ValueError("平仓收益不是有限数值")
    updated = await owner.db.execute("""
        UPDATE trading.trade_records SET realized_pnl=$1, pnl_source='exchange_order_matched',
            close_reason=COALESCE($2, close_reason), quantity=$3, price=$4, status='FILLED'
        WHERE strategy=$5 AND side=$6 AND id=(
            SELECT min(id) FROM trading.trade_records
            WHERE account_id=$7 AND symbol=$8 AND order_id=$9 AND executed_at <@ $10::tsrange
            HAVING count(*)=1)
    """, pnl, "STOP_LOSS" if is_stop else None, quantity, price, owner.strategy_name,
                                  side, owner.account_id, symbol, order_id, epoch)
    return updated == "UPDATE 1"


async def remaining_protection(client, symbol, position, close_side):
    """平尾仓后仍在交易所的硬止损及 TP2 不可重复补挂。"""
    from shared.condition_orders import classic_algo_is_terminal, strategy_owned_algos

    orders = await strategy_owned_algos(client, symbol=symbol)
    covered = []
    for slot, role in (("stop_loss_order_id", "STOP_LOSS"), ("tp2_order_id", "TAKE_PROFIT")):
        saved_id = getattr(position, slot)
        matches = [o for o in orders if o["order_type"] == role
                   and o["side"] == close_side and o.get("reduceOnly") is True
                   and (role == "STOP_LOSS" or o.get("tp_level") == 2)]
        if saved_id and not any(str(o["algo_id"]) == str(saved_id) for o in matches):
            if not await classic_algo_is_terminal(client, symbol, int(saved_id)):
                raise ValueError("此前的保护单状态未确认，暂停补挂")
        if len(matches) > 1:
            raise ValueError("同一档存在多笔保护单，暂停补挂")
        if matches:
            setattr(position, slot, matches[0]["algo_id"])
        else:
            setattr(position, slot, None)
        covered.append(bool(matches))
    return covered
