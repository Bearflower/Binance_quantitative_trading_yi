#!/usr/bin/env bash
set -euo pipefail
root=/root/trading_system
revision=$1
[[ "$revision" =~ ^[a-f0-9]{40}$ ]]
bundle="$root/.releases/mtpcs-shadow/$revision"
data="$root/data/mtpcs-direction-shadow"
name=trading_system-mtpcs-direction-shadow
url=http://trading_system-kline:8000/api/v1
network=trading-network-v2
original=trading_system-btc_eth
aggressive=trading_system-btc_eth_aggressive
anchor=72579274e86337787d8b43e9763ba750d9e57442

exec 9>"$root/.deploy_tmp/compose_deploy.lock"
flock -x 9
if docker container inspect "$name" >/dev/null 2>&1; then
  state=$(docker inspect -f '{{.State.Status}} {{index .Config.Labels "mtpcs.revision"}}' "$name")
  [[ "$state" == "running $revision" ]]
  printf 'Already running %s\n' "$name"
  exit 0
fi
for service in "$original" "$aggressive"; do
  docker exec "$service" cat /app/VERSION | grep -Fx "GIT_SHA=$anchor"
done
image=$(docker inspect -f '{{.Config.Image}}' "$original")
[[ "$image" =~ ^ghcr.io/bearflower/trading-btc-eth@sha256:[a-f0-9]{64}$ ]]
image_env=$(docker image inspect "$image" -f '{{range .Config.Env}}{{println .}}{{end}}' | cut -d= -f1)
if grep -Eq 'BINANCE|DATABASE|DB_PASSWORD|API_SECRET|API_KEY' <<<"$image_env"; then
  echo 'Unexpected credential environment in research base image' >&2
  exit 1
fi
install -d -m 700 -o 65534 -g 65534 "$data"
cd "$bundle"
sha256sum -c files.sha256
for variant in btc_eth btc_eth_aggressive; do
  docker exec -i "trading_system-$variant" python - "$variant" > "$variant.json" <<'PY'
import json
import logging
import sys
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
from shared.config_loader import load_strategy_config_with_metadata
config, meta = load_strategy_config_with_metadata('/app/strategies/' + sys.argv[1])
if not config:
    raise RuntimeError('effective production profile could not be exported')
reviewed = {
    'btc_eth': '309a888ad1a520e0ad3ba2001ba36c290d10c4c38ca4725838ffcfa18b92a564',
    'btc_eth_aggressive': 'fb9dacefcceed189e7e8aea1338e3e2295173ea1a535e6019f425d7552d6f14d',
}
if meta['config_hash'] != reviewed[sys.argv[1]]:
    raise RuntimeError('effective production profile changed during rollout')
profile = {key: config[key] for key in ('strategy', 'instance')}
profile['research_source'] = meta
profile['binance'] = {key: config['binance'][key] for key in ('leverage', 'position_ratio', 'order_optimization')}
print(json.dumps(profile, default=str, ensure_ascii=False))
PY
done
docker cp "$aggressive:/app/strategies/btc_eth_aggressive" "$bundle/btc_eth_aggressive"
docker cp "$aggressive:/app/shared" "$bundle/shared"
chmod -R a+rX "$bundle/btc_eth_aggressive" "$bundle/shared"
chmod 755 "$bundle"
chmod 644 ./*.py ./*.json
common=(--network "$network" --user 65534:65534 --read-only --tmpfs /tmp:rw,noexec,nosuid,size=32m
        --cap-drop ALL --security-opt no-new-privileges --pids-limit 128
        --cpus 0.5 --memory 512m --env PYTHONDONTWRITEBYTECODE=1 --env OPENBLAS_NUM_THREADS=1
        --env OMP_NUM_THREADS=1
        --mount "type=bind,src=$bundle/entry_study.py,dst=/app/strategies/btc_eth/shadow/entry_study.py,readonly"
        --mount "type=bind,src=$bundle/entry_ledger.py,dst=/app/strategies/btc_eth/shadow/entry_ledger.py,readonly"
        --mount "type=bind,src=$bundle/btc_eth_aggressive,dst=/app/strategies/btc_eth_aggressive,readonly"
        --mount "type=bind,src=$bundle/shared,dst=/app/shared,readonly"
        --mount "type=bind,src=$bundle,dst=/research,readonly"
        --mount "type=bind,src=$data,dst=/ledger")
command=(python -m strategies.btc_eth.shadow.entry_ledger --kline-url "$url"
         --original-config /research/btc_eth.json --aggressive-config /research/btc_eth_aggressive.json)
docker run --rm "${common[@]}" "$image" "${command[@]}" --ledger /tmp/smoke.sqlite3 --once
docker run -d --name "$name" --label "mtpcs.revision=$revision" --restart unless-stopped \
  "${common[@]}" "$image" "${command[@]}" --ledger /ledger/entries.sqlite3
for i in {1..20}; do
  if docker exec "$name" python -c 'import sqlite3; c=sqlite3.connect("file:/ledger/entries.sqlite3?mode=ro",uri=True); print(c.execute("SELECT at, json_array_length(records) FROM hours ORDER BY at DESC LIMIT 1").fetchone()); assert c.execute("SELECT count(*) FROM hours").fetchone()[0]>0' 2>/dev/null; then
    exit 0
  fi
  sleep 2
done
docker logs --tail 8 "$name" >&2
exit 1
