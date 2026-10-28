#!/usr/bin/env bash
# Execute one uploaded deployment script, always remove it, and preserve its status.
set -u

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "usage: run_remote_deploy.sh DEPLOY_SCRIPT" >&2
  exit 64
fi

REMOTE_DEPLOY_SCRIPT=$1

cleanup_deploy_script() {
  rc=$?
  trap - EXIT
  rm -f -- "$REMOTE_DEPLOY_SCRIPT"
  rmdir -- "$(dirname -- "$REMOTE_DEPLOY_SCRIPT")" 2>/dev/null || true
  exit "$rc"
}

trap cleanup_deploy_script EXIT
bash "$REMOTE_DEPLOY_SCRIPT"
