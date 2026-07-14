#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "error=must_run_as_root" >&2
  exit 1
fi

NODE=${1:-}
BACKUP=${2:-}

restore_or_remove() {
  local flat=$1
  local nested=$2
  local destination=$3
  if [[ -n "$BACKUP" && -e "$BACKUP/$flat" ]]; then
    install -D -m "$(stat -c %a "$BACKUP/$flat")" "$BACKUP/$flat" "$destination"
  elif [[ -n "$BACKUP" && -e "$BACKUP/$nested" ]]; then
    install -D -m "$(stat -c %a "$BACKUP/$nested")" "$BACKUP/$nested" "$destination"
  else
    rm -f "$destination"
  fi
}

case "$NODE" in
  temistocle)
    systemctl disable --now ralf-draftd.service 2>/dev/null || true
    restore_or_remove ralf-draftd.service etc/systemd/system/ralf-draftd.service /etc/systemd/system/ralf-draftd.service
    restore_or_remove ralf-draftd.env etc/ralf-draftd/ralf-draftd.env /etc/ralf-draftd/ralf-draftd.env
    restore_or_remove 10-firewall.conf etc/systemd/system/ralf-draftd.service.d/10-firewall.conf /etc/systemd/system/ralf-draftd.service.d/10-firewall.conf
    rm -rf /opt/ralf-draftd
    rmdir /etc/ralf-draftd /etc/systemd/system/ralf-draftd.service.d 2>/dev/null || true
    systemctl daemon-reload
    ;;
  sibilla)
    restore_or_remove remote-draft.env etc/ralfloop/remote-draft.env /etc/ralfloop/remote-draft.env
    ;;
  *)
    echo "usage: $0 {temistocle|sibilla} [backup_dir]" >&2
    exit 2
    ;;
esac
