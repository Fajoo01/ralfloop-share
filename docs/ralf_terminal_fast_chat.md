# Ralf terminal fast chat

Normal terminal conversation uses the read-only chat path:

- `POST /chat/stream`: native provider streaming exposed as NDJSON.
- `POST /chat`: non-streaming fast-path and stream compatibility fallback.
- `POST /tasks/run`: full agent workflow, used only by `ralf agent` or `/agent` after local confirmation.

The chat endpoints do not construct `RalfloopAgent`, sandboxes, capability routes,
RecursiveMAS, domain juries, approval requests, or tool dispatch. Repository context
is collected by the client with fixed read-only operations and sent as a bounded
snapshot. The backend does not read `cwd`.

NDJSON events:

```json
{"type":"start","provider":"ollama","model":"configured-model","session_id":"..."}
{"type":"token","text":"partial text"}
{"type":"error","message":"provider_inactivity_timeout"}
{"type":"done","ok":true,"duration_ms":123}
```

Tokens are forwarded as the provider emits them. The client falls back from
`/chat/stream` to `/chat` only for HTTP `404`, `405`, or `501`; it never falls back
to `/tasks/run`.

Sessions are JSON files under `~/.local/state/ralf/sessions/` (`0700` directory,
`0600` files) and are replaced atomically. They contain bounded conversation,
`cwd`, model/provider names, and essential timestamps. Repository snapshots and
protocol payloads are not persisted.

Local confirmation for agent mode only confirms entry into the full workflow.
It does not set `human_confirmed` and does not approve, reject, execute, promote,
run a canary, or apply a source update. Protected actions remain gated through
the existing Telegram approval path.
