#!/usr/bin/env bash
set -euo pipefail
umask 077
root=/root/trading_system
revision=$1
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
bundle="$root/.deploy_tmp/mtpcs-digest-$revision"
target="$root/ai_tuner/ops"
release="$root/.releases/mtpcs-digest/$revision"
files=(mtpcs_entry_probe.py mtpcs_shadow_summary.py mtpcs_observation.py digest_outbox.py daily_digest.py)
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
 'c8bb0455ede1ecace3b4d20cbe8a6c90cde22d10374fbf6f6383708827363d33' "$target/daily_digest.py" \
 '39fb18fb318fa84359333b22ed8c5386af9ffdef52adde82b8812aa2fac2bf49' "$target/digest_outbox.py" \
 '30d6adb838d9f1574d9eda394d386f15be11b877afa0c8c282d978cd6b5584c8' /etc/systemd/system/trading-daily-digest.service \
 | sha256sum -c
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

tuner_before=$(docker inspect -f '{{.Id}} {{.Image}}' ai-tuner)
mkdir -p "$release/preview/outbox" "$release/backup"
day=$(TZ=Asia/Shanghai date +%F)
DIGEST_OUTBOX_DIR="$release/preview/outbox" python3 "$bundle/mtpcs_observation.py" --state-dir "$release/preview/state"
python3 "$bundle/daily_digest.py" --dry-run \
  --outbox-dir "$release/preview/outbox" \
  --outbox-dir /var/lib/trading-digest/outbox \
  --outbox-dir "/var/lib/trading-digest/outbox/archive/$day" \
  --outbox-dir "$root/logs/ai_tuner/digest/outbox" \
  --outbox-dir "$root/logs/ai_tuner/digest/outbox/archive/$day" > "$release/combined-preview.txt"
python3 - "$release/preview/outbox/mtpcs.json" "$release/combined-preview.txt" <<'PY'
import json,sys
from pathlib import Path
entry=json.loads(Path(sys.argv[1]).read_text())
preview=Path(sys.argv[2]).read_text()
assert entry['status'] == 'ok',entry['summary']
assert preview.count('【交易系统每日巡检汇总】') == 1 and len(preview.encode()) < 24000
assert preview.count('7/7 MTPCS 开仓归因与影子观察') == 1
assert all(s in preview for s in ('首笔实盘入场','影子账','斐波那契','只加方向一致性'))
PY
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
python3 "$target/daily_digest.py" --dry-run > "$release/installed-preview.txt"
grep -Fq '7/7 MTPCS 开仓归因与影子观察' "$release/installed-preview.txt"
! grep -Fq '读取告警 ·' "$release/installed-preview.txt"
[[ $(docker inspect -f '{{.Id}} {{.Image}}' ai-tuner) == "$tuner_before" ]]
[[ $(systemctl show trading-daily-digest.timer -p ActiveState --value) == active ]]
printf '%s\n' "$revision active server_preview_ok" > "$release/verified"
trap - ERR
cat "$release/verified"
