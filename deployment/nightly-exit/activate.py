"""Activate one image under the shared Compose lock, restoring its predecessor on failure."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def write(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2))
    os.replace(tmp, path)


def main(sha, digest):
    if not re.fullmatch('[a-f0-9]{40}', sha) or not re.fullmatch('sha256:[a-f0-9]{64}', digest):
        raise ValueError('invalid release identity')
    root = Path('/root/trading_system')
    os.chdir(root)
    with open('.deploy_tmp/compose_deploy.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pinfile = Path('.deploy_tmp/ledger-risk-active.json')
        pin = json.loads(pinfile.read_text())
        previous = Path(pin['overlay'])
        overlay = json.loads(previous.read_text())
        base = 'ghcr.io/bearflower/trading-ai-tuner@sha256:729870d6697ac86e0dc8d253b308a3d256a6b02a1ecfb61e2c6e80516737fc7a'
        if (pin['status'] != 'active' or overlay['services']['ai-tuner']['image'] != base
                or run('docker','inspect','-f','{{.Config.Image}}','ai-tuner') != base):
            raise RuntimeError('active ai-tuner predecessor changed')
        release = root / '.releases/nightly-exit' / sha
        release.mkdir(parents=True, exist_ok=True)
        image = 'ghcr.io/bearflower/trading-ai-tuner@' + digest
        run('docker','pull',image)
        overlay['services']['ai-tuner']['image'] = image
        target = release / 'images.json'
        write(target, overlay)
        compose = ['docker','compose']
        for file in pin['stacks']['ai-tuner']:
            compose.extend(['-f',file])
        def activate(file):
            run(*compose,'-f',str(file),'up','-d','--no-deps','--pull','never','ai-tuner')
        try:
            activate(target)
            for _ in range(60):
                if run('docker','inspect','-f','{{.State.Health.Status}}','ai-tuner') == 'healthy':
                    break
                time.sleep(2)
            else:
                raise RuntimeError('ai-tuner health timeout')
            if '凌晨成交补录已注册' not in subprocess.getoutput('docker logs ai-tuner 2>&1'):
                raise RuntimeError('missing registration')
            state = release / 'state.json'
            write(state, dict(sha=sha,status='complete',changed=['ai-tuner'],images={'ai-tuner':image}))
            write(pinfile, dict(pin, sha=sha, successor_sha=sha, predecessor_sha=pin['sha'],
                                overlay=str(target), state=str(state)))
        except BaseException:
            activate(previous)
            raise

if __name__ == '__main__':
    main(*sys.argv[1:])
