"""Do not replay an already collected hour when publishing attribution labels."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from strategies.btc_eth.shadow import main


@pytest.mark.parametrize('skip', [None, '0', '1'])
def test_cutover_can_wait_for_regular_cron_without_changing_default(monkeypatch, skip):
    if skip is None:
        monkeypatch.delenv('SHADOW_SKIP_INITIAL_COLLECTION', raising=False)
    else:
        monkeypatch.setenv('SHADOW_SKIP_INITIAL_COLLECTION', skip)
    collector = AsyncMock()
    collector.run_id = 'xau-cutover'
    asyncio.run(main._collect_on_startup(collector))
    assert collector.run_once.await_count == (0 if skip == '1' else 1)
