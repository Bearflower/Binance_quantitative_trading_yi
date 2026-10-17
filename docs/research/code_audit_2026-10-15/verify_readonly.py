"""Offline audit probes. No strategy startup, network, database, or orders.

Run from repository root: python3 docs/research/code_audit_2026-10-15/verify_readonly.py
Extracts individual production methods through AST to avoid package startup effects.
These are bounded behavior probes, not an integration/backtest suite.
"""
import ast
import asyncio
import hashlib
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent

class Quiet:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None

def tree(path):
    return ast.parse((ROOT / path).read_text())

def method(path, name):
    node = next(n for n in ast.walk(tree(path))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return node

def extract(path, name):
    node = method(path, name)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    scope = dict(Decimal=Decimal, logger=Quiet())
    exec(compile(ast.fix_missing_locations(module), path, 'exec'), scope)
    return scope[name]

spec = importlib.util.spec_from_file_location('audit_config_loader', ROOT / 'shared/config_loader.py')
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)
loader.logger = Quiet()
configs, metadata = {}, {}
for name in ('btc_eth', 'btc_eth_aggressive', 'new_coin', 'hrs', 'grid'):
    configs[name], metadata[name] = loader.load_strategy_config_with_metadata(str(ROOT / 'strategies' / name))

results = []
def record(name, **evidence):
    results.append(dict(probe=name, status='passed', **evidence))

async def run():
    hrs = 'strategies/hrs/strategy.py'
    calls = [n for n in ast.walk(method(hrs, 'analyze')) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == 'score']
    assert len(calls) == 2 and all('price_change_24h' not in [k.arg for k in c.keywords] for c in calls)
    for fn, enabled in [('check_emm_conditions', 'emm_enabled'), ('check_semi_emm_conditions', 'semi_emm_enabled')]:
        result = extract('strategies/hrs/scoring_engine.py', fn)(SimpleNamespace(**{enabled: True}), 'short', None, 0.01, 0.2)
        assert result[0] is False
    record('hrs_standard_path_emm_missing_price_change', score_call_lines=[c.lineno for c in calls])

    generated = []
    class Pool:
        async def get_lv_rm_scan_range(self): return ['EXAMPLEUSDT']
    class FakeHRS:
        config = {'lv_rm': {'enabled': True}}
        candidate_pool = Pool()
        async def _check_lv_rm_entries(self, symbols, shorts, longs):
            shorts.append({'symbol': symbols[0]})
            generated.extend(shorts)
    # The real wrapper generates into temporary lists; it neither returns nor executes them.
    wrapper = method(hrs, '_run_lv_rm_only_cycle')
    attrs = [n.func.attr for n in ast.walk(wrapper) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert '_resolve_conflicts' not in attrs and 'execute_signal' not in attrs
    value = await extract(hrs, '_run_lv_rm_only_cycle')(FakeHRS())
    assert value is None and len(generated) == 1
    record('hrs_empty_pool_lv_signals_discarded', generated=1, returned=None)

    nc = 'strategies/new_coin/strategy.py'
    calls = [n for n in ast.walk(method(nc, '_execute_cycle')) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == 'should_entry']
    assert len(calls) == 1 and len(calls[0].args) == 3 and not calls[0].keywords
    record('newcoin_degraded_flag_omitted', line=calls[0].lineno,
           current_regular_min=configs['new_coin']['scoring']['technical']['min_total_score'],
           current_degraded_min=configs['new_coin']['scoring']['sentiment']['degraded_mode']['min_technical_score'])
    for close in range(1, 101):
        bars = [{'high': close + 2, 'close': close} for _ in range(5)]
        assert not bars[-1]['close'] > max(k['high'] for k in bars[-5:])
    record('newcoin_rebound_inequality', valid_ohlc_examples=100,
           invariant='last close <= last high <= max(inclusive recent highs)')

    class Cap:
        async def get_market_cap(self, symbol): return None
    fallback = extract('shared/market_cap.py', 'get_market_cap_with_fallback')
    ratios = [oi / await fallback(Cap(), 'EXAMPLEUSDT', oi, 0) for oi in (100, 1000, 100000)]
    assert ratios == [0.05] * 3
    record('hrs_fallback_ratio_constant', ratios=ratios)

    class Budget:
        async def get_effective_margin_limit(self): return 1000, 'probe'
        def get_min_position_margin(self): return None
    cfg = configs['btc_eth']
    fake = SimpleNamespace(capital_mgr=Budget(), config=cfg, positions={},
        risk_config={'position_sizing': {'safety_margin_ratio': 0.1}, 'position_management': {'volatility_target_risk': 0}},
        symbol_config={}, binance_config={'position_ratio': {'A': 0.24}})
    size, reason = await extract('strategies/btc_eth/strategy.py', '_calculate_position_size')(fake, 'A', Decimal('100'), None)
    assert size == Decimal('216') and reason == ''
    record('mtpcs_notional_sizing_no_leverage_factor', allocation=1000, safety=0.1, ratio=0.24, notional=str(size))

    fake = SimpleNamespace(single_position_margin=Decimal('50'), leverage=Decimal('2'))
    qty = extract('strategies/new_coin/executor.py', '_calculate_position_size')(fake, 100, 30)
    assert qty == Decimal('0.6')
    record('newcoin_margin_sizing', available_margin=30, leverage=2, price=100, quantity=str(qty))
    fake = SimpleNamespace(max_loss_percent=0.02, config={'position_sizing': {'single': {'hard_cap_usdt': 50, 'account_ratio_cap': 0.05}}})
    qty = extract('strategies/hrs/risk_manager.py', 'calculate_position_size')(fake, 1000, 0.05, 2, 100)
    assert qty == 1
    record('hrs_stop_risk_then_margin_caps', account=1000, stop_distance=0.05, leverage=2, margin_cap=50, quantity=qty)

    assert configs['grid']['signal_bot']['mode'] == 'semi' and configs['grid']['signal_bot']['enabled']
    for name in ('_initialize_grid_for_symbol', '_check_all_orders', '_check_all_risks', '_monitor_all_grids'):
        node = method('strategies/grid/strategy.py', name)
        guards = [n for n in node.body if isinstance(n, ast.If) and 'is_signal_mode' in ast.unparse(n.test)]
        assert guards and any(isinstance(n, ast.Return) for n in ast.walk(guards[0]))
    assert configs['grid']['realtime_alert']['mode'] == 'shadow'
    assert not configs['grid']['realtime_alert']['shadow_research']['enabled']
    record('grid_semi_guards_and_shadow_config', automatic_methods_guarded=4)

asyncio.run(run())
paths = set()
for strategy in configs:
    paths.update((ROOT / 'strategies' / strategy).glob('*.py'))
    paths.add(ROOT / 'strategies' / strategy / 'config.yaml')
    paths.update((ROOT / 'strategies' / strategy / 'tuning_overrides').glob('*'))
paths.update(ROOT / 'shared' / name for name in ('config_loader.py','capital_manager.py','market_cap.py','kline_service.py','position_ownership.py','order_fill_waiter.py','protection_retry.py','circuit_breaker.py'))
manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths) if p.is_file()}
result = {'scope': 'offline method/AST probes only; not live deployment or PnL verification',
          'config_metadata': metadata, 'checks': results, 'source_sha256': manifest}
(OUT / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'passed':len(results), 'config_metadata':metadata}, ensure_ascii=False, indent=2))
