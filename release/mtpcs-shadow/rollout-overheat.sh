#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

root=/root/trading_system
revision=$1
[[ "$revision" =~ ^[a-f0-9]{40}$ ]]
release="$root/.releases/mtpcs-shadow/$revision"
name=trading_system-mtpcs-direction-shadow
data="$root/data/mtpcs-direction-shadow"
ledger_source="$release/entry_ledger.py"
ops="$root/ai_tuner/ops"
report_files=(mtpcs_shadow_summary.py mtpcs_observation.py)

exec 9>"$root/.deploy_tmp/compose_deploy.lock"
flock -x 9
cd "$release"
sha256sum -c files.sha256
python3 database_permissions.py audit --manifest permissions.json > permission-audit.json
python3 -c 'import json; assert json.load(open("permission-audit.json"))["ok"] is True'

old=$(docker inspect -f '{{.Id}}' "$name")
[[ $(docker inspect -f '{{.State.Status}}' "$old") == running ]]
installed=$(docker inspect -f '{{index .Config.Labels "mtpcs.overheat_revision"}}' "$old")
if [[ "$installed" == "$revision" ]]; then
    [[ $(docker inspect -f '{{.State.Health.Status}}' "$old") == healthy ]]
    for f in "${report_files[@]}"; do cmp "$release/$f" "$ops/$f"; done
    cat "$release/verified"
    exit 0
fi
[[ "$installed" == '<no value>' || -z "$installed" ]]
[[ $(docker inspect -f '{{.State.Health.Status}}' "$old") == healthy ]]
health_rev=$(docker inspect -f '{{index .Config.Labels "mtpcs.health_revision"}}' "$old")
[[ "$health_rev" == 1512ab623857c3874521a8615921d403ff66e466 ]]
probe="$root/.releases/mtpcs-shadow/$health_rev/healthcheck.py"
printf '%s  %s\n' \
  'bf90392b4f880550f88a2541258d420f0b92294cdd084ce17df6d62fa5fbd373' "$ops/mtpcs_observation.py" \
  '210a82458c03628b53c99986fee97eabe79489c0a94e9bf96dd4ed8325c81af5' "$ops/mtpcs_shadow_summary.py" | sha256sum -c
systemctl is-active --quiet trading-daily-digest.timer
if systemctl is-active --quiet trading-daily-digest.service; then
    echo "The current digest is running" >&2
    exit 1
fi
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
md5sum entry_ledger.py
python3 "$probe" "$data/entries.sqlite3"
ledger_state() {
    docker exec "$1" python -c 'import sqlite3; d=sqlite3.connect("file:/ledger/entries.sqlite3?mode=ro", uri=True); print(*d.execute("SELECT count(*),min(at),max(at) FROM hours").fetchone())'
}
read -r baseline first last < <(ledger_state "$old")
[[ "$baseline" =~ ^[1-9][0-9]*$ && -n "$first" && -n "$last" ]]

# Use a forward-only boundary in production and exercise the paired collector on
# the latest eligible K lines in a disposable ledger before changing containers.
study_start=$(date -u -d '+1 hour' +%Y-%m-%dT%H:00:00)
printf '%s\n' "$study_start" > study-start-utc
docker run --rm "${common[@]}" "$image" "${command[@]}" --ledger /tmp/overheat-smoke.sqlite3 \
    --overheat-study-from "$last" --once | tee smoke.txt
grep -Eq '"observations": 10.*"overheat_observations": 5' smoke.txt
mkdir -p "$release/backup"
for f in "${report_files[@]}"; do cp -p "$ops/$f" "$release/backup/$f"; done

candidate=
installed_reports=()
rollback() {
    for f in "${installed_reports[@]}"; do
        cp -p "$release/backup/$f" "$ops/$f.rollback"
        mv -f "$ops/$f.rollback" "$ops/$f"
    done
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
    --label "mtpcs.health_revision=$health_rev" --label "mtpcs.overheat_revision=$revision" \
    --restart unless-stopped "${common[@]}" "${health[@]}" \
    "$image" "${command[@]}" --ledger /ledger/entries.sqlite3 --overheat-study-from "$study_start")
docker inspect "$old" "$candidate" | python3 -c '
import json, sys
old, new = json.load(sys.stdin)
collector = "/app/strategies/btc_eth/shadow/entry_ledger.py"
assert old["Config"]["Cmd"] + ["--overheat-study-from", sys.argv[1]] == new["Config"]["Cmd"]
assert old["Image"] == new["Image"]
assert old["Config"]["Labels"] == {
    k:v for k,v in new["Config"]["Labels"].items() if k != "mtpcs.overheat_revision"
}
assert set(old["NetworkSettings"]["Networks"]) == set(new["NetworkSettings"]["Networks"])
for block, ignored in (
    # Docker create records attachment defaults until detached start.
    ("Config", {"Hostname", "Labels", "AttachStdout", "AttachStderr", "Env", "Cmd"}),
    ("HostConfig", {"Mounts", "OomKillDisable"}),
):
    keys = (old[block].keys() | new[block].keys()) - ignored
    absent = object()
    differences = sorted(k for k in keys if old[block].get(k, absent) != new[block].get(k, absent))
    assert not differences, (block, differences)
# Docker can emit the same environment in a different enumeration order.
for env in (old["Config"]["Env"], new["Config"]["Env"]):
    assert len({item.split("=", 1)[0] for item in env}) == len(env)
assert sorted(old["Config"]["Env"]) == sorted(new["Config"]["Env"])
# Docker represents the enabled-by-default OOM killer as absent or false.
assert bool(old["HostConfig"].get("OomKillDisable")) == bool(new["HostConfig"].get("OomKillDisable"))
assert [m for m in old["HostConfig"]["Mounts"] if m["Target"] != collector] == [
    m for m in new["HostConfig"]["Mounts"] if m["Target"] != collector
]
before = {m["Destination"]:m for m in old["Mounts"]}
after = {m["Destination"]:m for m in new["Mounts"]}
assert set(before) == set(after)
assert before[collector]["Source"] == sys.argv[2] and after[collector]["Source"] == sys.argv[3]
assert not before[collector]["RW"] and not after[collector]["RW"]
assert {k:m for k,m in before.items() if k != collector} == {k:m for k,m in after.items() if k != collector}
print("Verified only the read-only collector mount and forward study flag change")
' "$study_start" "$bundle/entry_ledger.py" "$ledger_source"

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
docker exec "$candidate" md5sum /app/strategies/btc_eth/shadow/entry_ledger.py | cut -d' ' -f1 | grep -Fx "$(md5sum entry_ledger.py | cut -d' ' -f1)"
docker exec "$candidate" python /opt/mtpcs-shadow-health.py /ledger/entries.sqlite3
read -r current current_first current_last < <(ledger_state "$candidate")
[[ "$current" =~ ^[1-9][0-9]*$ && "$current_first" == "$first" && ( "$current_last" > "$last" || "$current_last" == "$last" ) ]]
(( current >= baseline ))
[[ "$(snapshot)" == "$protected" ]]
if docker logs "$candidate" 2>&1 | grep -E 'Traceback|"error"' >/dev/null; then
    echo "Shadow collector logged an error during cutover" >&2
    exit 1
fi
for f in "${report_files[@]}"; do
    install -m 644 "$release/$f" "$ops/$f.candidate"
    mv -f "$ops/$f.candidate" "$ops/$f"
    installed_reports+=("$f")
    cmp "$release/$f" "$ops/$f"
done
python3 "$ops/daily_digest.py" --dry-run > installed-preview.txt
grep -F '过热单项对照 ·' installed-preview.txt
grep -Fq '订单归档 · 扫描至' installed-preview.txt
grep -Fq '首笔实盘入场' installed-preview.txt
grep -Fq '当日日报结论' installed-preview.txt
if grep -Fq '影子账（' installed-preview.txt; then
    echo "Production digest returned a shadow ledger warning" >&2
    exit 1
fi
[[ "$(snapshot)" == "$protected" ]]
systemctl is-active --quiet trading-daily-digest.timer
docker rm "$old" >/dev/null
trap - EXIT
printf '%s running healthy restored_hours=%s latest_utc=%s overheat_start_utc=%s\n' "$revision" "$current" "$current_last" "$study_start" | tee verified
