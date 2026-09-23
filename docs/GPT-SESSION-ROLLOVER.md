# Bot-tazzi GPT session rollover

## Goal

Keep ChatGPT web sessions responsive by rotating long/heavy chats while keeping continuity outside the chat.

## Runtime

- dedicated Chrome profile: `/home/bandi/.local/share/bottazzi-gpt-browser/profile`
- dedicated Chrome config: `/home/bandi/.local/share/bottazzi-gpt-browser/config`
- disposable cache: `/tmp/bottazzi-gpt-browser-cache`
- CDP endpoint: `http://127.0.0.1:9238`
- systemd unit: `bottazzi-gpt-browser.service`

Port `9237` is already used by the WhatsApp browser and must not be reused.

## Handoff state

The durable handoff lives under `~/.local/state/bottazzi/gpt-session/`.

`current.json` is the current structured handoff. Every checkpoint also creates a timestamped copy under `archive/`.

The handoff is intentionally small and contains: goal, current state, repo/branch/worktree/commit, constraints, completed work, test results, action receipts, important files, do-not-touch list, open problems, and next action.

Authentication material, cookies, tokens and storage-state data are rejected from handoff records.

## Rollover policy

Default triggers are 36 turns, 120 minutes, 2 consecutive errors, 30 seconds of response latency, a phase boundary after the session is already mature, or a manual trigger.

The policy is separate from browser mechanics so it can later be fed by DOM/runtime metrics.

## Browser behavior

An automatic handoff creates a new blank tab, clears the dedicated browser HTTP cache through CDP, navigates the new tab to `https://chatgpt.com/`, submits the durable handoff, and closes only the selected worker source tab after a real new user turn is observed. Other ChatGPT tabs in the dedicated profile are left untouched.

The legacy manual `rotate --apply` command still rotates all local ChatGPT tabs and should not be used as the automatic shepherd path when unrelated tabs are present.

No rollover path deletes any server-side ChatGPT conversation. Automatic server-side deletion remains disabled.

## CLI

```bash
cd /home/bandi/ralfloop-bottazzi-gpt-rollover-20260923
/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python tools/bottazzi_gpt_session.py status
/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python tools/bottazzi_gpt_session.py decide --turns 36
/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python tools/bottazzi_gpt_session.py rotate
/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python tools/bottazzi_gpt_session.py rotate --apply
```

Without `--apply`, rotation is a dry run.

## Authentication

The dedicated profile intentionally does not copy cookies from another Chrome profile. It needs one interactive ChatGPT login once. After that, the profile persists while the cache remains disposable.

Do not commit or copy the profile directory, cookies or storage-state files to Git.

## Safety rule

Git/runtime state is authoritative. A new GPT chat must consume the handoff only after verifying the real branch, commit, running services and externally executed actions. This prevents a new chat from repeating already completed mutations.

## Session shepherd

`status` now probes the live ChatGPT DOM and reports whether the composer is ready, the current user/assistant turn counts, page age, live response latency, consecutive visible response errors, and whether interactive login/challenge handling is required.

Latency/error telemetry is page-local and non-secret. A lightweight `MutationObserver` is installed only in an inspected worker page and keeps state in the page `window`: a new user turn starts the response clock, the current clock remains live while a response is pending, a completed assistant response stores the last latency and resets the consecutive-error count, while a visible ChatGPT response/network error can increment the error streak at most once per user turn. No prompt or response text is persisted by this telemetry.

`shepherd` evaluates the rollover policy from those live metrics and reports total response latency separately from `response_idle_ms`, a page-local heartbeat derived from assistant DOM progress and native ChatGPT tool-card activity. Latency-only rollover is progress-based rather than wall-clock based. If a response remains pending but the active-generation indicator has disappeared, `--max-stall-ms` (default 60 s) bounds missing progress. If ChatGPT still reports an active response, visible assistant/tool heartbeat keeps it alive regardless of total duration; only ten minutes of continuous no-progress idle (`--max-active-stall-ms`, default 600 s) makes latency-only rollover eligible. Completed slow responses can also rotate normally. Turn, age and error reasons are not weakened by this guard. With `--apply --submit`, the controller opens a new ChatGPT tab, waits until the fresh page is fully loaded and settled past the server-rendered composer race, injects the durable handoff, confirms a real new user turn, persists the successor `source_chat`, and only then closes the selected source. If submit or successor persistence fails, the source stays open; a temporary successor tab may be closed locally without deleting its server-side conversation.

The successful successor target is persisted in `current.json` as `source_chat`. On later timer runs this identifies the worker even if unrelated ChatGPT tabs are open. If there are multiple tabs and the stored worker cannot be found, the shepherd fails closed instead of guessing. `--source-target-id` exists for explicit smoke/recovery operations.

The periodic units are `bottazzi-gpt-session-shepherd.service` and `bottazzi-gpt-session-shepherd.timer`. The production timer probes every 15 seconds after the one-time account login has been verified, so a 30-second latency/error threshold is observed promptly without a tight polling loop.

## One-time interactive login mode

Sibilla already exposes a bandi-owned X display on `:1`. `bottazzi-gpt-browser-login.service` runs the same dedicated profile and CDP port 9238 headed on that display, without copying cookies from any other browser. Use it only to complete the one-time ChatGPT login; then return to `bottazzi-gpt-browser.service`. Authentication remains under the dedicated profile while `/tmp/bottazzi-gpt-browser-cache` stays disposable.

The shepherd treats a visible anonymous composer as interaction-required rather than authenticated-ready, and a submitted handoff is only confirmed after the ChatGPT DOM shows a new user turn. Authentication redirects therefore fail closed and never justify closing the old local chat tab.

Authentication readiness is confirmed by a boolean same-origin `/api/auth/session` check; the controller never reads or persists account identity, cookies, tokens or storage state.

Production intentionally runs Chrome headed on the private bandi VNC display `:1`. On this host, Chrome headless reproducibly fell into the ChatGPT “Ci siamo quasi…” challenge and `/api/auth/session` returned unauthenticated with the same profile; headed mode preserves the authenticated session while keeping the dedicated profile/cache isolation unchanged.
