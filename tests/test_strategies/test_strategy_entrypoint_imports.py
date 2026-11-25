"""Resolve real -m entrypoints without executing live strategy code."""
import subprocess
import sys

import pytest


@pytest.mark.parametrize("strategy", ["btc_eth", "btc_eth_aggressive", "new_coin", "grid"])
def test_module_entrypoint_is_not_preimported(strategy):
    script = f"""
import asyncio, importlib, inspect, runpy, warnings
package = importlib.import_module('strategies.{strategy}')
original = package.main
calls = []
def fake_run(coro):
    calls.append(coro.cr_code)
    coro.close()
asyncio.run = fake_run
with warnings.catch_warnings():
    warnings.simplefilter('error', RuntimeWarning)
    runpy.run_module('strategies.{strategy}', run_name='__main__')
assert calls == [original.__code__]
assert package.main is original and callable(package.main)
"""
    result = subprocess.run([sys.executable,"-c",script],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("strategy", ["btc_eth", "btc_eth_aggressive", "new_coin", "grid"])
def test_public_main_remains_async_callable(strategy):
    script = f"""
import inspect
from strategies.{strategy} import main
assert callable(main) and inspect.iscoroutinefunction(main)
"""
    result = subprocess.run([sys.executable,"-c",script],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
