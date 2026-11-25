"""限定发布 07:31 宿主巡检及黄金校准影子。"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path('/root/trading_system')
STAGE = Path(sys.argv[1]).resolve()
NAME = 'trading_system-mtcps-xau-calibrated-shadow'
SERVICE = 'mtcps-xau-calibrated-shadow'
ENTRY = '/app/strategies/btc_eth/shadow/main.py'
JOURNAL = Path('ai_tuner/ops/hrs_acceptance.py')
OLD_MAIN = '70b518c203241180673c5feda089748f8d7d1196b14e724e6be1306ed4e3e437'
OLD_JOURNAL = '5b74806465209826cff49ccda8a2f949aef86579391f0fdee310e8e6850f125d'


def check(ok, message):
    if not ok:
        raise RuntimeError(message)


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def inspect():
    return json.loads(output('docker', 'inspect', NAME))[0]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(source, target):
    temp = target.with_name(target.name + '.inspection-next')
    shutil.copy2(source, temp)
    os.replace(temp, target)


def compose_file(filename, settings):
    path = STAGE / filename
    path.write_text(json.dumps({'services': {SERVICE: settings}}))
    return str(path)


def main():
    os.chdir(ROOT)
    os.umask(0o077)
    lock = open(ROOT / 'compose_deploy.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX)
    original = inspect()
    labels = original['Config']['Labels']
    check(labels['com.docker.compose.service'] == SERVICE, '影子服务标识已变化')
    check(output('docker','exec',NAME,'sha256sum',ENTRY).split()[0] == OLD_MAIN, '影子入口基线已变化')
    check(digest(ROOT / JOURNAL) == OLD_JOURNAL, '巡检脚本已变化')
    audit = [sys.executable, str(STAGE/'scripts/database_permissions.py'), 'audit',
             '--manifest', str(STAGE/'database/postgres/permissions.json')]
    subprocess.run(audit, check=True)
    env = dict(s.split('=', 1) for s in original['Config']['Env'])
    check('AI_TUNER_WRITE_TOKEN' not in env and 'AI_TUNER_WRITE_TOKEN' not in os.environ,
          '影子写入凭据边界已变化')
    compose = ['docker', 'compose', '-p', labels['com.docker.compose.project']]
    for file in labels['com.docker.compose.project.config_files'].split(','):
        compose += ['-f', file]
    # 复用实际运行镜像，明确保留校准影子没有写入凭据的边界。
    base = {'image': original['Config']['Image'], 'environment': {'AI_TUNER_WRITE_TOKEN': None}}
    rollback = compose + ['-f', compose_file('rollback.json', base)]
    new_file = STAGE/'strategies/btc_eth/shadow/main.py'
    new = dict(base, volumes=[dict(type='bind', source=str(new_file), target=ENTRY, read_only=True)])
    candidate = compose + ['-f', compose_file('shadow.json', new)]
    plan = json.loads(output(*candidate,'config','--format','json', SERVICE))['services'][SERVICE]
    defaults = json.loads(output('docker','image','inspect',original['Image']))[0]['Config']
    planned_env = dict(s.split('=',1) for s in defaults.get('Env',[]))
    for key, value in plan.get('environment',{}).items():
        if value is None:
            planned_env.pop(key,None)
        else:
            planned_env[key] = value
    check(planned_env == env and plan['command'] == original['Config']['Cmd'], '影子环境或入口漂移')
    backup = STAGE/'previous-hrs-acceptance.py'
    shutil.copy2(ROOT/JOURNAL, backup)
    up = ['up','-d','--no-deps','--no-build','--pull','never',SERVICE]
    try:
        subprocess.run(candidate + up, check=True)
        for _ in range(60):
            current = inspect()
            if current['State'].get('Health',{}).get('Status') == 'healthy':
                break
            time.sleep(2)
        check(current['State']['Health']['Status'] == 'healthy', '影子健康检查失败')
        check(current['Image'] == original['Image'] and sorted(current['Config']['Env']) == sorted(original['Config']['Env']), '影子运行配置漂移')
        check(output('docker','exec',NAME,'sha256sum',ENTRY).split()[0] == digest(new_file), '影子源码不一致')
        check(digest(ROOT / JOURNAL) == OLD_JOURNAL, '巡检基线已变化')
        install(STAGE/JOURNAL, ROOT/JOURNAL)
        check(digest(ROOT/JOURNAL) == digest(STAGE/JOURNAL), '巡检源码不一致')
        subprocess.run(audit, check=True)
    except BaseException:
        if digest(ROOT/JOURNAL) == digest(STAGE/JOURNAL):
            install(backup, ROOT/JOURNAL)
        subprocess.run(rollback + up, check=True)
        raise


if __name__ == '__main__':
    main()
