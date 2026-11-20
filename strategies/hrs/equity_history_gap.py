"""Reconstruct reviewed HRS checkpoint gaps from actual closed Binance marks."""
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from .equity_evidence import EvidenceBlocked, number, order_owners, owned_fill_positions
from .loss_gate import DAILY_LIMIT, ROLLING_LIMIT, SHANGHAI
from .risk_ledger import EquityCheckpoint

MINUTE = timedelta(minutes=1)
BAR_SOURCE = '/fapi/v1/markPriceKlines'


def _require(condition, reason):
    if not condition:
        raise EvidenceBlocked('history_gap_' + reason)


def _bars_by_close(bars, left, right):
    start = left.replace(second=0, microsecond=0)
    end = right.replace(second=0, microsecond=0)
    expected_count = (end - start) // MINUTE
    _require(0 < expected_count <= 1000 and len(bars) == expected_count, 'bar_coverage')
    result = {}
    for index, bar in enumerate(bars):
        opened = start + index * MINUTE
        open_ms = int(opened.timestamp()) * 1000
        _require(len(bar) >= 7 and bar[0] == open_ms and bar[6] == open_ms + 59999, 'bar_identity')
        opened_price, high, low, close = (number(value, 'history_gap_bar_price', positive=True) for value in bar[1:5])
        _require(low <= min(opened_price, close) <= max(opened_price, close) <= high, 'bar_price_range')
        result[opened + MINUTE] = (bar, close)
    return result


def reconstruct_gaps(manifest, account, ledger, checkpoints, fills, orders, opening, available_at):
    """Keep live points intact and reconstruct only constant-unit, proved paths.

    Reviewed occupied gaps may contain unchanged inventory or a single exit to
    flat. The existing economic ledger supplies exact income and capital events.
    The historical API supplies marks only; it has no authority over balances.
    """
    _require(manifest['account'] == account and manifest['segment'] == ledger.segment_id, 'segment_identity')
    collected = datetime.fromisoformat(manifest['available_at'])
    _require(collected.utcoffset() is not None and collected <= available_at, 'source_availability')
    _require(ledger.as_of - ledger.inception < timedelta(days=30), 'inception_only')
    endpoints = {left.as_of: right.as_of for left, right in zip(checkpoints, checkpoints[1:])}
    by_time = {point.as_of: point for point in checkpoints}
    owner_map = order_owners(account, orders)
    restored, retained = [], {}
    for item in manifest['gaps']:
        left, right = (datetime.fromisoformat(item[key]) for key in ('left', 'right'))
        _require(endpoints.get(left) == right and ledger.inception <= left < right <= min(ledger.as_of,collected)
                 and right - left > 2 * MINUTE, 'checkpoint_identity')
        units = by_time[left].units
        _require(by_time[right].units == units and not any(left < row.occurred_at <= right for row in ledger.cash_flows),
                 'unproven_cash_flow')
        inventory = owned_fill_positions(account, fills, orders, through=left, after=ledger.inception,
                                         opening_inventory=opening)
        held = {symbol: (qty, basis) for (strategy, symbol), (qty, basis) in inventory.items() if strategy == 'hrs' and qty}
        _require(held and set(held) == set(item['bars']), 'covered_symbols')
        owned_fill_positions(account, fills, orders, through=right, after=ledger.inception,
                             opening_inventory=opening)
        changes = [row for row in fills if left < row['trade_time_utc'] <= right
                   and owner_map.get((row['symbol'], str(row['order_id']))) == {'hrs'}]
        if changes:
            _require(len(changes) == 1, 'position_path')
            exit_fill = changes[0]
            qty, _ = held.get(exit_fill['symbol'], (Decimal(0), None))
            _require(qty != 0 and exit_fill['side'] == ('SELL' if qty > 0 else 'BUY')
                     and number(exit_fill['qty'], 'history_gap_exit_quantity', positive=True) == abs(qty), 'position_path')
        bars = {symbol: _bars_by_close(value, left, right) for symbol, value in item['bars'].items()}
        cursor = left.replace(second=0, microsecond=0) + MINUTE
        while cursor < right:
            post_exit = changes and changes[0]['trade_time_utc'] <= cursor
            cash = ledger.cash_allocation - sum(row.amount for row in ledger.cash_flows if row.occurred_at > cursor)
            income = sum(row.amount for row in ledger.incomes if row.occurred_at <= cursor)
            unrealized = Decimal(0)
            for symbol, (qty, basis) in held.items():
                if post_exit and symbol == changes[0]['symbol']:
                    continue
                bar, mark = bars[symbol][cursor]
                retained[f'{symbol}:{int(cursor.timestamp()) * 1000}'] = dict(
                    bar=bar, available_at=collected, source=BAR_SOURCE)
                unrealized += qty * (mark - basis)
            equity = number(cash + income + unrealized, 'history_gap_equity', positive=True)
            restored.append(EquityCheckpoint(ledger.segment_id, cursor, equity, units, equity / units))
            cursor += MINUTE
    _require(restored and len({point.as_of for point in restored}) == len(restored), 'duplicate_or_empty')
    return tuple(restored), retained


def replay_inception_latches(gate, samples):
    """Preserve latches and detect a sampled breach during the recovered history."""
    peak = samples[0].nav
    rolling, scope = gate.rolling_latched, gate.rolling_latch_scope
    today = samples[-1].as_of.astimezone(SHANGHAI).date()
    daily = gate.daily_latched and gate.day == today
    day_origin = None
    for sample in samples:
        peak = max(peak, sample.nav)
        if not rolling and Decimal(1) - sample.nav / peak >= ROLLING_LIMIT:
            rolling, scope = True, 'inception_segment'
        local = sample.as_of.astimezone(SHANGHAI)
        if local.date() != today:
            continue
        if local.time() == time.min:
            day_origin = sample.nav
        if day_origin is not None and sample.nav / day_origin - Decimal(1) <= -DAILY_LIMIT:
            daily = True
    if day_origin is not None and daily:
        return replace(gate, rolling_latched=rolling, rolling_latch_scope=scope,
                       day=today, day_origin_nav=day_origin, daily_latched=True)
    return replace(gate, rolling_latched=rolling, rolling_latch_scope=scope)


def plan_constant_gaps(account, ledger, checkpoints, fills, orders, opening, *, inception_only=True):
    """Bound automatic repairs to original, constant-economic occupied paths."""
    _require(not inception_only or ledger.as_of-ledger.inception<timedelta(days=30),'automatic_inception_only')
    owners=order_owners(account,orders);plan=[];total=0
    for left,right in zip(checkpoints,checkpoints[1:]):
        if right.as_of-left.as_of<=2*MINUTE:continue
        positions=owned_fill_positions(account,fills,orders,through=left.as_of,after=ledger.inception,opening_inventory=opening)
        held=sorted(symbol for (owner,symbol),(qty,_) in positions.items() if owner=='hrs' and qty)
        if not held:continue
        _require(ledger.inception<=left.as_of<right.as_of<=ledger.as_of
            and left.as_of.astimezone(SHANGHAI).date()==right.as_of.astimezone(SHANGHAI).date(),
            'automatic_boundary')
        _require(left.units==right.units,'automatic_units_change')
        _require(not any(left.as_of<event.occurred_at<=right.as_of for event in (*ledger.cash_flows,*ledger.incomes)),
            'automatic_economic_change')
        _require(not any(left.as_of<row['trade_time_utc']<=right.as_of
            and 'hrs' in owners.get((row['symbol'],str(row['order_id'])),set()) for row in fills),
            'automatic_position_change')
        count=(right.as_of.replace(second=0,microsecond=0)-left.as_of.replace(second=0,microsecond=0))//MINUTE
        _require(count>0 and count*len(held)<=128,'automatic_source_budget')
        if len(plan)>=8 or total+count*len(held)>128:break
        total+=count*len(held)
        plan.append(dict(left=left.as_of.isoformat(),right=right.as_of.isoformat(),symbols=held))
    return plan


def gap_nav_envelopes(manifest, ledger, samples, fills, orders, opening):
    """Genuine bar-derived portfolio bounds, distinct from observed checkpoints."""
    by_time={point.as_of:point for point in samples};bounds=[]
    for item in manifest['gaps']:
        left,right=(datetime.fromisoformat(item[key]) for key in ('left','right'))
        inventory=owned_fill_positions(account=manifest['account'],fills=fills,orders=orders,
            through=left,after=ledger.inception,opening_inventory=opening)
        held={symbol:value for (owner,symbol),value in inventory.items() if owner=='hrs' and value[0]}
        _require(set(held)==set(item['bars']),'automatic_envelope_symbols')
        bars={symbol:_bars_by_close(rows,left,right) for symbol,rows in item['bars'].items()}
        for at in sorted(next(iter(bars.values()))):
            cash=ledger.cash_allocation-sum(event.amount for event in ledger.cash_flows if event.occurred_at>at)
            income=sum(event.amount for event in ledger.incomes if event.occurred_at<=at)
            low=high=cash+income
            for symbol,(qty,basis) in held.items():
                bar,_=bars[symbol][at]
                upper,lower=(number(bar[key],'automatic_envelope_price',positive=True) for key in (2,3))
                low+=qty*((lower if qty>0 else upper)-basis)
                high+=qty*((upper if qty>0 else lower)-basis)
            units=by_time[left].units
            bounds.append(dict(as_of=at.isoformat(),source_units=str(units),low_nav=str(number(low,'automatic_envelope_equity',positive=True)/units),
                high_nav=str(number(high,'automatic_envelope_equity',positive=True)/units)))
    return bounds


def apply_conservative_bounds(gate, ledger, samples, bounds):
    """Retain possible peaks across bars, later samples and ordinary refreshes."""
    today=ledger.as_of.astimezone(SHANGHAI).date()
    origin_at=datetime.combine(today,time.min,SHANGHAI)
    origin=next((point for point in samples if point.as_of==origin_at),None)
    origin_nav=origin.nav if origin else (gate.day_origin_nav if gate.day==today else None)
    cutoff=max(ledger.inception,ledger.as_of-timedelta(days=30))
    events=[(point.as_of,point.nav,point.nav) for point in samples if cutoff<=point.as_of<=ledger.as_of]
    for bound in bounds:
        at=datetime.fromisoformat(bound['as_of'])
        _require(at.utcoffset() is not None and at<=ledger.as_of,'automatic_envelope_clock')
        low=number(bound['low_nav'],'automatic_envelope_nav',positive=True)
        high=number(bound['high_nav'],'automatic_envelope_nav',positive=True)
        _require(low<=high,'automatic_envelope_range')
        if at>=cutoff:events.append((at,low,high))
    peak=Decimal(0);daily_today=False;rolling=gate.rolling_latched;scope=gate.rolling_latch_scope
    for at,low,high in sorted(events):
        peak=max(peak,high)
        if Decimal(1)-low/peak>=ROLLING_LIMIT:
            rolling=True;scope=scope or ('rolling_30_day' if ledger.as_of-ledger.inception>=timedelta(days=30) else 'inception_segment')
        if origin_nav is not None and at.astimezone(SHANGHAI).date()==today and low/origin_nav-1<=-DAILY_LIMIT:
            daily_today=True
    return replace(gate,daily_latched=gate.daily_latched or daily_today,rolling_latched=rolling,rolling_latch_scope=scope,
        day=today if daily_today else gate.day,day_origin_nav=origin_nav if daily_today else gate.day_origin_nav)


def conservative_gap_latches(gate, manifest, ledger, samples, fills, orders, opening):
    """Unknown intrabar ordering is conservatively latched, never sampled as NAV."""
    return apply_conservative_bounds(gate,ledger,samples,gap_nav_envelopes(manifest,ledger,samples,fills,orders,opening))


def verify_mark_receipts(plan,known_at):
    """Bind every original response to exact scope, bytes and true availability."""
    from .equity_evidence import canonical
    def clock(value):
        value=datetime.fromisoformat(value) if isinstance(value,str) else value
        _require(isinstance(value,datetime) and value.utcoffset() is not None,'automatic_source_clock')
        return value
    available=clock(plan['available_at'])
    _require(available<=known_at,'automatic_source_future')
    receipts=plan['http_receipts'];used=set()
    for item in plan['gaps']:
        left,right=(datetime.fromisoformat(item[key]) for key in ('left','right'))
        start=int(left.replace(second=0,microsecond=0).timestamp())*1000
        end=int(right.replace(second=0,microsecond=0).timestamp())*1000-1
        for symbol,bars in item['bars'].items():
            matches=[(index,row) for index,row in enumerate(receipts) if row.get('params',{}).get('symbol')==symbol
                and row['params'].get('startTime')==start and row['params'].get('endTime')==end]
            _require(len(matches)==1,'automatic_source_scope')
            index,row=matches[0];params=row['params'];requested=clock(row['requested_at']);received=clock(row['received_at'])
            _require(index not in used and row.get('source')==BAR_SOURCE and params.get('interval')=='1m'
                and type(params.get('limit')) is int and len(bars)<=params['limit']<=128
                and requested<=received<=available and canonical(bars)[1]==row.get('response_sha256')
                and all(bar[6]<int(requested.timestamp()*1000) for bar in bars),'automatic_source_receipt_invalid')
            _bars_by_close(bars,left,right);used.add(index)
    _require(len(used)==len(receipts),'automatic_source_extra_receipt')


def revalue_gap_bounds(receipt,ledger,samples,fills,orders,opening):
    """Revalue original marks against current certified economics; never reuse old NAV."""
    by_time={point.as_of:point for point in samples}
    for gap in receipt['mark_gaps']:
        left,right=(datetime.fromisoformat(gap[key]) for key in ('left','right'))
        _require(left in by_time and right in by_time,'automatic_valuation_endpoints')
        for at in (left,right):
            later=[flow for flow in ledger.cash_flows if flow.occurred_at>at]
            _require(all(flow.nav_before is not None for flow in later),'automatic_valuation_unitization')
            units=ledger.units-sum((flow.amount/flow.nav_before for flow in later),Decimal(0))
            _require(by_time[at].units==units,'automatic_valuation_units')
        expected=plan_constant_gaps(receipt['account_id'],ledger,(by_time[left],by_time[right]),fills,orders,opening,inception_only=False)
        _require(expected==[dict(left=gap['left'],right=gap['right'],symbols=sorted(gap['bars']))],
            'automatic_valuation_path')
    return gap_nav_envelopes(dict(account=receipt['account_id'],gaps=receipt['mark_gaps']),ledger,samples,fills,orders,opening)
