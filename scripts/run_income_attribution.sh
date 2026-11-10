#!/bin/sh
set -eu

release_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
: "${ATTRIBUTION_IMAGE:?an immutable HRS image digest is required}"
case "$ATTRIBUTION_IMAGE" in
  ghcr.io/bearflower/trading-hrs@sha256:*) ;;
  *) echo 'invalid attribution image' >&2; exit 2 ;;
esac
output="${ATTRIBUTION_OUTPUT:-$release_dir/latest-auto.json}"
temporary="${output}.tmp.$$"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
docker run --rm --network trading-network-v2 \
  --env-file /root/trading_system/.env \
  -e DATABASE_HOST=postgres -e DATABASE_PORT=5432 \
  -e DATABASE_NAME=trading_platform -e DATABASE_USER=trading_user \
  --entrypoint python "$ATTRIBUTION_IMAGE" \
  -m scripts.reconcile_income_attribution "$@" > "$temporary"
python3 -m json.tool "$temporary" >/dev/null
mv "$temporary" "$output"
