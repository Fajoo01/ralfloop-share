#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "error=must_run_as_root" >&2
  exit 1
fi

NODE=${1:-}
BACKUP=${2:-}
case "$NODE" in
  temistocle)
    systemctl disable --now ralf-draftd.service 2>/dev/null || true
    if [[ -n "$BACKUP" && -d "$BACKUP" ]]; then
      [[ -e "$BACKUP/ralf-draftd.service" ]] && install -m 0644 "$BACKUP/ralf-draftd.service" /etc/systemd/system/ralf-draftd.service
      [[ -e "$BACKUP/ralf-draftd.env" ]] && install -m 0600 "$BACKUP/ralf-draftd.env" /etc/ralf-draftd/ralf-draftd.env
    else
      rm -f /etc/systemd/system/ralf-draftd.service
      rm -rf /etc/ralf-draftd /opt/ralf-draftd
    fi
    systemctl daemon-reload
    ;;
  sibilla)
    if [[ -n "$BACKUP" && -e "$BACKUP/remote-draft.env" ]]; then
      install -m 0600 "$BACKUP/remote-draft.env" /etc/ralfloop/remote-draft.env
    else
      rm -f /etc/ralfloop/remote-draft.env
    fi
    ;;
  *)
    echo "usage: $0 {temistocle|sibilla} [backup_dir]" >&2
    exit 2
    ;;
esac
