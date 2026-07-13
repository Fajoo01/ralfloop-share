# Telegram Domain Approval

The gate is disabled by default. Runtime must configure the Telegram numeric user
allowlist, optional chat allowlist, SQLite path, audit log path, and HMAC key file
outside Git.

Supported actions:

- `promote_domain`
- `run_domain_canary`
- `apply_domain_source_update`

Telegram text commands use the existing `rl:` prefix:

- `rl:approvals`
- `rl:approval REQUEST_ID`
- `rl:approve REQUEST_ID DIGEST`
- `rl:reject REQUEST_ID DIGEST REASON`

Approval only changes the request state. It never executes the action. Execution
requires:

```bash
python -m ralfloop_agent.domains.cli bandi execute-approved \
  --request-id REQUEST_ID
```

The request scope is bound to the action, bando, version, Git commit, domain
content, manifest, sources, rules, tests, readiness evidence, and canary plan
where applicable. If any bound artifact changes before execution, the request is
marked `stale` and a new approval is required.

Meowgram signs decision calls with:

- `X-Ralfloop-Timestamp`
- `X-Ralfloop-Nonce`
- `X-Ralfloop-Signature`

The backend rejects stale timestamps, reused nonces, changed bodies, unauthorized
Telegram numeric user IDs, unauthorized chats, and group messages when private
chat is required.

For proactive lab delivery, Ralfloop writes `--send-telegram` requests to a
JSONL outbox configured outside Git. Meowgram can consume it only when
`RALFLOOP_TELEGRAM_APPROVAL_OUTBOX_ENABLED=1`. Each request ID is delivered once
and recorded in a local state file.

No bot token, HMAC key, user ID, or chat ID belongs in this repository.
