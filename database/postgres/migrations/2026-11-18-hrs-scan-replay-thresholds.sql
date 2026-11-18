-- Existing scans retain NULL; no retrospective threshold attribution.
ALTER TABLE public.ai_tuner_universe_scan_runs
    ADD COLUMN IF NOT EXISTS dynamic_thresholds JSONB;
