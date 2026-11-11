# PM One-Way Grid Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) for tracking.

**Goal:** Make the existing V2.5.5 ETHUSDT order-grid executor use the user's PM subaccount in one-way mode, while refusing to trade the currently mismatched account.

**Architecture:** Keep the existing net-position engine and journal. Replace the exchange boundary with explicit PAPI private endpoints; normalize PM's flat position response and risk fields at that boundary. Keep execution default off and require verified one-way mode, leverage, collateral and risk limits before placing orders.

**Tech Stack:** Python, asyncio, Binance REST/WS, SQLite, pytest.

---

### Task 1: Correct configuration and endpoint contract

**Files:** `strategies/grid/execution/contracts.py`, `strategies/grid/execution/exchange.py`, `tests/test_grid_execution/test_config.py`, `tests/test_grid_execution/test_exchange.py`

- [x] Add failing tests requiring `ExecutionSettings.from_env(...).use_unified_account is True` and `GridExchange` to reject a non-PM client.
- [x] Run `python3 -m pytest -q tests/test_grid_execution/test_config.py tests/test_grid_execution/test_exchange.py`; expect the new tests to fail.
- [x] Change live settings to PM and construct `BinanceClient(..., use_unified_account=True)`. Use exact `/papi/v1/um/order` and `/papi/v1/um/algo/order` paths, with matching query/cancel/open paths; keep `/fapi/v1/exchangeInfo` and public ticker unsigned.
- [x] Run the two test files; expect pass.

### Task 2: Verify one-way PM account and empty position

**Files:** `strategies/grid/execution/exchange.py`, `tests/test_grid_execution/test_exchange.py`

- [x] Add failing tests: `dualSidePosition=True` rejects; `False` plus `marginType=CROSSED`, expected leverage and normal PM account accepts; PM `positionRisk=[]` yields one internal `ETHUSDT/BOTH/0` row, malformed/non-BOTH rows reject.
- [x] Run targeted tests and observe expected failures.
- [x] Implement `verify_account` using `/papi/v1/um/positionSide/dual`, `/papi/v1/um/symbolConfig`, `/papi/v1/account`; normalize only a successful empty PM position response.
- [x] Run targeted tests; expect pass.

### Task 3: PM equity, fills and income

**Files:** `strategies/grid/execution/exchange.py`, `strategies/grid/execution/engine.py`, `strategies/grid/execution/risk_tracker.py`, `tests/test_grid_execution/test_exchange.py`, `tests/test_grid_execution/test_risk_tracker.py`, `tests/test_grid_execution/test_start.py`

- [x] Add failing tests requiring `accountEquity` and `totalAvailableBalance` as PM risk/funding sources and exact `/papi/v1/um/userTrades` and `/papi/v1/um/income` queries.
- [x] Run the targeted tests and observe failures.
- [x] Implement the PM account, trade and income fields; reject zero/invalid available collateral and incomplete income evidence.
- [x] Run targeted tests; expect pass.

### Task 4: PM user stream and reconciliation

**Files:** `shared/binance_api.py`, `strategies/grid/execution/exchange.py`, `strategies/grid/execution/stream.py`, `tests/test_grid_execution/test_exchange.py`, `tests/test_grid_execution/test_stream.py`

- [x] Add failing tests for unsigned `/papi/v1/listenKey` routing to PAPI, PM keepalive response shape and `wss://fstream.binance.com/pm/ws/<listenKey>`.
- [x] Run targeted tests and observe failures.
- [x] Correct PM unsigned routing, PM listenKey handling and stream URL; retain reconnect REST reconciliation.
- [x] Run targeted tests; expect pass.

### Task 5: Full simulation and operational docs

**Files:** `tests/test_grid_execution/test_end_to_end.py`, `docs/deployment/grid-order-executor.md`

- [x] Run the existing full simulated cycle with PM account fields; cover PM flat `[]` and one-way mode gate in adapter tests.
- [x] Run `python3 -m pytest -q tests/test_grid_execution`; expect all pass.
- [x] Update deployment guide to identify the current hedge mode and PM zero collateral blockers, plus required one-way mode, transfer and hard-limit gates.
- [x] Run `git diff --check`, focused grid tests and syntax/config checks; capture exact results. Keep production execution disabled.
