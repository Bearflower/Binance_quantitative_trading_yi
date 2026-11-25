-- HRS decision evidence is append-only and uses the independent snapshot writer.
CREATE TABLE IF NOT EXISTS hrs.hrs_decision_evidence (
 evidence_id text PRIMARY KEY, run_id text NOT NULL, kind text NOT NULL,
 symbol text, event_time timestamptz NOT NULL, available_at timestamptz NOT NULL,
 payload_sha256 text NOT NULL, payload jsonb NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(available_at >= event_time)
);
CREATE INDEX IF NOT EXISTS hrs_decision_evidence_time ON hrs.hrs_decision_evidence(event_time,kind);
CREATE INDEX IF NOT EXISTS hrs_decision_evidence_run ON hrs.hrs_decision_evidence(run_id,kind);
DO $$
BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ai_tuner_snapshot_owner') THEN
  ALTER TABLE hrs.hrs_decision_evidence OWNER TO ai_tuner_snapshot_owner;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='ai_tuner_snapshot_writer') THEN
  GRANT USAGE ON SCHEMA hrs TO ai_tuner_snapshot_writer;
  GRANT SELECT,INSERT ON hrs.hrs_decision_evidence TO ai_tuner_snapshot_writer;
  REVOKE UPDATE,DELETE,TRUNCATE ON hrs.hrs_decision_evidence FROM ai_tuner_snapshot_writer;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trading_user') THEN
  -- CREATE can inherit broad schema default ACLs before ALTER OWNER. A later
  -- GRANT SELECT does not remove them; the trading process must remain read-only.
  REVOKE INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON hrs.hrs_decision_evidence FROM trading_user;
  GRANT SELECT ON hrs.hrs_decision_evidence TO trading_user;
 END IF;
END $$;
