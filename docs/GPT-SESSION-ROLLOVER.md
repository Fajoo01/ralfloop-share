# Bot-tazzi GPT session rollover

## Goal

Keep ChatGPT web sessions responsive by rotating long/heavy chats while keeping continuity outside the chat.

## Runtime

- dedicated Chrome profile: `/home/bandi/.local/share/bottazzi-gpt-browser/profile`
- dedicated Chrome config: `/home/bandi/.local/share/bottazzi-gpt-browser/config`
- disposable cache: `/run/bottazzi-gpt-browser-cache` (systemd `RuntimeDirectory`, recreated automatically after boot)
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

An automatic handoff creates a new blank tab, clears the dedicated browser HTTP cache through CDP, navigates the new tab to `https://chatgpt.com/`, submits the durable handoff, and confirms/persists the successor before touching the selected worker source. The old source conversation is then archived through the normal ChatGPT UI, but its local tab is deliberately kept open as a protected ghost tab; unrelated ChatGPT tabs in the dedicated profile are left untouched.

A ghost tab has two separate input paths. The native ChatGPT composer remains in the DOM for Bot-tazzi/CDP automation but is removed from normal mouse/tab interaction for the human. A distinct visible human composer is injected with a warning banner; text entered there is transferred through same-origin local storage to the current successor tab, which is named `bottazzi-active`, and that active tab is brought to the foreground with the draft loaded. Ghost tabs clear that window name so they cannot accidentally receive future human drafts. The human keyboard is also locked immediately when rollover starts, before the successor is created, with a visible "passaggio in corso" notice; a failed rollover removes that lock and restores the original source input instead of leaving keystrokes stranded.

Archiving is deliberately different from deletion: the controller accepts only the `Archive`/`Archivia` menu action and never selects `Delete`/`Elimina`. The transaction journal writes `archive_started` before the UI click and `source_archived` after confirmation. A missing history item is fail-closed on the first attempt and is considered an idempotent already-archived result only when recovering a transaction whose journal proves an archive attempt had already started.

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

`shepherd` evaluates the rollover policy from those live metrics and reports total response latency separately from `response_idle_ms`, a page-local heartbeat derived from assistant DOM progress and native ChatGPT tool-card activity. Latency-only rollover is progress-based rather than wall-clock based. If a response remains pending but the active-generation indicator has disappeared, `--max-stall-ms` (default 60 s) bounds missing progress. If ChatGPT still reports an active response, visible assistant/tool heartbeat keeps it alive regardless of total duration; only ten minutes of continuous no-progress idle (`--max-active-stall-ms`, default 600 s) makes latency-only rollover eligible. Completed slow responses can also rotate normally. Turn, age and error reasons are not weakened by this guard. With `--apply --submit`, the controller opens a new ChatGPT tab, waits until the fresh page is fully loaded and settled past the server-rendered composer race, injects the durable handoff, confirms a real new user turn, persists the successor `source_chat`, archives the old conversation, installs the human-input relay on the successor, and finally converts the selected source tab into a protected ghost instead of closing it. If submit or successor persistence fails, the source stays untouched; a temporary successor tab may be closed locally without deleting its server-side conversation.

The successful successor target is persisted in `current.json` as `source_chat`. On later timer runs this identifies the worker even if unrelated ChatGPT tabs are open. After a browser/host restart the old CDP target ID is expected to disappear: if the canonical `source_chat_url` is already open, the target ID is repaired. A visible Home tab is never hijacked for recovery; when the saved worker is missing, recovery creates a dedicated background tab, navigates that tab to the exact persisted `source_chat_url`, verifies authenticated/ready state and the canonical URL, and only then replaces the stale target ID. Recovery failure closes only that newly created tab and leaves the stored worker identity unchanged. Duplicate matches for the saved canonical URL remain ambiguous and fail closed. `--source-target-id` exists for explicit smoke/recovery operations.

The periodic units are `bottazzi-gpt-session-shepherd.service` and `bottazzi-gpt-session-shepherd.timer`. The production timer probes every 15 seconds after the one-time account login has been verified, so a 30-second latency/error threshold is observed promptly without a tight polling loop.

All commands that can mutate worker identity, adoption state or browser state are serialized by a non-blocking lock in the GPT state directory. A concurrent manual/timer operation returns `mutation_locked` instead of racing another mutation. Browser-changing adoption and rollover operations also use `mutation-journal.json`: the journal records only target IDs, canonical conversation URLs and transaction phases, never prompt text or authentication material. On the next apply cycle, an incomplete transaction is either completed from unambiguous browser/state evidence, rolled back when the original source is still intact, or left fail-closed with `mutation_recovery_required` if the state is ambiguous. A rollover never closes the source until the successor has a confirmed user turn, a canonical conversation URL and persisted source identity.

The guard no longer suppresses controller stderr or silently converts malformed/failed controller responses into a successful timer cycle. Empty or invalid JSON, controller `ok:false`, failed persistence and unresolved recovery make the systemd unit fail visibly in the journal. Expected operational states such as scan throttling, user snooze, `mutation_locked` and `temporary_access_limited` remain non-fatal; the temporary-access condition still arms the existing cooldown.

## External/app chat adoption

The periodic shepherd also runs `adopt-external --apply` before evaluating rollover. A dedicated watcher tab refreshes the authenticated ChatGPT sidebar at most every 30 seconds and detects account-synced conversations created by another client, including the mobile app.

The first scan is baseline-only: conversations already visible in the sidebar are marked as seen and are never adopted retroactively. A later conversation is eligible only when it is new to the watcher, is not already open in the dedicated browser, and is not the current worker conversation. When eligible, the existing worker tab is navigated to that conversation so its target ID remains the durable `source_chat` and the normal rollover policy continues to apply.
To reject late-rendered old history entries, a candidate must also appear before the current worker inside the history list; if the worker is absent from that list, adoption fails closed.
The handoff also stores the canonical worker conversation URL. If Chrome restarts and changes the CDP target ID, a unique tab with that URL repairs the stored source target automatically; duplicate matches fail closed.

Adoption is deferred while the worker has an active/pending response, unsent composer text, or is not ready. Once a conversation has passed the history-order guard, its canonical URL is retained as a pending candidate until it can be adopted; this lets a validated app/client conversation survive a worker rollover without being discarded merely because the successor worker is now newer in history. If the current worker is temporarily absent from the sidebar history, newly visible unseen conversations are not consumed: they are retained as bounded, one-hour `unvalidated_candidates` together with the canonical source URL that was missing. A later sidebar snapshot may promote such a candidate only when both URLs are visible and the candidate is proven newer than that original source; reversed order is rejected as late-rendered old history, while an unresolved candidate remains protected from the normal seen merge until validation or expiry. This lets the proof survive a worker rollover without weakening the history-order guard. The watcher never sends a message, never copies authentication material, and never deletes a server-side conversation. Its durable state stores only canonical conversation URLs, the watcher target ID and timestamps plus pending/unvalidated/last-adopted metadata; prompt/response text is not persisted.

## One-time interactive login mode

Sibilla already exposes a bandi-owned X display on `:1`. `bottazzi-gpt-browser-login.service` runs the same dedicated profile and CDP port 9238 headed on that display, without copying cookies from any other browser. Use it only to complete the one-time ChatGPT login; then return to `bottazzi-gpt-browser.service`. Authentication remains under the dedicated profile while `/run/bottazzi-gpt-browser-cache` stays disposable and is recreated by systemd after reboot.

The shepherd treats a visible anonymous composer as interaction-required rather than authenticated-ready, and a submitted handoff is only confirmed after the ChatGPT DOM shows a new user turn. Authentication redirects therefore fail closed and never justify closing the old local chat tab.

Authentication readiness is confirmed by a boolean same-origin `/api/auth/session` check; the controller never reads or persists account identity, cookies, tokens or storage state.

Production intentionally runs Chrome headed on the private bandi VNC display `:1`. On this host, Chrome headless reproducibly fell into the ChatGPT “Ci siamo quasi…” challenge and `/api/auth/session` returned unauthenticated with the same profile; headed mode preserves the authenticated session while keeping the dedicated profile/cache isolation unchanged.
