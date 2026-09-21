# Assistant v1 / Motor Judge resource gate checkpoint

Date: 2026-09-21
Branch: `feat/bottazzi-assistant-v1-20260921`

## Motor Judge

- Commit `71feaae` adds fail-closed resource admission for the `19196` sidecar.
- Required headroom: 16 GiB MemAvailable, 1 GiB SwapFree, 6500 MiB free VRAM.
- Startup is denied if Ollama has a loaded model or if `19194`, `19195`, or `19240` is active.
- `/run/ralfloop/inference-gpu.lock` is held for the lifetime of the DS4 process.
- Host cache profile is now 4 GiB unpinned; expert chunk cache 8 GiB unpinned.
- `19194` remained inactive and was never started or reconfigured.

## Live rollout

- Release: `71feaaee8c6056b9a41cf4ee6081ae9e357a3191`.
- Previous release: `dad17d3acd5253e5288deaee568ee8b9009b6b9a`.
- Rollback snapshot: `rollbacks/20260921-063743`.
- Sidecar `19196` is active.
- Canary prompts: 185 / 245 / 305 / 185 tokens.
- Canary durations: 64.037 / 43.702 / 74.263 / 59.450 seconds.
- No kernel OOM occurred during rollout.

## Fast lane

- Existing CPU-only `qwen2.5-3b` service on `19110` is reused instead of loading another Ollama copy.
- Assistant v1 fast-provider canary: 2.45 s wall, provider `assistant_fast_openai_compat`.
- Result stored in `benchmarks/assistant-v1-fast-provider-canary-20260921.json`.
- General/deep lanes remain unpromoted while resource arbitration with the Motor is being completed.
