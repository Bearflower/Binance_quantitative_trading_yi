#!/usr/bin/env bash
set -euo pipefail
variant=$1
base_id=$2
repository=$3
revision=$4
deploy_id=$5
bundle=release/mtpcs-entry-intent
scratch=$(mktemp -d)
container=$(docker create "$base_id")
trap 'docker rm "$container" >/dev/null; rm -rf "$scratch"' EXIT
export variant scratch bundle BASE_CONTAINER="$container"
python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path
import subprocess

bundle=Path(os.environ['bundle']).resolve()
scratch=Path(os.environ['scratch'])
variant=os.environ['variant']
for name, proof in json.loads((bundle/'sources.json').read_text())[variant].items():
    target=scratch/name
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['docker','cp',os.environ['BASE_CONTAINER']+':/app/'+name,str(target)],check=True)
    if hashlib.sha256(target.read_bytes()).hexdigest()!=proof['before']:
        raise ValueError('unexpected upstream source: '+name)
    subprocess.run(['git','apply','--unidiff-zero',str(bundle/variant/(name+'.diff'))],cwd=scratch,check=True)
    if hashlib.sha256(target.read_bytes()).hexdigest()!=proof['after']:
        raise ValueError('unexpected candidate source: '+name)
PY
printf 'GIT_SHA=%s\nDEPLOY_ID=%s\n' "$revision" "$deploy_id" > "$scratch/VERSION"
docker tag "$base_id" "mtpcs-base:$variant"
printf 'FROM mtpcs-base:%s\nCOPY . /app/\nRUN python -m compileall -q /app/shared /app/strategies\n' "$variant" > "$scratch/Dockerfile"
printf 'Dockerfile\n' > "$scratch/.dockerignore"
docker build --pull=false -t "$repository:entry-intent-$revision" "$scratch" >&2
docker push "$repository:entry-intent-$revision" >&2
docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$repository:entry-intent-$revision" | awk -v prefix="$repository@" 'index($0,prefix)==1'
