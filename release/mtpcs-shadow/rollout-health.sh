#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

root=/root/trading_system
revision=$1
[[ "$revision" =~ ^[a-f0-9]{40}$ ]]
release="$root/.releases/mtpcs-shadow/$revision"
name=trading_system-mtpcs-direction-shadow
data="$root/data/mtpcs-direction-shadow"
probe="$release/healthcheck.py"

exec 9>"$root/.deploy_tmp/compose_deploy.lock"
flock -x 9
cd "$release"
sha256sum -c files.sha256
python3 database_permissions.py audit --manifest permissions.json > permission-audit.json
python3 -c 'import json; assert json.load(open("permission-audit.json"))["ok"] is True'

old=$(docker inspect -f '{{.Id}}' "$name")
[[ $(docker inspect -f '{{.State.Status}}' "$old") == running ]]
installed=$(docker inspect -f '{{index .Config.Labels "mtpcs.health_revision"}}' "$old")
if [[ "$installed" == "$revision" ]]; then
    [[ $(docker inspect -f '{{.State.Health.Status}}' "$old") == healthy ]]
    docker exec "$old" python /opt/mtpcs-shadow-health.py /ledger/entries.sqlite3
    exit 0
fi
[[ "$installed" == '<no value>' || -z "$installed" ]]
[[ $(docker inspect -f '{{json .Config.Healthcheck}}' "$old") == null ]]
source_rev=$(docker inspect -f '{{index .Config.Labels "mtpcs.revision"}}' "$old")
[[ "$source_rev" == 285f62ba5f3d3b150b8813c3d7758f9c2c553866 ]]
bundle="$root/.releases/mtpcs-shadow/$source_rev"
(cd "$bundle" && sha256sum -c files.sha256)
image=$(docker inspect -f '{{.Config.Image}}' "$old")
[[ "$image" =~ ^ghcr.io/bearflower/trading-btc-eth@sha256:[a-f0-9]{64}$ ]]
source "$release/runtime.sh"

snapshot() {
    docker inspect -f '{{.Name}} {{.Id}} {{.Image}} {{.State.StartedAt}} {{.State.Status}}' \
        ai-tuner trading_system-btc_eth trading_system-btc_eth_aggressive trading_system-postgres
}
protected=$(snapshot)
md5sum healthcheck.py
python3 "$probe" "$data/entries.sqlite3"
ledger_state() {
    docker exec "$1" python -c 'import sqlite3; d=sqlite3.connect("file:/ledger/entries.sqlite3?mode=ro", uri=True); print(*d.execute("SELECT count(*),min(at),max(at) FROM hours").fetchone())'
}
read -r baseline first last < <(ledger_state "$old")
[[ "$baseline" =~ ^[1-9][0-9]*$ && -n "$first" && -n "$last" ]]

# Validate the frozen collector against the current K lines with an isolated ledger.
docker run --rm "${common[@]}" "$image" "${command[@]}" --ledger /tmp/health-smoke.sqlite3 --once

candidate=
rollback() {
    if [[ -n "$candidate" ]]; then
        docker rm -f "$candidate" >/dev/null || true
    fi
    if [[ $(docker inspect -f '{{.Name}}' "$old") == "/$name.rollback" ]]; then
        docker rename "$old" "$name"
    fi
    if [[ $(docker inspect -f '{{.State.Status}}' "$old") != running ]]; then
        docker start "$old" >/dev/null
    fi
}
trap 'rc=$?; if (( rc != 0 )); then rollback; fi' EXIT
candidate=$(docker create --name "$name.candidate" --label "mtpcs.revision=$source_rev" \
    --label "mtpcs.health_revision=$revision" --restart unless-stopped "${common[@]}" "${health[@]}" \
    "$image" "${command[@]}" --ledger /ledger/entries.sqlite3)
docker inspect "$old" "$candidate" | python3 -c '
import json, sys
old, new = json.load(sys.stdin)
probe = "/opt/mtpcs-shadow-health.py"
assert old["Image"] == new["Image"]
assert old["Config"]["Labels"] == {
    k:v for k,v in new["Config"]["Labels"].items() if k != "mtpcs.health_revision"
}
assert set(old["NetworkSettings"]["Networks"]) == set(new["NetworkSettings"]["Networks"])
for block, ignored in (
    # Docker create records attachment defaults until detached start.
    ("Config", {"Hostname", "Healthcheck", "Labels", "AttachStdout", "AttachStderr"}),
    ("HostConfig", {"Mounts", "OomKillDisable"}),
):
    assert {k:v for k,v in old[block].items() if k not in ignored} == {
        k:v for k,v in new[block].items() if k not in ignored
    }, block
# Docker represents the enabled-by-default OOM killer as absent or false.
assert bool(old["HostConfig"].get("OomKillDisable")) == bool(new["HostConfig"].get("OomKillDisable"))
assert old["HostConfig"]["Mounts"] == [m for m in new["HostConfig"]["Mounts"] if m["Target"] != probe]
assert {m["Destination"]:m for m in old["Mounts"]} == {
    m["Destination"]:m for m in new["Mounts"] if m["Destination"] != probe
}
probes = [m for m in new["Mounts"] if m["Destination"] == probe]
assert len(probes) == 1 and not probes[0]["RW"]
print("Same image, collector command, network, limits, security settings and existing mounts")
'

docker stop -t 10 "$old" >/dev/null
docker rename "$old" "$name.rollback"
docker rename "$candidate" "$name"
docker start "$candidate" >/dev/null

for i in {1..12}; do
    state=$(docker inspect -f '{{.State.Status}} {{.State.Health.Status}} {{.RestartCount}}' "$candidate")
    [[ "$state" == "running starting 0" || "$state" == "running healthy 0" ]]
    [[ "$state" == "running healthy 0" ]] && break
    sleep 5
done
[[ "$state" == "running healthy 0" ]]
docker exec "$candidate" md5sum /opt/mtpcs-shadow-health.py | cut -d' ' -f1 | grep -Fx "$(md5sum healthcheck.py | cut -d' ' -f1)"
docker exec "$candidate" python /opt/mtpcs-shadow-health.py /ledger/entries.sqlite3
read -r current current_first current_last < <(ledger_state "$candidate")
[[ "$current" =~ ^[1-9][0-9]*$ && "$current_first" == "$first" && ( "$current_last" > "$last" || "$current_last" == "$last" ) ]]
(( current >= baseline ))
[[ "$(snapshot)" == "$protected" ]]
if docker logs "$candidate" 2>&1 | grep -E 'Traceback|"error"' >/dev/null; then
    echo "Shadow collector logged an error during cutover" >&2
    exit 1
fi
docker rm "$old" >/dev/null
trap - EXIT
printf '%s running healthy restored_hours=%s latest_utc=%s\n' "$revision" "$current" "$current_last" | tee verified
