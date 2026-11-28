"""只读检查验收来源可用性；业务等待样本不等同于数据读取失败。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re


def assess(report, *, now, max_age_seconds):
    try:
        stamp = datetime.fromisoformat(report['generated_at'])
        if stamp.tzinfo is None or not 0 <= (now-stamp).total_seconds() <= max_age_seconds:
            return 'ERROR', 'HRS 验收报告过期或时间无效'
        if report.get('status') not in ('PENDING', 'OBSERVATION_READY'):
            return 'ERROR', 'HRS 验收报告状态无效'
        if report.get('database_snapshot_coherent') is not True or report.get('source_error_type'):
            safe = []
            for key in ('source_error_type', 'source_error_code'):
                value = report.get(key)
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z_]{1,100}', value):
                    safe.append(value)
            return 'ERROR', 'HRS 验收来源读取失败' + ('：' + ', '.join(safe) if safe else '')
        return 'OK', 'HRS 验收来源读取正常，业务验收状态：' + report['status']
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return 'ERROR', 'HRS 验收报告格式无效'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path,
                        default=Path('/root/trading_system/logs/hrs-acceptance/hrs-acceptance.json'))
    parser.add_argument('--max-age-seconds', type=int, default=1800)
    args = parser.parse_args()
    if args.max_age_seconds <= 0:
        parser.error('--max-age-seconds 必须为正整数')
    try:
        if args.report.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('报告文件超过读取上限')
        report = json.loads(args.report.read_text())
        status, message = assess(report, now=datetime.now(timezone.utc),
                                 max_age_seconds=args.max_age_seconds)
    except (OSError, ValueError):
        status, message = 'ERROR', 'HRS 验收报告无法读取'
    print(message)
    return 0 if status == 'OK' else 1


if __name__ == '__main__':
    raise SystemExit(main())
