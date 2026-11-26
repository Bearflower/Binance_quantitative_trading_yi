import pytest
from ai_tuner.ops.inspection_log_severity import severity

@pytest.mark.parametrize("line,expected", [
    ('{"level": "warning", "event": "scan_boundary_source_denied", "error": "coverage_missing"}', "warning"),
    ("2026-11-26 [warning  ] 条件单待核实 error='no eligible owner'", "warning"),
    ("2026-11-26 [error    ] 校准失败 error='权益缺失'", "error"),
    ("XAUUSDT funding refresh unavailable for 2026-11-26T00:00:00+00:00: FundingScheduleDataError", "warning"),
    ("2026-11-26 [error] XAUUSDT funding refresh unavailable: FundingScheduleDataError", "error"),
    ("ERROR: funding collection failed", "error"),
    ('INFO: 1.2.3.4 - "GET / HTTP/1.1" 500 Internal Server Error', "error"),
    ("TimeoutError", "error"),
    ('2026-11-25 13:26:12.582 UTC [1836944] user@db [local] ERROR: invalid value "[info]"', 'error'),
    ('{"level":"info","error_code":-2011}', "info"),
])
def test_levels_are_not_exception_name_keywords(line, expected):
    assert severity(line) == expected
