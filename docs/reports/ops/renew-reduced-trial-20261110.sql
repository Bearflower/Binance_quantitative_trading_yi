-- Same-ceiling 72-hour renewal. Review signed exchange inventory immediately
-- before execution. Run with psql -v end_command=ROLLBACK first, then COMMIT.
\set ON_ERROR_STOP on
BEGIN;
DO $policy$
DECLARE
    req record;
    current_policy record;
    allocation_amount numeric;
    changed integer;
    verified_equity numeric := 518.39241038;
    reason_text text := '2026-11-10 signed flat-account review; preserve reduced/capped modes and absolute ceilings for 72h trial; no risk expansion';
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('default', 7183));
    FOR req IN
        SELECT * FROM (VALUES
          ('btc_eth', 2::bigint, 'capped', 1::numeric, 0.20::numeric, 103.6556320840::numeric),
          ('btc_eth_aggressive', 2::bigint, 'reduced', 0.5::numeric, 0.15::numeric, 77.7417240630::numeric),
          ('hrs', 3::bigint, 'reduced', 0.5::numeric, 0.15::numeric, 26.18::numeric),
          ('new_coin', 2::bigint, 'capped', 1::numeric, 0.25::numeric, 129.5695401050::numeric)
        ) AS expected(strategy_id, revision, mode, multiplier, ratio_cap, ceiling)
    LOOP
        SELECT p.* INTO current_policy
        FROM trading.strategy_risk_policy p
        JOIN trading.strategy_risk_active a USING(account_id, strategy_id, revision)
        WHERE p.account_id = 'default' AND p.strategy_id = req.strategy_id;
        IF NOT FOUND OR current_policy.revision <> req.revision
          OR current_policy.mode <> req.mode
          OR current_policy.multiplier <> req.multiplier
          OR current_policy.ratio_cap <> req.ratio_cap
          OR current_policy.entry_ceiling <> req.ceiling
          OR current_policy.allow_add IS DISTINCT FROM false
          OR current_policy.freeze_allocation IS DISTINCT FROM true
          OR current_policy.expires_at <> '2026-11-12 02:54:46.815839+00'::timestamptz
        THEN
            RAISE EXCEPTION 'policy drift for %', req.strategy_id;
        END IF;

        -- The signed exchange gate verified this exact ETH fill and its full
        -- protective STOP. Its old-revision reservation remains occupied until
        -- flat; renewal must preserve that occupancy rather than release it.
        IF EXISTS (SELECT 1 FROM trading.entry_risk_reservation
                   WHERE account_id='default' AND strategy_id=req.strategy_id
                     AND state <> 'released'
                     AND NOT (req.strategy_id='btc_eth_aggressive'
                              AND client_order_id='risk_809096ebfb77a9649113a02386c8f7'
                              AND symbol='ETHUSDT' AND revision=2
                              AND state='submitting' AND quantity=0.012
                              AND margin=6.36372)) THEN
            RAISE EXCEPTION 'unexpected unresolved entry reservation for %', req.strategy_id;
        END IF;

        SELECT (entry->>'allocated_amount')::numeric INTO allocation_amount
        FROM (SELECT entries::jsonb AS entries FROM public.capital_allocation
              ORDER BY month DESC LIMIT 1) latest,
             LATERAL jsonb_array_elements(latest.entries) AS entry
        WHERE entry->>'strategy_id' = req.strategy_id;
        IF NOT FOUND OR allocation_amount IS NULL
          OR req.ceiling > LEAST(allocation_amount, verified_equity * req.ratio_cap) THEN
            RAISE EXCEPTION 'ceiling exceeds allocation or verified equity for %', req.strategy_id;
        END IF;

        INSERT INTO trading.strategy_risk_policy
          (account_id, strategy_id, revision, effective_at, expires_at,
           mode, allow_add, multiplier, ratio_cap, entry_ceiling,
           freeze_allocation, reason)
        VALUES
          ('default', req.strategy_id, req.revision + 1, now(), now() + interval '72 hours',
           req.mode, false, req.multiplier, req.ratio_cap, req.ceiling,
           true, reason_text);

        UPDATE trading.strategy_risk_active
        SET revision = req.revision + 1
        WHERE account_id='default' AND strategy_id=req.strategy_id
          AND revision=req.revision;
        GET DIAGNOSTICS changed = ROW_COUNT;
        IF changed <> 1 THEN
            RAISE EXCEPTION 'active CAS failed for %', req.strategy_id;
        END IF;

        INSERT INTO trading.strategy_risk_audit
          (account_id, strategy_id, previous_revision, revision, reason)
        VALUES ('default', req.strategy_id, req.revision, req.revision + 1, reason_text);
    END LOOP;
END
$policy$;

SELECT p.strategy_id, p.revision, p.mode, p.multiplier,
       p.ratio_cap, p.entry_ceiling, p.allow_add, p.freeze_allocation,
       p.effective_at, p.expires_at
FROM trading.strategy_risk_policy p
JOIN trading.strategy_risk_active a USING(account_id, strategy_id, revision)
WHERE p.account_id='default'
ORDER BY p.strategy_id;
:end_command;
