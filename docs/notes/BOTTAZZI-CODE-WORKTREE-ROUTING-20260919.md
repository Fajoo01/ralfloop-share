# Bot-tazzi code/worktree routing fix — 2026-09-19

## Reproduction
A local code-patch objective containing the guard phrase `Do not restart any service` was classified as `external_action` instead of `patch_allowed`.
This sent ordinary worktree maintenance into the protected local/Jellyfin approval path and prevented Bot-tazzi from patching code.

## Root cause
`src.routing_config.NEGATION_WORDS` recognized only Italian `non` and `senza`; English `not`, `never`, and `without` were not treated as negation scope markers.

## Fix
Extend deterministic negation recognition with `not`, `never`, `without`, and `dont`.
No permission, shell allowlist, canonical action, or external-action gate was widened.

## Verification
Targeted routing suites: 24 passed.
Exact regression sentence now routes to `patch_allowed`, `requires_confirmation=False`, skill `local_maintenance`.

## Worktree
`/home/bandi/ralfloop-code-maintenance-20260919`
Branch: `fix/code-worktree-maintenance-20260919`
Base: production commit `b5005a2`.

## Worktree execution wiring
Additional defects found during live canary:
- backend state/audit path was hardcoded to the historical scaffold;
- the real OpenShell adapter self-targeted port 19090 even for alternate backend instances;
- terminal `--cwd` was metadata only, while patch workflows received an empty sandbox.

Fixes:
- configurable `RALF_OPEN_SHELL_STATE_DIR` and `RALF_OPEN_SHELL_BASE_URL`, production defaults unchanged;
- `patch_allowed + local_maintenance` now dispatches to the existing coding harness when terminal cwd is an allowed Git worktree root;
- fail closed for non-Git/untrusted cwd;
- deterministic validator defaults to `git diff --check`.

Verification after wiring: 28 targeted tests passed.

## Deterministic patch fast-path
A further live canary showed that local patch tasks still entered the generic AgentGpuCoordinator before the coding harness. That could return `llama_cpp_unmanaged_process_on_port` or block while the shared GPU lifecycle was unrelated to the patch.

`local_maintenance + patch_allowed` now bypasses the generic reasoning/GPU handoff and dispatches directly to the bounded coding harness. Other agent tasks retain the existing GPU handoff.

Targeted routing/backend suite remains 28/28 green.

## Coding worker identity
The first real coding-harness canary failed because integration selected the worktree owner (`bandi`) as worker, while the installed `pi` coding worker belongs to `sibilla-cumana` and is not traversable from `bandi`'s process context.

The integration now uses `RALF_CODE_WORKER_USER` (default `sibilla-cumana`) and the harness accepts `RALF_PI_BIN`. On this host `sibilla-cumana` is in group `bandi`, so the dedicated worker can write the Baffoflix worktree without changing ownership.
