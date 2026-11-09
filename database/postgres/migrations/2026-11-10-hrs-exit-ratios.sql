-- Persist the exact small-position TP geometry before HRS can trade it.
-- Existing positions remain NULL and retain their legacy exit configuration.
ALTER TABLE hrs.hrs_positions
    ADD COLUMN IF NOT EXISTS exit_ratios JSONB;
