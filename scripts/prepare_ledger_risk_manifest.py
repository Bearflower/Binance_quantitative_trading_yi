#!/usr/bin/env python3
"""Create a BLOCKED review draft after rebase; never invent production evidence."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.ledger_risk_manifest import template,MANIFEST,MARKER,MIGRATIONS,digest,SHA,ReleaseBlocked

def prepare(base):
    def git(*args):return subprocess.check_output(['git',*args]).decode()
    base=git('rev-parse',base).strip()
    if not SHA.fullmatch(base):raise ReleaseBlocked('full baseline required')
    git('merge-base','--is-ancestor',base,'HEAD')
    marker=Path(MARKER);marker.parent.mkdir(parents=True,exist_ok=True);marker.write_text(MANIFEST+'\n')
    path=Path(MANIFEST);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and json.loads(path.read_text()).get('expected_production'):raise ReleaseBlocked('refuse to overwrite reviewed production evidence')
    path.write_text(json.dumps(template()))
    changes={}
    for line in git('diff','--no-renames','--name-status',base).splitlines():
        status,name=line.split('\t');changes[name]=status
    for name in git('ls-files','--others','--exclude-standard').splitlines():changes[name]='A'
    manifest=template();manifest['base_revision']=base
    manifest['allowed_changes']=[dict(status=status,path=name) for name,status in sorted(changes.items())]
    sources=set(git('ls-files','*.py').splitlines())|{name for name,status in changes.items() if status!='D'}|set(MIGRATIONS)
    manifest['source_checksums']={name:digest(Path(name).read_bytes()) for name in sorted(sources-{MANIFEST,MARKER}) if Path(name).is_file()}
    manifest['migrations']=[dict(path=name,sha256=digest(Path(name).read_bytes())) for name in MIGRATIONS]
    from datetime import datetime,timezone,timedelta
    manifest['historical_reports']=[dict(account_id=None,start_ms=int(datetime(2026,month,1,tzinfo=timezone(timedelta(hours=8))).timestamp()*1000),end_ms=int(datetime(2026,month+1,1,tzinfo=timezone(timedelta(hours=8))).timestamp()*1000),net_usdt=net,source_proof_sha256=None) for month,net in zip((8,9,10),('-115.45319304','-6.65665904','5.80965338'))]
    path.write_text(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',required=True)
    prepare(p.parse_args().base)
