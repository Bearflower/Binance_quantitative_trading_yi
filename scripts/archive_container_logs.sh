#!/usr/bin/env bash
set -euo pipefail

archive_dir=${1:?usage: archive_container_logs.sh ARCHIVE_DIR [COMPOSE_PROJECT]}
project=${2:-trading_system}
umask 077

if [ -e "$archive_dir" ]; then
  echo "archive already exists: $archive_dir" >&2
  exit 2
fi
mkdir -p "$archive_dir"
chmod 700 "$archive_dir"
printf 'service\tcontainer_id\tfirst_log_utc\tlast_log_utc\tbytes\tsha256\n' > "$archive_dir/manifest.tsv"

names=$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.Names}}')
if [ -z "$names" ]; then
  echo "no containers found for Compose project: $project" >&2
  exit 3
fi

count=0
for name in $names; do
  driver=$(docker inspect -f '{{.HostConfig.LogConfig.Type}}' "$name")
  [ "$driver" = json-file ] || continue
  id=$(docker inspect -f '{{.Id}}' "$name")
  log_file="$archive_dir/$name.log"
  docker logs --timestamps "$name" > "$log_file" 2>&1
  first=$(sed -n '1{s/ .*//;p;}' "$log_file")
  last=$(tail -n 1 "$log_file" | sed 's/ .*//')
  bytes=$(wc -c < "$log_file" | tr -d ' ')
  hash=$(sha256sum "$log_file" | cut -d ' ' -f 1)
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$id" "$first" "$last" "$bytes" "$hash" >> "$archive_dir/manifest.tsv"
  count=$((count + 1))
done

chmod 600 "$archive_dir"/*
printf 'archive=%s json_file_containers=%s\n' "$archive_dir" "$count"
