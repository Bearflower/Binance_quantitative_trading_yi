"""Static guardrails for the append-only funding schedule evidence migration."""

from pathlib import Path
import re


SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "database/postgres/init-scripts/14-ai-tuner-funding-schedule-evidence.sql"
)


def _ddl() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def test_evidence_tables_have_keys_timestamps_hashes_and_constraints():
    ddl = _ddl()
    assert "CREATE TABLE IF NOT EXISTS public.ai_tuner_funding_schedule_runs" in ddl
    assert "CREATE TABLE IF NOT EXISTS public.ai_tuner_funding_schedule_observations" in ddl
    assert re.search(r"attempt_id\s+UUID\s+NOT NULL", ddl, re.I)
    assert "PRIMARY KEY (attempt_id)" in ddl
    assert "PRIMARY KEY (attempt_id, symbol)" in ddl
    for column in ("scheduled_at", "received_at", "source_time", "next_funding_time"):
        assert re.search(rf"\b{column}\s+TIMESTAMPTZ\s+NOT NULL", ddl, re.I)
    assert ddl.count("CHAR(64) NOT NULL") >= 2
    assert "status IN ('success', 'failed')" in ddl
    assert "failure_code IS NOT NULL" in ddl
    assert "next_funding_time > source_time" in ddl
    assert "source_time <= received_at" in ddl
    assert "REFERENCES public.ai_tuner_funding_schedule_runs(attempt_id)" in ddl


def test_writer_is_limited_to_append_only_evidence_and_universe_reads():
    ddl = _ddl()
    assert "CREATE ROLE ai_tuner_funding_writer LOGIN" in ddl
    for table in ("runs", "observations"):
        name = f"public.ai_tuner_funding_schedule_{table}"
        assert f"REVOKE ALL ON {name} FROM PUBLIC, trading_user" in ddl
        assert f"GRANT SELECT, INSERT ON {name} TO ai_tuner_funding_writer" in ddl
        assert f"GRANT SELECT ON {name} TO trading_user" in ddl
    for table in ("ai_tuner_universe_scan_runs", "ai_tuner_universe_snapshots"):
        assert f"GRANT SELECT ON public.{table} TO ai_tuner_funding_writer" in ddl
    assert not re.search(
        r"GRANT\s+[^;]*\b(?:UPDATE|DELETE|TRUNCATE)\b[^;]*\bTO\s+ai_tuner_funding_writer\b",
        ddl,
        re.I,
    )
