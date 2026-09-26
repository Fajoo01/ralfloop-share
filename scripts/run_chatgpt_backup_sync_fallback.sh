#!/usr/bin/env bash
set -euo pipefail

# Fallback for issue #6: if the intended systemd timer is active, do nothing.
# Otherwise run the same user-owned backup + mirror pipeline under a non-blocking lock.
if systemctl is-active --quiet bottazzi-chatgpt-backup-sync.timer; then
  exit 0
fi

runtime_dir=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
state_dir=${CHATGPT_BACKUP_FALLBACK_STATE_DIR:-$HOME/.local/state/bottazzi/chatgpt-backup-fallback}
mkdir -p "$state_dir"
lock="$runtime_dir/bottazzi-chatgpt-backup-sync.lock"
exec 9>"$lock"
if ! flock -n 9; then
  exit 0
fi

set -a
# shellcheck disable=SC1091
source /home/bandi/selfhost/chatgpt-backup/chatgpt-backup.env
set +a

set +e
/usr/bin/node /home/bandi/ralfloop-chatgpt-backup-20260919/scripts/chatgpt_cdp_backup.mjs
rc=$?
set -e
printf '%s\n' "$rc" > "$state_dir/last-exit-code"
date --iso-8601=seconds > "$state_dir/last-run-at"
if [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

/home/bandi/ralfloop-chatgpt-backup-20260919/scripts/chatgpt_backup_mirror.sh
date --iso-8601=seconds > "$state_dir/last-success-at"
