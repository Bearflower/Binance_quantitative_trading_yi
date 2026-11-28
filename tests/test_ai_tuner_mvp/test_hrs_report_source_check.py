from datetime import datetime, timedelta, timezone
import pytest
from ai_tuner.ops.hrs_report_source_check import assess

NOW = datetime(2026, 11, 28, tzinfo=timezone.utc)

def report(**extra):
    return dict(generated_at=NOW.isoformat(), database_snapshot_coherent=True,
                status='PENDING', technical={'status': 'NOT_READY'},
                financial={'status': 'PENDING'}, **extra)

def test_natural_pending_is_not_a_source_failure():
    status, message = assess(report(), now=NOW, max_age_seconds=1800)
    assert status == 'OK' and 'PENDING' in message

def test_source_failure_is_not_hidden_by_fresh_generated_at():
    r = report(source_error_type='ReportEvidenceLimitExceeded',
               source_error_code='report_source_payload_budget_exceeded')
    r['database_snapshot_coherent'] = False
    status, message = assess(r, now=NOW, max_age_seconds=1800)
    assert status == 'ERROR' and 'report_source_payload_budget_exceeded' in message

@pytest.mark.parametrize('change', [
    {'generated_at': (NOW-timedelta(seconds=1801)).isoformat()},
    {'generated_at': (NOW+timedelta(seconds=1)).isoformat()},
    {'generated_at': 'invalid'}, {'generated_at': NOW.replace(tzinfo=None).isoformat()},
    {'database_snapshot_coherent': 'true'}, {'status': 'PASS<script>'},
])
def test_invalid_or_stale_reports_fail_closed(change):
    r = report(); r.update(change)
    assert assess(r, now=NOW, max_age_seconds=1800)[0] == 'ERROR'

def test_arbitrary_error_text_is_not_printed():
    r = report(source_error_type='password=secret', source_error_code='url?token=secret')
    r['database_snapshot_coherent'] = False
    status, message = assess(r, now=NOW, max_age_seconds=1800)
    assert status == 'ERROR' and 'secret' not in message
