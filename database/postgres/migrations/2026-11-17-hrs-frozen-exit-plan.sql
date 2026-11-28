-- Null is intentional for legacy positions: do not invent their entry-time plan.
ALTER TABLE hrs.hrs_positions ADD COLUMN IF NOT EXISTS exit_plan jsonb;
COMMENT ON COLUMN hrs.hrs_positions.exit_plan IS 'Versioned entry-time exit parameters, ATR and planned reference price. NULL denotes legacy/unproven geometry.';
