# Colibri/GLM slow review

## Architecture

`sources -> existing bandi retrieval -> fast draft -> immutable context packet -> glm-run -> schema validation -> deterministic validation -> candidate artifact -> Telegram outbox -> artifact-bound approval -> external dispatcher`

GLM is consultative. It cannot call tools, execute output, mutate a draft, approve an action, or dispatch externally. Interactive providers are unchanged.

## Runtime

- Provider: `colibri_glm`; model: `glm-5.2-colibri`.
- Stable launcher: `/home/sibilla-cumana/Dati/ralfloop-colibri/bin/glm-run`.
- Machine companion: `python -m ralfloop_agent.glm_review.machine` or `deploy/colibri/glm-run-machine`.
- State: `/home/sibilla-cumana/.local/state/ralfloop/glm-review`.
- Queue: SQLite, WAL, one worker lock, launcher lock, three attempts, persistent backoff.
- Automatic window: 23:00-07:30 `Europe/Rome`; timer checks every 15 minutes.
- Digest: 08:00 `Europe/Rome`; local Telegram outbox only. The existing trusted Telegram bridge performs delivery.
- `exit 75`: `deferred_resource_busy`.
- Queue schema v3: atomic SQLite CAS claim, unique `run_token`, worker identity, PID, heartbeat/lease, separate `resource_deferrals`, immutable attempt ledger.
- Queue schema v4: Director blackboard, assignment ledger, safe cache, per-round metrics.
- Worker file lock serializes the whole invocation; `--force` only bypasses the night window.
- `GLM_NGEN`/`GLM_TIMEOUT_SECONDS` override generic `RALFLOOP_*` defaults after validation. Grant default: 256 tokens, 7200 seconds.

## Director-worker cycle

`preflight -> utility gate -> GLM Director -> validated DAG -> bounded workers -> acceptance -> blackboard delta -> Director`

- Director output: `assign`, `revise_plan`, `request_human_input`, `conclude`, or `stop_budget_exhausted`.
- Defaults: 3 rounds, 5 assignments/round, 15 worker calls, 7200 seconds.
- Available workers/tools come from installed Ralf modules; assignment tools must be a subset of the worker allowlist.
- Write-capable workers and external actions require explicit approval. Director never executes tools.
- Grant context is capped near 500 tokens; known gaps are passed as facts, not rediscovery work.
- Cache key binds packet, goal, capability catalog, prompt/schema version, and model configuration.
- Every verified fact requires `evidence_refs`; completed assignments survive restart and are deduplicated by task type, inputs, and objective.
- Production review budget: 6,000 context characters and 128 generated tokens inside `CTX=2048`; canary uses 64.

Prompts, raw output and results are mode `0600`; task directories are mode `0700`. General audit stores metadata only. The adapter uses `shell=False`, stdin, a fixed launcher path, an environment allowlist, time/output limits and process-group termination.

## Input

JSON object containing `goal`, `draft`, `requirements`, compact `evidence`, optional `budget_summary`, `known_gaps`, `deadline`, and `external_action`. Originals are read-only; the task stores a hashed snapshot.

## Commands

```bash
ralf glm enqueue --type grant_review --input review.json
ralf glm enqueue --type grant_review --input review.json --immediate
ralf glm status TASK_ID
ralf glm list
ralf glm result TASK_ID
ralf glm retry TASK_ID
ralf glm cancel TASK_ID
ralf glm digest --dry-run
ralf approve TASK_ID
ralf reject TASK_ID --reason "reason"
ralf glm metrics
ralf glm pause
ralf glm resume
journalctl --user -u ralf-glm-worker.service -u ralf-glm-digest.service
systemctl --user disable --now ralf-glm-worker.timer
systemctl --user enable --now ralf-glm-worker.timer
```

Approval binds `task_id`, candidate SHA-256, artifact version, exact external action, approver identity/channel, and timestamp. Any candidate replacement invalidates the approval. Dispatch is denied unless that exact binding is approved.
GLM approval requests expire after 24 hours by default (`RALFLOOP_GLM_APPROVAL_TTL_SEC`).

## Deploy

Build from a committed tree, never from a dirty worktree:

```bash
python tools/build_ralfloop_production_release.py --repo REPO --commit COMMIT --output-root /home/sibilla-cumana/ralfloop-production/releases
```

Record the old target. Publish with an atomic symlink swap, install user units, run `systemctl --user daemon-reload`, enable both timers, and restart only `ralfloop-backend.service` when the HTTP health route or approval API changed.

## Rollback

Artifacts and SQLite data are forward-compatible and remain untouched:

```bash
vpnpc sudo sibilla -- /home/sibilla-cumana/ralfloop-production/bin/rollback-colibri-glm /home/sibilla-cumana/ralfloop-production/releases/PREVIOUS_COMMIT
```

The command disables new timers, restores the prior weekly service unit, atomically restores the previous release, restarts only the backend, and verifies backend, Meowgram and AgentCPM health.
