#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${1:?usage: rollback.sh BACKUP_DIR}"
ENV_FILE=/etc/ralfloop/fast-chat.env
DROPIN=/etc/systemd/system/ralfloop-backend.service.d/60-fast-chat-llama-cpp.conf
ENGINE_PY=/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python
REPO=/home/sibilla-cumana/ralfloop_agent_scaffold

test "$(id -u)" -eq 0
test -d "$BACKUP_DIR"

runuser -u sibilla-cumana -- \
  "$ENGINE_PY" -m ralfloop_agent.providers.llama_cpp_server stop || true

install -d -o root -g root -m 0755 /etc/ralfloop
install -d -o root -g root -m 0755 "$(dirname "$DROPIN")"

if test -f "$BACKUP_DIR/fast-chat.env.present"; then
  install -o root -g root -m 0644 "$BACKUP_DIR/fast-chat.env" "$ENV_FILE"
else
  printf '%s\n' 'RALF_CHAT_PROVIDER=ollama' >"$ENV_FILE"
  chown root:root "$ENV_FILE"
  chmod 0644 "$ENV_FILE"
fi

if test -f "$BACKUP_DIR/60-fast-chat-llama-cpp.conf.present"; then
  install -o root -g root -m 0644 \
    "$BACKUP_DIR/60-fast-chat-llama-cpp.conf" "$DROPIN"
else
  install -o root -g root -m 0644 \
    "$REPO/.ralf_run/ralf_llama_cpp_final_deploy/60-fast-chat-llama-cpp.conf" "$DROPIN"
fi

systemctl daemon-reload
systemctl restart ralfloop-backend.service

for _ in $(seq 1 30); do
  if test "$(systemctl is-active ralfloop-backend.service 2>/dev/null || true)" = active \
    && curl -fsS --max-time 2 http://127.0.0.1:19090/openapi.json >/dev/null; then
    exit 0
  fi
  sleep 2
done

exit 1
