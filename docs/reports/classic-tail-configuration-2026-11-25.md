# Classic account configuration and restore evidence — 2026-11-25

This report records completed configuration fixes, an isolated physical restore,
and a read-only account snapshot. It does **not** declare all cutover tail items
complete. Off-host archive hashes remain a separate acceptance gate owned by the
root task; bootstrap-role migration and account-ledger work have their own gates.

`rd` below means the private server directory
`/root/trading_system/.releases/pm-window-89160896`.

## Configuration changes and deployed identity

- `15b4cde6`: the shared loader accepts an explicit `{}` as a valid no-op overlay.
  Missing files, empty/comment-only documents, invalid YAML and non-mappings
  retain warning/fallback behavior.
- `b96e99fc`: remove retired MATICUSDT from Kline's fixed symbol list. It was not
  a dynamic registration. Binance exchangeInfo contained no MATICUSDT; production
  had four obsolete fixed tasks. Post-release tasks exactly matched the union of
  fixed and dynamic configurations: 136 tasks, zero MATIC tasks, none missing.
- `b3f8d69d`: retain HRS V20260819's explanatory comments and add explicit `{}`.
  The former 156-byte document was comments only. The guarded host edit preserved
  the complete merged configuration and its hash; before/after files and evidence
  are in `rd/logfix-release/hrs-empty-overlay/`.
- `564e795e`: data-backend now copies `dashboard/backend/config.yaml`. Normalize
  its substituted database port to an integer. A read-only simulation with the
  real container environment found no effective value changes; its only original
  difference was integer `5432` versus string `"5432"`, now prevented. The actual
  data service continues to construct connections from `DATABASE_*` environment
  variables. The shared dashboard config module also affects dashboard-api.
- `532321e6`: HRS configuration identity accepts an applied empty mapping while
  retaining file, mapping-type, version and effective-hash validation. Validate
  the mapping before merging. Without this accompanying fix, evidence-enabled
  HRS startup would reject the new loader's valid `{}` metadata.

Focused regression runs observed the new failures before the fixes. Final config,
overlay, HRS identity and startup-lifecycle suites: **71 passed, 1 skipped** (the
skipped test requires its own PostgreSQL DSN). Kline config/scheduler/registry:
**64 passed**. Existing Pydantic/pytest deprecation warnings were not hidden.

Production acceptance is recorded in
`rd/tail-fixes/configuration-acceptance.json`. HRS, data-backend and dashboard-api
all report source `532321e65da07d8ced5b26097f7d5ae10e4ea215`, deploy ID `00000D01`,
healthy status and zero restarts at acceptance. Verified image IDs:

| Container | Image ID |
|---|---|
| HRS | `sha256:835a1a2c2e27bde2f8fad405673fe02ff8cffe4d069adc401d7ab24506027cee` |
| data-backend | `sha256:39dce4c41ae2dde7c1c7ad142769472fe2136883bc1dfebd41514614f96a2ff1` |
| dashboard-api | `sha256:c9999cabec2852ba6683293374d7cae9527bbd68b084995806224c761f030c65` |
| ai-tuner | `sha256:4054408dfc4b28769aef0a433fba2316d0448e0c76883fd148e79b603760fac2` |
| Kline | `sha256:af667fa3e204f0f2485627d8463e3d54a6cebb083f33ae1f6dd1913bd6b2d8bc` |

The last two images report source
`be7484a7f9ef484a79187b801f0a37a6a2bdbe78`, deploy ID `00000B01`, and healthy
status. HRS's naturally generated receipt reports `version=V20260819` and unchanged
`config_hash=39e90a4d1b1cc526f42d14fb3248a9b292c0aa500a36e4df76159efda25fbb5e`.
The parent release also confirmed main initialization and unchanged native
protection. A receipt alone is insufficient: main writes it before database
connection and strategy initialization. No receipt was fabricated.

## Physical backup recovery proof

Private source archive directory:
`/root/trading_system/database/postgres/backups/codex-bootstrap-20261125-1804`.

| Source file | SHA256, unchanged before/after verification |
|---|---|
| `base.tar.gz` | `a1fd3aa7fe7c9c172584423b2fce5a379245720729e4de864ba4b74301cb196a` |
| `pg_wal.tar.gz` | `970eb455643794a0da27497f0127c07c876a6c87349abab7779af719b6c81aea` |
| `backup_manifest` | `bc1e933dd17062918e9643e70c3f67c250fc8f54559f621e2010b5d41b62715d` |

Actual recovery ran on Linux amd64 with the existing production PostgreSQL 15.19
image `sha256:54e0dd8e8cddcdbb9156c2486eed5a43eccb7214d7c6828097d41ce049fb3984`.
No image was pulled. The proposed macOS ARM recovery was not used as proof.

Evidence: `rd/db-root-repair/restore-proof/20261125T124835Z-ac83c9d203/`.
It is also retained privately at
`/Users/yl/.codex/pm-cutover/20261125-89160896/db-root-repair/physical/20261125T124835Z-ac83c9d203/`.
`result.json`, `pg_verifybackup.log`, and isolated container logs establish:

- Source hashes match; `pg_verifybackup` exits 0, including required WAL parsing.
- One disposable extraction only; the precheck required estimated extraction size
  plus 4 GiB of free space. No production volume or production socket was mounted.
- Recovery container: same image, no network, no published ports, one CPU, 1 GiB
  memory, read-only root filesystem, private socket, `listen_addresses=''`, and
  read-only SQL defaults. External archive/recovery actions were disabled.
- WAL redo completed at `12/3D216258`; server became ready. Both `postgres` and
  `trading_platform` were accessible. Read-only checks found 20 roles, 6 login
  roles, and 1 superuser. OID 10 was still `trading_user`, NOSUPERUSER, LOGIN,
  NOCREATEDB, NOCREATEROLE, NOREPLICATION, BYPASSRLS; no role was changed.
- Counts from the recovered backup: `trading.trade_records=5239`,
  `hrs.hrs_positions=0`, `hrs.hrs_decision_evidence=538808`,
  `trading.hrs_equity_evidence=12368`, `trading.income_ingestion_attempt=131108`.
  No business rows or password verifiers were printed.
- Completed in 150.68 seconds. All test containers and the unpacked data copy were
  removed; an independent check confirmed cleanup and 15,787,663,360 bytes free.
  Source archives were not removed or modified.

The first attempt failed before extraction because Python tarfile's native gzip
stream reader mishandled the archive's concatenated gzip members. The corrected
reader uses `gzip.open` followed by a tar stream; the source bytes were unchanged.
That failed attempt remains recorded, separate from the successful proof.

## Account snapshot and repeatable maintenance check

Snapshot interval: **12:58:25–12:58:28 UTC**. Private files:
`rd/db-root-repair/account-before-window.json` and
`rd/db-root-repair/account-before-window-ownership.json`.

`rd/db-root-repair/account-snapshot.py` runs existing clients inside data-backend
and grid-executor. Each account made exactly three GET requests: positionRisk,
openOrders and openAlgoOrders. A GET-only subclass guards the actual request
method; no account setup/verification helper that could mutate state is called.
Main-account ownership queries use an explicit PostgreSQL read-only transaction.
HRS local positions/pending timeouts are captured without an empty-position
assertion. Credentials and signed URLs are neither printed nor saved.

| Account/symbol | Position | Ordinary orders | Protective condition orders |
|---|---:|---:|---|
| Main ETHUSDT | +0.015 | 0 | Full-quantity reduce-only STOP + 1 take-profit |
| Main XRPUSDT | +26.1 | 0 | Full-quantity reduce-only STOP + 2 take-profits |
| Main BNBUSDT | +0.04 | 0 | Full-quantity reduce-only STOP + 2 take-profits |
| Grid subaccount | Flat | 0 | 0 |

Both accounts are classic USD-M. All eight main-account algo orders were NEW,
SELL, reduce-only, and each matched exactly one `CONDITIONAL_ORDER` creation
receipt owned by `btc_eth_aggressive`; sides and recorded condition roles agreed.
The three unexpired claims also name that strategy (their recorded state is
PENDING, not rewritten by this observation). HRS local positions and timeout
records were empty. This is a timed baseline, not a claim that a live account
cannot change before admission is closed.

Repeat after the maintenance window, without overwriting before evidence:

```sh
python3 /root/trading_system/.releases/pm-window-89160896/db-root-repair/account-snapshot.py --output account-after-window.json
```

Compare position symbol/side/quantity and order identity/type/quantity/trigger,
reduce-only/close-position flags and status. Market price/unrealized-PnL changes
must not be mistaken for order mutations. Investigate fills or strategy actions
using their real execution evidence; do not reset PnL, edit snapshots, or recreate
orders merely to make the two files match.

The isolated restore proves these source archives can recover on the matching
platform. **Off-host archive transfer/hash acceptance remains with the root
task and is not asserted by this report.**
