"""Actual HRS scoring/risk/exit orchestration from frozen point-in-time facts.

Historical decision inputs are required. Recorded chosen actions are never an
input. Entries use the manifest-bound execution model and its declared assumptions.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from strategies.hrs import core_decision as core
from strategies.hrs import exit_geometry as exits
from strategies.hrs.scoring_engine import ScoringEngine
from strategies.hrs.pattern import PatternRecognizer
from strategies.hrs.risk_budget import assess_entry, calculate_open_position_risk
from strategies.hrs.loss_gate import GateHistory, LossGateState, evaluate_loss_gate
from strategies.hrs.risk_ledger import (EquityCheckpoint, LedgerState, LedgerObservation,
    IncomeEvent, CashFlow, PositionMark, reconcile_ledger, LedgerNotReady)

from ..broker import PortfolioView
from ..models import Bar, DataBlocked, EquityPoint, OrderIntent, Position, parse_utc
from .base import PreparedContext

SNAPSHOT_REQUIRED_KEYS = ("as_of", "complete", "rows")
StateLoader = Callable[[datetime], Mapping[str, Optional[Mapping[str, Any]]]]
ZERO = Decimal("0")


class _Thresholds:
    """Frozen scan result implements the scorer's existing threshold protocol."""
    def __init__(self, raw, decision_at):
        self.__dict__.update(raw)
        self.computed_at = parse_utc(raw['computed_at'])
        if self.computed_at > decision_at:
            raise DataBlocked(['hrs_dynamic_thresholds_future'])
        required = ('funding_rate_short','funding_rate_long','oi_market_cap_short',
            'oi_market_cap_long','ema20_short','ema20_long','funding_rate_emm_long',
            'funding_rate_emm_short','oi_market_cap_emm','sample_count',
            'funding_sample_count','oi_market_cap_sample_count','ema_sample_count')
        if any(key not in raw for key in required):
            raise DataBlocked(['hrs_dynamic_thresholds_incomplete'])
    def is_valid(self, min_sample_size=None):
        minimum = 10 if min_sample_size is None else min_sample_size
        return self.funding_sample_count >= minimum and self.oi_market_cap_sample_count >= minimum


class HrsAdapter:
    strategy_id = 'hrs'
    requires_external_snapshot = True
    requires_attested_funding = True
    requires_decision_calendar = True
    execution_interval = '1h'
    extra_intervals = ()
    batch_bar_updates = True
    REQUIRED_STATE_TABLES = ('hrs_positions','hrs_blacklist','hrs_active_symbols')
    REQUIRED_INTERVALS = ('1h','4h')
    EXIT_OVERRIDE_FIELDS = frozenset(exits.mode_exit_parameters({},'standard'))

    def __init__(self, state_loader: StateLoader, *, exit_parameter_overrides=None,
                 experiment_id=None, execution_costs=None):
        self._exit_overrides={}
        if exit_parameter_overrides and (not isinstance(experiment_id,str) or not experiment_id.strip()):
            raise ValueError('exit_override_requires_experiment_identity')
        for mode,values in (exit_parameter_overrides or {}).items():
            if mode not in ('standard','lv_rm') or not isinstance(values,Mapping) or set(values)-self.EXIT_OVERRIDE_FIELDS:
                raise ValueError('unsupported_exit_override')
            normalized={key:Decimal(str(value)) for key,value in values.items()}
            for key,value in normalized.items():
                if not value.is_finite() or value<0 or (key!='atr_multiplier' and not key.endswith('_percent') and value==0):
                    raise ValueError('invalid_exit_override')
                if key.endswith('_percent') and (value>1 if key.startswith('target') else value>=1):
                    raise ValueError('invalid_exit_percentage')
            self._exit_overrides[mode]=normalized
        self.experiment_id=experiment_id
        costs=execution_costs or {}
        if set(costs)-{'fee_rate','slippage'}:
            raise ValueError('unsupported_execution_cost')
        self._execution_costs={key:Decimal(str(value)) for key,value in costs.items()}
        if any(not value.is_finite() or not ZERO<=value<1 for value in self._execution_costs.values()):
            raise ValueError('invalid_execution_cost')
        self._state_loader = state_loader
        self._state = {}
        self._bars = {}
        self._latest_bars = {}
        self._event_bar = None
        self._decision_event = None
        self._latest_snapshot = None
        self._runtime_ready = False
        self._last_decisions = {}
        self._pending = set()
        self._reservations = {}
        self._durable_margin_reservations = {}
        self._protected = set()
        self._protection = {}
        self._best = {}
        self._reviews = {}
        self._last_monitor = {}
        self._seq = 0
        self._income_seq = 0
        self._pending_income = []
        self._pending_flows = []
        self._pending_funding = {}
        self._known_funding = {}
        self._funding_marks = {}
        self._funding_gaps = []
        self._deferred_marks = []
        self._deferred_flows = []
        self._deferred_outcomes = {}
        self._reconciled_points = []
        self.audit = []

    def prepare(self, portal):
        start = portal.window_start
        raw_state = self._state_loader(start)
        universe = portal.universe()
        gaps, state = [], {}
        for name in self.REQUIRED_STATE_TABLES:
            snapshot, missing = _validate_snapshot(name,raw_state.get(name),parse_utc(start))
            gaps.extend(missing)
            if snapshot is not None:
                state[name] = snapshot
        for symbol in universe.symbols:
            for interval in self.REQUIRED_INTERVALS:
                if not self._has_bars(portal,symbol,interval):
                    gaps.append('kline_empty:'+symbol+':'+interval)
                try:
                    self._bars[(symbol,interval)] = list(portal.bars(symbol,interval,as_of=start))
                except DataBlocked:
                    self._bars[(symbol,interval)] = []
        if gaps:
            raise DataBlocked(gaps)
        self._state = state
        self._blacklisted = {row["symbol"] for row in state["hrs_blacklist"]["rows"] if row.get("symbol")}
        self._portal = portal
        self._start = start
        return PreparedContext(self.strategy_id,portal.as_of,universe,portal.config_version(),
            PortfolioView((),{},ZERO),{'symbols':list(universe.symbols),'state':state})

    @staticmethod
    def _has_bars(portal,symbol,interval):
        try:
            return bool(portal.bars(symbol,interval))
        except DataBlocked:
            return False

    def _bootstrap(self,ctx):
        if self._runtime_ready:
            return
        raw = self._portal.hrs_replay_state(self._start)
        try:
            self.config = raw['config']
            if raw['config_hashes'] != ctx.config_hashes.as_dict():
                raise DataBlocked(['hrs_effective_config_hash_mismatch'])
            self._allocation = _positive(raw['allocation'])
            self._account_equity = _positive(raw['account_equity'])
            self._policy = raw['entry_policy']
            self._market_guard_config = raw['market_circuit_breaker_config']
            self._runtime = dict(raw['runtime'])
            for key in ('signal_timestamps','pending_timeout_entries','pause_until',
                        'circuit_breakers','daily_open_count','daily_reset_date','consecutive_losses',
                        'drawdown_pause_until','stop_loss_monitor','market_cooldowns','total_pnl'):
                if key not in self._runtime:
                    raise DataBlocked(['hrs_runtime_missing:'+key])
            ledger = raw['ledger']
            self._segment = ledger['segment_id']
            self._units = _positive(ledger['units'])
            self._history_from = parse_utc(ledger['history_complete_from'])
            self._history = [EquityCheckpoint(self._segment,parse_utc(row['as_of']),
                _positive(row['equity']),_positive(row['units']),_positive(row['nav']))
                for row in ledger['history']]
            if not self._history or max(item.as_of for item in self._history) > self._start:
                raise DataBlocked(['hrs_bootstrap_nav_history_invalid'])
            self._allow_inception = ledger.get('allow_inception_segment') is True
            self._gate = (LossGateState.from_dict(ledger['gate_state'])
                          if ledger['gate_state'] is not None else None)
            self._scorer = ScoringEngine(self.config)
            self._recognizer = PatternRecognizer(self.config)
            reservations=raw['margin_reservations']
            if not isinstance(reservations,list):
                raise DataBlocked(['hrs_initial_margin_reservation_inventory_missing'])
            for reservation in reservations:
                if reservation['state']=='released':continue
                symbol=reservation['symbol'];terminal=reservation['terminal_order']
                quantity=_positive(reservation['quantity'])
                positions=[p for p in self._state['hrs_positions']['rows'] if p['symbol']==symbol]
                if (symbol in self._durable_margin_reservations or terminal.get('status')!='FILLED'
                        or terminal.get('symbol')!=symbol or _positive(terminal['executedQty'])!=quantity
                        or _positive(terminal['origQty'])!=quantity or len(positions)!=1
                        or _positive(positions[0]['entry_quantity'])!=quantity):
                    raise DataBlocked(['hrs_initial_margin_reservation_unproven:'+symbol])
                self._durable_margin_reservations[symbol]=dict(margin=_positive(reservation['margin']),
                    terminal=True,position_id=positions[0]['position_id'])
        except (KeyError,TypeError,ValueError,ArithmeticError) as exc:
            raise DataBlocked(['hrs_bootstrap_invalid:'+str(exc)]) from exc
        self._runtime_ready = True

    def initial_positions(self,ctx):
        """Initial state needs fill-cost and protection facts, not merely DB rows."""
        self._bootstrap(ctx)
        result=[]
        self._initial_orders=[]
        self._closed_pnl={}
        for row in self._state['hrs_positions']['rows']:
            try:
                symbol=row['symbol']
                if symbol not in ctx.universe.symbols:
                    raise ValueError('position_outside_universe')
                opened=parse_utc(row['opened_at'])
                mark_at=parse_utc(row['mark_observed_at'])
                if opened>self._start or not timedelta(0)<=self._start-mark_at<=timedelta(seconds=120):
                    raise ValueError('position_time_invalid')
                direction=row['direction'].upper()
                if direction not in ('SHORT','LONG'):
                    raise ValueError('position_direction_invalid')
                entry,current=_positive(row['entry_quantity']),_positive(row['remaining_quantity'])
                if current>entry or type(row['target1_reached']) is not bool or type(row['target2_reached']) is not bool:
                    raise ValueError('position_quantity_or_flags_invalid')
                ratios=tuple(Decimal(str(value)) for value in row['exit_ratios'])
                if len(ratios)!=2 or any(not value.is_finite() or value<0 for value in ratios) or ratios[0]<=0 or sum(ratios)>1:
                    raise ValueError('exit_ratios_invalid')
                params=dict(row['exit_parameters'])
                for key in exits.mode_exit_parameters(self.config,row['entry_mode']):
                    if key=='target2_close_percent' and Decimal(str(params[key]))==0:
                        if ratios!=(Decimal(1),ZERO) or Decimal(str(params['target1_close_percent']))!=1:
                            raise ValueError('initial_full_tp1_params_invalid')
                    else:
                        _positive(params[key])
                protective={key:_positive(row[key]) for key in ('stop_loss_price','tp1_price','tp2_price')}
                position=Position(position_id=row['position_id'],symbol=symbol,direction=direction,
                    entry_price=_positive(row['entry_price']),entry_quantity=entry,current_quantity=current,
                    atr=_positive(row['atr']),grade='',entry_time=opened,entry_mode=row['entry_mode'],
                    exit_ratios=ratios,exit_parameters=params,tp1_hit=row['target1_reached'],tp2_hit=row['target2_reached'],
                    **protective)
                restored={'position':position,'mark_price':_positive(row['mark_price']),'mark_observed_at':mark_at}
                for key in ('entry_fee_remaining','entry_slippage_remaining','funding_accrued','mae','mfe'):
                    value=Decimal(str(row[key]))
                    if not value.is_finite() or (key in ('entry_fee_remaining','entry_slippage_remaining') and value>0):
                        raise ValueError('opening_cost_invalid')
                    restored[key]=value
                self._closed_pnl[position.position_id]=Decimal(str(row['net_pnl_realized_to_date']))
                self._protection[symbol]=protective
                self._protected.add(position.position_id)
                self._best[position.position_id]=_positive(row['best_price'])
                self._reviews[position.position_id]=parse_utc(row['next_review_at'])
                self._initial_orders.append(self._intent(symbol,direction,current,'STOP_LOSS',order_type='STOP',
                    trigger_price=protective['stop_loss_price'],reduce_only=True))
                filters=self._portal.symbol_filters(symbol)
                for index,role in enumerate(('tp1','tp2')):
                    quantity=(entry*ratios[index]//filters['step_size'])*filters['step_size']
                    if quantity>0 and not (position.tp1_hit if index==0 else position.tp2_hit):
                        self._initial_orders.append(self._intent(symbol,direction,quantity,role.upper(),
                            order_type='TAKE_PROFIT',trigger_price=protective[role+'_price'],reduce_only=True,close_quantity=quantity))
                result.append(restored)
            except (KeyError,TypeError,ValueError,ArithmeticError) as exc:
                raise DataBlocked(['hrs_initial_position_execution_state_missing:'+str(exc)]) from exc
        return tuple(result)

    def initialize_portfolio(self,ctx):
        equity=self._marked_equity(ctx.portfolio,as_of=self._start)
        current=next((sample for sample in self._history if sample.as_of==self._start),None)
        if current is None or current.equity!=equity or current.units!=self._units:
            raise DataBlocked(['hrs_opening_equity_checkpoint_mismatch'])
        self._ledger_state=LedgerState(self._segment,self._history_from,self._start,
            ctx.portfolio.realized_equity,ZERO,self._units,equity,equity/self._units)

    def cash_flow_events(self,ctx):
        return self._portal.hrs_cash_flows()

    @staticmethod
    def _marked_equity(portfolio, *, as_of=None):
        equity=portfolio.realized_equity
        for position in portfolio.positions:
            if position.is_open():
                mark=portfolio.last_prices.get(position.symbol)
                if mark is None:
                    raise DataBlocked(['hrs_position_mark_missing:'+position.symbol])
                marked_at=portfolio.last_price_times.get(position.symbol)
                if as_of is not None and (marked_at is None or not timedelta(0)<=as_of-marked_at<=timedelta(seconds=120)):
                    raise DataBlocked(['hrs_stale_position_mark:'+position.symbol])
                equity+=position.current_quantity*(mark-position.entry_price)*(1 if position.direction=='LONG' else -1)
        return equity

    def on_cash_flow(self,row,ctx):
        if row['segment_id']!=self._segment:
            raise DataBlocked(['hrs_cashflow_segment_mismatch'])
        amount=Decimal(str(row['amount']))
        if not amount.is_finite():
            raise DataBlocked(['hrs_cashflow_amount_invalid'])
        if self._pending_funding or self._deferred_marks:
            self._deferred_flows.append((row,ctx.portfolio,tuple(self._pending_funding)))
            return
        nav=self._marked_equity(ctx.portfolio,as_of=parse_utc(row["occurred_at"]))/self._units
        self._units+=amount/nav
        if self._units<=0:
            raise DataBlocked(['hrs_cashflow_units_nonpositive'])
        self._pending_flows.append(CashFlow(row['event_id'],amount,parse_utc(row['occurred_at']),self._segment))

    def initial_orders(self,ctx):
        return tuple(self._initial_orders)

    def configure_execution(self,ctx):
        self._bootstrap(ctx)
        entry_type = self.config.get('trading',{}).get('order_type',{}).get('entry','LIMIT')
        if entry_type == 'LIMIT':
            model = self._portal.hrs_replay_state(self._start).get('execution_model')
            if not isinstance(model,dict) or model.get('version') != 'hrs_1m_volume_v1':
                raise DataBlocked(['hrs_entry_execution_resolution_unsupported'])
            self._execution_model = model
            self.execution_interval = '1m'
            self.extra_intervals = ('1m',)
            for symbol in ctx.universe.symbols:
                bars = self._portal.bars(symbol,'1m')
                selected = [bar for bar in bars if self._start < bar.close_time <= self._portal.as_of]
                expected = int((self._portal.as_of-self._start).total_seconds()/60)
                if (len(selected)!=expected or not selected or selected[0].open_time!=self._start
                        or selected[-1].close_time!=self._portal.as_of
                        or any(right.open_time!=left.close_time for left,right in zip(selected,selected[1:]))):
                    raise DataBlocked(['hrs_execution_subbar_coverage_gap:'+symbol])
        elif entry_type != 'MARKET':
            raise DataBlocked(['hrs_entry_order_type_unsupported'])
        model=self._portal.hrs_replay_state(self._start).get('monitoring_model')
        try:
            if not isinstance(model,dict) or model.get('version')!='hrs_cycle_monitor_v1':
                raise ValueError('version_missing_or_unsupported')
            cadence=model['cadence_seconds']
            configured=self.config.get('cycle',{}).get('check_interval_seconds',3600)
            anchor=parse_utc(model['anchor_at'])
            if type(cadence) is not int or cadence<=0 or cadence%60 or cadence!=configured:
                raise ValueError('cadence_config_or_resolution_mismatch')
            if anchor.second or anchor.microsecond:
                raise ValueError('anchor_not_on_execution_boundary')
            if self.execution_interval=='1h' and (cadence%3600 or anchor.minute):
                raise ValueError('cadence_requires_one_minute_execution')
        except (KeyError,TypeError,ValueError) as exc:
            raise DataBlocked(['hrs_monitoring_model_invalid:'+str(exc)]) from exc
        self._monitor_cadence=cadence
        self._monitor_anchor=anchor
        self._record(self._start,'hrs_execution_assumptions',dict(
            entry_model='hrs_1m_volume_v1' if entry_type=='LIMIT' else 'next_bar_open_market_v1',
            monitoring_model='hrs_cycle_monitor_v1',cadence_seconds=cadence,
            funding_model='economic_exposure_receipt_reconciliation_v1',
            software_close_model='observed_quote_market_proxy_v1',
            configured_close_type=self.config.get('trading',{}).get('order_type',{}).get('close','LIMIT'),
            exact_exchange_parity=False,
            limitations=['Software closes execute at the observed quote with modeled costs; live LIMIT close matching, retry timing and order-book priority are not reproduced.']))

    def on_bar(self,bar,ctx):
        key = (bar.symbol,bar.interval)
        rows = self._bars.setdefault(key,[])
        if not rows or bar.close_time > rows[-1].close_time:
            rows.append(bar)
        self._latest_bars[bar.symbol+'|'+bar.interval] = bar
        self._event_bar = bar if bar.interval == '1h' else None
        self._portfolio = ctx.portfolio

    def decision_events(self,ctx):
        return self._portal.hrs_decision_calendar(ctx.universe.symbols)

    def on_decision(self,event,ctx):
        self._decision_event=event
        self._portfolio=ctx.portfolio

    def on_snapshot(self,snap,ctx):
        self._latest_snapshot = snap

    def _record(self,at,kind,payload):
        self.audit.append({'ts':at,'type':kind,'payload':payload})

    def _intent(self,symbol,direction,quantity,reason,**kwargs):
        self._seq += 1
        return OrderIntent(self.strategy_id,symbol,self._seq,direction.upper(),
                           kwargs.pop('order_type','MARKET'),quantity,reason,**kwargs)

    def intents(self,ctx):
        try:
            return self._intents(ctx)
        except (KeyError,TypeError,ValueError,ArithmeticError,IndexError) as exc:
            raise DataBlocked(['hrs_decision_inputs_invalid:'+str(exc)]) from exc

    def _intents(self,ctx):
        event=self._decision_event
        self._decision_event=None
        if event is not None:
            now=parse_utc(event['available_at'])
            symbol=event['symbol']
            decision_at=parse_utc(event['decision_at'])
            identity=(decision_at,now)
            history=self._bars.get((symbol,'1h'),[])
            if not history:
                raise DataBlocked(['hrs_decision_kline_history_missing:'+symbol])
        else:
            # Direct adapter contract tests can consume an explicit bar. The
            # unified HRS engine exclusively drives the attested calendar.
            bar=self._event_bar
            if bar is None:
                return ()
            now,symbol=bar.close_time,bar.symbol
            decision_at=now
            identity=now
        if self._last_decisions.get(symbol)==identity:
            return ()
        self._bootstrap(ctx)
        raw=self._portal.hrs_inputs(symbol,now,decision_at=decision_at)
        if 'config_hashes' not in raw:
            raise DataBlocked(['hrs_decision_config_identity_missing'])
        if raw['config_hashes']!=ctx.config_hashes.as_dict():
            raise DataBlocked(['hrs_config_change_requires_partition'])
        try:
            scan, market, risk = raw['candidate_scan'],raw['market'],raw['risk']
            if scan['complete'] is not True or parse_utc(scan['as_of']) > now:
                raise DataBlocked(['hrs_candidate_scan_incomplete'])
            for key in ('short_candidates','long_candidates','lv_rm_symbols','dynamic_thresholds'):
                if key not in scan:
                    raise DataBlocked(['hrs_candidate_scan_missing:'+key])
            if not (parse_utc(market['source_observed_at']) <= now < parse_utc(market['expires_at'])):
                raise DataBlocked(['hrs_market_inputs_stale:'+symbol])
            for key in ('oi_usd','funding_rate','volume_24h','entry_price','market_cap','has_market_cap',
                        'market_cap_source'):
                if key not in market:
                    raise DataBlocked(['hrs_market_inputs_missing:'+key])
            for key in ('oi_usd','funding_rate','volume_24h','entry_price','market_cap'):
                value=Decimal(str(market[key]))
                if not value.is_finite() or (key in ('oi_usd','volume_24h','market_cap') and value<0) or (key=='entry_price' and value<=0):
                    raise DataBlocked(['hrs_market_input_invalid:'+key])
            for key in ('pool_index','pool_index_12h',
                        'allocation','entry_policy','account_equity_excluding_hrs'):
                if key not in risk:
                    raise DataBlocked(['hrs_risk_inputs_missing:'+key])
        except (KeyError,TypeError,ValueError) as exc:
            raise DataBlocked(['hrs_decision_inputs_invalid:'+str(exc)]) from exc
        allocation=Decimal(str(risk['allocation']))
        if not allocation.is_finite() or allocation<0:
            raise DataBlocked(['hrs_allocation_invalid'])
        if allocation<self._allocation:
            leverage=_positive(self.config.get('trading',{}).get('leverage',2))
            occupied=sum((p.current_quantity*p.entry_price/leverage for p in ctx.portfolio.positions if p.is_open()),ZERO)
            if occupied>allocation+Decimal('0.00000001'):
                raise DataBlocked(['hrs_allocation_cut_liquidation_not_wired'])
        self._allocation=allocation
        self._policy=risk['entry_policy']
        other_equity=Decimal(str(risk['account_equity_excluding_hrs']))
        if not other_equity.is_finite():
            raise DataBlocked(['hrs_other_account_equity_invalid'])
        self._account_equity=_positive(other_equity+self._marked_equity(ctx.portfolio,as_of=now))
        score_at=parse_utc(raw.get('score_available_at',raw['decision_at']))
        if not decision_at<=score_at<=now or parse_utc(scan['as_of'])>score_at:
            raise DataBlocked(['hrs_score_input_availability_invalid:'+symbol])
        klines = [_kline(item) for item in self._bars[(symbol,'1h')] if item.close_time<=score_at]
        klines4 = [_kline(item) for item in self._bars[(symbol,'4h')] if item.close_time<=score_at]
        if 'analysis_klines_1h' in raw or 'analysis_klines_4h' in raw:
            if not isinstance(raw.get('analysis_klines_1h'),list) or not isinstance(raw.get('analysis_klines_4h'),list):
                raise DataBlocked(['hrs_captured_score_history_incomplete:'+symbol])
            klines=raw['analysis_klines_1h']
            klines4=raw['analysis_klines_4h']
        if not klines4:
            raise DataBlocked(['hrs_4h_warmup_missing:'+symbol])
        self._scorer.set_dynamic_thresholds(_Thresholds(scan['dynamic_thresholds'],score_at)
            if scan['dynamic_thresholds'] is not None else None)
        has_cap = (market['has_market_cap'] is True and
                   market['market_cap_source'] in ('coingecko','coinmarketcap','cache_hit') and
                   Decimal(str(market['market_cap'])) > ZERO)
        ratio = float(Decimal(str(market['oi_usd'])) / Decimal(str(market['market_cap']))) if has_cap else 0.
        directions = tuple(d for d in ('short','long') if symbol in scan[d+'_candidates'])
        try:
            arguments = dict(symbol=symbol,klines_1h=klines,klines_4h=klines4,
                oi_market_cap_ratio=ratio,funding_rate=float(market['funding_rate']),
                has_market_cap=has_cap,decision_at=score_at,config=self.config,scoring_engine=self._scorer)
            if directions:
                analysis = core.analyze_standard(**arguments,directions=directions,pattern_recognizer=self._recognizer)
                origin = 'nonempty_pool'
            elif self.config.get('lv_rm',{}).get('enabled',True) and symbol in scan['lv_rm_symbols']:
                analysis = core.analyze_lv_rm(**arguments)
                origin = ('nonempty_pool' if scan['short_candidates'] or scan['long_candidates'] else 'empty_pool')
            else:
                analysis = {'short':None,'long':None}
                origin = 'nonempty_pool'
        except core.DecisionInputsUnavailable as exc:
            raise DataBlocked(['hrs_'+str(exc)+':'+symbol]) from exc
        self._last_decisions[symbol] = identity
        self._portfolio = ctx.portfolio
        result = self._exit_intents(symbol,now,analysis,ctx,market)
        candidates = []
        for direction in ('short','long'):
            scored_data = analysis[direction]
            if scored_data is None:
                continue
            scored = scored_data.get('_scoring_result')
            self._record(now,'hrs_score',{'symbol':symbol,'direction':direction,'inputs':raw,
                'result':scored.to_dict() if scored else scored_data,'should_entry':scored_data['should_entry']})
            if not scored_data['should_entry']:
                self._runtime['signal_timestamps'].pop(symbol+':'+direction,None)
            variants = ([scored] if scored_data['should_entry'] else [])
            if scored is not None:
                for shadow in scored.shadows:
                    eligible = self._scorer.should_entry(shadow)
                    self._record(now,'hrs_shadow',dict(symbol=symbol,direction=direction,result=shadow.to_dict(),should_entry=eligible))
                    if eligible:
                        variants.append(shadow)
            for variant in variants:
                variant_origin = origin if variant is scored else 'shadow_scoring'
                signal = dict(symbol=symbol,direction=direction,score=variant.total_score,
                    technical_score=variant.technical_score,sentiment_score=variant.sentiment_score,
                    entry_mode=variant.entry_mode,signal_origin=variant_origin)
                if core.is_shadow_entry_mode(self.config,variant.entry_mode,variant_origin):
                    if variant is scored:
                        self._record(now,'hrs_shadow',signal)
                    continue
                candidates.append(signal)
                if variant is scored:
                    self._runtime['signal_timestamps'].setdefault(symbol+':'+direction,now.isoformat())
        accepted,rejected = core.resolve_signal_conflicts(
            [s for s in candidates if s['direction']=='short'],[s for s in candidates if s['direction']=='long'])
        for rejected_signal in rejected:
            self._record(now,'hrs_reject',dict(rejected_signal,reason='conflict'))
        for signal in accepted:
            denial = self._entry_denial(signal,now,ctx,market,risk,klines)
            if denial:
                self._record(now,'hrs_reject',dict(signal,reason=denial))
                continue
            mode = signal['entry_mode']
            params = self.exit_parameters(mode)
            atr = exits.calculate_atr(klines,self.config.get('atr',{}).get('period',14))
            if atr <= 0:
                raise DataBlocked(['hrs_atr_unavailable:'+symbol])
            filters = self._portal.symbol_filters(symbol)
            if filters.get('min_qty') is None:
                raise DataBlocked(['hrs_exchange_min_qty_missing:'+symbol])
            price = _positive(market['entry_price'])
            stop = exits.stop_loss_price(signal['direction'],price,atr,filters['tick_size'],params,self.config)
            targets = tuple(exits.take_profit_price(signal['direction'],role,price,atr,
                filters['tick_size'],params,self.config) for role in ('tp1','tp2'))
            fee,slip = self._pressure()
            positions = self._risk_positions(ctx.portfolio)
            open_risk = calculate_open_position_risk(positions,fee_rate=fee,pressure_slippage=slip)
            if open_risk is None:
                raise DataBlocked(['hrs_open_position_risk_unknown'])
            open_risk += sum((item["risk"] for item in self._reservations.values()),ZERO)
            leverage = Decimal(str(self.config.get('trading',{}).get('leverage',2)))
            occupied = sum((p.current_quantity*p.entry_price/leverage for p in ctx.portfolio.positions if p.is_open()),ZERO)
            policy = self._policy
            effective = min(self._allocation*Decimal(str(policy['multiplier'])),
                self._account_equity*Decimal(str(policy['ratio_cap'])),Decimal(str(policy['entry_ceiling'])))
            budget = self.config.get('trading',{}).get('risk_budget',{})
            decision = assess_entry(allocation=self._allocation,entry_price=price,stop_price=stop,
                direction=signal['direction'],existing_open_risk=open_risk,leverage=leverage,
                remaining_margin=effective-occupied,qty_step=filters['step_size'],min_qty=filters['min_qty'],
                min_notional=filters['min_notional'],fee_rate=fee,pressure_slippage=slip,
                single_risk_ratio=min(Decimal('.01'),Decimal(str(budget.get('single_loss_ratio','.01')))),
                portfolio_risk_ratio=min(Decimal('.03'),Decimal(str(budget.get('portfolio_loss_ratio','.03')))),
                take_profit_ratios=tuple(Decimal(str(params[k])) for k in ('target1_close_percent','target2_close_percent')),
                take_profit_prices=targets)
            self._record(now,'hrs_budget',dict(symbol=symbol,direction=signal['direction'],entry_mode=mode,
                experiment_id=self.experiment_id,allowed=decision.allowed,reason=decision.reason,
                quantity=str(decision.quantity),worst_loss=str(decision.worst_loss),risk_limit=str(decision.risk_limit),
                allocation=str(self._allocation),planned_stop=str(stop),geometry_reference_price=str(price),
                pressure_fee_rate=str(fee),pressure_slippage=str(slip),exit_ratios=list(map(str,decision.exit_ratios))))
            if not decision.allowed:
                continue
            reservation_denial=self._reservation_denial(symbol,decision.quantity,price,ctx,now,risk,effective)
            if reservation_denial:
                self._record(now,'hrs_reject',dict(signal,reason=reservation_denial))
                continue
            entry_type = self.config.get('trading',{}).get('order_type',{}).get('entry','LIMIT')
            extra = {}
            if entry_type == 'LIMIT':
                model = getattr(self,'_execution_model',None)
                if model is None:
                    raise DataBlocked(['hrs_entry_execution_resolution_unsupported:'+symbol])
                timeout = self.config.get('trading',{}).get('entry_timeout',{}).get('minutes',15)
                if model.get('timeout_minutes') != timeout:
                    raise DataBlocked(['hrs_execution_timeout_config_mismatch'])
                extra = dict(order_type='LIMIT',limit_price=price,trigger_price=price,execution_model=model,
                             expires_at=now+timedelta(minutes=timeout))
            elif entry_type != 'MARKET':
                raise DataBlocked(['hrs_entry_order_type_unsupported'])
            result.append(self._intent(symbol,signal['direction'],decision.quantity,'ENTRY',atr=Decimal(str(atr)),
                entry_mode=mode,geometry_reference_price=price,exit_ratios=decision.exit_ratios,exit_parameters=params,**extra))
            self._durable_margin_reservations[symbol]=dict(margin=decision.quantity*price/leverage,terminal=False,position_id=None)
            self._pending.add(symbol)
            self._reservations[symbol]={'risk':decision.worst_loss,'margin':decision.quantity*price/leverage,
                                        'direction':signal['direction']}
        return tuple(result)

    def _reservation_denial(self,symbol,quantity,price,ctx,now,risk,effective):
        """The live final store rejects after sizing; it never shrinks the order."""
        try:
            occupancy=risk['account_occupancy']
            observed=parse_utc(occupancy['observed_at'])
            if (occupancy.get('version')!='hrs_account_occupancy_v1'
                    or not timedelta(0)<=now-observed<=timedelta(seconds=120)
                    or not isinstance(occupancy['other_positions'],list)):
                raise ValueError('missing_or_stale_inventory')
            other_margin=ZERO;other_symbols=set()
            for row in occupancy['other_positions']:
                amount=Decimal(str(row['positionAmt']))
                if not amount.is_finite():raise ValueError('nonfinite_quantity')
                if amount==ZERO:continue
                if (not row['strategy_id'] or row['strategy_id']=='hrs' or row['symbol'] in other_symbols
                        or not re.fullmatch('[0-9a-f]{64}',row['ownership_evidence_sha256'])):
                    raise ValueError('owner_missing_or_duplicate')
                other_symbols.add(row['symbol'])
                other_margin+=abs(amount)*_positive(row['markPrice'])/_positive(row['leverage'])
        except (KeyError,TypeError,ValueError,ArithmeticError) as exc:
            raise DataBlocked(['hrs_account_occupancy_unproven:'+str(exc)]) from exc
        current_symbols={p.symbol for p in ctx.portfolio.positions if p.is_open()}
        if current_symbols & other_symbols:
            raise DataBlocked(['hrs_account_occupancy_owner_conflict'])
        occupied=current_symbols|other_symbols|set(self._durable_margin_reservations)
        maximum=int(self.config.get('trading',{}).get('max_positions',3))
        if maximum<=0:raise DataBlocked(['hrs_max_positions_invalid'])
        if len(occupied|{symbol})>maximum:
            return 'position_slots_include_account_and_reservations'
        leverage=_positive(self.config.get('trading',{}).get('leverage',2))
        self._marked_equity(ctx.portfolio,as_of=now)  # Actual mark freshness, never now-stamped cache.
        hrs_margin=sum((p.current_quantity*ctx.portfolio.last_prices[p.symbol]/leverage for p in ctx.portfolio.positions if p.is_open()),ZERO)
        retained=sum((r['margin'] for r in self._durable_margin_reservations.values()),ZERO)
        requested=quantity*price/leverage
        self._record(now,'hrs_account_reservation',dict(symbol=symbol,hrs_marked_margin=str(hrs_margin),
            other_margin=str(other_margin),retained_margin=str(retained),requested_margin=str(requested),
            effective_cap=str(effective),occupied_symbols=sorted(occupied),source_observed_at=observed.isoformat()))
        if hrs_margin+other_margin+retained+requested>effective:
            return 'entry_ceiling_includes_account_and_durable_reservations'
        return None

    def _release_margin_reservations(self,ctx,at):
        held={p.symbol for p in ctx.portfolio.positions if p.is_open()}
        pending=ctx.portfolio.pending_entry_symbols
        working=ctx.portfolio.working_exit_symbols
        for symbol,reservation in tuple(self._durable_margin_reservations.items()):
            if reservation['terminal'] and symbol not in held|set(pending)|set(working):
                self._durable_margin_reservations.pop(symbol)
                self._record(at,'hrs_margin_reservation_released',dict(symbol=symbol,
                    margin=str(reservation['margin']),reason='proven_virtual_flat_without_orders'))

    def exit_parameters(self,entry_mode):
        """Experiment overlays affect new entries, never captured source config."""
        parameters=exits.mode_exit_parameters(self.config,entry_mode)
        parameters.update(self._exit_overrides.get('lv_rm' if entry_mode=='lv_rm' else 'standard',{}))
        first,second=(Decimal(str(parameters[key])) for key in ('target1_close_percent','target2_close_percent'))
        if not ((first==1 and second==0) or (ZERO<first<1 and ZERO<second<1 and first+second<1)):
            raise ValueError('invalid_exit_ratios')
        return parameters

    def equity_checkpoints(self):
        """Read-only NAV history for the qualified experiment report."""
        return tuple(sorted(getattr(self,'_history',()),key=lambda sample:sample.as_of))

    def _pressure(self):
        trading = self.config.get('trading',{})
        budget = trading.get('risk_budget',{})
        return (max(Decimal(str(trading.get('fee_rate',0))),Decimal(str(budget.get('fee_rate_pressure','.0005'))),
                    self._execution_costs.get('fee_rate',ZERO)),
                max(Decimal('.02'),Decimal(str(budget.get('slippage_pressure','.02'))),
                    self._execution_costs.get('slippage',ZERO)))

    def _risk_positions(self,portfolio):
        result={}
        for p in portfolio.positions:
            if p.is_open():
                protective=self._protection.get(p.symbol,{})
                result[p.symbol]=dict(remaining_quantity=p.current_quantity,entry_price=p.entry_price,
                    risk_stop_price=protective.get('stop_loss_price'),direction=p.direction.lower(),
                    algo_ids={'sl':p.position_id if protective else None})
        return result

    def _entry_denial(self,signal,now,ctx,market,risk,klines):
        symbol,direction = signal['symbol'],signal['direction']
        if self._pending_funding or self._deferred_marks:
            return 'funding_pending'
        if symbol in self._pending or any(p.symbol==symbol and p.is_open() for p in ctx.portfolio.positions):
            return 'position_or_entry_exists'
        if self._policy.get('mode') != 'reduced' or self._policy.get('allow_add') is not False:
            return 'risk_policy_not_reduced'
        try:
            bounds=[]
            for key in ('effective_at','expires_at'):
                value=self._policy[key]
                instant=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
                if not isinstance(instant,datetime) or instant.tzinfo is None or instant.utcoffset() is None:
                    raise ValueError('timezone_missing')
                bounds.append(parse_utc(instant))
            effective,expires=bounds
            if expires<=effective:raise ValueError('invalid_interval')
        except (KeyError,TypeError,ValueError) as exc:
            raise DataBlocked(['hrs_entry_policy_timebounds_missing_or_invalid:'+str(exc)]) from exc
        if now<effective:return 'risk_policy_not_effective'
        if now>=expires:return 'risk_policy_expired'
        gate_denial=self._loss_gate_denial(now,ctx)
        if gate_denial:
            return gate_denial
        if self._runtime['pending_timeout_entries']:
            return 'pending_timeout_entry'
        if symbol in self._blacklisted:
            return 'blacklisted'
        monitor=self._runtime['stop_loss_monitor'].get(symbol)
        if monitor:
            if now <= parse_utc(monitor['monitor_until']):
                if abs(Decimal(str(market['entry_price']))/Decimal(str(monitor['entry_price']))-1) >= Decimal(str(self.config.get('trading',{}).get('blacklist',{}).get('reverse_surge_percent','.05'))):
                    self._blacklisted.add(symbol)
                    return 'blacklisted_after_loss'
            else:
                self._runtime['stop_loss_monitor'].pop(symbol,None)
        cb = self.config.get('circuit_breaker',{})
        existing_cb=self._runtime['circuit_breakers'].get(symbol)
        if existing_cb is not None and now < parse_utc(existing_cb['expires_at']):
            return 'symbol_circuit_breaker'
        change=core.price_change_1h(klines)
        if cb.get('enabled',True) and change is not None and abs(change)>=cb.get('price_change_threshold',.30):
            self._runtime['circuit_breakers'][symbol]={'triggered_at':now.isoformat(),
                'expires_at':(now+timedelta(minutes=cb.get('duration_minutes',60))).isoformat(),'price_change':change}
            return 'symbol_circuit_breaker'
        if self._market_guard_config is not None:
            cooldowns={key:parse_utc(value) for key,value in self._runtime['market_cooldowns'].items()}
            allow,level,updated=core.evaluate_market_guard(self._market_guard_config,direction,symbol,change,
                risk['pool_index'],risk['pool_index_12h'],now,cooldowns)
            self._runtime['market_cooldowns']={key:value.isoformat() for key,value in updated.items()}
            if not allow:
                return 'market_circuit_breaker:'+level
        pause=self._runtime['pause_until']
        if pause is not None:
            if now < parse_utc(pause):
                return 'paused'
            self._runtime['pause_until']=None
            self._runtime['consecutive_losses']=0
        drawdown_pause=self._runtime['drawdown_pause_until']
        if drawdown_pause is not None and now < parse_utc(drawdown_pause):
            return 'drawdown_paused'
        for key in ('exchange_position_clear','claims_clear'):
            if key not in risk:
                raise DataBlocked(['hrs_entry_stage_proof_missing:'+key])
        if not risk['exchange_position_clear'] or not risk['claims_clear']:
            return 'exchange_position_or_claim_conflict'
        if 'pre_entry_oi_usd' not in market:
            raise DataBlocked(['hrs_entry_stage_proof_missing:pre_entry_oi_usd'])
        if Decimal(str(market['pre_entry_oi_usd'])) < Decimal(str(self.config.get('candidate_pool',{}).get('liquidity',{}).get('min_oi_usd',10000000))):
            return 'oi_degraded'
        threshold=Decimal(str(self.config.get('trading',{}).get('abandon',{}).get('funding_rate_reversal_threshold',0)))
        if 'pre_entry_funding_rate' not in market:
            raise DataBlocked(['hrs_entry_stage_proof_missing:pre_entry_funding_rate'])
        funding=Decimal(str(market['pre_entry_funding_rate']))
        if (direction=='short' and funding <= threshold) or (direction=='long' and funding >= threshold):
            return 'funding_reversed'
        key=symbol+':'+direction
        timestamps=self._runtime['signal_timestamps']
        first=parse_utc(timestamps.setdefault(key,now.isoformat()))
        timeout=self.config.get('trading',{}).get('abandon',{}).get('timeout_bars',6)
        if now-first >= timedelta(hours=timeout):
            timestamps.pop(key,None)
            return 'signal_timeout'
        counts=self._runtime['daily_open_count']
        if self._runtime['daily_reset_date'] != now.date().isoformat():
            counts={'short':0,'long':0}
            self._runtime['daily_open_count']=counts
            self._runtime['daily_reset_date']=now.date().isoformat()
        trading=self.config.get('trading',{})
        reserved_directions=[item['direction'] for item in self._reservations.values()]
        if sum(counts.values())+len(reserved_directions)>=trading.get('max_daily_positions',3) or counts[direction]+reserved_directions.count(direction)>=trading.get('max_daily_same_direction',2):
            return 'daily_position_limit'
        return None

    def _loss_gate_denial(self,now,ctx):
        equity=self._marked_equity(ctx.portfolio,as_of=now)
        current=EquityCheckpoint(self._segment,now,equity,self._units,equity/self._units)
        self._history=[p for p in self._history if p.as_of!=now]+[current]
        gate=evaluate_loss_gate(self._gate,current,GateHistory(tuple(self._history),self._history_from,now,True),
                                allow_inception_segment=self._allow_inception)
        self._gate=gate.state
        if not gate.allow_new_risk:
            return 'loss_gate:'+','.join(gate.reasons)
        return None

    def on_entry_fill(self,position,ctx):
        self._portfolio=ctx.portfolio
        if position.position_id in self._protected:
            return ()
        if not position.entry_complete:
            return ()
        self._pending.discard(position.symbol)
        self._reservations.pop(position.symbol,None)
        if position.symbol in self._durable_margin_reservations:
            self._durable_margin_reservations[position.symbol].update(terminal=True,position_id=position.position_id)
        if position.exit_parameters is None or position.exit_ratios is None or position.atr<=0:
            raise DataBlocked(['hrs_filled_entry_geometry_missing:'+position.symbol])
        params=position.exit_parameters
        reference=position.geometry_reference_price or position.entry_price
        filters=self._portal.symbol_filters(position.symbol)
        direction=position.direction.lower()
        self._protection[position.symbol]={'stop_loss_price':exits.stop_loss_price(direction,reference,
            position.atr,filters['tick_size'],params,self.config),
            'tp1_price':exits.take_profit_price(direction,'tp1',reference,position.atr,filters['tick_size'],params,self.config),
            'tp2_price':exits.take_profit_price(direction,'tp2',reference,position.atr,filters['tick_size'],params,self.config)}
        self._protected.add(position.position_id)
        self._best[position.position_id]=position.entry_price
        self._reviews[position.position_id]=position.entry_time+timedelta(hours=float(params['max_holding_hours']))
        self._runtime['daily_open_count'][direction]+=1
        result=[self._intent(position.symbol,direction,position.current_quantity,'STOP_LOSS',order_type='STOP',
            trigger_price=self._protection[position.symbol]['stop_loss_price'],reduce_only=True)]
        for index,role in enumerate(('tp1','tp2')):
            ratio=position.exit_ratios[index]
            quantity=(position.entry_quantity*ratio//filters['step_size'])*filters['step_size']
            if quantity>ZERO:
                result.append(self._intent(position.symbol,direction,quantity,role.upper(),order_type='TAKE_PROFIT',
                    trigger_price=self._protection[position.symbol][role+'_price'],reduce_only=True,close_quantity=quantity))
        return tuple(result)

    def on_execution_bar(self,bar,ctx):
        """Poll trailing state only on the declared, versioned monitor schedule.

        Minute-close quotes are a stated sampling model, not intraminute peaks.
        New native protection becomes active after this close for future bars.
        """
        now=bar.close_time
        elapsed=(now-self._monitor_anchor).total_seconds()
        if elapsed<0 or elapsed%self._monitor_cadence or self._last_monitor.get(bar.symbol)==now:
            return ()
        self._last_monitor[bar.symbol]=now
        result=[]
        for position in ctx.portfolio.positions:
            if position.symbol!=bar.symbol or not position.is_open() or not position.entry_complete:
                continue
            if position.position_id not in self._protected:
                raise DataBlocked(['hrs_position_protection_not_initialized:'+position.symbol])
            direction=position.direction.lower()
            current=bar.close
            best=self._best[position.position_id]
            best=min(best,current) if direction=='short' else max(best,current)
            self._best[position.position_id]=best
            if position.tp2_hit:
                params=position.exit_parameters
                filters=self._portal.symbol_filters(position.symbol)
                trigger=exits.trailing_stop_price(direction,best,position.atr,filters['tick_size'],params,self.config)
                software_multiplier=Decimal(str(params['trailing_atr_multiplier']))
                retracement=current-best if direction=='short' else best-current
                if retracement>=position.atr*software_multiplier:
                    result.append(self._intent(position.symbol,direction,position.current_quantity,'TRAILING_STOP',
                        reduce_only=True,reference_price=current,close_quantity=position.current_quantity))
                else:
                    self._protection[position.symbol]['stop_loss_price']=trigger
                    result.append(self._intent(position.symbol,direction,position.current_quantity,'STOP_LOSS',
                        order_type='STOP',trigger_price=trigger,reduce_only=True))
                self._record(now,'hrs_monitor',{'symbol':position.symbol,'position_id':position.position_id,
                    'model':'hrs_cycle_monitor_v1','cadence_seconds':self._monitor_cadence,
                    'quote':str(current),'best_price':str(best),'trailing_price':str(trigger)})
        return tuple(result)

    def _exit_intents(self,symbol,now,analysis,ctx,market):
        result=[]
        for position in ctx.portfolio.positions:
            if position.symbol!=symbol or not position.is_open():
                continue
            if position.position_id not in self._protected:
                raise DataBlocked(['hrs_position_protection_not_initialized:'+symbol])
            direction=position.direction.lower()
            current=Decimal(str(market['entry_price']))
            if not position.tp1_hit and now>=self._reviews[position.position_id]:
                cfg=self.config.get('trading',{}).get('time_stop',{}).get('reanalyze',{})
                held=None
                if cfg.get('enabled',True):
                    cap=Decimal(str(market['market_cap']))
                    oi=Decimal(str(market['oi_usd']))
                    held=core.score_holding(symbol=symbol,direction=direction,
                        klines_1h=[_kline(bar) for bar in self._bars[(symbol,'1h')]],
                        klines_4h=[_kline(bar) for bar in self._bars[(symbol,'4h')]],
                        oi_market_cap_ratio=float(oi/cap) if cap>0 and oi>0 else 0.,
                        funding_rate=float(market['funding_rate']),has_market_cap=cap>0 and oi>0,
                        decision_at=now,config=self.config,scoring_engine=self._scorer,pattern_recognizer=self._recognizer)
                    self._record(now,'hrs_holding_score',{'symbol':symbol,'result':held.to_dict()})
                if held is not None and held.total_score>=cfg.get('hold_threshold',4):
                    self._reviews[position.position_id]=now+timedelta(hours=float(position.exit_parameters['max_holding_hours']))
                else:
                    result.append(self._intent(symbol,direction,position.current_quantity,'TIME_STOP',reduce_only=True,
                        reference_price=current,close_quantity=position.current_quantity))
        return result

    def on_execution_events(self,events,ctx,at=None):
        if not self._runtime_ready:
            return
        for event in events:
            payload=event.get('payload',{})
            amounts=[]
            if event.get('type')=='fill':
                amounts=[('fee',Decimal(str(payload['fee']))),('fee',Decimal(str(payload['slippage'])))]
                if payload.get('side','').startswith('CLOSE_'):
                    amounts.append(('realized_pnl',Decimal(str(payload['realized_pnl']))))
            elif event.get('type')=='funding_settlement':
                amounts=[('funding',Decimal(str(payload['contribution'])))]
                position_id=payload['position_id']
                closed=Decimal(str(payload.get('closed_contribution','0')))
                if closed:
                    self._closed_pnl[position_id]=self._closed_pnl.get(position_id,ZERO)+closed
            for kind,amount in amounts:
                if amount!=ZERO:
                    self._income_seq+=1
                    self._pending_income.append(IncomeEvent('replay-'+str(self._income_seq),kind,amount,
                        at or event['ts'],self._segment,True))
            if event.get('type') in ('entry_timeout','reject') and payload['symbol'] in self._durable_margin_reservations:
                self._durable_margin_reservations[payload['symbol']]['terminal']=True
                self._pending.discard(payload['symbol'])
                self._reservations.pop(payload['symbol'],None)
            if event.get('type') == 'entry_timeout':
                self._pending.discard(event['payload']['symbol'])
                self._reservations.pop(event['payload']['symbol'],None)
            if event.get('type') != 'fill' or not event['payload'].get('side','').startswith('CLOSE_'):
                continue
            payload=event['payload']
            symbol=payload['symbol']
            position=next((p for p in ctx.portfolio.positions if p.symbol==symbol),None)
            if position is None:
                continue
            pnl=Decimal(str(payload['net_pnl']))
            accumulated=getattr(self,'_closed_pnl',{})
            accumulated[position.position_id]=accumulated.get(position.position_id,ZERO)+pnl
            self._closed_pnl=accumulated
            if position.is_open() or payload['reason']=='ENTRY_TIMEOUT':
                continue
            now=event['ts']
            if self._pending_funding:
                self._deferred_outcomes[position.position_id]=(symbol,position.entry_price,payload['reason'],now)
                continue
            total=accumulated.pop(position.position_id)
            self._apply_closed_outcome(symbol,position.entry_price,payload['reason'],total,now)
        self._release_margin_reservations(ctx,at)

    def _apply_closed_outcome(self,symbol,entry_price,reason,total,now):
        if total>ZERO:
            self._runtime['consecutive_losses']=0
        elif total<ZERO:
            self._runtime['consecutive_losses']+=1
            loss=self.config.get('trading',{}).get('consecutive_loss',{})
            if self._runtime['consecutive_losses']>=loss.get('max_count',3):
                self._runtime['pause_until']=(now+timedelta(days=loss.get('pause_days',2))).isoformat()
            hours=self.config.get('trading',{}).get('blacklist',{}).get('check_hours',24)
            self._runtime['stop_loss_monitor'][symbol]={'entry_price':str(entry_price),
                'monitor_until':(now+timedelta(hours=hours)).isoformat()}
        if reason=='TIME_STOP':
            self._runtime['total_pnl']=str(Decimal(str(self._runtime['total_pnl']))+total)

    def on_funding_due(self,key,economic_time):
        if not self._pending_funding:
            self._deferred_units=self._units
        self._pending_funding[key]=economic_time
        self._record(economic_time,'hrs_funding_pending',dict(settlement_id=key))

    def funding_is_pending(self,key):
        return key in self._pending_funding

    def on_funding_unproven(self,key,error):
        self._funding_gaps.append(str(error))

    def on_funding_received(self,key,payload,mark,received_at):
        self._pending_funding.pop(key)
        self._known_funding[key]=Decimal(str(payload['contribution']))
        self._funding_marks[payload['symbol']]=mark

    def _funding_adjusted_equity(self,portfolio,at,unpaid):
        # Reconstruct an economic valuation only after the missing amount is known.
        # Source times remain unchanged; receipt time never freshens an old quote.
        prices=dict(portfolio.last_prices); times=dict(portfolio.last_price_times)
        for position in portfolio.positions:
            if not position.is_open():
                continue
            marked_at=times.get(position.symbol)
            if marked_at is None or not timedelta(0)<=at-marked_at<=timedelta(seconds=120):
                mark=self._funding_marks.get(position.symbol)
                if mark is not None and timedelta(0)<=at-mark['marked_at']<=timedelta(seconds=120):
                    prices[position.symbol]=mark['price']; times[position.symbol]=mark['marked_at']
        view=PortfolioView(portfolio.positions,prices,portfolio.realized_equity,times)
        return self._marked_equity(view,as_of=at)+sum((self._known_funding[key] for key in unpaid),ZERO)

    def _reconcile_funding_valuations(self,received_at):
        units=self._deferred_units
        flow_index=0
        flows=sorted(self._deferred_flows,key=lambda item:parse_utc(item[0]['occurred_at']))
        for at,portfolio,unpaid in self._deferred_marks:
            while flow_index<len(flows) and parse_utc(flows[flow_index][0]['occurred_at'])<=at:
                row,before,flow_unpaid=flows[flow_index]
                flow_at=parse_utc(row['occurred_at'])
                nav=self._funding_adjusted_equity(before,flow_at,flow_unpaid)/units
                amount=Decimal(str(row['amount']))
                units+=amount/nav
                if units<=ZERO:
                    raise DataBlocked(['hrs_cashflow_units_nonpositive'])
                self._pending_flows.append(CashFlow(row['event_id'],amount,flow_at,self._segment,
                    nav_before=nav,valuation_evidence='funding_receipt:'+received_at.isoformat()))
                flow_index+=1
            equity=self._funding_adjusted_equity(portfolio,at,unpaid)
            self._append_checkpoint(at,equity,units)
            realized=portfolio.realized_equity+sum((self._known_funding[key] for key in unpaid),ZERO)
            self._reconciled_points.append(EquityPoint(at,realized,equity,any(p.is_open() for p in portfolio.positions)))
            self._record(received_at,'hrs_funding_valuation_reconciled',dict(economic_as_of=at.isoformat(),
                equity=str(equity),units=str(units),settlement_ids=list(unpaid)))
        if flow_index!=len(flows):
            raise DataBlocked(['hrs_pending_funding_cashflow_valuation_missing'])
        self._units=units
        self._deferred_marks=[]; self._deferred_flows=[]
        for position_id,(symbol,entry_price,reason,_closed_at) in self._deferred_outcomes.items():
            total=self._closed_pnl.pop(position_id)
            self._apply_closed_outcome(symbol,entry_price,reason,total,received_at)
        self._deferred_outcomes={}

    def reconciled_equity_points(self):
        points=tuple(self._reconciled_points)
        self._reconciled_points=[]
        return points

    def finalize_funding(self):
        if self._pending_funding:
            raise DataBlocked(['hrs_funding_pending_or_unproven:'+','.join(self._pending_funding),*self._funding_gaps])

    def _append_checkpoint(self,at,equity,units):
        current=EquityCheckpoint(self._segment,at,equity,units,equity/units)
        self._history=[sample for sample in self._history if sample.as_of!=at]+[current]
        evaluation=evaluate_loss_gate(self._gate,current,
            GateHistory(tuple(self._history),self._history_from,at,True),allow_inception_segment=self._allow_inception)
        self._gate=evaluation.state

    def on_portfolio_mark(self,at,ctx):
        self._bootstrap(ctx)
        if self._pending_funding:
            self._deferred_marks.append((at,ctx.portfolio,tuple(self._pending_funding)))
            return False
        if self._deferred_marks:
            self._deferred_marks.append((at,ctx.portfolio,()))
            self._reconcile_funding_valuations(at)
        equity=self._marked_equity(ctx.portfolio,as_of=at)
        if hasattr(self,'_ledger_state'):
            marks=tuple(PositionMark(p.position_id,self._segment,
                p.current_quantity*(1 if p.direction=='LONG' else -1),p.entry_price,
                ctx.portfolio.last_prices[p.symbol],ctx.portfolio.last_price_times[p.symbol],True) for p in ctx.portfolio.positions if p.is_open())
            observation=LedgerObservation(at,self._segment,tuple(self._pending_income),tuple(self._pending_flows),
                marks,at,at,True,True)
            try:
                self._ledger_state=reconcile_ledger(self._ledger_state,observation)
            except LedgerNotReady as exc:
                raise DataBlocked(['hrs_replay_ledger_not_ready:'+str(exc)]) from exc
            if self._ledger_state.equity!=equity:
                raise DataBlocked(['hrs_replay_ledger_broker_equity_mismatch'])
            self._units=self._ledger_state.units
            self._pending_income=[]
            self._pending_flows=[]
        self._append_checkpoint(at,equity,self._units)
        return True

    def protective_prices(self,symbol):
        return self._protection.get(symbol)


def _kline(bar):
    return dict(open_time=int(bar.open_time.timestamp()*1000),open=str(bar.open),high=str(bar.high),
                low=str(bar.low),close=str(bar.close),volume=str(bar.volume),quote_volume=str(bar.quote_volume))


def _positive(value):
    result=Decimal(str(value))
    if not result.is_finite() or result<=ZERO:
        raise ValueError('nonpositive_or_invalid_value')
    return result


def _validate_snapshot(
    table: str, raw: Any, portal_as_of: datetime
) -> Tuple[Optional[Mapping[str, Any]], List[str]]:
    """校验单表历史快照记录，返回（通过校验的快照, gap 列表）。

    gap 码口径：``missing_at_as_of`` / ``invalid_snapshot`` / ``incomplete_at_as_of`` /
    ``as_of_missing`` / ``as_of_invalid`` / ``as_of_mismatch`` / ``rows_missing``。
    ``rows == []`` 在 ``complete is True`` 且 ``as_of`` 一致时合法，不产生 gap。
    """
    if raw is None:
        return None, ["{0}:missing_at_as_of".format(table)]
    if not isinstance(raw, Mapping):
        return None, ["{0}:invalid_snapshot".format(table)]

    gaps: List[str] = []
    if "as_of" not in raw:
        gaps.append("{0}:as_of_missing".format(table))
    else:
        snapshot_as_of = _parse_as_of(raw.get("as_of"))
        if snapshot_as_of is None:
            gaps.append("{0}:as_of_invalid".format(table))
        elif snapshot_as_of != portal_as_of:
            gaps.append("{0}:as_of_mismatch".format(table))

    if raw.get("complete") is not True:
        gaps.append("{0}:incomplete_at_as_of".format(table))

    rows = raw.get("rows")
    if "rows" not in raw or not isinstance(rows, (list, tuple)):
        gaps.append("{0}:rows_missing".format(table))

    if gaps:
        return None, gaps
    return raw, gaps


def _parse_as_of(value: Any) -> Optional[datetime]:
    """把快照 ``as_of`` 统一转 aware UTC datetime；无法解析返回 None（不读时钟）。"""
    if not isinstance(value, (datetime, str)):
        return None
    try:
        return parse_utc(value)
    except (TypeError, ValueError):
        return None
