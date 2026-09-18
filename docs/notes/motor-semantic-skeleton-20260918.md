# Bot-tazzi Motor semantic skeleton — 2026-09-18

## Hypothesis

Use the read-only Grammar MCP as a structural pre-encoder for Motor context. The goal is not to replace the DeepSeek tokenizer or embeddings. Natural-language context is reduced before inference while the original DS4/V4.1 model remains unchanged.

The useful grammar signal is morphology + lemma + valency/pattern evidence. The first prototype uses morphology/lemma only; valency is deliberately deferred until sentence-role extraction is implemented.

## Safety invariants

- Never prune negation, conditionals, modality, chronology or quantities.
- Goal/rules remain surface-exact in Judge-case compaction.
- Recent conversation tail remains surface-exact; older turns are compressed.
- Duplicate old context may merge, but contradictory text must remain distinct.
- Provenance refs and timestamps survive compaction.
- Grammar MCP failure preserves text rather than failing into aggressive pruning.
- This branch is offline research only; no production release or service switch.

## Architecture

```text
raw conversation / memory evidence
        |
        +--> newest turns + goal/rules ---------- exact surface
        |
        +--> older context --> Grammar MCP --> morphology/lemma
                                 |
                                 +--> protected semantic operators
                                 +--> article pruning / canonicalization
                                 +--> duplicate collapse
        |
        +--> semantic skeleton + refs + chronology
                         |
                         +--> Bot-tazzi Motor Judge
                         +--> raw evidence remains oracle/fallback
```

This is deliberately compatible with the existing compact semantic critic (`semantic_judge/compact.py`): typed compact evidence is preferred over a new opaque embedding language.

## Initial offline checkpoint

Synthetic chronological email-approval context, 10 history facts, newest two turns exact:

- raw JudgeCase: 1849 chars / 415 lexical units
- compact JudgeCase: 1565 chars / 347 lexical units
- ratio: 0.846 chars / 0.836 lexical
- facts: 10 -> 8 through duplicate collapse
- Grammar MCP: 54 unique words queried, 47 with analyses
- unit tests: 6/6 PASS at this checkpoint

This is intentionally conservative and not yet enough compression. Next stages should test valency-based predicate/argument extraction and multi-resolution context summaries before considering live integration.

## External inspiration

- LLMLingua (EMNLP 2023): coarse-to-fine prompt compression.
- LongLLMLingua (ACL 2024): question-aware long-context compression and key-information reordering.
- Gist tokens (Mu et al. 2023): learned reusable prompt compression; useful as a future trained comparison, not the first implementation.
- AMR-style predicate/argument graphs: inspiration for a later grammar/valency skeleton, not a requirement to adopt AMR wholesale.

## First live attempt

A RAW vs compact Judge comparison on `127.0.0.1:19196` is **not valid yet**. Both requests failed closed before a model verdict (`raw_text=null`). The sidecar stayed healthy on the port, but its journal showed CUDA low-VRAM staging exhaustion and `V4.1 layer-major prefill failed at layer 14`.

This is not evidence that RAW and compact semantics agree. It is evidence that long-context prefill pressure is already a practical failure mode on the current 8 GiB path. Any future benchmark must distinguish:

1. semantic agreement when both prompts complete;
2. whether compression moves a case below an OOM/chunk boundary;
3. latency and bytes read only for successful runs.

Do not restart or alter production `19194`/judge `19196` as part of this research branch. Fix/benchmark the runtime separately or use a bounded context that completes on both paths.

## Exact DS4 tokenizer checkpoint

The benchmark now supports optional exact token counting through `ds4 --dump-tokens`; paths are supplied as CLI arguments and are not hard-coded.

On the current synthetic 10-turn approval context, using the real DeepSeek V4.1 tokenizer:

- RAW payload: 558 tokens; CLI-rendered: 593 tokens.
- SAFE grammar+timeline: 373 payload; 408 rendered — about 31% fewer rendered tokens.
- DENSE predicate/caveman: 385 payload; 420 rendered — about 29% fewer rendered tokens.

Decision: KEEP SAFE as the current reference encoder. DENSE is useful research evidence but currently loses on actual tokenizer count despite fewer characters. Strange punctuation/mini-DSL syntax is not free under the pretrained tokenizer.

## Real SessionStore benchmark

Measured locally on the 10 largest non-trivial SessionStore records; raw message text was never printed or committed. Session IDs are represented only by short SHA-256 digests.

Using the exact DS4 `--dump-tokens` tokenizer path, SAFE reduced 4,820 tokens to 4,186 (`0.8685`, about 13.2% saved). DENSE produced 4,265 (`0.8849`) and lost to SAFE in every case, so DENSE is research-only.

Savings increase with context size. With an adaptive threshold of 1,000 raw characters, the eligible sample totals 3,656 -> 3,110 DS4 tokens (`0.8507`, about 14.9% saved). The longest real record measured 1,768 -> 1,391 tokens (`0.7868`).

A fail-closed protected-atom guard now checks negation, condition/modality words, temporal operators, email addresses, URLs, numeric/alphanumeric IDs and comparison operators. Any mismatch causes that segment to fall back to normalized RAW text. The current 10-session SAFE benchmark triggered zero guard fallbacks.

## Current 19196 admission boundary

Read-only journal inspection shows the managed judge sidecar (`prefill_chunk=128`, stage 768 MiB, reserve 512 MiB) completed a 193-token rendered prompt on 2026-09-18, but later rendered prompts at 301, 323, 550 and 648 tokens all failed at V4.1 layer 14 with the same staging-space refusal (`attn_out_a`/`attn_out_b`).

This makes long-context compression an admission-control concern as well as a latency optimization. No service was restarted or reconfigured during this investigation. Do not promote compressed prompts to authoritative Judge input until verdict-equivalence testing is available; for now the integration is telemetry-only behind `BOTTAZZI_MOTOR_SKELETON_SHADOW`.

## Exact prompt-budget preflight

The research branch now renders the exact DeepSeek V4.1 Judge `system + user + assistant-prefix` shape and verifies it byte-for-byte against `ds4_test --ds41-render`. Exact token counts are obtained offline with `ds4 --dump-tokens`, so admission can be decided before inference.

A minimal current Judge request is already 164 rendered tokens. The observed healthy live request was 193 tokens; current research budget is therefore conservatively 192 until the runtime is repaired/rebenchmarked.

A compact system-prompt candidate was measured at 134 tokens for the same minimal dossier versus 164 with the current system prompt, saving 30 fixed tokens. It is research-only: no live Judge prompt has been changed.

## Post-OOM staging recovery candidate

Source inspection explains why 19196 stays broken after one long-prefill failure: CUDA `ds4_gpu_commands_active()` is always false, so the normal conditional end-command fence is skipped; the low-VRAM staging epoch is reset only after successful explicit fences. A V4.1 layer failure can therefore leave the arena logically occupied and poison later requests, including small ones.

A minimal failure-only patch calls `ds4_gpu_synchronize()` after logging a failed streaming V4.1 layer. The successful hot path is unchanged. The standalone patch is stored at `docs/patches/ds4-v41-stage-recovery-20260918.patch`; it is not deployed to 19196 and must pass isolated CUDA build/testing first.

### Recovery build validation

The isolated recovery worktree `/home/bandi/ds4-stage-recovery-20260918` was rebuilt with `make -B ds4-server CUDA_ARCH=sm_75`. Full CUDA compilation and final link completed with exit 0; the resulting `ds4-server --help` executes successfully. The patch also applies cleanly in dry-run against the current live-WIP source tree.

This is build validation only. No recovery binary was started against the model, no deliberate OOM was triggered, and neither the 19194 production service nor the 19196 Judge sidecar was restarted or replaced.

## Admission planner shadow checkpoint

A pure admission planner now evaluates the authoritative Judge request before any inference. It returns only `RAW`, `SAFE_CANDIDATE`, or `REVIEW`; it never executes a model request and never authorizes an action. Exact token counting remains externalized through a counter so tests do not require DS4.

`BOTTAZZI_MOTOR_ADMISSION_SHADOW=1` adds telemetry only. Exact tokenizer/model paths must be supplied through `BOTTAZZI_MOTOR_TOKENIZER_BIN` and `BOTTAZZI_MOTOR_MODEL_PATH`; missing configuration fails closed in telemetry and does not alter the Judge result.

Offline exact-token measurements on cases produced by the real `build_verification_case()` path:

- base verification case: 226 tokens -> `REVIEW` at budget 192;
- normal patch evidence: 273 tokens -> `REVIEW`;
- patch plus 12 context facts: 477 tokens; SAFE facts became 486, so the planner correctly reports no gain and keeps `REVIEW`.

This shows that the current 192-token observed-safe bound is primarily a runtime/protocol constraint, not a long-context-compression problem. A research-only compact system prompt reduces the same cases to 190, 237 and 441 tokens respectively; only the base case crosses below the observed bound. No prompt protocol has been changed in production.
