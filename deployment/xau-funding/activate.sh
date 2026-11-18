#!/usr/bin/env bash
set -euo pipefail
cd /root/trading_system

sha="$1"
digest="$2"
collector_sha="$3"
[[ "$sha" =~ ^[0-9a-f]{40}$ && "$digest" =~ ^sha256:[0-9a-f]{64}$ && "$collector_sha" =~ ^[0-9a-f]{64}$ ]]
image="ghcr.io/bearflower/trading-ai-tuner@$digest"
previous="ghcr.io/bearflower/trading-ai-tuner@sha256:ec831eabd3d386c4121278753e77c8edfdd7ce52080ef40735f1d0a2a795a2b9"
service=ai-tuner-funding-schedule
current_image() { docker inspect -f '{{.Image}}' "$service"; }
health() { docker inspect -f '{{.State.Status}} {{.State.Health.Status}}' "$service"; }
running() { docker ps --format '{{.Names}} {{.ID}}' | awk '$1 != "ai-tuner-funding-schedule"' | sort; }
wait_healthy() {
  for _ in $(seq 1 40); do
    if [[ "$(health)" == "running healthy" ]]; then return 0; fi
    sleep 3
  done
  return 1
}

mkdir -p .deploy_tmp
exec 9>.deploy_tmp/compose_deploy.lock
flock -n 9
old_id="$(docker image inspect -f '{{.Id}}' "$previous")"
[[ "$(current_image)" == "$old_id" && "$(health)" == "running healthy" ]]
before="$(running)"
docker pull "$image"
new_id="$(docker image inspect -f '{{.Id}}' "$image")"
[[ "$(docker run --rm --network none --entrypoint sha256sum "$image" /app/ai_tuner/funding_schedule/collector.py | cut -d ' ' -f 1)" == "$collector_sha" ]]
docker run --rm --network none --entrypoint cat "$image" /app/VERSION | grep -Fx "GIT_SHA=$sha"
release=".releases/xau-funding/$sha"
mkdir -p ".releases/xau-funding"
mkdir "$release"
chmod 700 "$release"
printf '%s\n' "$before" > "$release/excluded-containers.txt"
printf 'services:\n  %s:\n    image: %s\n' "$service" "$previous" > "$release/rollback.yml"
printf 'services:\n  %s:\n    image: %s\n' "$service" "$image" > "$release/image.yml"
compose=(docker compose -f docker-compose.yml -f docker-compose.logging.yml --profile funding-schedule)

rollback() {
  rc=$?
  trap - EXIT
  printf 'XAU_FUNDING_ACTIVATION_FAILED sha=%s rc=%s\n' "$sha" "$rc" >&2
  if "${compose[@]}" -f "$release/rollback.yml" up -d --no-deps --pull never "$service" && wait_healthy && [[ "$(current_image)" == "$old_id" ]]; then
    printf 'XAU_FUNDING_ROLLBACK_OK\n' >&2
  else
    printf 'XAU_FUNDING_ROLLBACK_FAILED\n' >&2
  fi
  exit "$rc"
}
trap rollback EXIT
"${compose[@]}" -f "$release/image.yml" up -d --no-deps --pull never "$service"
wait_healthy
[[ "$(current_image)" == "$new_id" ]]
[[ "$(docker exec "$service" sha256sum /app/ai_tuner/funding_schedule/collector.py | cut -d ' ' -f 1)" == "$collector_sha" ]]
[[ "$(running)" == "$before" ]]
trap - EXIT
printf 'XAU_FUNDING_DEPLOY_SUCCESS sha=%s digest=%s\n' "$sha" "$digest" | tee "$release/activation.txt"
