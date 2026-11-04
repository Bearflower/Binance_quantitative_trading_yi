# Grid realtime M3 evidence and preregistration

Status: **draft, not frozen**. Production remains `shadow`; `shadow_research.enabled=false`.

## Evidence inventory at 2026-11-04 (Beijing time)

| Evidence | Observed state | Research use |
|---|---|---|
| ETHUSDT `price_samples_1s` | 2026-10-08 through 2026-11-03: 27 complete Beijing calendar days in the prior read-only audit | Descriptive development data; the 30-day accumulation gate was not yet met |
| Recent `agg_trades` | Rolling 72-hour ID interval checked; a recent 24-hour sample sum of 904,593 matched the retained aggregate trade count | Supports only the retained interval; it does not attest older raw IDs |
| Queue checkpoints | Persistence began 2026-11-03 09:42; the prior audit found no gap or overflow in the covered period | Less than 72 hours of queue evidence; earlier history cannot be reconstructed |
| Reference eligibility | A numeric snapshot sent 2026-11-03 22:05 became `STALE` by 2026-11-04 09:55; the six-hour `reference.max_age_seconds` rule suspended proactive termination advice | Session `ACTIVE`, matching reference ID, fresh heartbeat and `health={}` cannot override `STALE` |
| Risk events | One risk event observed in the prior production audit | Insufficient to establish alert burden, coverage or false-positive rates |

These observations are a dated snapshot, not a claim that the current production state remains the same. The older schema did not save reference eligibility at each checkpoint. Its historical statuses must be reported as `UNKNOWN`. Schema v3 saves status and feed/storage health prospectively; `aggtrade_integrity_checkpoint` saves rolling aggregate-trade ID continuity prospectively. The first new checkpoint after deployment is the start of each new evidence stream. Historical gaps are not backfilled.

## Daily evidence report

Run `scripts/audit_grid_shadow_window.py --grid-db GRID_DB --agg-db AGG_DB --start-ms START --end-ms END` against read-only copies or production databases opened in read-only mode. Use Beijing calendar-day boundaries. The report separates:

- `VALID`, `STALE`, `NO_ACTIVE_REFERENCE`, `SYNC_UNCERTAIN`, `MISSING`, `INVALID` and `UNKNOWN` observation intervals;
- `status_observed_ms` and `numeric_reference_availability` use the full Beijing-day window as denominator; `unobserved_ms` remains separate. The last checkpoint state is carried forward only to the next checkpoint when the gap is at most `max_gap_ms` (default 5 seconds). Across a larger gap, at most the first second is attributed; the rest stays unobserved. This is an audit estimate with up to one checkpoint interval of transition uncertainty, not proof of an exact state change millisecond. `eligible_time_ms` additionally requires a healthy feed, normal storage and completed warm-up;
- feed, storage, queue and warm-up exclusions, plus checkpoint gaps;
- eligible and excluded non-health risk events, counted by persisted `event.decision_ms` (including direct urgent events without `first_held_ms`); legacy events missing that field are reported separately and cannot be assigned to a validation window;
- complete one-second sample coverage and aggregate-trade ID continuity within the retained range or a continuous chain of persisted attestations.

`evidence_complete=false` or an `UNKNOWN` period means evidence is insufficient. A complete market second axis does not by itself prove every raw trade was captured. `data_window_eligible=true` only describes the queue, market and reference evidence. It does not certify the shadow boundary, preregistration or independent validation, so the report always leaves `formal_m3_eligible=false`. A valid subinterval may be described separately without declaring M3 passed.

## Independent validation preregistration form

Fill, review and timestamp this form **before** the first independent validation day. Once frozen, do not edit targets after looking at validation results. Existing observed data may be used for development and descriptive analysis, not relabeled as independent validation.

| Target | Frozen value / definition | Decision |
|---|---|---|
| Maximum alert burden per day, by normal/urgent level | TBD | Pending |
| Actionable coverage after subtracting 60 seconds of reaction time | TBD | Pending |
| Normal alert false-positive ceiling and 15/30/60-minute observation window | TBD | Pending |
| Urgent alert false-positive ceiling and observation window | TBD | Pending |
| Minimum event count per market regime and confidence-interval width | TBD | Pending |
| Minimum numeric-reference availability (VALID time and eligible opportunity share) | ≥90% VALID time across all independent validation days, user selected 2026-11-04; report opportunity-conditioned share separately; missing evidence and downtime remain in the full-time denominator | Product target selected; full preregistration pending |
| Candidate profiles and selection rule | TBD | Pending |
| Frozen at (Beijing time), author, commit hash | TBD | Pending |
| Independent period start, next Beijing calendar day after freeze | TBD | Pending |

M3 first requires at least 30 complete accumulation days. Independent validation then needs at least 30 complete Beijing calendar days after the freeze and enough events in each preregistered regime. Report missing days, status exclusions and event counts. M4 remains gated on M3 and the required AC checks; no change here enables formal `alert`.
