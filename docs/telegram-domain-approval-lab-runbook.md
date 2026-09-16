# Telegram Domain Approval Lab Runbook

## 1. Purpose

This runbook describes the lab deployment and smoke test for the Ralfloop human
approval gate used before domain/bando promotion, deterministic canary execution,
or source/rule updates.

The supported flow is:

```text
Telegram -> Meowgram -> Ralfloop backend -> SQLite approval store
```

The lab proves only approval delivery and dry-run execution. It must not promote a
domain and must not execute a real canary.

## 2. Safety invariants

- Do not read passwords from files.
- Do not use `sudo -S`.
- Do not print or copy the HMAC key.
- Do not print Telegram bot tokens.
- Do not expose the approval API on `0.0.0.0`.
- Keep the approval API bound to `127.0.0.1:19090`.
- Do not put numeric Telegram user IDs in Git-tracked files.
- Use numeric Telegram user IDs only in external runtime configuration.
- Keep `RALFLOOP_TELEGRAM_APPROVAL_AUTO_EXECUTE=0`.
- Telegram approval changes request state only. It does not execute actions.
- The only lab execution is `bandi execute-approved --dry-run`.
- Do not run `bandi promote`.
- Do not execute a real canary.
- Do not restart RecursiveMAS, SearXNG, Docker, or unrelated production runtimes for this lab.

## 3. Runtime layout

Repository:

```text
/home/sibilla-cumana/ralfloop_agent_scaffold
```

Backend service:

```text
ralfloop-backend.service
```

Backend command:

```text
/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python \
  -m uvicorn openshell_backend.app:app \
  --host 127.0.0.1 \
  --port 19090
```

Meowgram service:

```text
meowgram.service
```

Meowgram listener:

```text
/srv/projects/Meowgram/src/meowgram/bot.py
```

Runtime-only files:

```text
/etc/ralfloop/telegram-approval.env
/etc/ralfloop/telegram-approval.hmac
/var/lib/ralfloop/domain-approvals.sqlite3
/var/lib/ralfloop/domain-approval-outbox.jsonl
/var/lib/ralfloop/domain-approval-outbox.state.json
/var/log/ralfloop/domain-approval-audit.jsonl
```

None of these runtime files belongs in Git.

## 4. Telegram commands

The Meowgram interface uses the existing `rl:` text prefix, not slash commands:

```text
rl:approvals
rl:approval REQUEST_ID
rl:approve REQUEST_ID DIGEST
rl:reject REQUEST_ID DIGEST MOTIVO
```

Example:

```text
rl:approve apr_7F3K9M2Q 9C41-A27B
```

Authorization uses the real numeric Telegram user ID from `event.sender_id`.
Username and display name are not authorization factors.

## 5. Deployment

Deployment is runtime-only. Do not commit the runtime configuration.

Create an external environment file similar to:

```text
RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE=1
RALFLOOP_TELEGRAM_APPROVAL_AUTO_EXECUTE=0
RALFLOOP_TELEGRAM_APPROVAL_TTL_SEC=3600
RALFLOOP_TELEGRAM_APPROVAL_MAX_PENDING=20
RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS=<TELEGRAM_NUMERIC_USER_ID>
RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS=
RALFLOOP_TELEGRAM_APPROVAL_REQUIRE_PRIVATE_CHAT=1
RALFLOOP_TELEGRAM_APPROVAL_DB=/var/lib/ralfloop/domain-approvals.sqlite3
RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG=/var/log/ralfloop/domain-approval-audit.jsonl
RALFLOOP_TELEGRAM_APPROVAL_HMAC_KEY_FILE=/etc/ralfloop/telegram-approval.hmac
RALFLOOP_TELEGRAM_APPROVAL_API_URL=http://127.0.0.1:19090
RALFLOOP_TELEGRAM_APPROVAL_OUTBOX=/var/lib/ralfloop/domain-approval-outbox.jsonl
RALFLOOP_TELEGRAM_APPROVAL_OUTBOX_STATE=/var/lib/ralfloop/domain-approval-outbox.state.json
RALFLOOP_TELEGRAM_APPROVAL_OUTBOX_ENABLED=1
RALFLOOP_TELEGRAM_APPROVAL_OUTBOX_POLL_SEC=5
RALFLOOP_TELEGRAM_APPROVAL_DELIVERY_CHAT_IDS=<TELEGRAM_NUMERIC_USER_ID>
```

Systemd drop-ins should use `EnvironmentFile=/etc/ralfloop/telegram-approval.env`
for both:

```text
ralfloop-backend.service
meowgram.service
```

The gate is disabled by default in repository templates. Runtime configuration is
the only place where the lab enables it.

## 6. Backend readiness

A single `curl` immediately after:

```bash
sudo systemctl restart ralfloop-backend.service
```

is not reliable. Use a readiness loop that checks both service state and
`/openapi.json` for up to 60-90 seconds:

```bash
OPENAPI_OK=0

for i in $(seq 1 30); do
  STATE="$(systemctl is-active ralfloop-backend.service 2>/dev/null || true)"
  CODE="$(
    curl --max-time 5 -sS \
      -o "$AUDIT/openapi-after-restart.json" \
      -w '%{http_code}' \
      http://127.0.0.1:19090/openapi.json \
      2>/dev/null || true
  )"

  printf 'attempt=%02d backend=%s http=%s\n' "$i" "$STATE" "${CODE:-000}"

  if [ "$STATE" = active ] && [ "$CODE" = 200 ]; then
    OPENAPI_OK=1
    break
  fi

  sleep 2
done

test "$OPENAPI_OK" = 1
```

Fail explicitly if the backend never returns HTTP 200.

## 7. OpenAPI verification

After readiness succeeds, verify the approval endpoints:

```bash
curl -fsS http://127.0.0.1:19090/openapi.json > "$AUDIT/openapi.json"

python3 - "$AUDIT/openapi.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)

paths = sorted(
    path
    for path in data.get("paths", {})
    if path.startswith("/domain-approvals")
)

print("\n".join(paths))

required = {
    "/domain-approvals/requests",
    "/domain-approvals/{request_id}",
    "/domain-approvals/{request_id}/decision",
    "/domain-approvals/{request_id}/cancel",
}

missing = sorted(required - set(paths))
assert not missing, f"Endpoint mancanti: {missing}"

print("APPROVAL_ENDPOINTS_OK")
PY
```

Expected approval routes:

```text
GET  /domain-approvals/health
POST /domain-approvals/requests
GET  /domain-approvals/requests
GET  /domain-approvals/{request_id}
POST /domain-approvals/{request_id}/decision
POST /domain-approvals/{request_id}/cancel
```

## 8. ForwardRef troubleshooting

Observed incident:

```text
PydanticUserError:
ForwardRef('Request') is not fully defined
```

Observed route:

```text
POST /domain-approvals/{request_id}/decision
```

Cause:

```python
from __future__ import annotations
```

combined with a local import inside `register_domain_approval_routes()`:

```python
from fastapi import Request
```

and endpoint annotation:

```python
request: Request
```

With postponed annotations, FastAPI/Pydantic could not resolve `Request` while
building `app.openapi()`.

Correct fix:

```python
from starlette.requests import Request as StarletteRequest
```

at module global scope, and:

```python
request: StarletteRequest
```

on the endpoint. Do not use `request: "Request"` and do not disable OpenAPI.

Regression test:

```text
tests/test_domain_approval_openapi.py
```

It imports `openshell_backend.app`, calls `app.openapi()`, verifies the
`/domain-approvals` routes, and checks that `request` is not exposed as an
application request body or query parameter.

## 9. Runtime files and permissions

Generate the HMAC key outside Git:

```bash
openssl rand -hex 32 | sudo tee /etc/ralfloop/telegram-approval.hmac >/dev/null
sudo chown root:sibilla-cumana /etc/ralfloop/telegram-approval.hmac
sudo chmod 0640 /etc/ralfloop/telegram-approval.hmac
```

Do not run `cat` on the key. Record only metadata and checksum:

```bash
sudo stat /etc/ralfloop/telegram-approval.hmac
sudo sha256sum /etc/ralfloop/telegram-approval.hmac
```

Runtime directories:

```bash
sudo install -d -o sibilla-cumana -g sibilla-cumana -m 0750 /var/lib/ralfloop
sudo install -d -o sibilla-cumana -g sibilla-cumana -m 0750 /var/log/ralfloop
```

The backend must write the SQLite database and append the audit log. Meowgram
must read the HMAC key to sign decisions.

## 10. Meowgram restart

Restart Meowgram only after the backend OpenAPI check passes:

```bash
sudo systemctl restart meowgram.service
```

Wait for active state:

```bash
for i in $(seq 1 20); do
  STATE="$(systemctl is-active meowgram.service 2>/dev/null || true)"
  echo "attempt=$i meowgram=$STATE"
  [ "$STATE" = active ] && break
  sleep 2
done
```

Check logs without printing environment or secrets:

```bash
sudo journalctl -u meowgram.service --since '-5 minutes' --no-pager -n 200
```

## 11. Telegram smoke test

Use only the deterministic ACT canary plan:

```json
{
  "exact_input": "Qual è la scadenza per presentare candidatura al Bando ACT 2026?",
  "expected_domain": "fondazione_unipolis_act_2026",
  "expected_version": "1.0.0",
  "expected_rule_id": "application_deadline",
  "expected_deterministic_answer": "2026-04-09T13:00:00+02:00",
  "jury_expected": false,
  "recursive_mas_expected": false,
  "legacy_fallback_expected": false,
  "side_effects": false
}
```

Create the request:

```bash
.venv/bin/python -m ralfloop_agent.domains.cli \
  bandi request-approval \
  --action run_domain_canary \
  --bando fondazione_unipolis_act_2026 \
  --version 1.0.0 \
  --canary-plan "$PLAN" \
  --send-telegram
```

The Telegram message must include:

- action;
- bando;
- version;
- request ID;
- short digest;
- expiration;
- canary question;
- expected rule and answer;
- `rl:approve REQUEST_ID DIGEST`;
- `rl:reject REQUEST_ID DIGEST MOTIVO`;
- notice that approval does not execute the action.

Reply from the allowlisted private chat:

```text
rl:approve REQUEST_ID DIGEST
```

Do not add quotes or other prefixes.

## 12. Approval verification

Check state:

```bash
.venv/bin/python -m ralfloop_agent.domains.cli \
  bandi approval-status \
  --request-id "$REQUEST_ID"
```

Expected:

```text
status=approved
consumed_at=null
```

Not expected:

```text
executed
consumed
```

The backend verifies:

- HMAC signature;
- timestamp window;
- nonce replay protection;
- numeric Telegram user ID allowlist;
- optional chat allowlist;
- private chat requirement;
- request ID;
- short digest;
- pending request state.

## 13. Dry-run execution

The lab ends with:

```bash
.venv/bin/python -m ralfloop_agent.domains.cli \
  bandi execute-approved \
  --request-id "$REQUEST_ID" \
  --dry-run
```

The dry-run:

- reloads the stored request;
- verifies `approved`;
- recalculates scope hashes;
- detects stale scope changes;
- identifies the approved action;
- does not consume the request;
- does not promote domains;
- does not execute the canary;
- does not send operational output.

Re-check state after dry-run:

```bash
.venv/bin/python -m ralfloop_agent.domains.cli \
  bandi approval-status \
  --request-id "$REQUEST_ID"
```

Expected:

```text
status=approved
consumed_at=null
```

Never run `execute-approved` without `--dry-run` during this lab.

## 14. Production invariants

Before and after the lab:

- `ralfloop-recursive-mas.service` must not be restarted.
- `ralfloop_searxng` must not be restarted.
- No `recursive_mas_worker.py` process should remain.
- No domain should be promoted.
- No canary should be executed.
- No request should be consumed by a dry-run.
- `RALFLOOP_TELEGRAM_APPROVAL_AUTO_EXECUTE=0` must remain true.

## 15. Rollback

Rollback must restore only the files changed for the lab and restart only:

```text
ralfloop-backend.service
meowgram.service
```

It must not touch RecursiveMAS, SearXNG, Docker, unrelated production runtimes, or domain data.

Rollback shape:

```bash
sudo cp --preserve=all "$AUDIT/backend-env.before" /etc/ralfloop/telegram-approval.env
sudo rm -f /etc/systemd/system/ralfloop-backend.service.d/telegram-approval.conf
sudo rm -f /etc/systemd/system/meowgram.service.d/telegram-approval.conf
sudo systemctl daemon-reload
sudo systemctl restart ralfloop-backend.service
sudo systemctl restart meowgram.service
```

Verify rollback scripts with:

```bash
bash -n "$AUDIT/rollback.sh"
```

## 16. Troubleshooting matrix

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `/openapi.json` returns 500 with `ForwardRef('Request')` | Local `Request` import and postponed annotations | Use global `StarletteRequest` import and restart backend |
| `/domain-approvals/...` returns 404 | Backend not restarted or routes not registered | Restart `ralfloop-backend.service`, then readiness loop |
| Telegram reply says backend unreachable | Meowgram cannot reach `127.0.0.1:19090` or HMAC key missing | Check backend health, key path, Meowgram env |
| `hmac_key_missing` | `RALFLOOP_TELEGRAM_APPROVAL_HMAC_KEY_FILE` unset or unreadable | Fix runtime env and permissions |
| `signature_invalid` | Body/header/key mismatch | Check Meowgram and backend use same key file |
| `replay_detected` | Nonce reused | Send a fresh Telegram command |
| `signature_expired` | Timestamp outside window | Check system clocks and retry |
| `unauthorized` | Numeric Telegram user ID not allowlisted | Fix runtime allowlist outside Git |
| `private_chat_required` | Command sent from group/channel | Use private chat |
| `scope_digest_mismatch` | User replied with wrong digest | Reply using exact digest from request |
| `stale` during dry-run | Bound files changed after approval | Create a new approval request |
| `already_consumed` | Request was executed without dry-run earlier | Create a new request; investigate audit |

## 17. Success criteria

The lab is successful only if all are true:

- Backend readiness loop returns HTTP 200 for `/openapi.json`.
- OpenAPI includes all approval endpoints.
- Meowgram restarts and accepts `rl:approve`.
- A real Telegram message is sent to the allowlisted numeric user.
- A private-chat `rl:approve REQUEST_ID DIGEST` changes state to `approved`.
- HMAC and replay checks pass.
- `execute-approved --dry-run` succeeds.
- The request remains `approved` with `consumed_at=null`.
- No domain is promoted.
- No canary is executed.
- No unrelated production service is restarted.
- No secret is printed.
- No commit is made as part of the lab.
