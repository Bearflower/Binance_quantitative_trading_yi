"""生成宿主巡检补丁；不执行脚本，不读取配置，不发送通知。"""
from pathlib import Path
import argparse

FUNCTIONS = '''# HRS 验收来源检查：自然等待样本与来源读取失败分开。
check_hrs_acceptance_source() {
    local message
    if message=$(python3 /root/trading_system/ai_tuner/ops/hrs_report_source_check.py); then
        record_result "OK" "$message"
    else
        record_result "ERROR" "${message:-HRS 验收来源检查执行失败}"
    fi
}

# 失败的一次性任务已归档，但仍保留历史失败提醒。
check_archived_task_failures() {
    local failures message
    if ! failures=$(python3 /root/trading_system/ai_tuner/ops/container_inventory.py --historical-failures); then
        record_result "ERROR" "历史任务失败清单核验失败"
        return 1
    fi
    if ! message=$(printf '%s' "$failures" | python3 -c 'import json,sys; rows=json.load(sys.stdin); print("历史一次性任务失败已归档（非当前宕机）："+str(len(rows))+" 项；"+"；".join(r["name"]+"："+r["failure_summary"] for r in rows)) if rows else None'); then
        record_result "ERROR" "历史任务失败清单格式无效"
    elif [[ -n "$message" ]]; then
        record_result "WARN" "$message"
    fi
}

'''


def patch(source):
    if 'check_hrs_acceptance_source()' in source:
        raise ValueError('已包含本次检查，拒绝重复打补丁')
    anchor = '# 主执行函数\nmain() {'
    if source.count(anchor) != 1 or source.count('    check_docker_logs\n') != 1:
        raise ValueError('巡检脚本入口已变化，必须重新核验')
    if 'container_inventory.py --names' not in source:
        raise ValueError('巡检尚未使用已核验容器清单')
    return source.replace(anchor, FUNCTIONS+anchor).replace(
        '    check_docker_logs\n',
        '    check_docker_logs\n    check_hrs_acceptance_source\n    check_archived_task_failures\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    result = patch(args.source.read_text())
    with args.output.open('x') as out:
        out.write(result)

if __name__ == '__main__':
    main()
