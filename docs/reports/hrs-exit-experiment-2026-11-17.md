# HRS exit experiment delivery and current evidence

The actual local discovery result is **BLOCKED_DATA**. The adjacent JSON records the searched paths, an empty HRS raw-snapshot inventory, zero replay runs, and the remaining concrete missing input: historical HRS replay snapshots. No parameters were selected. Older trade-summary audits and engineering fixtures do not supply the missing candidate, denial, inventory, mark and public-input history.

`hrs-exit-experiment-registration-2026-11-17.json` freezes the actual production effective configuration and 12 candidates for each of standard/EMM and LV-RM. Its production loader reported an unapplied comments-only overlay; the actual process tuning-loaded configuration hash was verified equal to the captured fallback configuration. This condition is retained explicitly rather than describing the overlay as applied. Production fee is 4 bps per leg, while 2 bps slippage is an explicitly declared experiment assumption. Research is preregistered for November 18–December 1, followed by complete December and January holdout months. These prospective dates do not certify future data completeness or sample counts.

## Running the CLI

```sh
python -m ai_tuner.hrs_exit_experiment \
  --snapshot-root /path/to/private/frozen/snapshots \
  --registration /path/to/preregistered-exit-experiment.json \
  --stage research --output /path/to/research-report.json

python -m ai_tuner.hrs_exit_experiment \
  --snapshot-root /path/to/private/frozen/snapshots \
  --registration /path/to/preregistered-exit-experiment.json \
  --stage holdout --selection /path/to/frozen-selection.json \
  --research-report /path/to/research-report.json \
  --output /path/to/holdout-report.json
```

Omitting `--registration` performs discovery and writes a concrete blocked report. Exit code 2 means blocked data or a replay violation; code 0 means evidence is available. `AVAILABLE` does not authorize changing a production overlay. `performance_accepted` remains false pending formal source/parity/qualification review; holdout comparisons are reported separately.

## Registration contract

The registration is an immutable JSON object with:

- `schema_version: 1` and a unique `experiment_id`.
- `baseline.config`: the actual effective production config; `baseline.source: production_effective`; its canonical `effective_config_sha256`; the three captured `config_hashes` (`base_config_hash`, `params_manifest_hash`, `overlay_hash`); and the production `code_commit`. The snapshot's actual config and identities must match. The CLI never replaces historical hashes with candidate hashes.
- `candidates`: unique `candidate_id`, `mode` (`standard` or `lv_rm`) and `exit_parameters`. Each mode has exactly one empty-override baseline and at most 12 total candidates. Each neighbor changes one parameter. A paired TP allocation may change both original-quantity fractions when explicitly registered as `factor: tp_allocation`.
- `windows`: exactly one `research` window followed chronologically by two nonoverlapping `holdout` windows. Each contains `window_id`, timezone-aware `start`/`end` and an explicit `snapshot_dir`. Holdouts are complete Asia/Shanghai calendar months. Research runs never execute holdout windows.
- A frozen `cost_model`: `fee_bps`, `slippage_bps`, `stop_offset_pct`, `tp_offset_pct`, `max_leverage`. Baseline, 1.5× and 2× fee/slippage tiers use the same registered source window.
- `minimum_closed_lifecycles`, at least 20, frozen before results are inspected. This conservative floor matches the existing HRS closed-trade readiness minimum; a registration can require more samples.

Supported parameter names are `atr_multiplier`, `emergency_percent`, `min_absolute_percent`, `target1_atr_multiplier`, `target1_close_percent`, `target2_atr_multiplier`, `target2_close_percent`, `trailing_atr_multiplier`, and `max_holding_hours`. The explicit replay adapter experiment API applies them to new entries in the selected mode. Existing position parameters remain unchanged. The scorer, candidate policy, 1%/3% limits and add-position policy retain captured behavior. Unsupported factors, including a new TP1 fee-breakeven rule or a new holding-extension rule, are rejected rather than simulated as implemented.

The current boundary policy requires a proven flat opening inventory and no remaining open positions at the end. Windows requiring counterfactual position handover or more lifecycle history stay blocked; the CLI does not liquidate positions at an artificial boundary.

## Required source evidence

Each snapshot must pass the actual `DataPortal` hash/time checks and provide raw HRS bootstrap, inputs, decision-availability calendar, external cashflows, prospective funding schedule, funding settlement marks, market snapshots, universe, config identities, exchange filters and required bar coverage. Opening state is supplied as `state_tables` inside the manifest-bound `hrs_replay_state.json`, or as a manifest-bound `hrs_state_tables.json`. It contains the genuine `hrs_positions`, `hrs_blacklist` and `hrs_active_symbols` snapshots at the window start.

Accepted data runs through `DataPortal → HrsAdapter → VirtualBroker → run_replay`; recorded chosen signals do not supply experiment decisions. Missing inputs produce `BLOCKED_DATA`, and future-data access produces `FAIL`.

## Metrics and frozen holdout selection

Reports retain separate source-manifest, registration and candidate hashes. Metrics use cashflow-adjusted ledger NAV, exact Shanghai midnight returns, intrawindow drawdown, completed position lifecycles, net PnL divided by actual filled entry stress risk, worst lifecycle R, longest losing streak, direction groups, monthly net PnL and removal of the largest lifecycle profit. Partial TP legs share their original position risk denominator. Each mode meets its own sample minimum; standard-mode experiments group standard, EMM and semi-EMM exits, while LV-RM samples stay separate. Whole-portfolio lifecycle diagnostics are retained alongside the targeted-mode metrics. Unavailable risk facts remain missing. Remaining-position profit giveback is explicitly unavailable until the lifecycle peak-equity path is exported; no zero is substituted.

A frozen selection contains `registration_sha256`, `research_report_sha256` and `candidate_by_mode`. The referenced research report must be AVAILABLE and belong to exactly the same registration. Holdout execution evaluates only the selected candidate and its baseline, and does not choose a candidate from holdout performance.

Both months must independently show no deterioration in net return per entry risk, drawdown, worst lifecycle R and profit after removing the largest winner; all cost tiers are compared. Reports include trade retention and a deterministic bootstrap confidence interval for the mean lifecycle-R difference. A confidence interval crossing zero cannot establish improvement. Formal qualification remains a separate decision supported by the complete source and live-parity evidence.
