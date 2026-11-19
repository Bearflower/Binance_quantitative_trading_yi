"""Entry-only remediation policy. Never changes holdings allocation or closes positions.

Missing/expired policy fails closed. Reservations have no TTL: ambiguous exchange
outcomes retain their entire original margin until terminal order AND zero position
are proven. This deliberately overcounts partial/complete fills alongside holdings.
"""
from contextlib import asynccontextmanager
import asyncio
import copy
from dataclasses import asdict, dataclass, replace
from datetime import timedelta, datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
import hashlib
import json
import os


class EntryDenied(RuntimeError):
    """New risk could not be proven safe; monitoring must continue."""
    def __init__(self,message,*,reason_code=None,context=None):
        super().__init__(message)
        self.reason_code=reason_code or (message if isinstance(message,str) and message.replace('_','').isalnum() and ' ' not in message else 'entry_denied')
        self.context=dict(context or {})


@dataclass(frozen=True)
class ScopedRiskSnapshot:
    """Opt-in HRS slots; account_margin still includes every account holding."""
    account_margin: Decimal
    equity: Decimal
    allocation: Decimal
    max_slots: int
    slot_scope: str
    slot_evidence: dict
    financial_receipt: dict | None = None


async def _finish_shielded(operation):
    """Finish one held-connection transaction before propagating cancellation."""
    task = asyncio.create_task(operation)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
    task.result()
    return cancelled


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

    Never round the entry or an exit leg up. A single viable lot exits at TP1.
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
    # The smallest executable entry has no divisible remainder. Close the
    # entire lot at TP1; the hard STOP still protects the entire lot.
    if viable(quantity, prices[0]):
        return (Decimal(1), Decimal(0))
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
        # A small original signal cannot be halved into an exchange-valid
        # order. Keep its minimum executable lot only when it does not exceed
        # the original signal; the durable margin ceiling still applies.
        required = max(minimum, (notional/price/step).to_integral_value(rounding=ROUND_UP)*step)
        if required > q0:
            raise EntryDenied('reduced quantity below exchange minimum')
        quantity = required
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


def _hrs_budget_json(value):
    """Stable JSON contract shared by reservation evidence and final authorization."""
    from dataclasses import is_dataclass
    # Decisions also occur inside locked context dictionaries. JSON invokes this
    # callback recursively; Decimal/datetime keep their existing string contract.
    def normalize(item):
        return asdict(item) if is_dataclass(item) else str(item)
    return json.loads(json.dumps(value, default=normalize, sort_keys=True))


def _validate_hrs_floor_decision(decision, inputs):
    from strategies.hrs.risk_budget import FLOOR, assess_budget_inputs
    try:
        expected = assess_budget_inputs(inputs)
        captured = _hrs_budget_json(decision)
        # Context is bound separately to the policy and durable reservation.
        fields = set(asdict(expected)) - {'policy_revision', 'client_order_id', 'effective_allocation'}
        normalized = _hrs_budget_json(expected)
        if (not expected.allowed or expected.budget_version != FLOOR
                or not isinstance(captured, dict) or set(captured) != set(normalized)
                or captured.get('allowed') is not True
                or any(captured.get(key) != normalized[key] for key in fields)):
            raise ValueError('decision does not reproduce assessor inputs')
        return expected
    except (ValueError, TypeError, AttributeError, ArithmeticError) as exc:
        raise EntryDenied('invalid HRS minimum-order budget evidence', reason_code='hrs_budget_evidence_invalid') from exc


def _bind_hrs_floor_local_to_parent(parent, local, account, digest):
    """Bind only loss-relevant local fields to the immutable authorized parent."""
    from strategies.hrs.equity_evidence import canonical
    try:
        payload = json.loads(parent) if isinstance(parent,str) else parent
        if canonical(payload)[1] != digest or payload['account_id'] != account:
            raise ValueError('immutable parent hash or account mismatch')
        raw_local = payload['capture']['local']
        if not isinstance(local,dict) or not isinstance(raw_local,dict) or set(local) != set(raw_local):
            raise ValueError('parent local inventory mismatch')
        def identity(value):
            if isinstance(value,bool) or not isinstance(value,(str,int)) or not str(value).strip():
                raise ValueError('invalid protection identity')
            value=str(value).strip()
            return str(int(value)) if value.isdecimal() else value
        def fields(row):
            return (number(row['remaining_quantity']), number(row['entry_price']),
                    number(row['risk_stop_price']), row['direction'],
                    identity(row['position_id']), identity(row['algo_ids']['sl']))
        if any(fields(local[symbol]) != fields(raw_local[symbol]) for symbol in local):
            raise ValueError('risk fields differ from immutable parent')
        return payload
    except (KeyError,ValueError,TypeError,AttributeError,ArithmeticError,EntryDenied) as exc:
        raise EntryDenied('HRS local risk differs from immutable parent', reason_code='hrs_budget_parent_risk_mismatch') from exc


def _hrs_floor_parent_margin(payload):
    """All-account margin from an already hash-verified immutable inventory."""
    try:
        result=Decimal(0)
        for position in payload['capture']['inventory']:
            quantity=number(abs(Decimal(str(position['positionAmt']))),zero=True)
            if quantity:
                result+=quantity*number(position['markPrice'])/number(position['leverage'])
        return result
    except (KeyError,ValueError,TypeError,AttributeError,ArithmeticError,EntryDenied) as exc:
        raise EntryDenied('HRS immutable parent margin unavailable',reason_code='hrs_budget_parent_margin_invalid') from exc


def _validate_hrs_financial_receipt(receipt, row, *, account, symbol, signal_id, checked_at):
    """Verify an actual independently written receipt before using its finances."""
    from strategies.hrs.decision_capture import canonical_payload
    from strategies.hrs.entry_occupancy import SLOT_SCOPE
    try:
        if not isinstance(receipt,dict) or set(receipt) != {
                'evidence_id','run_id','payload_sha256','event_time','available_at'} or not row:
            raise ValueError('financial receipt missing')
        if any(not isinstance(receipt[k],str) or not receipt[k] for k in receipt):
            raise ValueError('financial receipt identity missing')
        for key in ('evidence_id','run_id','payload_sha256'):
            if row[key] != receipt[key]: raise ValueError('financial receipt identity mismatch')
        event,available=(datetime.fromisoformat(receipt[k]) for k in ('event_time','available_at'))
        payload=json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
        observed=datetime.fromisoformat(payload['slot_inputs']['observed_at'])
        if (row['kind'] != 'entry_stage_inputs' or row['symbol'] != symbol
                or row['event_time'] != event or row['available_at'] != available
                or event.tzinfo is None or available.tzinfo is None or observed.tzinfo is None
                or not observed <= event <= available <= checked_at
                or checked_at-observed > timedelta(seconds=120)
                or not available <= row['recorded_at'] <= checked_at
                or canonical_payload(payload)[1] != receipt['payload_sha256']
                or payload['stage'] != 'reservation_inventory'
                or payload['account_id'] != account or payload['signal_id'] != str(signal_id)
                or payload['slot_scope'] != SLOT_SCOPE
                or datetime.fromisoformat(payload['observed_at']) != event
                or canonical_payload(payload['exchange_positions'])[1] != canonical_payload(payload['slot_inputs']['inventory'])[1]):
            raise ValueError('financial receipt source mismatch')
        number(payload['account_equity']);number(payload['effective_allocation'])
        return payload
    except (KeyError,ValueError,TypeError,AttributeError,ArithmeticError,EntryDenied) as exc:
        raise EntryDenied('HRS financial receipt unavailable or changed',reason_code='hrs_budget_financial_receipt_invalid') from exc


def _hrs_floor_receipt_margin(payload):
    # Receipt inventory has already passed the two-source ownership proof.
    return _hrs_floor_parent_margin(dict(capture=dict(inventory=payload['slot_inputs']['inventory'])))


def _validate_hrs_floor_finance(context, slot_context, financial, pending, allocation, policy, expected):
    """Bind sizing to independent captured finances and current durable ceilings."""
    try:
        margin=_hrs_floor_receipt_margin(financial)
        equity=number(financial['account_equity'])
        effective=number(financial['effective_allocation'])
        reserved_margin=sum((number(row['margin']) for row in pending),Decimal(0))
        cap=min(allocation*number(policy['multiplier']),number(policy['entry_ceiling']),
                effective,equity*number(policy['ratio_cap']))
        remaining=cap-margin-reserved_margin
        if (number(context['full_allocation']) != allocation
                or expected.budget_inputs['allocation'] != allocation
                or number(context['snapshot_account_equity']) != equity
                or number(context['snapshot_effective_allocation']) != effective
                or number(context['account_margin'],zero=True) != margin
                or number(slot_context['account_margin'],zero=True) != margin
                or number(context['reserved_margin'],zero=True) != reserved_margin
                or number(context['margin_cap']) != cap
                or number(context['remaining_margin']) != remaining
                or expected.quantity*expected.budget_inputs['entry_price']/expected.budget_inputs['leverage'] > remaining):
            raise ValueError('captured sizing exceeds actual finance sources')
        return cap,remaining
    except (KeyError,ValueError,TypeError,AttributeError,ArithmeticError,EntryDenied) as exc:
        raise EntryDenied('HRS allocation or physical margin sources changed',reason_code='hrs_budget_finance_source_mismatch') from exc


def _refine_hrs_floor(sized, geometry, values, pending, allocation, remaining, context):
    from strategies.hrs.risk_budget import calculate_open_position_risk, assess_budget_inputs
    initial = _validate_hrs_floor_decision(geometry.get('hrs_budget_decision'), geometry.get('hrs_budget_inputs'))
    inputs = initial.budget_inputs
    if (geometry.get('hrs_budget_source') != 'hrs_live_assessor_v1'
            or sized.quantity != initial.quantity or sized.original_quantity != initial.quantity
            or sized.risk != initial.worst_loss or sized.original_risk != initial.worst_loss
            or sized.price != inputs['entry_price'] or sized.leverage != inputs['leverage']
            or sized.margin != sized.quantity*sized.price/sized.leverage
            or sized.exit_ratios != initial.exit_ratios
            or inputs['fee_rate'] < Decimal('.0005')
            or inputs['pressure_slippage'] < Decimal('.02')):
        raise EntryDenied('HRS planned order differs from budget evidence', reason_code='hrs_budget_plan_mismatch')
    local = values.slot_evidence['local']
    by_symbol = {}
    for symbol, position in local.items():
        risk = calculate_open_position_risk({symbol: position}, fee_rate=inputs['fee_rate'], pressure_slippage=inputs['pressure_slippage'])
        if risk is None:
            raise EntryDenied('HRS local protection risk unavailable', reason_code='hrs_open_risk_unknown')
        by_symbol[symbol] = risk
    local_risk = dict(by_symbol)
    # A retained intent may already be represented by a live protected position.
    # Keep the larger exposure for that symbol, including every pending symbol.
    reserved_risk = {}
    for row in pending:
        risk = number(row['pressure_risk'])
        reserved_risk[row['symbol']] = reserved_risk.get(row['symbol'], Decimal(0)) + risk
    for symbol, risk in reserved_risk.items():
        by_symbol[symbol] = max(by_symbol.get(symbol, Decimal(0)), risk)
    current_risk = sum(by_symbol.values(), Decimal(0))
    if remaining < initial.minimum_quantity*sized.price/sized.leverage:
        raise EntryDenied('minimum_order_exceeds_margin', reason_code='minimum_order_exceeds_margin',
                          context=dict(context, remaining_margin=str(remaining), existing_open_risk=str(current_risk)))
    locked_inputs = dict(inputs, allocation=allocation, existing_open_risk=current_risk,
                         remaining_margin=min(remaining, sized.margin))
    refined = assess_budget_inputs(locked_inputs)
    if not refined.allowed:
        raise EntryDenied(refined.reason, reason_code=refined.reason,
                          context=dict(context, remaining_margin=str(remaining), existing_open_risk=str(current_risk)))
    if refined.quantity > sized.quantity:
        raise EntryDenied('HRS locked quantity cannot increase', reason_code='hrs_budget_quantity_increased')
    budget_context = _hrs_budget_json(dict(context, source='hrs_account_lock_v1',
        local_positions=local, pending_reservations=[dict(row) for row in pending],
        local_risk_by_symbol=local_risk, reservation_risk_by_symbol=reserved_risk,
        open_risk_by_symbol=by_symbol, existing_open_risk=current_risk,
        full_allocation=allocation, remaining_margin=remaining,
        planned_quantity=sized.quantity, planned_budget_inputs=inputs,
        planned_budget_decision=geometry['hrs_budget_decision']))
    refined = replace(refined, policy_revision=context['policy_revision'],
                      effective_allocation=values.allocation)
    geometry.update(hrs_budget_inputs=_hrs_budget_json(refined.budget_inputs),
                    hrs_budget_decision=_hrs_budget_json(refined), hrs_budget_context=budget_context,
                    quantity=str(refined.quantity), exit_ratios=[str(x) for x in refined.exit_ratios])
    resized = replace(sized, quantity=refined.quantity, original_quantity=refined.quantity,
        margin=refined.quantity*sized.price/sized.leverage, risk=refined.worst_loss,
        original_risk=refined.worst_loss, exit_ratios=refined.exit_ratios)
    return resized, refined, budget_context


_HRS_PENDING_COLUMNS = ('client_order_id,account_id,strategy_id,symbol,revision,quantity,'
                        'original_quantity,price,leverage,margin,pressure_risk,original_pressure_risk,state')


def _bind_hrs_floor_pending_to_rows(context, slot_context, pending, *, account, client_id):
    """Require the complete current durable sources, permitting only state progress."""
    try:
        captured=context['pending_reservations']
        def normalized(rows):
            if not isinstance(rows,(list,tuple)):
                raise ValueError('pending sources must be a list')
            result={}
            for row in rows:
                identity=row['client_order_id']
                if (not isinstance(identity,str) or not identity or identity == client_id
                        or identity in result or row['account_id'] != account
                        or row['strategy_id'] != 'hrs'
                        or not isinstance(row['symbol'],str) or not row['symbol']
                        or row['state'] not in ('reserved','submitting')):
                    raise ValueError('invalid pending source identity or scope')
                revision=number(row['revision'])
                if revision != revision.to_integral_value():
                    raise ValueError('invalid pending policy revision')
                amounts=tuple(number(row[key]) for key in (
                    'quantity','original_quantity','price','leverage','margin','pressure_risk'))
                original=row['original_pressure_risk']
                original=None if original is None else number(original)
                result[identity]=(row['symbol'],revision,amounts,original,row['state'])
            return result
        expected,actual=normalized(captured),normalized(pending)
        if set(expected) != set(actual):
            raise ValueError('pending source set changed')
        for identity, old in expected.items():
            current=actual[identity]
            if old[:-1] != current[:-1] or (old[-1],current[-1]) not in (
                    ('reserved','reserved'),('reserved','submitting'),('submitting','submitting')):
                raise ValueError('pending source financial values changed')
        symbols=sorted({values[0] for values in actual.values()})
        if context['reservation_symbols'] != symbols or slot_context['reservation_symbols'] != symbols:
            raise ValueError('pending slot symbol set changed')
    except (KeyError,ValueError,TypeError,AttributeError,ArithmeticError,EntryDenied) as exc:
        raise EntryDenied('HRS pending reservation sources changed',reason_code='hrs_budget_pending_source_mismatch') from exc


def _validate_hrs_floor_context(geometry, expected, row):
    """Bind the recomputed result to the captured locked ownership/risk sources."""
    context = geometry.get('hrs_budget_context')
    slot = geometry.get('slot_context')
    proof = geometry.get('slot_occupancy_proof')
    if (not isinstance(context,dict) or not isinstance(slot,dict) or not isinstance(proof,dict)
            or context.get('source') != 'hrs_account_lock_v1'
            or context.get('account_id') != row['account_id']
            or context.get('policy_revision') != row['revision']
            or any(context.get(k) != slot.get(k) or not context.get(k)
                   for k in ('checked_at','evidence_sha256','as_of','ownership_claims'))
            or context.get('evidence_sha256') != proof.get('equity_evidence_sha256')):
        raise EntryDenied('HRS locked risk context unavailable',reason_code='hrs_budget_context_invalid')
    try:
        initial = _validate_hrs_floor_decision(context['planned_budget_decision'],context['planned_budget_inputs'])
        price,leverage=initial.budget_inputs['entry_price'],initial.budget_inputs['leverage']
        sized=SizedEntry(initial.quantity,initial.quantity,price,leverage,
            initial.quantity*price/leverage,initial.worst_loss,initial.worst_loss,initial.exit_ratios)
        original_geometry=dict(hrs_budget_source='hrs_live_assessor_v1',
            hrs_budget_inputs=context['planned_budget_inputs'],hrs_budget_decision=context['planned_budget_decision'])
        values=ScopedRiskSnapshot(number(context['account_margin'],zero=True),Decimal(1),
            number(geometry['hrs_budget_decision']['effective_allocation']),3,context['slot_scope'],
            dict(local=context['local_positions']))
        _, replayed, replayed_context = _refine_hrs_floor(sized,original_geometry,values,
            context['pending_reservations'],number(context['full_allocation']),
            number(context['remaining_margin']),slot)
        if (_hrs_budget_json(replayed.budget_inputs) != _hrs_budget_json(expected.budget_inputs)
                or any(context[k] != replayed_context[k] for k in (
                    'local_risk_by_symbol','reservation_risk_by_symbol','open_risk_by_symbol','existing_open_risk','planned_quantity'))):
            raise ValueError('locked inputs do not reproduce risk sources')
    except (KeyError,ValueError,TypeError,ArithmeticError) as exc:
        raise EntryDenied('HRS locked risk context invalid',reason_code='hrs_budget_context_invalid') from exc


def account_id():
    return os.environ.get('TRADE_ACCOUNT_ID', 'default')


class RiskPolicyStore:
    def __init__(self, db, account=None):
        self.pool = getattr(db, 'pool', None)
        self.account = account or account_id()
        self.reservation_audit = None
        if self.pool is None:
            raise EntryDenied('risk policy database unavailable')

    async def policy(self, strategy, connection=None, *, submitting_client_id=None):
        async def read(conn):
            row = await conn.fetchrow('''SELECT p.* FROM trading.strategy_risk_policy p
                JOIN trading.strategy_risk_active a USING(account_id,strategy_id,revision)
                WHERE p.account_id=$1 AND p.strategy_id=$2
                AND p.effective_at <= clock_timestamp() AND p.expires_at > clock_timestamp()''', self.account,strategy)
            if not row:
                raise EntryDenied('missing or expired risk policy')
            from shared.equity_entry_gate import enforce_equity_gate, EquityGateDenied
            try:
                await enforce_equity_gate(conn, strategy, self.account, row['revision'],
                                         submitting_client_id=submitting_client_id)
            except EquityGateDenied as exc:
                raise EntryDenied(str(exc)) from exc
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
        self.reservation_audit = None
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
            context={}
            scoped=isinstance(snapshot_values,ScopedRiskSnapshot)
            saved_geometry=dict(geometry or {})
            floor = False
            refined_decision = budget_context = None
            if strategy == 'hrs':
                from strategies.hrs.risk_budget import FLOOR, STRICT, policy_budget_version
                try:
                    active_version = policy_budget_version(policy)
                except ValueError as exc:
                    raise EntryDenied('invalid HRS policy budget version', reason_code='hrs_budget_version_invalid') from exc
                proposed_version = saved_geometry.get('hrs_risk_budget_version', STRICT)
                if proposed_version not in (STRICT, FLOOR) or proposed_version != active_version:
                    raise EntryDenied('HRS policy budget version mismatch', reason_code='hrs_budget_version_mismatch')
                floor = active_version == FLOOR
                if floor and not scoped:
                    raise EntryDenied('HRS minimum-order budget requires owned slots', reason_code='hrs_budget_scope_required')
            if scoped:
                from strategies.hrs.entry_occupancy import SLOT_SCOPE,HrsOccupancyDenied,prove_hrs_slot_ownership
                values=snapshot_values
                if strategy!='hrs' or values.slot_scope!=SLOT_SCOPE:
                    raise EntryDenied('unsupported strategy slot scope',reason_code='entry_slot_scope_invalid')
                if type(values.max_slots) is not int or not 1<=values.max_slots<=3:
                    raise EntryDenied('HRS slot limit must remain at most three',reason_code='hrs_slot_limit_invalid')
                row=await conn.fetchrow('''SELECT g.*,clock_timestamp() AS checked_at,e.payload
                    FROM trading.strategy_equity_gate g LEFT JOIN trading.hrs_equity_evidence e
                    ON e.account_id=g.account_id AND e.evidence_sha256=g.evidence_sha256
                    WHERE g.account_id=$1 AND g.strategy_id='hrs' ''',self.account)
                if (row is None or row['ready'] is not True or row['allowed'] is not True
                        or row['policy_revision']!=revision or row['payload'] is None
                        or not row['as_of']<=row['checked_at']<row['valid_until']
                        or not timedelta(0)<row['valid_until']-row['as_of']<=timedelta(seconds=120)):
                    raise EntryDenied('current HRS equity ownership evidence unavailable',reason_code='hrs_slot_equity_unavailable')
                pending_symbols=await conn.fetch("SELECT DISTINCT symbol FROM trading.entry_risk_reservation WHERE account_id=$1 AND strategy_id='hrs' AND state<>'released'",self.account)
                reservations={r['symbol'] for r in pending_symbols}
                metadata=dict(checked_at=row['checked_at'].isoformat(),as_of=row['as_of'].isoformat(),
                    policy_revision=row['policy_revision'],strategy_id='hrs')
                context=dict(slot_scope=SLOT_SCOPE,account_id=self.account,account_symbols=None,hrs_symbols=None,
                    reservation_symbols=sorted(reservations),occupied_symbols=None,observed_slots=None,requested_slots=None,
                    max_slots=values.max_slots,evidence_sha256=row['evidence_sha256'],**metadata)
                claims=await conn.fetch("SELECT id,symbol,strategy,claim_state FROM trading.position_claims WHERE claim_state IN ('PENDING','ACTIVE') AND expires_at>clock_timestamp()")
                ownership_claims=[dict(row) for row in claims]
                context['ownership_claims']=ownership_claims
                try:
                    proof=prove_hrs_slot_ownership(self.account,dict(account_id=self.account,evidence_sha256=row['evidence_sha256'],payload=row['payload']),values.slot_evidence,claims,checked_at=row['checked_at'])
                except HrsOccupancyDenied as exc:
                    raise EntryDenied('HRS slot ownership cannot be proven',reason_code=exc.reason_code,
                        context=context) from exc
                if proof['as_of']!=row['as_of'].isoformat():
                    raise EntryDenied('HRS equity projection does not match parent',reason_code='hrs_slot_equity_mismatch')
                holdings,equity,allocation=values.account_margin,values.equity,values.allocation
                if number(holdings,zero=True)<number(proof['account_margin'],zero=True):
                    raise EntryDenied('account margin omits verified inventory',reason_code='hrs_slot_account_margin_understated',
                        context=dict(account_margin=str(holdings),inventory_margin=proof['account_margin'],account_symbols=proof['account_symbols'],hrs_symbols=proof['hrs_symbols'],max_slots=values.max_slots))
                owned=set(proof['hrs_symbols'])
                occupied=owned|reservations
                context=dict(slot_scope=SLOT_SCOPE,account_id=self.account,account_symbols=proof['account_symbols'],hrs_symbols=proof['hrs_symbols'],
                    reservation_symbols=sorted(reservations),occupied_symbols=sorted(occupied),observed_slots=len(occupied),
                    requested_slots=len(occupied|{symbol}),max_slots=values.max_slots,
                    evidence_sha256=row['evidence_sha256'],observed_at=proof['observed_at'],account_margin=str(holdings),ownership_claims=ownership_claims,**metadata)
                if symbol in proof['account_symbols']:
                    raise EntryDenied('existing account holding forbids additions',reason_code='entry_no_additions',context=context)
                if len(occupied|{symbol})>values.max_slots:
                    raise EntryDenied('concurrent position slots include pending reservations',reason_code='entry_position_slots',context=context)
                if floor:
                    parent = _bind_hrs_floor_local_to_parent(row['payload'],values.slot_evidence['local'],
                        self.account,row['evidence_sha256'])
                    receipt = values.financial_receipt
                    financial = await self._financial_receipt(conn,receipt,symbol,identity,row['checked_at'])
                    from strategies.hrs.decision_capture import canonical_payload
                    if (canonical_payload(financial['slot_inputs'])[1] != canonical_payload(values.slot_evidence)[1]
                            or number(holdings,zero=True) != _hrs_floor_receipt_margin(financial)
                            or number(equity) != number(financial['account_equity'])
                            or number(allocation) != number(financial['effective_allocation'])):
                        raise EntryDenied('HRS snapshot differs from independent financial receipt',
                            reason_code='hrs_budget_finance_source_mismatch')
                    saved_geometry.update(hrs_financial_receipt=receipt,signal_id=str(identity))
                    context.update(financial_receipt=receipt,symbol=symbol,signal_id=str(identity))
                saved_geometry.update(slot_scope=SLOT_SCOPE,slot_occupancy_proof=proof,slot_context=context)
            else:
                holdings,equity,allocation = snapshot_values[:3]
            # Re-read under the policy lock so a smaller allocation cannot be missed.
            full_allocation = await current_allocation(conn,strategy)
            allocation = min(number(allocation), full_allocation)
            if not scoped and len(snapshot_values) == 5:
                owned_symbols,max_slots = snapshot_values[3:]
                pending_symbols = await conn.fetch("SELECT DISTINCT symbol FROM trading.entry_risk_reservation WHERE account_id=$1 AND strategy_id=$2 AND state<>'released'",self.account,strategy)
                occupied_symbols = set(owned_symbols) | {r['symbol'] for r in pending_symbols}
                if len(occupied_symbols | {symbol}) > max_slots:
                    raise EntryDenied('concurrent position slots include pending reservations',reason_code='entry_position_slots',context=dict(occupied_symbols=sorted(occupied_symbols),requested_slots=len(occupied_symbols|{symbol}),max_slots=max_slots))
            cap = min(number(allocation),number(equity)*number(policy['ratio_cap']),number(policy['entry_ceiling']))
            if floor:
                cap = min(cap,full_allocation*number(policy['multiplier']))
            used = await conn.fetchval('''SELECT COALESCE(sum(margin),0) FROM trading.entry_risk_reservation
                WHERE account_id=$1 AND strategy_id=$2 AND state <> 'released' ''',self.account,strategy)
            if floor:
                pending = await conn.fetch(f"""SELECT {_HRS_PENDING_COLUMNS}
                    FROM trading.entry_risk_reservation WHERE account_id=$1 AND strategy_id='hrs'
                    AND state<>'released' ORDER BY client_order_id""", self.account)
                remaining = cap - number(holdings,zero=True) - number(used,zero=True)
                sized, refined_decision, budget_context = _refine_hrs_floor(
                    sized,saved_geometry,values,pending,full_allocation,remaining,
                    dict(context,locked_policy=_hrs_budget_json(policy),
                         snapshot_effective_allocation=values.allocation,snapshot_account_equity=values.equity,
                         margin_cap=cap,reserved_margin=used))
            if number(holdings,zero=True)+used+sized.margin > cap:
                raise EntryDenied('entry ceiling includes holdings and all durable reservations',reason_code='entry_margin_ceiling',context=dict(context,account_margin=str(holdings),reserved_margin=str(used),entry_margin=str(sized.margin),margin_cap=str(cap)))
            # Existing symbol claim must already be acquired by the ordinary ownership protocol.
            claim = await conn.fetchrow('''UPDATE trading.position_claims SET expires_at='infinity'
                WHERE symbol=$1 AND strategy=$2 AND claim_state IN ('PENDING','ACTIVE') RETURNING id''',symbol,strategy)
            if not claim:
                raise EntryDenied('entry has no exclusive symbol claim')
            await conn.execute('''INSERT INTO trading.entry_risk_reservation
                (client_order_id,account_id,strategy_id,revision,symbol,quantity,original_quantity,price,leverage,margin,state,geometry,pressure_risk,original_pressure_risk)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'reserved',$11::jsonb,$12,$13)''',client_id,self.account,strategy,revision,symbol,
                sized.quantity,sized.original_quantity,sized.price,sized.leverage,sized.margin,json.dumps(_hrs_budget_json(saved_geometry),sort_keys=True),sized.risk,sized.original_risk)
        if scoped:
            # Expose only a durably committed result to this call's HRS capture.
            # Live HRS owns one store instance per invocation, without reuse.
            self.reservation_audit=copy.deepcopy(dict(client_order_id=client_id,
                slot_occupancy_proof=saved_geometry['slot_occupancy_proof'],slot_context=saved_geometry['slot_context']))
            if floor:
                self.reservation_audit.update(risk_decision=refined_decision,
                    budget_decision=saved_geometry['hrs_budget_decision'], budget_context=budget_context)
        return client_id

    async def _financial_receipt(self, conn, receipt, symbol, identity, checked_at):
        if not isinstance(receipt,dict) or not receipt.get('evidence_id'):
            raise EntryDenied('HRS financial receipt missing',reason_code='hrs_budget_financial_receipt_invalid')
        row=await conn.fetchrow("""SELECT * FROM hrs.hrs_decision_evidence
            WHERE evidence_id=$1""",receipt['evidence_id'])
        return _validate_hrs_financial_receipt(receipt,row,account=self.account,symbol=symbol,
            signal_id=identity,checked_at=checked_at)

    async def authorize_hrs_floor(self, conn, policy, client_id, decision, *, quantity=None, submitting=False):
        """Recheck current durable intent and immutable sources on this connection."""
        from strategies.hrs.risk_budget import FLOOR, policy_budget_version
        if (policy_budget_version(policy) != FLOOR or policy['mode'] != 'reduced'
                or policy['allow_add'] is not False or decision is None
                or decision.budget_version != FLOOR or decision.allowed is not True
                or decision.client_order_id != client_id
                or decision.policy_revision != policy['revision']):
            raise EntryDenied('HRS minimum-order submission decision unavailable')
        _validate_hrs_floor_decision(decision,decision.budget_inputs)
        qty=number(decision.quantity if quantity is None else quantity)
        row=await conn.fetchrow("""SELECT * FROM trading.entry_risk_reservation
            WHERE client_order_id=$1 AND account_id=$2 AND strategy_id='hrs'
            AND state IN ('reserved','submitting')""",client_id,self.account)
        if not row or row['revision'] != policy['revision'] or (submitting and row['state'] != 'submitting'):
            raise EntryDenied('HRS durable submission reservation changed')
        geometry=json.loads(row['geometry']) if isinstance(row['geometry'],str) else row['geometry']
        expected=_validate_hrs_floor_decision(geometry.get('hrs_budget_decision'),geometry.get('hrs_budget_inputs'))
        _validate_hrs_floor_context(geometry,expected,row)
        context=geometry['hrs_budget_context']
        receipt_identity=geometry.get('signal_id')
        expected_client='risk_' + hashlib.sha256(f'{self.account}:hrs:{receipt_identity}'.encode()).hexdigest()[:30]
        if (not isinstance(receipt_identity,str) or not receipt_identity or expected_client != client_id
                or context.get('signal_id') != receipt_identity or context.get('symbol') != row['symbol']
                or context.get('financial_receipt') != geometry.get('hrs_financial_receipt')):
            raise EntryDenied('HRS financial receipt intent changed',reason_code='hrs_budget_financial_receipt_invalid')
        pending=await conn.fetch(f"""SELECT {_HRS_PENDING_COLUMNS}
            FROM trading.entry_risk_reservation WHERE account_id=$1 AND strategy_id='hrs'
            AND state<>'released' AND client_order_id<>$2 ORDER BY client_order_id""",self.account,client_id)
        _bind_hrs_floor_pending_to_rows(context,geometry['slot_context'],pending,
                                       account=self.account,client_id=client_id)
        if context.get('locked_policy') != _hrs_budget_json(policy):
            raise EntryDenied('HRS locked policy evidence changed')
        gate=await conn.fetchrow('''SELECT *,clock_timestamp() AS checked_at
            FROM trading.strategy_equity_gate WHERE account_id=$1 AND strategy_id='hrs' ''',self.account)
        if (not gate or gate['evidence_sha256'] != context['evidence_sha256']
                or gate['ready'] is not True or gate['allowed'] is not True
                or gate['policy_revision'] != policy['revision']
                or not gate['as_of'] <= gate['checked_at'] < gate['valid_until']
                or not timedelta(0) < gate['valid_until']-gate['as_of'] <= timedelta(seconds=120)):
            raise EntryDenied('HRS current equity parent changed',reason_code='hrs_budget_finance_parent_changed')
        parent=await conn.fetchrow("""SELECT payload FROM trading.hrs_equity_evidence
            WHERE account_id=$1 AND evidence_sha256=$2""",self.account,context['evidence_sha256'])
        if not parent:
            raise EntryDenied('HRS immutable equity parent missing')
        payload=_bind_hrs_floor_local_to_parent(parent['payload'],context['local_positions'],
                                               self.account,context['evidence_sha256'])
        financial=await self._financial_receipt(conn,geometry.get('hrs_financial_receipt'),
            row['symbol'],geometry.get('signal_id'),gate['checked_at'])
        from strategies.hrs.entry_occupancy import prove_hrs_slot_ownership, HrsOccupancyDenied
        claims=await conn.fetch("SELECT id,symbol,strategy,claim_state FROM trading.position_claims WHERE claim_state IN ('PENDING','ACTIVE') AND expires_at>clock_timestamp()")
        try:
            proof=prove_hrs_slot_ownership(self.account,dict(account_id=self.account,
                evidence_sha256=gate['evidence_sha256'],payload=payload),financial['slot_inputs'],claims,
                checked_at=gate['checked_at'])
            _bind_hrs_floor_local_to_parent(payload,financial['slot_inputs']['local'],self.account,gate['evidence_sha256'])
            if proof != geometry['slot_occupancy_proof']:
                raise EntryDenied('HRS receipt ownership proof changed',reason_code='hrs_budget_financial_receipt_invalid')
        except HrsOccupancyDenied as exc:
            raise EntryDenied('HRS receipt ownership changed',reason_code=exc.reason_code) from exc
        allocation=await current_allocation(conn,'hrs')
        _validate_hrs_floor_finance(context,geometry['slot_context'],financial,pending,
                                   allocation,policy,expected)
        if not (geometry.get('hrs_risk_budget_version') == FLOOR
                and geometry.get('hrs_budget_source') == 'hrs_live_assessor_v1'
                and _hrs_budget_json(expected.budget_inputs) == _hrs_budget_json(decision.budget_inputs)
                and row['quantity'] == row['original_quantity'] == qty == decision.quantity == expected.quantity
                and row['pressure_risk'] == row['original_pressure_risk'] == decision.worst_loss == expected.worst_loss
                and row['price'] == expected.budget_inputs['entry_price']
                and row['leverage'] == expected.budget_inputs['leverage']
                and row['margin'] == qty*row['price']/row['leverage']):
            raise EntryDenied('HRS durable order risk or geometry changed')
        return dict(row)

    async def release_hrs_precheck_denial(self, symbol, decision, *, reason_code):
        """Release this denied intent only while reserved proves no HTTP attempt.

        Never infer safety from age or from a failed authorization. The exact
        account/client/symbol/revision and reserved state must still match under
        the same account lock used to enter submitting.
        """
        client_id=getattr(decision,'client_order_id',None)
        revision=getattr(decision,'policy_revision',None)
        if (not isinstance(client_id,str) or not client_id.startswith('risk_')
                or not isinstance(revision,int) or isinstance(revision,bool)
                or revision <= 0 or not isinstance(symbol,str) or not symbol):
            return False
        async with self.pool.acquire() as conn, conn.transaction():
            await self.lock(conn,'hrs')
            row=await conn.fetchrow("""SELECT * FROM trading.entry_risk_reservation
                WHERE client_order_id=$1 AND account_id=$2 AND strategy_id='hrs'
                  AND symbol=$3 AND revision=$4 AND state='reserved'""",
                client_id,self.account,symbol,revision)
            if row is None:
                return False
            await self._release_proven_unsent(conn,'hrs',client_id,row,'reserved',
                denial=dict(stage='executor_precheck',reason_code=reason_code))
            return True

    async def _release_proven_unsent(self, conn, strategy, client_id, row, state, *, denial=None):
        resolution={'submission_resolution':'denied_before_http'}
        if denial is not None:
            resolution['submission_denial']=denial
        await conn.execute("""UPDATE trading.entry_risk_reservation
            SET state='released',updated_at=clock_timestamp(),
                geometry=COALESCE(geometry,'{}'::jsonb) || $5::jsonb
            WHERE client_order_id=$1 AND account_id=$2 AND strategy_id=$3 AND state=$4""",
            client_id, self.account, strategy, state,json.dumps(resolution))
        await conn.execute("""UPDATE trading.position_claims
            SET claim_state='RELEASED',released_at=clock_timestamp(),reason='risk_denied_before_http'
            WHERE symbol=$1 AND strategy=$2 AND claim_state='PENDING'
              AND NOT EXISTS(SELECT 1 FROM trading.entry_risk_reservation
                WHERE account_id=$3 AND symbol=$1 AND state<>'released')""",
            row['symbol'], strategy, self.account)

    @asynccontextmanager
    async def submitting(self, strategy, client_id, *, risk_decision=None):
        # Persist submitting BEFORE network write; crash/timeout retains occupancy.
        early_error, row = None, None
        async with self.pool.acquire() as conn, conn.transaction():
            await self.lock(conn,strategy)
            try:
                policy = await self.policy(strategy,conn)
            except EntryDenied as exc:
                # A reserved row has never entered the submitting state, hence
                # cannot have reached HTTP under this state machine.
                unsent = await conn.fetchrow("""SELECT * FROM trading.entry_risk_reservation
                    WHERE client_order_id=$1 AND account_id=$2 AND strategy_id=$3
                      AND state='reserved' FOR UPDATE""",client_id,self.account,strategy)
                if unsent:
                    await self._release_proven_unsent(conn,strategy,client_id,unsent,'reserved')
                early_error = exc
            else:
                row = await conn.fetchrow("""UPDATE trading.entry_risk_reservation
                    SET state='submitting',updated_at=clock_timestamp()
                    WHERE client_order_id=$1 AND account_id=$2 AND strategy_id=$3
                    AND revision=$4 AND state='reserved' RETURNING *""",client_id,self.account,strategy,policy['revision'])
                if not row or policy['mode'] != 'reduced':
                    if row:
                        await self._release_proven_unsent(conn,strategy,client_id,row,'submitting')
                    else:
                        # A revision change can prevent the UPDATE from matching.
                        # Only a still-reserved row proves no HTTP was attempted;
                        # submitting/unknown rows must retain their occupancy.
                        unsent = await conn.fetchrow("""SELECT * FROM trading.entry_risk_reservation
                            WHERE client_order_id=$1 AND account_id=$2 AND strategy_id=$3
                              AND state='reserved' FOR UPDATE""",client_id,self.account,strategy)
                        if unsent:
                            await self._release_proven_unsent(conn,strategy,client_id,unsent,'reserved')
                    early_error = EntryDenied('submission is not authorized')
        if early_error is not None:
            raise early_error
        # Final authorize with activation lock held through exchange submit.
        pending_error = None
        async with self.pool.acquire() as conn:
            transaction = conn.transaction()
            await transaction.__aenter__()
            finished = False
            try:
                await self.lock(conn,strategy)
                async def validate_floor(current):
                    if strategy != 'hrs':
                        return
                    from strategies.hrs.risk_budget import FLOOR, policy_budget_version
                    try:
                        floor = policy_budget_version(current) == FLOOR
                    except ValueError as exc:
                        raise EntryDenied('invalid HRS policy budget version') from exc
                    if floor or getattr(risk_decision,'budget_version',None) == FLOOR:
                        await self.authorize_hrs_floor(conn,current,client_id,risk_decision,submitting=True)
                async def final_authorization():
                    current = await self.policy(strategy, conn, submitting_client_id=client_id)
                    if current['revision'] != row['revision'] or current['mode'] != 'reduced':
                        raise EntryDenied('policy changed at HTTP submission')
                    await validate_floor(current)
                from shared.order_submission_authorization import bind_submission_authorization
                with bind_submission_authorization(final_authorization) as attempt:
                    body_started = False
                    try:
                        policy = await self.policy(strategy,conn,submitting_client_id=client_id)
                        if policy['revision'] != row['revision'] or policy['mode'] != 'reduced':
                            raise EntryDenied('policy changed immediately before submission')
                        await validate_floor(policy)
                        body_started = True
                        yield dict(row)
                    except BaseException as exc:
                        # Unknown HTTP and cancellation must not roll back a
                        # consumer invalidation. Re-raise only after its commit.
                        pending_error = exc
                    async def complete():
                        if attempt.transport_started:
                            from shared.equity_entry_gate import invalidate_after_submission
                            await invalidate_after_submission(conn,strategy,self.account)
                        elif (attempt.denied_before_transport
                              or (not body_started and isinstance(pending_error,EntryDenied))):
                            await self._release_proven_unsent(conn,strategy,client_id,row,'submitting')
                        await transaction.__aexit__(None,None,None)
                    cancelled = await _finish_shielded(complete())
                    finished = True
                    if cancelled is not None:
                        pending_error = cancelled
            except BaseException as exc:
                if not finished:
                    await _finish_shielded(transaction.__aexit__(type(exc),exc,exc.__traceback__))
                raise
        if pending_error is not None:
            raise pending_error

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


async def entry_allowed(strategy, strategy_id, *, adding=False, stage='submit', risk_decision=None, quantity=None,
                        denial=None):
    def denied(reason_code):
        if denial is not None:
            denial.update(stage=stage,reason_code=reason_code)
        return False
    try:
        store = RiskPolicyStore(getattr(strategy,'db_manager',None) or getattr(strategy,'db',None))
        policy = await store.policy(strategy_id)
        if policy['mode'] != 'reduced' or adding:
            return denied('entry_mode_not_authorized')
        if strategy_id == 'btc_eth_aggressive':
            return True
        if strategy_id != 'hrs' or stage not in ('analysis','submit'):
            return denied('entry_stage_not_authorized')
        if stage == 'analysis':
            return True
        if risk_decision is None or risk_decision.allowed is not True or quantity is None:
            return denied('entry_risk_decision_missing_or_denied')
        try:
            qty = number(quantity)
            limit = number(risk_decision.risk_limit)
            loss = number(risk_decision.worst_loss)
            planned = number(risk_decision.quantity)
            valid = (qty == planned and loss <= limit and policy['allow_add'] is False
                     and risk_decision.policy_revision == policy['revision']
                     and isinstance(risk_decision.client_order_id, str)
                     and risk_decision.client_order_id.startswith('risk_'))
            if not valid:
                return denied('entry_risk_decision_mismatch')
            from strategies.hrs.risk_budget import FLOOR, STRICT, policy_budget_version
            version = policy_budget_version(policy)
            if getattr(risk_decision, 'budget_version', STRICT) != version:
                return denied('hrs_budget_version_mismatch')
            if version != FLOOR:
                return True
            async with store.pool.acquire() as conn:
                await store.authorize_hrs_floor(conn,policy,risk_decision.client_order_id,
                                                risk_decision,quantity=qty)
            return True
        except EntryDenied as exc:
            return denied(exc.reason_code)
    except EntryDenied as exc:
        return denied(exc.reason_code)
    except Exception:
        # Deliberately expose only a code, never exception text or raw evidence.
        return denied('entry_authorization_error')


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
