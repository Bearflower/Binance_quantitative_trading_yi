"""隔离执行巡检 Bash 函数，验证分类和故障路径，不运行通知入口。"""
from __future__ import annotations
import argparse
import re
import shlex
import subprocess
from pathlib import Path


def verify(script: Path, classifier: Path) -> int:
    source = script.read_text()
    functions = []
    for name in ('check_project_status', 'filter_expected_order_lookup_debug', 'check_docker_logs'):
        match = re.search(r'^' + name + r'\(\) \{\n.*?^\}', source, re.M | re.S)
        if match is None:
            raise ValueError('巡检函数缺失: ' + name)
        functions.append(match.group())
    stub = r'''
DOCKER_CONTAINER_PATTERN=''
record_result() { printf '%s|%s\n' "$1" "$2"; }
python3() {
    if [[ "$1" == */container_inventory.py ]]; then
        if [[ "$CASE" == inventory_failure ]]; then return 1; fi
        printf 'app\n'
    else
        command python3 "$CLASSIFIER" "${@:2}"
    fi
}
docker() {
    if [[ "$1" == ps ]]; then printf 'app\n'; return; fi
    if [[ "$1" == inspect ]]; then
        case "$3" in
            *State.Status*) if [[ "$CASE" == stopped ]]; then echo exited; else echo running; fi;;
            *State.Health.Status*) echo healthy;;
            *State.Running*) echo true;;
            *Config.Image*) echo example/app:current;;
        esac
        return
    fi
    if [[ "$1" == logs ]]; then
        printf '%s\n' "$LOG_FIXTURE"
        return
    fi
    return 2
}
'''
    cases = [
        ('healthy', 'check_project_status', '', ['OK|Docker 容器运行正常：app'], ['ERROR|']),
        ('stopped', 'check_project_status', '', ['ERROR|Docker 容器未运行：app'], []),
        ('inventory_failure', 'check_project_status', '', ['ERROR|容器清单核验失败'], ['运行正常']),
        ('inventory_failure', 'check_docker_logs', '', ['ERROR|容器清单核验失败'], ['日志无错误']),
        ('warning', 'check_docker_logs', 'XAUUSDT funding refresh unavailable for 2026-11-26T00:00:00+00:00: FundingScheduleDataError', ['WARN|'], ['ERROR|']),
        ('error', 'check_docker_logs', '2026-11-26 [error    ] 校准失败 error=缺少期初权益', ['ERROR|'], []),
        ('warning', 'check_docker_logs', '{"level":"warning","event":"scan_boundary_source_denied","error_code":"coverage_missing"}', ['WARN|'], ['ERROR|']),
        ('http500', 'check_docker_logs', 'INFO: 1.2.3.4 - "GET / HTTP/1.1" 500 Internal Server Error', ['ERROR|'], []),
        ('http404', 'check_docker_logs', 'INFO: 1.2.3.4 - "GET /error.aspx HTTP/1.1" 404 Not Found', ['OK|'], ['ERROR|']),
    ]
    for case, function, logs, present, absent in cases:
        code = ('\n'.join(functions) + '\n' + stub + '\nCLASSIFIER=' + shlex.quote(str(classifier.resolve()))
                + '\nCASE=' + shlex.quote(case) + '\nLOG_FIXTURE=' + shlex.quote(logs) + '\n' + function)
        result = subprocess.run(['bash', '-c', code], text=True, capture_output=True, timeout=20)
        for value in present:
            assert value in result.stdout, (case, result.stdout, result.stderr)
        for value in absent:
            assert value not in result.stdout, (case, result.stdout, result.stderr)
    print(f'{len(cases)} 个巡检适配场景通过；未执行通知入口')
    return len(cases)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('script', type=Path)
    parser.add_argument('classifier', type=Path)
    args = parser.parse_args()
    verify(args.script, args.classifier)
