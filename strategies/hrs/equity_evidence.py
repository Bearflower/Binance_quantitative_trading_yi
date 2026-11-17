"""Conservative evidence checks for prospective HRS equity.

No row is human-reviewed merely because it came from a derived attribution table.
The existing report reader owns income identity/amount reconciliation; this module
accepts only fully attributed reports and proves positions independently from
signed exchange fills and durable order owners. Unsupported evidence stays blocked.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

from .loss_gate import SHANGHAI
from .risk_ledger import IncomeEvent, LedgerNotReady, LedgerObservation, PositionMark, aware


class EvidenceBlocked(LedgerNotReady):
    pass


def number(value, name, *, positive=False):
    try:
        if value is None or isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or (positive and result <= 0):
            raise ValueError()
        return result
    except Exception:
        raise EvidenceBlocked(name) from None


def canonical(value):
    def encode(item):
        if isinstance(item, datetime):
            return aware(item, 'evidence').isoformat()
        if isinstance(item, Decimal):
            return str(number(item, 'nonfinite_evidence'))
        raise TypeError(type(item).__name__)
    payload = json.dumps(value, default=encode, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return payload, hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class AllocationEvidence:
    amount: Decimal
    identity: str
    effective_at: datetime
    source: dict


def allocation_evidence(row, as_of):
    if not row or str(row.get('month', ''))[:7] != as_of.astimezone(SHANGHAI).strftime('%Y-%m'):
        raise EvidenceBlocked('allocation_month_missing')
    entries = json.loads(row['entries'], parse_float=Decimal) if isinstance(row.get('entries'), str) else row.get('entries')
    if not isinstance(entries, list):
        raise EvidenceBlocked('allocation_identity_missing')
    matches = [item for item in entries if isinstance(item, dict) and item.get('strategy_id') == 'hrs']
    if len(matches) != 1 or row.get('id') is None:
        raise EvidenceBlocked('allocation_identity_ambiguous')
    amount = number(matches[0].get('allocated_amount'), 'allocation_amount_invalid', positive=True)
    effective = row.get('created_at')
    if not isinstance(effective, datetime):
        raise EvidenceBlocked('allocation_time_missing')
    # Production capital_allocation.created_at is explicitly Beijing naive.
    effective = effective.replace(tzinfo=SHANGHAI) if effective.tzinfo is None else effective
    if effective > as_of:
        raise EvidenceBlocked('allocation_time_future')
    source = dict(id=row['id'], month=str(row['month']), allocated_amount=str(amount), effective_at=effective)
    return AllocationEvidence(amount, canonical(source)[1], effective, source)


def attributed_incomes(report, account, segment):
    if (report.get('id_match_and_coverage_complete') is not True or report.get('coverage_gaps')
            or report.get('missing_income_fills') or report.get('unattributed')):
        raise EvidenceBlocked('income_or_fill_coverage_incomplete')
    events = []
    seen = {}
    kinds = {'REALIZED_PNL': 'realized_pnl', 'COMMISSION': 'fee', 'FUNDING_FEE': 'funding'}
    for row in report.get('events', ()):
        if row.get('account_id') != account or row.get('asset') != 'USDT' or row.get('reason') or not row.get('strategy'):
            raise EvidenceBlocked('income_attribution_unproven')
        if row.get('income_type') not in kinds or not row.get('tran_id') or not row.get('event_time_utc'):
            raise EvidenceBlocked('income_identity_missing')
        if row['strategy'] != 'hrs':
            continue
        event = IncomeEvent(f"{row['income_type']}:{row['tran_id']}", kinds[row['income_type']],
                            number(row.get('amount'), 'income_amount_invalid'),
                            datetime.fromisoformat(row['event_time_utc']), segment, True)
        aware(event.occurred_at, 'income')
        old = seen.setdefault(event.event_id, event)
        if old != event:
            raise EvidenceBlocked('conflicting_income_identity')
        if old is event:
            events.append(event)
    return tuple(events)


def order_owners(account, rows):
    result = {}
    for row in rows:
        if row.get('account_id') != account or row.get('order_id') is None or not row.get('symbol'):
            continue
        key = (row['symbol'], str(row['order_id']))
        result.setdefault(key, set()).add(row.get('strategy'))
    return result


def owned_fill_positions(account, fills, orders, *, through=None, after=None, opening_inventory=()):
    owners = order_owners(account, orders)
    result = {(row['strategy'],row['symbol']):(number(row['quantity'],'opening_quantity'),number(row['entry_price'],'opening_basis',positive=True)) for row in opening_inventory}
    seen = {}
    for row in sorted(fills, key=lambda item: (item.get('trade_time_utc'), str(item.get('trade_id')))):
        if row.get('account_id') != account:
            raise EvidenceBlocked('fill_account_mismatch')
        at = aware(row.get('trade_time_utc'), 'fill')
        if (through is not None and at > through) or (after is not None and at <= after):
            continue
        key = (row.get('symbol'), str(row.get('trade_id')))
        if key in seen:
            if seen[key] != row:
                raise EvidenceBlocked('fill_identity_conflict')
            continue
        seen[key] = row
        choices = owners.get((row.get('symbol'), str(row.get('order_id'))), set())
        if len(choices) != 1 or None in choices:
            raise EvidenceBlocked('fill_order_owner_unproven')
        strategy = next(iter(choices))
        side = row.get('side')
        if side not in {'BUY', 'SELL'}:
            raise EvidenceBlocked('fill_side_missing')
        qty = number(row.get('qty'), 'fill_quantity_invalid', positive=True) * (1 if side == 'BUY' else -1)
        price = number(row.get('price'), 'fill_price_invalid', positive=True)
        position_key = (strategy, row['symbol'])
        old_qty, old_basis = result.get(position_key, (Decimal(0), Decimal(0)))
        new_qty = old_qty + qty
        if old_qty == 0 or old_qty * qty > 0:
            basis = (abs(old_qty) * old_basis + abs(qty) * price) / abs(new_qty)
        elif new_qty == 0:
            basis = Decimal(0)
        elif old_qty * new_qty > 0:
            basis = old_basis
        else:
            basis = price
        result[position_key] = (new_qty, basis)
    return result


def _inventory(rows,*,basis_required=True):
    if not isinstance(rows, list):
        raise EvidenceBlocked('position_inventory_missing')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get('symbol'):
            raise EvidenceBlocked('position_inventory_invalid')
        qty = number(row.get('positionAmt'), 'position_quantity_missing')
        if qty == 0:
            continue
        if row.get('positionSide', 'BOTH') != 'BOTH' or row['symbol'] in result:
            raise EvidenceBlocked('hedged_or_duplicate_position')
        result[row['symbol']] = (qty, number(row.get('entryPrice'), 'position_entry_missing', positive=True) if basis_required or row.get('entryPrice') is not None else None)
    return result


def _inventory_agrees(primary,independent):
    # PM account V2 provides a complete quantity inventory, not cost basis.
    # No basis is synthesized. PositionRisk basis still needs exact full-fill
    # proof; an independently supplied basis must also agree if present.
    return set(primary)==set(independent) and all(primary[symbol][0]==qty and
        (basis is None or primary[symbol][1]==basis) for symbol,(qty,basis) in independent.items())


def prove_positions(account, segment, as_of, inventory, account_inventory, local, fills, orders,
                    marks, *, bootstrap, inception=None, opening_inventory=()):
    actual = _inventory(inventory)
    independent = _inventory(account_inventory,basis_required=False)
    if not _inventory_agrees(actual,independent):
        raise EvidenceBlocked('position_quantity_inventory_conflict')
    # Without a certified opening inventory, a partial cached historical sum
    # cannot establish who owns any pre-existing account quantity. This service
    # conservatively opens its first segment only from two empty account views.
    if bootstrap and actual:
        raise EvidenceBlocked('bootstrap_opening_inventory_unproven')
    # The service certifies a zero account anchor and collects EVERY relevant
    # symbol continuously from it. Facts preceding that anchor are irrelevant.
    positions = owned_fill_positions(account, fills, orders, through=as_of, after=inception,opening_inventory=opening_inventory)
    hrs_positions = owned_fill_positions(account, fills, orders, through=as_of, after=inception,opening_inventory=opening_inventory) if inception else positions
    if bootstrap and local:
        raise EvidenceBlocked('bootstrap_hrs_not_flat')
    output = []
    for symbol, (qty, entry) in actual.items():
        matches = [(strategy, held, basis) for (strategy, sym), (held, basis) in positions.items()
                   if sym == symbol and held != 0]
        if len(matches) != 1 or matches[0][1:] != (qty, entry):
            raise EvidenceBlocked('position_order_owner_unproven')
        owner = matches[0][0]
        if owner != 'hrs':
            if symbol in local or hrs_positions.get(('hrs', symbol), (Decimal(0),))[0] != 0:
                raise EvidenceBlocked('position_owner_conflict')
            continue
        if bootstrap:
            raise EvidenceBlocked('bootstrap_hrs_not_flat')
        expected, basis = hrs_positions.get(('hrs', symbol), (Decimal(0), Decimal(0)))
        tracked = local.get(symbol)
        if not tracked or expected != qty or basis != entry:
            raise EvidenceBlocked('position_quantity_or_basis_unproven')
        tracked_qty = number(tracked.get('remaining_quantity'), 'local_position_quantity_missing')
        sign = 1 if tracked.get('direction') == 'long' else -1 if tracked.get('direction') == 'short' else 0
        if sign == 0 or tracked_qty * sign != qty or not tracked.get('position_id'):
            raise EvidenceBlocked('position_quantity_local_conflict')
        mark = marks.get(symbol)
        if not isinstance(mark, dict) or mark.get('symbol') != symbol or mark.get('time') is None:
            raise EvidenceBlocked('mark_identity_missing')
        marked_at = datetime.fromtimestamp(int(mark['time']) / 1000, timezone.utc)
        if not timedelta(0) <= as_of - marked_at <= timedelta(seconds=120):
            raise EvidenceBlocked('mark_stale_or_future')
        output.append(PositionMark(tracked['position_id'], segment, qty, entry,
                                   number(mark.get('markPrice'), 'mark_price_invalid', positive=True), marked_at, True))
    for (strategy, symbol), (qty, _) in hrs_positions.items():
        if strategy == 'hrs' and qty != 0 and symbol not in actual:
            raise EvidenceBlocked('position_quantity_missing_on_exchange')
    if set(local) != {symbol for (strategy, symbol), (qty, _) in hrs_positions.items() if strategy == 'hrs' and qty != 0}:
        raise EvidenceBlocked('local_position_inventory_conflict')
    return tuple(output)


def flat_boundary_observation(state, boundary, available_at, fills, orders, incomes, *, coverage_complete,opening_inventory=()):
    """Value a historically flat boundary from a genuine segment, not today's state."""
    if (coverage_complete is not True or boundary < state.inception or boundary > available_at
            or boundary < state.as_of):
        raise EvidenceBlocked('boundary_coverage_missing')
    positions = owned_fill_positions(next(iter({row['account_id'] for row in fills}), ''), fills, orders,
                                    through=boundary, after=state.inception,opening_inventory=opening_inventory)
    if any(owner == 'hrs' and qty != 0 for (owner, _), (qty, _) in positions.items()):
        raise EvidenceBlocked('boundary_not_flat')
    events = tuple(event for event in incomes if event.occurred_at <= boundary)
    # Effective inventory is reconstructed at boundary; acquisition time is kept
    # separately in the immutable service evidence, never claimed as live capture.
    return LedgerObservation(boundary, state.segment_id, events, (), (), boundary, boundary, True, True)


@dataclass(frozen=True)
class AllocationChange:
    event_id: str
    delta: Decimal
    occurred_at: datetime
    not_before: datetime
    transaction_id: int | None
    evidence_sha256: str


def _selected_allocation(rows, at):
    active=[]
    for raw in rows:
        if raw.get('status')!='active' or str(raw.get('month',''))[:7]!=at.astimezone(SHANGHAI).strftime('%Y-%m'):
            continue
        row=dict(raw)
        if isinstance(row.get('created_at'),str):
            row['created_at']=datetime.fromisoformat(row['created_at'])
        proof=allocation_evidence(row,at)
        active.append((proof.effective_at,proof))
    if not active:
        raise EvidenceBlocked('allocation_active_gap')
    active.sort(key=lambda item:item[0],reverse=True)
    if len(active)>1 and active[0][0]==active[1][0]:
        raise EvidenceBlocked('allocation_active_identity_ambiguous')
    return active[0][1]


def allocation_event_changes(baseline_rows, journal, current_rows, inception, as_of):
    """Replay raw row mutations in serialized COMMIT order, not latest-minus-first.

    The database audit trigger serializes every modifying transaction. Statements
    inside ONE transaction are applied together: intermediate rows were never an
    externally visible allocation. Distinct committed transactions remain distinct
    deltas even when their final amount nets to zero. Calendar month selection is
    deterministic from the complete baseline plus mutations and gets an exact
    month-boundary event of its own.
    """
    rows={str(row['id']):dict(row) for row in baseline_rows}
    if len(rows)!=len(baseline_rows):
        raise EvidenceBlocked('allocation_event_chain_duplicate_baseline')
    previous=_selected_allocation(rows.values(),inception).amount
    changes=[]
    month_start=datetime.combine(inception.astimezone(SHANGHAI).date().replace(day=1),datetime.min.time(),SHANGHAI)
    def next_month(value):
        return value.replace(year=value.year+1,month=1) if value.month==12 else value.replace(month=value.month+1)
    boundary=next_month(month_start)

    def calendar_until(end):
        nonlocal previous,boundary
        while boundary<=end:
            value=_selected_allocation(rows.values(),boundary).amount
            if value!=previous:
                digest=canonical(dict(source=list(rows.values()),effective_at=boundary))[1]
                changes.append(AllocationChange('calendar:'+boundary.isoformat(),value-previous,boundary,boundary,None,digest))
            previous=value
            boundary=next_month(boundary)

    ordered=sorted(journal,key=lambda item:item['event_id'])
    groups=[]
    for event in ordered:
        if groups and groups[-1][0]['transaction_id']==event['transaction_id']:
            groups[-1].append(event)
        else:
            groups.append([event])
    seen_transactions=set()
    last_statement=inception
    for group in groups:
        txid=group[0]['transaction_id']
        if txid in seen_transactions:
            raise EvidenceBlocked('allocation_event_chain_transaction_order')
        seen_transactions.add(txid)
        first=min(aware(event['statement_at'],'allocation_event') for event in group)
        last=max(event['statement_at'] for event in group)
        if first<last_statement or last>as_of:
            raise EvidenceBlocked('allocation_event_chain_time_unproven')
        calendar_until(first)
        if boundary<=last:
            raise EvidenceBlocked('allocation_calendar_transaction_boundary_unproven')
        for event in group:
            old,event_new=event.get('old_row'),event.get('new_row')
            identity=str((event_new or old or {}).get('id'))
            if event['operation']=='INSERT':
                if old is not None or identity in rows or event_new is None:
                    raise EvidenceBlocked('allocation_event_chain_insert_conflict')
            elif event['operation'] in ('UPDATE','DELETE'):
                if old is None or rows.get(identity)!=old:
                    raise EvidenceBlocked('allocation_event_chain_missing')
            else:
                raise EvidenceBlocked('allocation_event_chain_operation')
            if event_new is None:
                rows.pop(identity,None)
            else:
                rows[identity]=dict(event_new)
        value=_selected_allocation(rows.values(),last).amount
        if value!=previous:
            digest=canonical(group)[1]
            changes.append(AllocationChange('journal:'+str(group[-1]['event_id']),value-previous,last,first,txid,digest))
        previous=value
        last_statement=last
    calendar_until(as_of)
    if rows!={str(row['id']):dict(row) for row in current_rows}:
        raise EvidenceBlocked('allocation_event_chain_current_mismatch')
    return tuple(changes)


def certify_opening_inventory(account,segment,as_of,inventory,independent,local,fills,orders,marks,claims,*,covered_symbols):
    """Prove current owners from a complete interval ending at actual inventory.

    For every current symbol P_now == sum(all signed fills) proves that its
    position was zero at that interval's lower bound. A cached partial subtotal
    is insufficient: the caller must supply fresh continuous coverage for each
    relevant symbol, exact order owners and its durable active claim.
    """
    actual=_inventory(inventory)
    if not _inventory_agrees(actual,_inventory(independent,basis_required=False)):
        raise EvidenceBlocked('opening_inventory_conflict')
    if not set(actual)<=set(covered_symbols):
        raise EvidenceBlocked('opening_fill_coverage_missing')
    selected=[row for row in fills if row['symbol'] in actual]
    positions=owned_fill_positions(account,selected,orders,through=as_of)
    owners={}
    for symbol,(qty,basis) in actual.items():
        matches=[(owner,amount,entry) for (owner,sym),(amount,entry) in positions.items() if sym==symbol and amount!=0]
        if len(matches)!=1 or matches[0][1:]!=(qty,basis):
            raise EvidenceBlocked('opening_position_order_fill_proof_missing')
        choices={row['strategy'] for row in claims if row.get('symbol')==symbol and row.get('id') is not None}
        if choices!={matches[0][0]}:
            raise EvidenceBlocked('opening_owner_claim_unproven')
        owners[symbol]=matches[0][0]
    hrs=prove_positions(account,segment,as_of,inventory,independent,local,selected,orders,marks,bootstrap=False)
    opening=tuple(dict(strategy=owners[symbol],symbol=symbol,quantity=str(qty),entry_price=str(entry))
                  for symbol,(qty,entry) in sorted(actual.items()))
    return hrs,opening


def attribute_funding_from_inventory(report,account,as_of,fills,orders,opening_inventory,*,coverage_complete,available_at=None):
    """Independent event-time owner proof, never a human-review override.

    Start from a verified CURRENT inventory, reverse every complete intervening
    signed fill, and require a unique strategy position matching the account net
    at settlement. Current ownership by itself is never a funding attribution.
    """
    if coverage_complete is not True:
        raise EvidenceBlocked('funding_fill_coverage_missing')
    owners=order_owners(account,orders)
    events=[dict(row) for row in report.get('events',())]
    changed=set()
    proofs=[]
    for event in events:
        if event.get('strategy') or event.get('income_type')!='FUNDING_FEE' or event.get('reason')!='funding_owner_unproven':
            continue
        if event.get('account_id')!=account or event.get('asset')!='USDT' or not event.get('event_time_utc'):
            continue
        at=datetime.fromisoformat(event['event_time_utc'])
        if at>as_of:
            raise EvidenceBlocked('funding_event_future')
        symbol=event['symbol']
        balances={row['strategy']:number(row['quantity'],'funding_anchor_quantity') for row in opening_inventory if row['symbol']==symbol}
        net=sum(balances.values(),Decimal(0))
        used=[]
        ambiguous=False
        for row in fills:
            when=aware(row['trade_time_utc'],'funding_fill')
            if row['symbol']!=symbol or not at<=when<=as_of:
                continue
            if when==at:
                ambiguous=True
                break
            choices=owners.get((symbol,str(row['order_id'])),set())
            if len(choices)!=1 or None in choices or row['side'] not in ('BUY','SELL'):
                ambiguous=True
                break
            owner=next(iter(choices))
            qty=number(row['qty'],'funding_fill_quantity',positive=True)*(1 if row['side']=='BUY' else -1)
            net-=qty
            balances[owner]=balances.get(owner,Decimal(0))-qty
            used.append(str(row['trade_id']))
        active=[(owner,qty) for owner,qty in balances.items() if qty!=0]
        if ambiguous or len(active)!=1 or active[0][1]!=net or net==0:
            continue
        event['strategy']=active[0][0]
        event['reason']=None
        event['attribution_source']='verified_fill_inventory_interval_v1'
        key=(event['account_id'],event['income_type'],str(event['tran_id']))
        changed.add(key)
        proofs.append(dict(event_identity=key,as_of=at,available_at=available_at or as_of,current_inventory=opening_inventory,
                           reversed_fill_ids=sorted(used),strategy=active[0][0],quantity_at_event=str(net)))
    if not changed:
        return report
    result=dict(report,events=events)
    result['unattributed']=[row for row in report.get('unattributed',()) if (row['account_id'],row['income_type'],str(row['tran_id'])) not in changed]
    totals={}
    for event in events:
        if not event.get('strategy'):
            continue
        bucket=totals.setdefault(event['strategy'],dict(realized_pnl_usdt=Decimal(0),commission_usdt=Decimal(0),funding_usdt=Decimal(0),net_usdt=Decimal(0)))
        component={'REALIZED_PNL':'realized_pnl_usdt','COMMISSION':'commission_usdt','FUNDING_FEE':'funding_usdt'}[event['income_type']]
        amount=number(event['amount'],'funding_income_amount')
        bucket[component]+=amount
        bucket['net_usdt']+=amount
    result['by_strategy']={owner:{key:str(value) for key,value in values.items()} for owner,values in totals.items()}
    result['attributed_usdt_total']=str(sum((values['net_usdt'] for values in totals.values()),Decimal(0)))
    result['unattributed_count']=len(result['unattributed'])
    result['unattributed_usdt_total']=str(sum((number(row['amount'],'unattributed_amount') for row in result['unattributed'] if row['asset']=='USDT'),Decimal(0)))
    result['unattributed_usdt_absolute']=str(sum((abs(number(row['amount'],'unattributed_amount')) for row in result['unattributed'] if row['asset']=='USDT'),Decimal(0)))
    ready=not result.get('coverage_gaps') and not result.get('missing_income_fills') and not result['unattributed'] and result.get('account_coverage_complete') is True
    result['id_match_and_coverage_complete']=ready
    result['strategy_attribution_complete']=ready
    result['verified_funding_proofs']=proofs
    result['source_report_id']=report.get('report_id')
    result['attribution_extension_version']='verified-fill-inventory-v1'
    result.pop('report_id',None)
    result['report_id']=canonical(result)[1]
    return result


def historical_boundary_observation(state,boundary,available_at,fills,orders,incomes,*,coverage_complete,
                                    opening_inventory=(),historical_bars=None,account):
    """Reconstruct inventory and use the last fully CLOSED source mark-price bar.

    The mark retains the real bar close timestamp before the boundary. Availability
    is the real later collection time; neither a current price nor an open bar is
    labelled historical. This follows the ledger's existing <=120s mark-age rule.
    """
    if coverage_complete is not True or not state.as_of<=boundary<=available_at:
        raise EvidenceBlocked('boundary_coverage_missing')
    positions=owned_fill_positions(account,fills,orders,through=boundary,after=state.inception,opening_inventory=opening_inventory)
    marks=[]
    for (owner,symbol),(qty,basis) in positions.items():
        if owner!='hrs' or qty==0:
            continue
        key=f'{symbol}:{int(boundary.timestamp()*1000)}'
        evidence=(historical_bars or {}).get(key)
        bar=evidence.get('bar') if isinstance(evidence,dict) else None
        if not isinstance(bar,list) or len(bar)<7:
            raise EvidenceBlocked('historical_mark_bar_missing')
        start=(int(boundary.timestamp()*1000)//60000)*60000-60000
        if int(bar[0])!=start or int(bar[6])!=start+59999:
            raise EvidenceBlocked('historical_mark_bar_identity_conflict')
        received=evidence.get('available_at')
        received=datetime.fromisoformat(received) if isinstance(received,str) else received
        marked=datetime.fromtimestamp(int(bar[6])/1000,timezone.utc)
        if received is None or received>available_at or not timedelta(0)<=boundary-marked<=timedelta(seconds=120):
            raise EvidenceBlocked('historical_mark_bar_availability_invalid')
        marks.append(PositionMark('reconstructed:'+state.segment_id+':'+symbol,state.segment_id,qty,basis,
                                  number(bar[4],'historical_mark_price',positive=True),marked,True))
    return LedgerObservation(boundary,state.segment_id,tuple(event for event in incomes if event.occurred_at<=boundary),
                             (),tuple(marks),boundary,boundary,True,True)


def require_funding_publication(account,start,as_of,fills,orders,current_inventory,forecasts,published,report):
    """Separate published-page completeness from expected economic settlement.

    Deadlines come only from independently observed venue forecasts. A missing
    receipt remains pending even if the income API returned a complete empty
    page. Published zero rates require the venue's actual settlement mark.
    No fixed interval or deadline is inferred from an income event.
    """
    owners=order_owners(account,orders)
    def balances_at(at,strict=False):
        balances={(row['strategy'],row['symbol']):number(row['quantity'],'funding_quantity') for row in current_inventory}
        for row in fills:
            when=aware(row['trade_time_utc'],'funding_fill')
            if not at<=when<=as_of:
                continue
            choices=owners.get((row['symbol'],str(row['order_id'])),set())
            if len(choices)!=1 or None in choices or row['side'] not in ('BUY','SELL'):
                raise EvidenceBlocked('funding_inventory_history_unproven')
            if when==at:
                if strict:
                    raise EvidenceBlocked('funding_same_time_fill_ambiguous')
                continue
            key=(next(iter(choices)),row['symbol'])
            balances[key]=balances.get(key,Decimal(0))-number(row['qty'],'funding_fill_quantity',positive=True)*(1 if row['side']=='BUY' else -1)
        return balances
    relevant={symbol for (owner,symbol),qty in balances_at(start).items() if owner=='hrs' and qty!=0}
    for row in fills:
        if start<row['trade_time_utc']<=as_of and 'hrs' in owners.get((row['symbol'],str(row['order_id'])),set()):
            relevant.add(row['symbol'])
    valid=[]
    for row in forecasts:
        source=aware(row['source_time'],'funding_forecast')
        received=aware(row['received_at'],'funding_forecast_available')
        due=aware(row['next_funding_time'],'funding_deadline')
        if not source<=received<=as_of or received-source>timedelta(seconds=120) or not source<due or received>due:
            continue
        valid.append(dict(row,source_time=source,received_at=received,next_funding_time=due))
    for symbol in relevant:
        # At least one real predeadline schedule must cover the opening of each
        # held episode. Retrospective rate pages cannot manufacture forecasts.
        held=balances_at(start).get(('hrs',symbol),Decimal(0))
        first=start if held else min(row['trade_time_utc'] for row in fills if row['symbol']==symbol and start<row['trade_time_utc']<=as_of and 'hrs' in owners.get((symbol,str(row['order_id'])),set()))
        if not any(row['symbol']==symbol and row['source_time']<=first<=row['next_funding_time'] for row in valid):
            raise EvidenceBlocked('funding_schedule_opening_coverage_missing')
        if balances_at(as_of).get(('hrs',symbol),Decimal(0)) and not any(row['symbol']==symbol and row['source_time']<=as_of<row['next_funding_time'] for row in valid):
            raise EvidenceBlocked('funding_schedule_current_coverage_missing')
    proofs=[]
    known={(row['symbol'],row['next_funding_time']) for row in valid}
    for symbol,rows in published.items():
        for row in rows:
            known.add((symbol,datetime.fromtimestamp(int(row['fundingTime'])/1000,timezone.utc)))
    deadlines=sorted((symbol,due) for symbol,due in known if start<due<=as_of)
    used=set()
    for symbol,due in deadlines:
        qty=balances_at(due,strict=True).get(('hrs',symbol),Decimal(0))
        if qty==0:
            continue
        active=[(owner,q) for (owner,sym),q in balances_at(due,strict=True).items() if sym==symbol and q!=0]
        if active!=[('hrs',qty)]:
            raise EvidenceBlocked('funding_settlement_owner_ambiguous')
        next_due=min((at for sym,at in known if sym==symbol and at>due),default=as_of+timedelta(microseconds=1))
        rates=[row for row in published.get(symbol,()) if int(row.get('fundingTime',0))==int(due.timestamp()*1000)]
        if len(rates)>1:
            raise EvidenceBlocked('funding_rate_identity_conflict')
        receipts=[row for row in report.get('events',()) if row.get('account_id')==account and row.get('symbol')==symbol and row.get('income_type')=='FUNDING_FEE' and row.get('strategy')=='hrs' and row.get('asset')=='USDT' and not row.get('reason') and row.get('tran_id') is not None and due<=datetime.fromisoformat(row['event_time_utc'])<next_due]
        if len(receipts)==1 and str(receipts[0]['tran_id']) not in used:
            used.add(str(receipts[0]['tran_id']))
            proof=dict(symbol=symbol,deadline=due,quantity=str(qty),resolution='income_receipt',income_identity=str(receipts[0]['tran_id']))
        elif rates and number(rates[0].get('fundingRate'),'funding_rate_invalid')==0:
            number(rates[0].get('markPrice'),'funding_zero_mark_missing',positive=True)
            proof=dict(symbol=symbol,deadline=due,quantity=str(qty),resolution='authoritative_zero_rate',published_rate=rates[0])
        else:
            raise EvidenceBlocked('funding_settlement_pending')
        proofs.append(proof)
    return proofs


def certify_reduce_only_lifecycle(account,symbol,current_quantity,fills,orders,signed_orders,start,as_of,*,coverage_complete):
    """Infer only a reducing order's owner from a fully proved unique position.

    Complete signed fills plus current inventory establish actual zero quantity
    at the lower bound. Every increasing fill requires an independent durable
    owner. An unowned reducing order additionally needs its authenticated venue
    reduceOnly flag, exact terminal executed quantity and unique owned inventory
    at every constituent fill. A ticker, review label or net subtotal is not an
    ownership proof.
    """
    if coverage_complete is not True:
        raise EvidenceBlocked('lifecycle_fill_coverage_missing')
    seen={}
    for row in fills:
        if row.get('account_id')!=account or row.get('symbol')!=symbol or not start<=aware(row['trade_time_utc'],'lifecycle_fill')<=as_of:
            raise EvidenceBlocked('lifecycle_fill_identity_or_range')
        key=str(row.get('trade_id'))
        if key in seen and seen[key]!=row:
            raise EvidenceBlocked('lifecycle_fill_conflict')
        seen[key]=row
    rows=sorted(seen.values(),key=lambda row:(row['trade_time_utc'],int(row['trade_id'])))
    def signed(row):
        if row.get('side') not in ('BUY','SELL'):
            raise EvidenceBlocked('lifecycle_fill_side_missing')
        return number(row['qty'],'lifecycle_fill_quantity',positive=True)*(1 if row['side']=='BUY' else -1)
    total=sum((signed(row) for row in rows),Decimal(0))
    if total!=number(current_quantity,'lifecycle_current_quantity'):
        raise EvidenceBlocked('lifecycle_zero_anchor_unproven')
    owners=order_owners(account,orders)
    balances={}
    proofs={}
    for row in rows:
        identity=str(row['order_id']);qty=signed(row)
        choices=owners.get((symbol,identity),set())
        if len(choices)>1 or None in choices:
            raise EvidenceBlocked('lifecycle_owner_conflict')
        if choices:
            owner=next(iter(choices))
        else:
            response=signed_orders.get(row['order_id']) or signed_orders.get(identity)
            active=[(owner,held) for owner,held in balances.items() if held!=0]
            if (not isinstance(response,dict) or str(response.get('orderId'))!=identity or response.get('symbol')!=symbol
                    or response.get('side')!=row['side'] or response.get('reduceOnly') is not True
                    or response.get('status')!='FILLED' or len(active)!=1 or active[0][1]*qty>=0 or abs(qty)>abs(active[0][1])):
                raise EvidenceBlocked('lifecycle_reduce_only_owner_unproven')
            executed=sum((abs(signed(item)) for item in rows if str(item['order_id'])==identity),Decimal(0))
            if executed!=number(response.get('executedQty'),'lifecycle_executed_quantity',positive=True) or executed>number(response.get('origQty'),'lifecycle_original_quantity',positive=True):
                raise EvidenceBlocked('lifecycle_terminal_fill_quantity_unproven')
            owner=active[0][0]
            old=proofs.get(identity)
            if old and old['strategy']!=owner:
                raise EvidenceBlocked('lifecycle_owner_changed_during_fill')
            proofs[identity]=dict(account_id=account,symbol=symbol,order_id=identity,strategy=owner,
                source_kind='certified_unique_inventory_reduce_only_close',certified_start_quantity='0',coverage_start=start,coverage_through=as_of,
                full_fill_ids=[str(item['trade_id']) for item in rows],prior_owned_order_ids=sorted({str(item['order_id']) for item in rows if item['trade_time_utc']<row['trade_time_utc'] and (symbol,str(item['order_id'])) in owners}),
                response={key:response.get(key) for key in ('orderId','symbol','side','status','reduceOnly','executedQty','origQty')})
        balances[owner]=balances.get(owner,Decimal(0))+qty
    return tuple(proofs.values())
