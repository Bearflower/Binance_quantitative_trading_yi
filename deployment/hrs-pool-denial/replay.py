"""Exercise the deployed decision path with the captured MMT pool values."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from shared.circuit_breaker import CircuitBreaker, load_circuit_breaker_config
from strategies.hrs.decision_capture import DecisionRecorder
from strategies.hrs.strategy import HRSStrategy


async def replay():
    strategy = object.__new__(HRSStrategy)
    strategy.config = {}
    strategy._filter_timeout_signals = AsyncMock(side_effect=lambda signals, direction: signals)
    strategy._log_signal_to_db = AsyncMock()
    strategy._update_signal_resolution = AsyncMock()
    strategy._send_cycle_summary = AsyncMock()
    strategy.execute_signal = AsyncMock(side_effect=AssertionError('filtered signal reached execution'))
    strategy.candidate_pool = MagicMock()
    strategy.decision_recorder = DecisionRecorder(AsyncMock())
    strategy.decision_recorder.record = AsyncMock()
    strategy.circuit_breaker = CircuitBreaker(load_circuit_breaker_config(), None, pool='hrs')
    strategy.circuit_breaker.load_index = AsyncMock(return_value=0.0004178555095485919)
    strategy.circuit_breaker.load_index_12h = AsyncMock(return_value=0.026569169188703893)
    signal = dict(symbol='MMTUSDT',direction='short',entry_mode='standard',score=6.55,signal_id=5847)
    await strategy._finalize_and_execute_signals([signal],[],source_symbols=['MMTUSDT'])
    strategy.execute_signal.assert_not_awaited()
    strategy._update_signal_resolution.assert_awaited_once_with(5847,'pool_circuit_breaker')
    receipts = [x.args[2] for x in strategy.decision_recorder.record.await_args_list
                if x.args[1]=='pool_circuit_preflight_denial']
    assert len(receipts)==1 and receipts[0]['is_shadow'] is False
    assert receipts[0]['pool_index_12h']==0.026569169188703893
    print('HRS_POOL_REPLAY_OK')


asyncio.run(replay())
