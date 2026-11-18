-- Invoke with psql -XqAt -v ON_ERROR_STOP=1 -v window_start=... -v window_end=....
-- This export retains only XAUUSDT observations from successful batches.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT jsonb_build_object(
    'scheduled_at_ms', floor(extract(epoch FROM r.scheduled_at) * 1000)::bigint,
    'request_started_at_ms', floor(extract(epoch FROM r.request_started_at) * 1000)::bigint,
    'received_at_ms', floor(extract(epoch FROM o.received_at) * 1000)::bigint,
    'payload_hash', o.payload_hash,
    'raw_payload', o.raw_payload
)
FROM public.ai_tuner_funding_schedule_observations o
JOIN public.ai_tuner_funding_schedule_runs r USING (attempt_id)
WHERE o.symbol = 'XAUUSDT'
  AND r.status = 'success'
  AND r.scheduled_at >= :'window_start'::timestamptz - interval '5 minutes'
  AND r.scheduled_at < :'window_end'::timestamptz
ORDER BY r.scheduled_at, o.received_at, r.attempt_id;
COMMIT;
