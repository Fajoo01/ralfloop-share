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

A rollover creates a new blank tab, clears the dedicated browser HTTP cache through CDP, closes only old local ChatGPT tabs in this dedicated browser, and navigates the new tab to `https://chatgpt.com/`.

It does **not** delete any server-side ChatGPT conversation. Old chats should be archived first at the ChatGPT account level. Automatic deletion remains disabled.

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
