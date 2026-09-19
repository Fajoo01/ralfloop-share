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
