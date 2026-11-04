#!/usr/bin/env python3
"""Package only reviewed release scripts/migrations from a clean exact Git commit."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.ledger_risk_manifest import MANIFEST,IMAGES,MIGRATIONS,SHA,ReleaseBlocked,validate_tree

SCRIPTS=['scripts/scoped_ledger_risk_release.py','scripts/ledger_risk_manifest.py','scripts/ledger_risk_runtime_config.py']

def read_digests(directory):
    directory=Path(directory)
    if {p.name for p in directory.iterdir()}!={name+'.txt' for name in IMAGES}:raise ReleaseBlocked('exact seven digest artifacts required')
    result={}
    for name in IMAGES:
        value=(directory/(name+'.txt')).read_text().strip()
        if not re.fullmatch(r'ghcr\.io/bearflower/trading-'+re.escape(name)+r'@sha256:[0-9a-f]{64}',value):raise ReleaseBlocked('immutable image digest required')
        result[name]=value
    return result

def package(sha,digests,output):
    if not SHA.fullmatch(sha):raise ReleaseBlocked('full SHA required')
    def git(*args):return subprocess.check_output(['git',*args])
    if git('rev-parse','HEAD').decode().strip()!=sha or git('status','--porcelain').strip():raise ReleaseBlocked('clean exact committed checkout required')
    manifest=json.loads(git('show',sha+':'+MANIFEST))
    from scripts.select_deploy_mode import _read_changes
    validate_tree(manifest,_read_changes(manifest['base_revision'],sha),lambda path:git('show',sha+':'+path),manifest['base_revision'])
    images=read_digests(digests);output=Path(output);output.mkdir(parents=True,exist_ok=False)
    for path in [MANIFEST]+SCRIPTS+MIGRATIONS:
        dest=output/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(git('show',sha+':'+path))
    (output/'image-digests.json').write_text(json.dumps(images,sort_keys=True))
    (output/'release-sha').write_text(sha)

def main():
    p=argparse.ArgumentParser();p.add_argument('--sha',required=True);p.add_argument('--digests',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    try:package(args.sha,args.digests,args.output)
    except Exception as exc:print('Release packaging blocked: '+str(exc),file=sys.stderr);return 2
    return 0
if __name__=='__main__':raise SystemExit(main())
