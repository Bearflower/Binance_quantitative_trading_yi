-- Existing rows have unknown account scope. Keep NULL until authenticated by
-- a signed exchange order query; never backfill them from a default account.
ALTER TABLE public.condition_orders ADD COLUMN IF NOT EXISTS account_id text;
