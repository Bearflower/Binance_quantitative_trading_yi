# HRS raw evidence export

`ai_tuner.hrs_evidence_export` converts retained raw evidence into the existing
`replay.snapshot_sealer` / `DataPortal` format. It runs the actual HRS adapter,
virtual broker and replay engine before publishing the requested manifest.
A seal proves local integrity; it does not attest exchange origin, exchange fill
parity, adequate research samples or profitable performance.

## Inputs

`read_evidence_sources(connection, account_id, window_start=..., as_of=...)` accepts
an already acquired **SELECT-only** asyncpg-like connection. It uses one read-only,
repeatable-read transaction and bounded queries for:

- `hrs.hrs_decision_evidence`, including complete scan envelopes, actual scope,
  analyses and their original bars, raw provider responses and execution batches;
- `trading.hrs_equity_evidence` and actual equity checkpoints;
- `public.ai_tuner_funding_schedule_observations`.

The function does not load credentials or connect to production. It returns a raw
source dictionary. The optional `read_market_export(path)` imports actual kline
rows from the existing `ai_tuner.replay.raw_export` directory, checking its file
hashes and row counts. Historical bar revision uncertainty remains uncertainty.

A source JSON object contains `decision_evidence`, `equity_evidence`,
`equity_checkpoints`, `funding_observations` and optional `market_bars` and
`cash_flows`. Preserve the original database envelopes and payload hashes.
`market_bars` entries identify `symbol`, `interval`, source `postgres_klines` or
`public_klines`, original `rows`, and the recorder-canonical `payload_sha256`.
Alternatively, captured public `/fapi/v1/klines` supplies the bars. Continuous
actual 1m data is required for the LIMIT execution model; 1h and 4h analysis bars
remain the actual scored history. No interpolation or generated prices are used.

The live `configuration_evidence` preserves parsed public documents and original
byte hashes. The exporter preserves those identities and verifies the effective
configuration metadata; it does not hash parsed JSON and pretend that hash is the
original YAML byte identity. Configuration changes require a separate window.

`entry_stage_inputs` is raw evidence, not a chosen action. Its payload contains
`exchange_positions`, `claims`, `pool_index`, `pool_index_12h`,
`pre_entry_oi_usd` and `pre_entry_funding_rate`; the envelope retains actual
availability. Missing entry-stage facts block only when the real strategy reaches
that stage. Provider asset IDs and market-cap provenance remain in the bundle.
Recorded scores, `should_entry` and execution outcomes never become entry
instructions. Actual execution batches supply controller order and release time.

## Opening state and availability

Choose a real captured `scan_start.event_time` for `window_start`. Its real later
availability is retained. Opening HRS inventory must be flat; nonflat starts need
fill-cost, accumulated funding and MAE/MFE proof that the current position rows do
not establish.

An earlier ledger point can bridge to this boundary only with a
`proven_flat_unchanged_v1` receipt: complete hashed source archives at the ledger
anchor and through the boundary, actual agreeing inventories, independently
proved owners, no HRS fills or income, no allocation mutations, no HRS risk
intents and no unresolved reservation. Released historical reservations do not
block the bridge. Every decision must occur after the actual proof is available;
no decision is moved backward or supplied a future observation.

For account normalization, supply a genuinely observed `hrs_marked_equity` in the
financial context, or a complete available proof of an unchanged flat interval.
Subtracting stale ledger equity from a current total account balance is not an
unconditional fallback.

Funding forecasts use the existing independent prospective timetable validator;
there is no assumed eight-hour cadence. Actual `/fapi/v1/fundingRate` publications
must include their settlement mark. Economic time and publication receipt remain
separate. Missing forecast coverage or marks blocks export. A cash-flow label
`verified: true` alone is insufficient: it must match retained ledger source facts
and their real availability. Late flows needing unsupported receipt accounting
remain blocked rather than being backdated.

Current explicit policy limitations are captured non-HRS account inventory and
unreleased durable reservations, whose account-wide occupancy model is being
integrated separately. These return named qualification gaps. Software LIMIT
close matching, retry timing and order-book priority also retain the replay's
declared `observed_quote_market_proxy_v1` approximation; no exact exchange parity
is claimed.

## CLI

Use a private directory outside Git, or a Git-ignored destination:

```sh
python -m ai_tuner.hrs_evidence_export \
  --sources /private/export/hrs-raw-sources.json \
  --raw-market-export /private/export/market-raw \
  --output-root /private/export/replay \
  --window-start 2026-11-17T00:00:00Z --as-of 2026-11-17T03:00:00Z \
  --participation-rate .01 \
  --monitor-cadence-seconds 3600 --monitor-anchor 2026-11-17T00:00:00Z
```

The participation rate and monitor model are explicit research assumptions. The
monitor cadence must match the captured strategy configuration. The optional
`--allow-inception-segment` permits only the labelled inception gate; it never
claims thirty-day history.

Exit 0 means `SEALED`, with `source_attested: false` and
`performance_qualified: false`. Run the preregistered exit experiment CLI for
research qualification. Exit 2 means `BLOCKED_DATA`; inspect `gap_bundle`.
Without raw sources the CLI writes a gap bundle and no replay manifest. Current
configuration files, summary PnL and exchange-info snapshots cannot reconstruct
missing August–September decisions or certify complete historical performance.
