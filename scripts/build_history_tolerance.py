"""Build-only reviewed overlays on immutable production bases; no deployment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'deploy/history-tolerance/bases.json'


def prepare(name):
    plan = json.loads(PLAN.read_text())
    group = next(g for g in plan['groups'] if g['name'] == name)
    if not re.fullmatch(r'ghcr.io/bearflower/[a-z0-9-]+@sha256:[0-9a-f]{64}', group['base']):
        raise ValueError('unpinned_base')
    sha = os.environ['SOURCE_SHA']
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('invalid_source_sha')
    destination = ROOT / '.history-build' / name
    destination.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for relative in group['files']:
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or path.suffix != '.py':
            raise ValueError('unsafe_overlay_path')
        source = ROOT / path
        target = destination / 'app' / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    deploy = f"{(int(os.environ['RUN_NUMBER']) << 8) | int(os.environ['RUN_ATTEMPT']):08X}"
    (destination/'app/VERSION').write_text(f"GIT_SHA={sha}\nDEPLOY_ID={deploy}\nBUILD_RUN_ID={os.environ['RUN_ID']}\n")
    dockerfile = f"FROM {group['base']}\nCOPY app/ /app/\nLABEL org.opencontainers.image.revision={sha}\n"
    if name == 'hrs':
        dockerfile += f'ENV HRS_IMAGE_REVISION={sha}\n'
    (destination/'Dockerfile').write_text(dockerfile)
    receipt = dict(group, source=sha, deploy_id=deploy, hashes=hashes)
    (destination/'source.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n')
    return str(destination.relative_to(ROOT))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['matrix','prepare']);parser.add_argument('--name')
    args=parser.parse_args()
    if args.action=='matrix':print(json.dumps({'include':json.loads(PLAN.read_text())['groups']},separators=(',',':')))
    else:print(prepare(args.name))


if __name__=='__main__':main()
