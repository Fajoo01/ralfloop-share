#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
getent passwd bandi >/dev/null
getent passwd sibilla-cumana >/dev/null

if ! getent group ralf-gpu >/dev/null; then
  groupadd --system ralf-gpu
fi
usermod -a -G ralf-gpu sibilla-cumana

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 \
  "$repo_root/scripts/ralf_agentcpm_lifecycle_broker.py" \
  /usr/local/libexec/ralf-agentcpm-lifecycle-broker
install -o root -g root -m 0644 \
  "$repo_root/deploy/systemd/ralf-agentcpm-lifecycle-broker.service" \
  /etc/systemd/system/ralf-agentcpm-lifecycle-broker.service

systemctl daemon-reload
systemctl enable --now ralf-agentcpm-lifecycle-broker.service

socket_path=/run/ralf-agentcpm-lifecycle/control.sock
for _ in $(seq 1 50); do
  [[ -S "$socket_path" ]] && break
  sleep 0.1
done
[[ -S "$socket_path" ]]
[[ $(stat -c '%U' "$socket_path") == bandi ]]
[[ $(stat -c '%G' "$socket_path") == ralf-gpu ]]
[[ $(stat -c '%a' "$socket_path") == 660 ]]

# Canary is deliberately read-only: installation never transitions AgentCPM.
runuser -u sibilla-cumana -- python3 - "$socket_path" <<'PY'
import json
import socket
import sys

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(10)
    client.connect(sys.argv[1])
    client.sendall(b'{"action":"status"}\n')
    reply = json.loads(client.makefile("rb").readline())
if not reply.get("ok") or reply.get("action") != "status":
    raise SystemExit(f"AgentCPM status canary failed: {reply}")
print(json.dumps(reply, sort_keys=True))
PY
