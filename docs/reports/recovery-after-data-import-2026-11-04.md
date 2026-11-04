# Third-stage recovery: successful data import with noisy stdout

The preceding after-MELI recovery failed after replacing data-backend because the original consumer gate compared the entire stdout with `canonical-reader-import-ok`. The actual successful Python import emits three structlog configuration lines followed by that marker. This new entrypoint corrects the gate only in its recovery class; it does not patch or silently replay the original release executable.

## Exact boundary

The only accepted preceding state is `after-meli-recovery/state.json` with original release SHA/manifest digest, status failed, step ReleaseBlocked, no_automatic_rollback true and changed services exactly in this order:

1. execution-facts-collector
2. hrs-strategy
3. btc-eth-aggressive-strategy
4. btc-eth-strategy
5. new-coin-strategy
6. data-backend

The original activating pin, manifest, images, Compose/effective configurations and earlier evidence remain bound by the nested approvals. A new complete current container snapshot is separately approved and checked. Every upgraded service must be healthy and use its service-mapped reviewed image with source proof; unmodified/excluded service identities remain original and tuner remains stopped. All four upgraded strategies must have structured startup evidence. Data-backend must prove its actual canonical reader import in its own container. The inherited fresh signed exposure/order/close checks retain the authorized MELI close, ARIA/PFE protection and unchanged allocation. No economic exception is broadened.

## Consumer import parser

The last nonempty output line must equal `canonical-reader-import-ok` exactly. Prior lines may be structured JSON events or recognized `YYYY-MM-DD HH:MM:SS [debug|info|warning padded] message` structlog records. Timestamps are parsed. Unrecognized output and traceback/error/critical/fatal text reject the probe; a successful marker embedded in JSON or followed by further nonempty output does not pass. Stdout and stderr are captured together, so stderr diagnostics cannot silently escape review. The import assertion still checks DataService's actual method identity and the canonical published-report reader binding. It executes in the named consumer's reviewed container.

## Approval and invocation

Run the separate reviewed code checkout:

```sh
python scripts/recover_after_data.py --root /root/trading_system --bundle ORIGINAL_BUNDLE --approval PRIVATE_DATA_APPROVAL.json
```

Default is read-only validation with no new recovery artifact directory. Only `--execute` performs the remaining rollout. Both modes hold the existing deployment lock. `after-data-recovery/` must not already exist; an interrupted attempt cannot be blindly rerun.

Approval fields:

- `reason`: review of this precise import-marker failure
- `recovery_code_sha256`: raw SHA256 of reviewed recover_after_data.py
- `meli_approval`: complete after-MELI approval, retaining all signed evidence paths and prior approvals
- `previous_artifacts`: raw SHA256 for `state.json`, `approval.json`, `validated-live.json`, `strategy_gate-proof.json`, and each of the four `<strategy-service>-startup.json` files under the existing after-meli-recovery directory
- `containers`: complete project service mapping to reviewed `{id,image_id,started_at,config_sha256}`; config digest is canonical `{Config,HostConfig}`

Regenerate and review code hashes in nested approvals to match this code set. Two small inherited extension points changed: AfterMeliRuntime's audit directory is a class attribute (old default unchanged), and AfterHRSRuntime's previously fixed pair of upgraded services is returned by a method (old default unchanged). The third-stage subclass supplies its new directory and six upgraded services. Original recorded approvals/state/proofs are never rewritten to match new hashes.

## Remaining actions and failures

The exact suffix is data-backend consumer_gate; dashboard-api up and consumer_gate; freeze_gate; ai-tuner up; final_gate; success. It does not re-run up for any of the six completed services, policy activation, migrations, collector startup or HRS startup/recreation. Strategy startup is read-only revalidated.

The third-stage directory privately records full approval, fresh signed/consumer/startup evidence and progress. Consumer gate records its successful import output, recent logs and existing DB/exchange probe before advancing. The inherited final gate still checks audited historical report publication/HTTP response, authorized MELI endpoint, allocation and excluded containers. Failure preserves pins and records a separate failed state. There is no automatic rollback, order write or permissive second attempt.

## Verification

TDD reproduced the noisy-stdout failure before implementation. Targeted tests cover actual timestamped text and JSON logs, misleading markers, errors, exact preceding state/artifacts, container approval, target-specific import, four strategy startup proofs, suffix order, dry-run/execute separation and failures. The new module achieved 100% measured statement and branch coverage (94 statements, 14 branches); Docker/exchange calls are mocked. The complete scoped suite is rerun for delivery. No production execution or push was performed by the implementer.
