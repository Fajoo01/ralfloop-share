#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=/home/sibilla-cumana/ralfloop-production
CURRENT="$ROOT/current"
NEW="$ROOT/releases/bottazzi-gmail-manual-drafts-arci-bundle-20261008-v4"
EXPECTED_OLD="$ROOT/releases/bottazzi-gmail-manual-drafts-20261008-v3"
SERVICE=ralfloop-backend.service
OLD="$(readlink -f "$CURRENT")"
test "$OLD" = "$EXPECTED_OLD"
test -f "$NEW/.patch_commit"
grep -q '72ea43d6 email_exact_body_no_ollama' "$NEW/.patch_commit"
test -f "$NEW/ralfloop_agent/unified_assistant/core.py"
test -f "$NEW/ralfloop_agent/unified_assistant/runtime.py"
rollback() {
  echo "ROLLBACK_TO=$OLD"
  ln -s "$OLD" "$CURRENT.next-exactbody-rollback"
  mv -Tf "$CURRENT.next-exactbody-rollback" "$CURRENT"
  systemctl restart "$SERVICE"
}
trap rollback ERR
ln -s "$NEW" "$CURRENT.next-exactbody"
mv -Tf "$CURRENT.next-exactbody" "$CURRENT"
systemctl restart "$SERVICE"
READY=0
for _ in $(seq 1 35); do
  if curl -fsS --max-time 2 http://127.0.0.1:19090/assistant/v1/status | grep -q '"assistant_name":"Bot-tazzi"'; then
    READY=1
    break
  fi
  sleep 1
done
test "$READY" -eq 1
test "$(readlink -f "$CURRENT")" = "$NEW"
test "$(systemctl is-active "$SERVICE")" = "active"
trap - ERR
echo "DEPLOY_EXACTBODY_OK old=$OLD new=$NEW pid=$(systemctl show -p MainPID --value "$SERVICE")"
