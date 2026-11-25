"""只更新共享市值服务的资产名录，并验证下游读取的真实报价。"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path('/root/trading_system')
STAGE = Path(sys.argv[1]).resolve()
NAME = 'trading_system-market-cap'
SERVICE = 'market-cap-service'
TARGET = '/app/services/market_cap/assets.json'
SYMBOLS = {'SAHARAUSDT': 'sahara-ai', 'MONUSDT': 'monad', 'ONUSDT': 'orochi-network'}
BASE_SHA = 'a22539caa7bff0d23692f56a036269b31397d15e1763c463aa21e7e712dd78d1'


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def inspect():
    return json.loads(output('docker', 'inspect', NAME))[0]


def verify_quotes():
    read = """import sys, json, urllib.request
body = json.dumps({'symbols': sys.argv[2:]}).encode()
request = urllib.request.Request('http://127.0.0.1:8080/' + sys.argv[1], data=body,
                                 headers={'Content-Type':'application/json'})
print(json.dumps(json.load(urllib.request.urlopen(request, timeout=45))))
"""
    for route in ('collect', 'read'):
        quotes = json.loads(output('docker','exec',NAME,'python','-c',read,route,*SYMBOLS))
        require(set(quotes) == set(SYMBOLS), '缺少报价')
        for symbol, key in SYMBOLS.items():
            q = quotes[symbol]
            require(q.get('provider') == 'coingecko' and q.get('asset_id') == key and
                    q.get('is_proxy') is False and q['value'] > 0 and
                    0 <= time.time()-q['as_of'] <= 3600, '报价校验失败 ' + symbol)
        print(route, {s:q['asset_id'] for s,q in quotes.items()}, flush=True)


def main():
    os.chdir(ROOT)
    os.umask(0o077)
    lock = open(ROOT/'compose_deploy.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX)
    old = inspect()
    labels = old['Config']['Labels']
    require(labels['com.docker.compose.service'] == SERVICE, '容器标识变化')
    base = output('docker','exec',NAME,'cat',TARGET)
    require(hashlib.sha256((base+'\n').encode()).hexdigest() == BASE_SHA, '已有名录发生变化')
    prior = json.loads(base)
    asset_file = STAGE/'services/market_cap/assets.json'
    body = asset_file.read_bytes()
    data = json.loads(body)
    require(set(data) - set(prior) == set(SYMBOLS) and
            all(data.get(key) == value for key,value in prior.items()), '名录变更超出范围')
    audit = [sys.executable, str(STAGE/'scripts/database_permissions.py'), 'audit',
             '--manifest', str(STAGE/'database/postgres/permissions.json')]
    subprocess.run(audit, check=True)
    compose = ['docker','compose','-p',labels['com.docker.compose.project']]
    for file in labels['com.docker.compose.project.config_files'].split(','):
        compose += ['-f',file]
    overlay = STAGE/'assets.json.compose'
    overlay.write_text(json.dumps({'services': {SERVICE: {'image': old['Config']['Image'],
        'volumes':[{'type':'bind','source':str(asset_file),'target':TARGET,'read_only':True}]}}}))
    candidate = compose + ['-f',str(overlay)]
    plan = json.loads(output(*candidate,'config','--format','json',SERVICE))['services'][SERVICE]
    env = dict(s.split('=',1) for s in old['Config']['Env'])
    defaults = json.loads(output('docker','image','inspect',old['Image']))[0]['Config']
    projected = dict(s.split('=',1) for s in defaults['Env'])
    projected.update(plan['environment'])
    require(projected == env and plan['command'] == old['Config']['Cmd'], '服务配置漂移')
    up = ['up','-d','--no-deps','--no-build','--pull','never',SERVICE]
    try:
        subprocess.run(candidate + up, check=True)
        for _ in range(60):
            current = inspect()
            if current['State']['Health']['Status'] == 'healthy':
                break
            time.sleep(2)
        require(current['State']['Health']['Status'] == 'healthy' and current['Image'] == old['Image'],
                '服务健康或镜像校验失败')
        require(output('docker','exec',NAME,'sha256sum',TARGET).split()[0] ==
                hashlib.sha256(body).hexdigest(), '容器名录校验失败')
        verify_quotes()
        subprocess.run(audit, check=True)
    except BaseException:
        subprocess.run(compose + up, check=True)
        raise


if __name__ == '__main__':
    main()
