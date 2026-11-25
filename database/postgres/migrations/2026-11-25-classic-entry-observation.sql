BEGIN;
ALTER TABLE trading.entry_order_observation
    DROP CONSTRAINT IF EXISTS entry_order_observation_source_check,
    ADD CONSTRAINT entry_order_observation_source_check
        CHECK (source IN ('signed_pm_get_order_v1', 'signed_classic_get_order_v1'));
COMMIT;
