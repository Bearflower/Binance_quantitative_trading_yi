"""Entry-only remediation policy. Never changes holdings allocation or closes positions.

Missing/expired policy fails closed. Reservations have no TTL: ambiguous exchange
outcomes retain their entire original margin until terminal order AND zero position
are proven. This deliberately overcounts partial/complete fills alongside holdings.
"""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import hashlib
import json
import os


class EntryDenied(RuntimeError):
    """New risk could not be proven safe; monitoring must continue."""


def number(value, *, zero=False):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or (result < 0 if zero else result <= 0):
            raise ValueError()
        return result
    except Exception as exc:
        raise EntryDenied('invalid finite risk input') from exc


@dataclass(frozen=True)
class SizedEntry:
    original_quantity: Decimal
    quantity: Decimal
    price: Decimal
    leverage: Decimal
    margin: Decimal
    risk: Decimal
    original_risk: Decimal
    exit_ratios: tuple[Decimal, Decimal] = (Decimal('0.3'), Decimal('0.3'))


def executable_exit_ratios(quantity, step, minimum, notional, prices, requested):
    """Keep a risk-reduced lot tradable with one TP and a viable remainder.

    Never round the entry or an exit leg up. Below two exchange lots, deny entry.
    """
    ratios = tuple(number(r, zero=True) for r in requested)
    if len(ratios) != 2 or sum(ratios) >= 1 or sum(ratios) <= 0:
        raise EntryDenied('invalid take profit ratios')
    def viable(q, price):
        return q >= minimum and q * price >= notional
    def floor(q):
        return (q / step).to_integral_value(rounding=ROUND_DOWN) * step
    legs = (floor(quantity * ratios[0]), floor(quantity * ratios[1]))
    if all(viable(q, p) for q, p in zip((*legs, quantity-sum(legs)), prices)):
        return ratios
    # A two-leg exit uses the first target and leaves one exchange-valid lot
    # for the trailing/stop path. The hard stop still covers the whole entry.
    first = floor(quantity / 2)
    if viable(first, prices[0]) and viable(quantity-first, prices[2]):
        ratio = first / quantity
        # Decimal division may round a recurring fraction just below the
        # executable lot (e.g. 0.01 / 0.03). Raise the representation by one
        # ULP until the downstream floor exactly reproduces the chosen lot.
        for _ in range(3):
            if floor(quantity * ratio) == first:
                return (ratio, Decimal(0))
            ratio = ratio.next_plus()
    raise EntryDenied('take profit or remainder infeasible without rounding up')


def size_reduced_entry(signal, filters, multiplier, stop_offset, tp_ratios, *, tp_offset=None):
    """Use the exact exchange tick-rounded limit and adverse stop-limit price.

    Fees (10 bps round trip) and slippage (20 bps) are conservative pressure
    allowances, not a guarantee of maximum loss through gaps or stop-limit failure.
    """
    q0 = number(signal['quantity'])
    step, tick = number(filters['stepSize']), number(filters['tickSize'])
    minimum, notional = number(filters['minQty']), number(filters['minNotional'])
    price = (number(signal['entry_price']) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
    stop, leverage = number(signal['initial_stop_loss']), number(signal['leverage'])
    if leverage != leverage.to_integral_value():
        raise EntryDenied('fractional leverage')
    mult, offset = number(multiplier), number(stop_offset, zero=True)
    if mult > Decimal('.5') or offset >= 1:
        raise EntryDenied('reduced multiplier must be at most 0.5')
    direction = signal['direction']
    if direction == 'LONG' and stop < price:
        adverse = stop * (1-offset)
        distance = price-adverse
    elif direction == 'SHORT' and stop > price:
        adverse = stop * (1+offset)
        distance = adverse-price
    else:
        raise EntryDenied('stop is on wrong side of exact entry')
    adverse = (adverse/tick).quantize(Decimal(1), rounding=ROUND_HALF_UP)*tick
    distance = abs(price-adverse)
    per_unit = distance + (price+adverse)*Decimal('.0005') + price*Decimal('.002')
    quantity = (q0*mult/step).to_integral_value(rounding=ROUND_DOWN)*step
    if quantity < minimum or quantity*price < notional:
        raise EntryDenied('reduced quantity below exchange minimum')
    leg_prices = [min(price, stop)] * 2
    if tp_offset is not None:
        offset_tp = number(tp_offset, zero=True)
        if offset_tp >= 1:
            raise EntryDenied('invalid TP offset')
        leg_prices = []
        for level in (1, 2):
            target = number(signal[f'tp{level}_price'])
            if (direction == 'LONG' and target <= price) or (direction == 'SHORT' and target >= price):
                raise EntryDenied('TP on wrong side of entry')
            limit = target*(1-offset_tp if direction=='LONG' else 1+offset_tp)
            leg_prices.append((limit/tick).quantize(Decimal(1),rounding=ROUND_HALF_UP)*tick)
    ratios = executable_exit_ratios(quantity,step,minimum,notional,
                                    leg_prices+[adverse],tp_ratios)
    return SizedEntry(q0,quantity,price,leverage,quantity*price/leverage,
                      quantity*per_unit,q0*per_unit,ratios)


def account_id():
    return os.environ.get('TRADE_ACCOUNT_ID', 'default')


class RiskPolicyStore:
    def __init__(self, db, account=None):
        self.pool = getattr(db, 'pool', None)
        self.account = account or account_id()
        if self.pool is None:
            raise EntryDenied('risk policy database unavailable')

    async def policy(self, strategy, connection=None):
        async def read(conn):
            row = await conn.fetchrow('''SELECT p.* FROM trading.strategy_risk_policy p
                JOIN trading.strategy_risk_active a USING(account_id,strategy_id,revision)
                WHERE p.account_id=$1 AND p.strategy_id=$2
                AND p.effective_at <= clock_timestamp() AND p.expires_at > clock_timestamp()''', self.account,strategy)
            if not row:
                raise EntryDenied('missing or expired risk policy')
            return dict(row)
        if connection is not None:
            return await read(connection)
        async with self.pool.acquire() as conn:
            return await read(conn)

    async def lock(self, conn, strategy):
        # Account lock serializes strategy reservations and activation; hash is stable in PG.
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1, 7183))',self.account)

    async def reserve(self, strategy, symbol, identity, sized, revision, snapshot, *, geometry=None):
        """Snapshot precedes lock; retained reservations cover concurrent submitted fills."""
        client_id = 'risk_' + hashlib.sha256(f'{self.account}:{strategy}:{identity}'.encode()).hexdigest()[:30]
        # No pool connection is held while snapshot helpers perform DB/exchange reads.
        # Full retained reservations account for all intervening authorized fills.
        snapshot_values = await snapshot()
        async with self.pool.acquire() as conn, conn.transaction():
            await self.lock(conn,strategy)
            policy = await self.policy(strategy,conn)
            if policy['revision'] != revision or policy['mode'] != 'reduced' or policy['allow_add']:
                raise EntryDenied('policy changed or does not permit reduced entry')
            old = await conn.fetchrow('SELECT * FROM trading.entry_risk_reservation WHERE client_order_id=$1',client_id)
            if old:
                # Never resubmit an uncertain intent, even with a deterministic exchange id.
                raise EntryDenied('intent already reserved; reconcile instead of resubmitting')
            if await conn.fetchval("SELECT 1 FROM trading.entry_risk_reservation WHERE account_id=$1 AND symbol=$2 AND state<>'released' LIMIT 1",self.account,symbol):
                raise EntryDenied('symbol already has an unresolved entry intent')
            holdings,equity,allocation = snapshot_values[:3]
            # Re-read under the policy lock so a smaller allocation cannot be missed.
            allocation = min(number(allocation), await current_allocation(conn,strategy))
            if len(snapshot_values) == 5:
                owned_symbols,max_slots = snapshot_values[3:]
                pending_symbols = await conn.fetch("SELECT DISTINCT symbol FROM trading.entry_risk_reservation WHERE account_id=$1 AND strategy_id=$2 AND state<>'released'",self.account,strategy)
                occupied_symbols = set(owned_symbols) | {r['symbol'] for r in pending_symbols}
                if len(occupied_symbols | {symbol}) > max_slots:
                    raise EntryDenied('concurrent position slots include pending reservations')
            cap = min(number(allocation),number(equity)*number(policy['ratio_cap']),number(policy['entry_ceiling']))
            used = await conn.fetchval('''SELECT COALESCE(sum(margin),0) FROM trading.entry_risk_reservation
                WHERE account_id=$1 AND strategy_id=$2 AND state <> 'released' ''',self.account,strategy)
            if number(holdings,zero=True)+used+sized.margin > cap:
                raise EntryDenied('entry ceiling includes holdings and all durable reservations')
            # Existing symbol claim must already be acquired by the ordinary ownership protocol.
            claim = await conn.fetchrow('''UPDATE trading.position_claims SET expires_at='infinity'
                WHERE symbol=$1 AND strategy=$2 AND claim_state IN ('PENDING','ACTIVE') RETURNING id''',symbol,strategy)
            if not claim:
                raise EntryDenied('entry has no exclusive symbol claim')
            await conn.execute('''INSERT INTO trading.entry_risk_reservation
                (client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state,geometry,pressure_risk,original_pressure_risk)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'reserved',$11::jsonb,$12,$13)''',client_id,self.account,strategy,revision,symbol,
                sized.quantity,sized.original_quantity,sized.price,sized.leverage,sized.margin,json.dumps(geometry or {},sort_keys=True),sized.risk,sized.original_risk)
        return client_id

    @asynccontextmanager
    async def submitting(self, strategy, client_id):
        # Persist submitting BEFORE network write; crash/timeout retains occupancy.
        async with self.pool.acquire() as conn, conn.transaction():
            await self.lock(conn,strategy)
            policy = await self.policy(strategy,conn)
            row = await conn.fetchrow('''UPDATE trading.entry_risk_reservation SET state='submitting'
                WHERE client_order_id=$1 AND account_id=$2 AND strategy_id=$3
                AND revision=$4 AND state='reserved' RETURNING *''',client_id,self.account,strategy,policy['revision'])
            if not row or policy['mode'] != 'reduced':
                raise EntryDenied('submission is not authorized')
        # Final authorize with activation lock held through exchange submit.
        async with self.pool.acquire() as conn, conn.transaction():
            await self.lock(conn,strategy)
            policy = await self.policy(strategy,conn)
            if policy['revision'] != row['revision'] or policy['mode'] != 'reduced':
                raise EntryDenied('policy changed immediately before submission')
            yield dict(row)

    async def reconcile(self, strategy, exchange):
        """Read only exchange reconciliation; no cancels, closes, or TTL releases."""
        async with self.pool.acquire() as conn, conn.transaction():
            await self.lock(conn,strategy)
            rows = await conn.fetch('''SELECT * FROM trading.entry_risk_reservation
                WHERE account_id=$1 AND strategy_id=$2 AND state <> 'released' ''',self.account,strategy)
            for row in rows:
                try:
                    order = await exchange.get_order_by_client_id(row['symbol'],row['client_order_id'])
                    positions = await exchange.get_position(row['symbol'])
                    if not isinstance(order,dict) or not isinstance(positions,list):
                        continue
                    if order.get('symbol') != row['symbol'] or order.get('clientOrderId') != row['client_order_id']:
                        continue
                    terminal = order.get('status') in ('FILLED','CANCELED','EXPIRED','REJECTED','EXPIRED_IN_MATCH')
                    if any(p.get('symbol') != row['symbol'] for p in positions):
                        continue
                    matching = [p for p in positions if p.get('symbol') == row['symbol']]
                    zero = bool(matching) and all(number(abs(Decimal(str(p['positionAmt']))),zero=True)==0 for p in matching)
                    if terminal and (positions == [] or zero):
                        # PM positionRisk can omit a flat symbol. An empty filtered
                        # response alone is not proof; require the independent UM
                        # account inventory and unfiltered risk inventory to agree,
                        # with no orders left that could create/reduce a position.
                        account_positions = await exchange.get_um_account_positions()
                        all_positions = await exchange.get_position()
                        ordinary = await exchange.get_open_orders(row['symbol'])
                        algos = await exchange.get_open_algo_orders(row['symbol'])
                        zero = False
                        if all(isinstance(value,list) for value in
                               (account_positions,all_positions,ordinary,algos)):
                            flat_inventories = all(
                                not any(p.get('symbol') == row['symbol'] and
                                        number(abs(Decimal(str(p['positionAmt']))),zero=True) != 0
                                        for p in inventory)
                                for inventory in (account_positions,all_positions)
                            )
                            # Both calls are symbol-scoped. Even an order lacking a
                            # symbol field must block release rather than be ignored.
                            no_orders = not ordinary and not algos
                            zero = flat_inventories and no_orders
                    executed = number(order['executedQty'],zero=True)
                    # Filled orders only release after position is proven flat. Until then
                    # reserve full margin plus actual holdings (conservative double count).
                    if terminal and zero:
                        await conn.execute("UPDATE trading.entry_risk_reservation SET state='released', updated_at=now() WHERE client_order_id=$1",row['client_order_id'])
                        await conn.execute("UPDATE trading.position_claims SET claim_state='RELEASED',released_at=now(),reason='risk_reconciled_flat' WHERE symbol=$1 AND strategy=$2 AND claim_state IN ('PENDING','ACTIVE')",row['symbol'],strategy)
                except Exception:
                    continue  # Unknown order/transport/cancel result stays occupied indefinitely.
            known = {r['client_order_id'] for r in rows}
            orders = await exchange.get_open_orders()
            if not isinstance(orders,list):
                raise EntryDenied('cannot prove absence of legacy pending orders')
            for order in orders:
                if str(order.get('reduceOnly','false')).lower() == 'true':
                    continue
                if order.get('clientOrderId') not in known:
                    raise EntryDenied('unreconciled legacy pending entry blocks reduced mode')


async def allocation_frozen(db, account=None):
    """Fail closed until a separate verified allocation-publication CAS is implemented.

    Remediation never modifies allocated_amount: that field can trigger liquidation
    in existing CapitalManager enforcement. This gate also freezes missing policies.
    """
    return True


async def entry_allowed(strategy, strategy_id, *, adding=False):
    try:
        store = RiskPolicyStore(getattr(strategy,'db_manager',None) or getattr(strategy,'db',None))
        policy = await store.policy(strategy_id)
        return policy['mode'] == 'reduced' and strategy_id == 'btc_eth_aggressive' and not adding
    except Exception:
        return False


async def current_allocation(connection, strategy):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    month=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')
    row=await connection.fetchrow('''SELECT entries FROM public.capital_allocation
        WHERE left(month::text,7)=$1 AND status='active' ORDER BY created_at DESC LIMIT 1''',month)
    if not row:
        raise EntryDenied('no current active monthly allocation')
    entries=row['entries']
    if isinstance(entries,str): entries=json.loads(entries)
    matches=[e for e in entries if e.get('strategy_id')==strategy]
    if len(matches)!=1:
        raise EntryDenied('missing or duplicate strategy allocation')
    return number(matches[0]['allocated_amount'])


async def prepare_aggressive_entry(strategy, symbol, signal):
    """Reserve once, before leverage, using exchange truth and active allocation.

    Repeated invocations with a prepared intent retain q0 and the exact quantity;
    the submitting state machine prevents any repeated exchange write.
    """
    try:
        if symbol in strategy.positions:
            raise EntryDenied('remediation forbids additions')
        store=RiskPolicyStore(strategy.db_manager)
        policy=await store.policy('btc_eth_aggressive')
        if policy['mode']!='reduced' or policy['allow_add']:
            raise EntryDenied('reduced entry unavailable')
        if signal.get('_risk_client_id'):
            async with store.pool.acquire() as conn:
                row=await conn.fetchrow('SELECT * FROM trading.entry_risk_reservation WHERE client_order_id=$1',signal['_risk_client_id'])
                if not row or row['state']!='reserved' or row['revision']!=policy['revision'] or row['symbol']!=symbol:
                    raise EntryDenied('intent already submitted or changed')
                geometry = row['geometry']
                if isinstance(geometry, str): geometry = json.loads(geometry)
                signal['_risk_exit_ratios'] = geometry['exit_ratios']
                signal['quantity'],signal['entry_price']=row['quantity'],row['price']
                return True
        await store.reconcile('btc_eth_aggressive',strategy.binance)
        filters=await strategy._get_symbol_precision(symbol)
        partial=strategy._get_grade_risk(signal.get('grade','A'))['partial_take_profit']
        sized=size_reduced_entry(signal,filters,policy['multiplier'],
            strategy.risk_config.get('stop_limit_order',{}).get('offset_pct',.002),
            [partial['tp1_close_ratio'],partial['tp2_close_ratio']],
            tp_offset=strategy.risk_config.get('tp_limit_order',{}).get('offset_pct',.0015))
        signal['_risk_exit_ratios'] = [str(value) for value in sized.exit_ratios]
        # Identity must come from the original signal, never generated on each retry.
        identity=signal.get('signal_id') or signal.get('timestamp')
        if identity is None:
            raise EntryDenied('signal lacks stable identity')
        identity=f"{symbol}:{signal['direction']}:{identity}"
        async def snapshot():
            # Already established production recovery/ownership verifier; unknown => deny.
            holdings=await strategy._owned_exchange_margin_for_allocation()
            equity=await strategy._get_account_equity(symbol)
            async with store.pool.acquire() as conn:
                allocation=await current_allocation(conn,'btc_eth_aggressive')
            owned_symbols=set()
            positions=await strategy.binance.get_position()
            if not isinstance(positions,list):
                raise EntryDenied('cannot verify position slots')
            for position in positions:
                amount=Decimal(str(position['positionAmt']))
                number(abs(amount),zero=True)
                if amount == 0: continue
                sym=position['symbol']
                owned=await strategy._owns_exchange_position(sym,amount)
                if owned is None: raise EntryDenied('unverified slot owner')
                if owned: owned_symbols.add(sym)
            owned_symbols.update(sym for sym,pos in strategy.positions.items() if number(pos.current_quantity,zero=True)>0)
            max_slots=int(strategy.risk_config.get('position_management',{}).get('max_concurrent_positions',2))
            if max_slots<=0:raise EntryDenied('invalid position slot limit')
            return number(holdings,zero=True),number(equity),allocation,owned_symbols,max_slots
        geometry = entry_geometry(signal, filters, strategy.risk_config, partial)
        client_id=await store.reserve('btc_eth_aggressive',symbol,identity,sized,policy['revision'],snapshot,geometry=geometry)
        signal['_risk_client_id']=client_id
        signal['_risk_original_quantity']=sized.original_quantity
        signal['_risk_stop']=str(signal['initial_stop_loss'])
        signal['quantity'],signal['entry_price']=sized.quantity,sized.price
        return True
    except Exception as exc:
        signal['skip_reason']=f'entry risk policy: {exc}'
        return False


async def submit_aggressive_entry(strategy,symbol,signal):
    """Only new entry orders pass here. All protective/reduce-only paths stay intact."""
    if getattr(getattr(strategy, 'frequency_controller', None), 'state_ready', False) is not True:
        raise EntryDenied('frequency state unavailable')
    store=RiskPolicyStore(strategy.db_manager)
    client_id=signal.get('_risk_client_id')
    if not client_id or signal.get('_risk_stop')!=str(signal.get('initial_stop_loss')):
        raise EntryDenied('entry has no immutable risk reservation')
    # Resolve any slow exchange/filter lookup before the final policy lock/check.
    filters=await strategy._get_symbol_precision(symbol)
    geometry=entry_geometry(signal,filters,strategy.risk_config,strategy._get_grade_risk(signal.get('grade','A'))['partial_take_profit'])
    async with store.submitting('btc_eth_aggressive',client_id) as reservation:
        saved_geometry=reservation['geometry']
        if isinstance(saved_geometry,str):saved_geometry=json.loads(saved_geometry)
        if saved_geometry != geometry:
            raise EntryDenied('reserved stop/TP geometry or filters changed')
        if reservation['symbol']!=symbol:
            raise EntryDenied('reservation symbol mismatch')
        if getattr(getattr(strategy, 'frequency_controller', None), 'state_ready', False) is not True:
            raise EntryDenied('frequency state unavailable')
        return await strategy.binance.place_order(symbol=symbol,
            side='BUY' if signal['direction']=='LONG' else 'SELL',
            quantity=reservation['quantity'],price=reservation['price'],order_type='LIMIT',client_order_id=client_id)


def entry_geometry(signal,filters,risk_config,partial):
    return {"signal": {key:str(signal[key]) for key in ('direction','initial_stop_loss','tp1_price','tp2_price','leverage','grade')},
            "partial_take_profit": {key:str(partial[key]) for key in ("tp1_close_ratio","tp2_close_ratio")},
            "exit_ratios": [str(value) for value in signal.get('_risk_exit_ratios',
                (partial['tp1_close_ratio'],partial['tp2_close_ratio']))],
            "filters": {key:str(filters[key]) for key in ('stepSize','tickSize','minQty','minNotional')},
            "stop_offset": str(risk_config.get('stop_limit_order',{}).get('offset_pct',.002)),
            "tp_offset": str(risk_config.get('tp_limit_order',{}).get('offset_pct',.0015))}


async def activate_policies(pool,candidates,*,expected_revision,valid_hours):
    """Administrative CAS; all policies and audit rows commit together.

    Runtime role intentionally has no INSERT/UPDATE policy privileges. Caller uses
    migration/admin credentials and explicit expected revision; never auto-retries.
    """
    if not candidates or valid_hours<=0 or valid_hours>24*31:
        raise EntryDenied('invalid activation validity')
    accounts={c['account_id'] for c in candidates}
    if len(accounts)!=1 or len({c['strategy_id'] for c in candidates})!=len(candidates):
        raise EntryDenied('activation must contain unique strategies from one account')
    account=next(iter(accounts))
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,7183))',account)
        for candidate in candidates:
            strategy=candidate['strategy_id']
            previous=await conn.fetchval('SELECT revision FROM trading.strategy_risk_active WHERE account_id=$1 AND strategy_id=$2',account,strategy)
            if (previous or 0)!=expected_revision:
                raise EntryDenied('active policy revision changed; review fresh candidate')
            if not candidate['reason'].strip() or candidate['allow_add']:
                raise EntryDenied('remediation requires reason and forbids additions')
            if 'allocation_amount' in candidate:
                latest=await current_allocation(conn,strategy)
                if latest!=number(candidate['allocation_amount']):
                    raise EntryDenied('allocation changed since candidate was reviewed')
            revision=expected_revision+1
            await conn.execute('''INSERT INTO trading.strategy_risk_policy
                (account_id,strategy_id,revision,effective_at,expires_at,mode,allow_add,multiplier,ratio_cap,entry_ceiling,reason)
                VALUES($1,$2,$3,now(),now()+make_interval(hours=>$4),$5,$6,$7,$8,$9,$10)''',account,strategy,revision,valid_hours,
                candidate['mode'],False,number(candidate['multiplier'],zero=True),number(candidate['ratio_cap'],zero=True),
                number(candidate['entry_ceiling'],zero=True),candidate['reason'])
            await conn.execute('''INSERT INTO trading.strategy_risk_active VALUES($1,$2,$3)
                ON CONFLICT(account_id,strategy_id) DO UPDATE SET revision=EXCLUDED.revision''',account,strategy,revision)
            await conn.execute('''INSERT INTO trading.strategy_risk_audit(account_id,strategy_id,previous_revision,revision,reason)
                VALUES($1,$2,$3,$4,$5)''',account,strategy,expected_revision,revision,candidate['reason'])
    return expected_revision+1


async def activate_policy(pool,candidate,*,expected_revision,valid_hours):
    return await activate_policies(pool,[candidate],expected_revision=expected_revision,valid_hours=valid_hours)


async def recover_reserved_protection(strategy):
    """Protect proved reserved fills using the audited stop, even without TradeLogger.

    Never releases a reservation, cancels protection, or guesses ownership. Polling
    later fills covers only missing quantity. Entry policy expiry cannot block this.
    """
    import structlog
    log=structlog.get_logger()
    try:
        store=RiskPolicyStore(strategy.db_manager)
        async with store.pool.acquire() as conn:
            rows=await conn.fetch("SELECT * FROM trading.entry_risk_reservation WHERE account_id=$1 AND strategy_id=$2 AND state<>'released'",store.account,'btc_eth_aggressive')
        for row in rows:
            try:
                symbol=row['symbol']
                order=await strategy.binance.get_order_by_client_id(symbol,row['client_order_id'])
                if not isinstance(order,dict) or order.get('symbol')!=symbol or order.get('clientOrderId')!=row['client_order_id']:
                    continue
                filled=number(order['executedQty'],zero=True)
                if filled==0:continue
                geometry=row['geometry']
                if isinstance(geometry,str):geometry=json.loads(geometry)
                direction=geometry['signal']['direction']
                if order.get('side')!=('BUY' if direction=='LONG' else 'SELL'):continue
                positions=await strategy.binance.get_position(symbol)
                if not isinstance(positions,list):continue
                matching=[p for p in positions if p.get('symbol')==symbol]
                if len(matching)!=1:continue
                amount=Decimal(str(matching[0]['positionAmt']))
                qty=number(abs(amount),zero=True)
                if qty==0 or qty>filled or filled>row['quantity']:continue
                if (amount>0)!=(direction=='LONG'):continue
                side='SELL' if direction=='LONG' else 'BUY'
                orders=await strategy.binance.get_open_algo_orders(symbol)
                if not isinstance(orders,list):continue
                covered=number(strategy._hard_stop_covered_qty(orders,symbol,side),zero=True)
                missing=qty-covered
                if missing<=0:continue
                stop=number(geometry['signal']['initial_stop_loss'])
                offset=number(geometry['stop_offset'],zero=True)
                tick=number(geometry['filters']['tickSize'])
                limit=stop*(1-offset if direction=='LONG' else 1+offset)
                stop=(stop/tick).quantize(Decimal(1),rounding=ROUND_HALF_UP)*tick
                limit=(limit/tick).quantize(Decimal(1),rounding=ROUND_HALF_UP)*tick
                oid=await strategy._place_conditional_order_and_record(symbol,side,'STOP',stop,limit,missing,'STOP_LOSS',strategy.strategy_name)
                if not oid:raise EntryDenied('reserved fill hard-stop placement failed')
            except Exception as exc:
                log.error('reserved fill protection requires retry',symbol=row['symbol'],error=str(exc))
    except Exception as exc:
        log.warning('reserved fill recovery unavailable; normal protection continues',error=str(exc))
