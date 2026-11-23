#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
root=/root/trading_system
revision=$1
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
bundle="$root/.deploy_tmp/mtpcs-digest-$revision"
target="$root/ai_tuner/ops"
release="$root/.releases/mtpcs-digest/$revision"
files=(mtpcs_order_archive.py mtpcs_observation.py)
exec 9>"$root/.deploy_tmp/compose_deploy.lock"
flock -x 9

cd "$bundle"
sha256sum -c manifest.sha256
if [[ -f "$release/verified" ]]; then
    for name in "${files[@]}"; do cmp "$bundle/$name" "$target/$name"; done
    cat "$release/verified"
    exit 0
fi
if systemctl is-active --quiet trading-daily-digest.service; then
    echo 'Digest is running. Retry the host-only release after it exits.' >&2
    exit 1
fi
printf '%s  %s\n' \
 '704294a93814cecc2daac0903f771c45f686477ae44faf33da31651ae1e5c6e4' "$target/daily_digest.py" \
 '372e0d90a76a66f96649b8c900b4edfa6b615f62e636385be37b4cfe26392cd9' "$target/digest_outbox.py" \
 '9394a838e5833b17f3d55c0c945036416465f9cc38091dc1778034279651d0a5' "$target/mtpcs_observation.py" \
 '03cf09bc8dbe806234bbd61bfbfb11696bab7a7069c6de772a49d055b967b0fd' "$target/hrs_observation.py" \
 '30d6adb838d9f1574d9eda394d386f15be11b877afa0c8c282d978cd6b5584c8' /etc/systemd/system/trading-daily-digest.service \
 | sha256sum -c
for name in digest_outbox.py mtpcs_shadow_summary.py mtpcs_entry_probe.py; do cmp "$bundle/$name" "$target/$name"; done
actual=$(systemctl show trading-daily-digest.timer -p ActiveState --value)
[[ "$actual" == active ]]
systemctl cat trading-daily-digest.timer | grep -Fx 'OnCalendar=*-*-* 07:35:00 Asia/Shanghai'
permission_dir=/root/trading_system/.releases/hrs-optimization/471844f45bacbf3b313e6ecb6d6c9370b112a573
printf '%s  %s\n' \
 'edae14be4ca16b43e5cd481f96f0a5b1ebcebc29b436d8485284c208a2622d60' "$permission_dir/database_permissions.py" \
 '57a484abe1826f408ccd3cfc73d134ed18d57c8709c4d3e57bdd2a9a158322fb' "$permission_dir/permissions.json" \
 | sha256sum -c
mkdir -p "$release"
python3 "$permission_dir/database_permissions.py" audit --manifest "$permission_dir/permissions.json" > "$release/permission-audit.json"
python3 - "$release/permission-audit.json" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))['ok'] is True
PY

snapshot() {
    docker inspect -f '{{.Name}} {{.Id}} {{.Image}} {{.State.StartedAt}}' \
        ai-tuner trading_system-btc_eth trading_system-btc_eth_aggressive trading_system-postgres
}
runtime_before=$(snapshot)
mkdir -p "$release/preview/outbox" "$release/backup"
python3 "$bundle/mtpcs_observation.py" --dry-run > "$release/section-preview.txt"
grep -Fq '订单归档 · 扫描至' "$release/section-preview.txt"

for name in "${files[@]}"; do
    if [[ -e "$target/$name" ]]; then
        cp -p "$target/$name" "$release/backup/$name"
    fi
done
installed=()
rollback() {
    for name in "${installed[@]}"; do
        if [[ -f "$release/backup/$name" ]]; then
            cp -p "$release/backup/$name" "$target/$name.rollback"
            mv -f "$target/$name.rollback" "$target/$name"
        else
            rm -f "$target/$name"
        fi
    done
}
trap rollback ERR
for name in "${files[@]}"; do
    install -m 644 "$bundle/$name" "$target/$name.candidate"
    mv -f "$target/$name.candidate" "$target/$name"
    installed+=("$name")
    cmp "$target/$name" "$bundle/$name"
done
# Run the ordinary write path once without sending or writing the live outbox.
DIGEST_OUTBOX_DIR="$release/preview/outbox" python3 "$target/mtpcs_observation.py"
python3 - "$release/preview/outbox/mtpcs.json" /var/lib/trading-digest/mtpcs/order_archive_checkpoint.json <<'PYRUN'
import json,sys
from pathlib import Path
entry=json.loads(Path(sys.argv[1]).read_text())
state=json.loads(Path(sys.argv[2]).read_text())
assert '订单归档 · 扫描至' in entry['text'],entry['summary']
assert type(state['covered_until_ms']) is int
print(next(line for line in entry['text'].splitlines() if line.startswith('订单归档')))
PYRUN
python3 "$target/daily_digest.py" --dry-run > "$release/installed-preview.txt"
grep -Fq '7/8 MTPCS 开仓归因与影子观察' "$release/installed-preview.txt"
grep -Fq '当日日报结论' "$release/installed-preview.txt"
grep -Fq '订单归档 · 扫描至' "$release/installed-preview.txt"
grep -Fq '还要等多久' "$release/installed-preview.txt"
grep -Fq '8/8 HRS 四模式与影子验收' "$release/installed-preview.txt"
grep -Fq '完整对账闭仓' "$release/installed-preview.txt"
grep -Fq '同信号双侧齐全' "$release/installed-preview.txt"
grep -Fq '近七日实盘' "$release/installed-preview.txt"
if grep -Fq '订单归档（' "$release/installed-preview.txt"; then
    echo 'Production observation preview returned a read warning' >&2
    false
fi
[[ $(snapshot) == "$runtime_before" ]]
[[ $(systemctl show trading-daily-digest.timer -p ActiveState --value) == active ]]
printf '%s\n' "$revision active server_preview_ok" > "$release/verified"
trap - ERR
cat "$release/verified"
