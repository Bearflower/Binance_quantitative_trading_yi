"""两套 MTPCS 使用同一套普通平仓单的成交与盈亏核验。"""
import asyncio
import hashlib
import json
from decimal import Decimal

import structlog

from shared.mtpcs_close_progress import _context, save_close_progress

logger = structlog.get_logger()

from shared.entry_fill_evidence import _positive_decimal, _positive_id


async def confirmed_close_order(client, symbol, side, order_id, quantity):
    """终态订单必须属于本次减仓请求，撤单可能包含真实部分成交。"""
    order = await client.get_order(symbol, int(order_id))
    if (not isinstance(order, dict) or str(order.get("orderId")) != str(order_id)
            or order.get("symbol") != symbol or order.get("side") != side
            or order.get("reduceOnly") is not True
            or order.get("status") not in ("FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED")):
        return None
    executed = Decimal(str(order.get("executedQty")))
    original = Decimal(str(order.get("origQty")))
    if (not executed.is_finite() or not original.is_finite()
            or original != quantity or not 0 <= executed <= original
            or (order["status"] == "FILLED" and executed != original)):
        return None
    return order


def verified_close_fill(record, order, trades):
    """完整成交明细必须精确覆盖终态单的已成交量，包括部分成交后撤销。"""
    if (order.get("status") not in ("FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH")
            or order.get("reduceOnly") is not True):
        raise ValueError("平仓单终态或减仓身份未确认")
    order_id = _positive_id(record["order_id"])
    symbol, side = record["symbol"], record["side"]
    if (not symbol or side not in ("BUY", "SELL")
            or _positive_id(order.get("orderId")) != order_id
            or order.get("symbol") != symbol or order.get("side") != side):
        raise ValueError("平仓订单身份不匹配")
    executed = _positive_decimal(order.get("executedQty"))
    original = _positive_decimal(order.get("origQty"))
    if executed > original or (order["status"] == "FILLED" and executed != original):
        raise ValueError("平仓成交量无效")
    if not isinstance(trades, list) or not 0 < len(trades) < 1000:
        raise ValueError("平仓成交集合不完整")
    quantity, quote, seen = Decimal(0), Decimal(0), set()
    for trade in trades:
        trade_id = _positive_id(trade.get("id"))
        if (trade_id in seen or _positive_id(trade.get("orderId")) != order_id
                or trade.get("symbol") != symbol or trade.get("side") != side):
            raise ValueError("平仓成交身份不匹配或重复")
        seen.add(trade_id)
        qty = _positive_decimal(trade.get("qty"))
        quantity += qty
        quote += qty * _positive_decimal(trade.get("price"))
    if quantity != executed:
        raise ValueError("平仓成交数量不完整")
    return quantity, quote / quantity


async def record_close_fills(client, order, is_stop):
    """仅写精确的下单回执，成交缺失时留待交易所事实对账。"""
    owner = client.trade_logger
    if owner is None:
        return False
    symbol, order_id, side = order["symbol"], str(order["orderId"]), order["side"]
    fills = await client.get_user_trades(symbol, order_id=order_id, limit=1000)
    record = dict(symbol=symbol, order_id=order_id, side=side)
    quantity, price = verified_close_fill(record, order, fills)
    epoch = owner._record_epoch(client)
    if not owner._receipt_in_epoch(order, epoch) or len(owner._epoch_trades(fills, epoch)) != len(fills):
        raise ValueError("平仓成交不在本账户交易时段内")
    pnl = sum((Decimal(str(fill["realizedPnl"])) for fill in fills), Decimal(0))
    if not pnl.is_finite():
        raise ValueError("平仓收益不是有限数值")
    updated = await owner.db.execute("""
        UPDATE trading.trade_records SET realized_pnl=$1, pnl_source='exchange_order_matched',
            close_reason=COALESCE($2, close_reason), quantity=$3, price=$4, status=$11
        WHERE strategy=$5 AND side=$6 AND id=(
            SELECT min(id) FROM trading.trade_records
            WHERE account_id=$7 AND symbol=$8 AND order_id=$9 AND executed_at <@ $10::tsrange
            HAVING count(*)=1)
    """, pnl, "STOP_LOSS" if is_stop else None, quantity, price, owner.strategy_name,
                                  side, owner.account_id, symbol, order_id, epoch, order["status"])
    return updated == "UPDATE 1"


def _pending_payload(strategy, symbol, position, order, is_stop):
    """退出订单身份独立于仓位内存，平仓后仍能恢复真实盈亏。"""
    if (order.get('symbol') != symbol or order.get('reduceOnly') is not True
            or order.get('side') != ('SELL' if position.direction == 'LONG' else 'BUY')
            or position.direction not in ('LONG', 'SHORT')):
        raise ValueError('待结算退出方向或币种不匹配')
    executed = _positive_decimal(order.get('executedQty'))
    original = _positive_decimal(order.get('origQty'))
    if executed > original:
        raise ValueError('待结算退出量无效')
    _positive_id(order.get('orderId'))
    snapshot = {key: order[key] for key in ('symbol','side','orderId','status','reduceOnly',
                                           'executedQty','origQty','time','updateTime') if key in order}
    return {'status':'pending', 'context':_context(strategy), 'symbol':symbol,
            'entry':{'direction':position.direction,
                     'entry_order_id':getattr(position, 'entry_order_id', None),
                     'tp2_order_id':getattr(position, 'tp2_order_id', None),
                     'initial_quantity':str(getattr(position, 'initial_quantity', '0'))},
            'order':snapshot, 'is_stop':bool(is_stop)}


def _pending_key(payload):
    identity = json.dumps([payload['context'], payload['symbol'], str(payload['order']['orderId'])], sort_keys=True)
    return 'close-pnl:' + hashlib.sha256(identity.encode()).hexdigest()


async def _save_pending_state(strategy, key, payload):
    db = getattr(strategy, 'db_manager', None)
    if db is None:
        return False
    try:
        result = await db.execute(
            'INSERT INTO public.strategy_states(strategy_name,state_key,state_data,updated_at) '
            'VALUES($1,$2,$3::jsonb,NOW()) ON CONFLICT(strategy_name,state_key) '
            'DO UPDATE SET state_data=EXCLUDED.state_data,updated_at=NOW()',
            strategy.instance_id, key, json.dumps(payload))
        if result != 'INSERT 0 1':
            raise ValueError('待结算状态未确认写入一行')
        return True
    except Exception as exc:
        logger.error('平仓盈亏待结算状态保存失败，不能宣称已持久化', error=str(exc), state_key=key)
        return False


async def record_or_defer_close(strategy, symbol, position, order, is_stop):
    """先存可重试身份再尝试实际记账；异常不阻塞减仓或保护。"""
    try:
        payload = _pending_payload(strategy, symbol, position, order, is_stop)
        key = _pending_key(payload)
        await _save_pending_state(strategy, key, payload)
        if not await record_close_fills(strategy.binance, order, is_stop):
            logger.warning('平仓实际盈亏待精确账本记录', symbol=symbol, order_id=order['orderId'])
            return False
        payload['status'] = 'done'
        await _save_pending_state(strategy, key, payload)
        return True
    except Exception as exc:
        logger.warning('平仓实际盈亏待完整成交证据', symbol=symbol, error=str(exc))
        return False


async def retry_pending_close_pnl(strategy):
    """每轮监控重试持久待结算单，空仓和进程重启后也执行。"""
    db = getattr(strategy, 'db_manager', None)
    if db is None:
        return 0
    completed = 0
    try:
        context = _context(strategy)
        cursor = getattr(strategy, '_pending_close_pnl_cursor', '')
        rows = await db.fetch_all(
            "SELECT state_key,state_data FROM public.strategy_states WHERE strategy_name=$1 "
            "AND state_key LIKE 'close-pnl:%' AND state_data->>'status' IN ('pending','awaiting_terminal') "
            "AND state_key > $2 ORDER BY state_key LIMIT 100", strategy.instance_id, cursor)
        for row in rows:
            # Advance before API I/O so a permanently unavailable receipt or timeout
            # cannot starve all later identities. Wrap once the key range is drained.
            strategy._pending_close_pnl_cursor = row['state_key']
            try:
                payload = row['state_data']
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if payload['context'] != context or row['state_key'] != _pending_key(payload):
                    continue
                prior = payload['order']
                direction = payload['entry']['direction']
                if (direction not in ('LONG', 'SHORT')
                        or prior['side'] != ('SELL' if direction == 'LONG' else 'BUY')):
                    continue
                order = await confirmed_close_order(strategy.binance, payload['symbol'], prior['side'],
                                                    prior['orderId'], _positive_decimal(prior['origQty']))
                if order is None:
                    continue
                awaiting = payload['status'] == 'awaiting_terminal'
                if not awaiting and (order['status'] != prior['status']
                        or Decimal(str(order['executedQty'])) != _positive_decimal(prior['executedQty'])):
                    continue
                zero_fill = awaiting and Decimal(str(order['executedQty'])) == 0
                if zero_fill or await record_close_fills(strategy.binance, order, payload['is_stop']):
                    payload = dict(payload, status='done')
                    if await _save_pending_state(strategy, row['state_key'], payload):
                        completed += 1
            except Exception as exc:
                logger.warning('平仓盈亏重试证据仍不完整', state_key=row['state_key'], error=str(exc))
        if len(rows) < 100:
            strategy._pending_close_pnl_cursor = ''
    except Exception as exc:
        logger.warning('读取待结算平仓订单失败，继续持仓保护', error=str(exc))
    return completed


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


def schedule_pending_close_pnl(strategy):
    """记账重试在独立有界任务运行，不能拖住当前持仓保护周期。"""
    running = getattr(strategy, '_pending_close_pnl_task', None)
    if running is not None and not running.done():
        return
    if getattr(strategy, 'db_manager', None) is None:
        return
    async def run():
        try:
            await asyncio.wait_for(retry_pending_close_pnl(strategy), timeout=10)
        except asyncio.TimeoutError:
            logger.warning('平仓盈亏重试超时，待下一周期继续')
    strategy._pending_close_pnl_task = asyncio.create_task(run())


def remember_close_order(strategy, symbol, position, order_id, quantity, reason):
    """Retain accepted ordinary close orders independently of their query outcome."""
    pending = getattr(position, 'pending_close_orders', None)
    if pending is None:
        pending = position.pending_close_orders = {}
    try:
        context = _context(strategy)
    except ValueError:
        # Missing accounting identity cannot stop an already accepted risk exit.
        # This untrusted intent cannot authorize a later software TP retry.
        context = None
    pending[str(_positive_id(order_id))] = {
        'context': context, 'symbol': symbol,
        'side': 'SELL' if position.direction == 'LONG' else 'BUY',
        'quantity': str(_positive_decimal(quantity)), 'reason': reason,
    }


async def reconcile_pending_close_orders(strategy, symbol, position):
    """Only an identity-matched terminal receipt permits another software TP order.

    Pending orders remain durable across restarts. This function never submits or
    cancels orders; a caller performing risk reduction may proceed after False.
    """
    pending = getattr(position, 'pending_close_orders', {})
    if not pending:
        return True
    async def reconcile():
        for order_id, intent in list(pending.items()):
            if (intent['context'] != _context(strategy) or intent['symbol'] != symbol
                    or intent['side'] != ('SELL' if position.direction == 'LONG' else 'BUY')
                    or not isinstance(intent['reason'], str) or not intent['reason']):
                return False
            order = await confirmed_close_order(strategy.binance, symbol, intent['side'],
                                                _positive_id(order_id), _positive_decimal(intent['quantity']))
            if order is None:
                return False
            executed = Decimal(str(order['executedQty']))
            if executed > 0:
                await strategy._write_confirmed_close_pnl(
                    symbol, position, intent['side'], intent['reason'], order, executed)
            # The next exchange position sync establishes current quantity; only
            # stage progress is restored here, avoiding a second local subtraction.
            reason = intent['reason']
            position.close_progress[reason] = position.close_progress.get(reason, Decimal('0')) + executed
            del pending[order_id]
            if not await save_close_progress(strategy, symbol, position):
                return False
        return True
    try:
        return await asyncio.wait_for(reconcile(), timeout=5)
    except Exception as exc:
        logger.warning('未终态减仓继续等待同单核验，暂停重复软件止盈', symbol=symbol, error=str(exc))
        return False


async def queue_unconfirmed_close(strategy, symbol, position, order_id, is_stop):
    """Keep unknown terminal outcomes retryable even after a risk exit removes the lot."""
    from shared.mtpcs_close_progress import IO_TIMEOUT_SECONDS
    intent = position.pending_close_orders[str(order_id)]
    if intent['context'] is None:
        return False
    payload = {
        'status': 'awaiting_terminal', 'context': intent['context'], 'symbol': symbol,
        'entry': {'direction': position.direction,
                  'entry_order_id': getattr(position, 'entry_order_id', None),
                  'tp2_order_id': getattr(position, 'tp2_order_id', None),
                  'initial_quantity': str(position.initial_quantity)},
        'order': {'symbol': symbol, 'side': intent['side'], 'orderId': _positive_id(order_id),
                  'origQty': intent['quantity']}, 'is_stop': bool(is_stop),
    }
    try:
        return await asyncio.wait_for(_save_pending_state(strategy, _pending_key(payload), payload),
                                      timeout=IO_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning('未终态减仓身份存盘超时，继续风险退出', symbol=symbol, order_id=order_id)
        return False
