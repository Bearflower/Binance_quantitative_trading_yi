#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

revision=$1
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
root=/root/trading_system
ops="$root/ai_tuner/ops"
release="$root/.releases/mtpcs-digest-windows/$revision"
files=(mtpcs_shadow_summary.py mtpcs_observation.py)
exec 9>"$root/.deploy_tmp/compose_deploy.lock"
flock -x 9

cd "$release"
sha256sum -c files.sha256
if [[ -f verified ]]; then
    for file in "${files[@]}"; do cmp "$file" "$ops/$file"; done
    cat verified
    exit 0
fi

systemctl is-active --quiet trading-daily-digest.timer
if systemctl is-active --quiet trading-daily-digest.service; then
    echo "Daily digest is running" >&2
    exit 1
fi
printf '%s  %s\n' \
    '4719cbb1ff047a0bee4aa3d02a5009652376051f705a727f20d5d6a532c72990' "$ops/mtpcs_observation.py" \
    '5468aeb6f60f3a5a80f38783d3a52e369cca5710b1d8db900db50ab7e86ca802' "$ops/mtpcs_shadow_summary.py" \
    | sha256sum -c
python3 database_permissions.py audit --manifest permissions.json > permission-audit.json
python3 -c 'import json; assert json.load(open("permission-audit.json"))["ok"] is True'

snapshot() {
    docker inspect -f '{{.Name}} {{.Id}} {{.Image}} {{.State.StartedAt}} {{.State.Status}}' \
        trading_system-mtpcs-direction-shadow trading_system-btc_eth trading_system-btc_eth_aggressive ai-tuner
}
runtime_before=$(snapshot)
mkdir -p backup
for file in "${files[@]}"; do cp -p "$ops/$file" "backup/$file"; done
installed=()
rollback() {
    for file in "${installed[@]}"; do
        cp -p "backup/$file" "$ops/$file.rollback"
        mv -f "$ops/$file.rollback" "$ops/$file"
    done
}
trap 'rc=$?; if (( rc != 0 )); then rollback; fi' EXIT

for file in "${files[@]}"; do
    install -m 644 "$file" "$ops/$file.candidate"
    mv -f "$ops/$file.candidate" "$ops/$file"
    installed+=("$file")
    cmp "$file" "$ops/$file"
done

python3 "$ops/daily_digest.py" --dry-run > installed-preview.txt
grep -Fq 'MTPCS 开仓归因与影子观察' installed-preview.txt
grep -F '全量影子账 · 北京时间 ' installed-preview.txt
grep -F '过热同期对照 · 北京时间 ' installed-preview.txt | grep -F '（两组共同截止）'
grep -E '同期激进基线 [0-9]+ 笔结算，胜率.*只加过热禁开 [0-9]+ 笔结算，胜率' installed-preview.txt
grep -Fq '首笔实盘入场' installed-preview.txt
grep -Fq '当日日报结论' installed-preview.txt
[[ "$(snapshot)" == "$runtime_before" ]]
systemctl is-active --quiet trading-daily-digest.timer
printf '%s installed preview_verified\n' "$revision" > verified
trap - EXIT
cat verified
