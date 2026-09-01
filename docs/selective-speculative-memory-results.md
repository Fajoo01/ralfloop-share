# Selective speculative memory: results

Commit base: `ddb91ef`. Production profile/default: unchanged. Runtime:
`llama.cpp 6b80c74f2`, Qwen3.5-35B-A3B Q4_K_M, CPU, isolated port `19196`.

| Problem | Cause | Change | Verification |
|---|---|---|---|
| Knowledge, speculation, and recurrent state could be conflated | They have different correctness rules | Added independent `RetrievalDecision`, deterministic pool selection, and exact-token `PromptStateDecision` | Unit tests fail closed on identity changes |
| Previous 128/128 acceptance was not representative | Exact repeated output | Six-class, isolated OFF/GLOBAL/PROJECT/SESSION matrix | Acceptance spans 18.8–54.7%; all retained outputs correct |
| Prompt cache contaminated initial runs | Server prompt cache/checkpoints survived requests | Clean suite uses `--cache-ram 0 --ctx-checkpoints 0 --cache-reuse 0 --no-cache-prompt` | Every clean row reports zero cached prompt tokens |
| Global history may contaminate drafts | One shared overwrite-on-collision table | Added incompatible project/host/environment/path seeds | Wrong-host pool: 7.70 tok/s versus OFF 8.00; mixed GLOBAL 8.28 |
| Pools/checkpoints could grow without bounds | Runtime pool is fixed but proposed catalogs were not bounded | Hard byte/count quotas plus LRU | Quota/eviction tests pass |
| Runtime has no request pool identifier | `ngram-mod` owns one table per speculative context | Simulated scopes with sequential fresh processes | No simultaneous model copies; no production change |

## Benchmark matrix

Full raw rows and server logs are in `benchmarks/qwen35/selective-ngram/`. Summary:
`benchmarks/qwen35/selective-ngram-matrix.md` and
`benchmarks/qwen35/selective-ngram-summary.json`.

Key comparisons:

| Class | OFF tok/s | GLOBAL tok/s / accept | PROJECT tok/s / accept | SESSION tok/s / accept | Result |
|---|---:|---:|---:|---:|---|
| exact_repeat | 8.03 | 8.56 / 33.9% | 9.38 / 45.6% | 8.62 / 45.6% | PROJECT +16.9%, not repeated by SESSION |
| near_repeat | 7.98 | 7.09 / 29.8% | 8.13 / 45.3% | 5.70 / 40.6% | high variance; SESSION −28.5% |
| same_project_different_file | 8.01 | 8.55 / 32.1% | 8.26 / 37.0% | 8.88 / 37.0% | no consistent narrow-pool lead |
| same_domain_different_host | 8.00 | 8.28 / 28.8% | 7.45 / 30.6% | 5.08 / 30.6% | narrow pool regresses |
| unrelated_project | 7.64 | 10.09 / 48.7% | 9.32 / 43.5% | 9.31 / 43.5% | GLOBAL wins |
| novel_prose | 8.00 | 7.88 / 18.8% | 7.49 / 18.8% | 7.87 / 18.8% | selector must choose OFF |

Means use two target runs per cell, except the repeated `near_repeat/PROJECT` and
contamination cells (three). Correctness is 100% in the final clean matrix. The prior
prompt-cache-confounded evidence is preserved separately under
`benchmarks/qwen35/selective-ngram-confounded/` and excluded from conclusions.

## Contamination

`same_domain_different_host/contaminated_global` prewarms production `host-a`, then
targets staging `host-b`: 35.7% acceptance, 7.70 tok/s, versus OFF 8.00. More accepted
draft tokens did not imply faster decode. `unrelated_project/contaminated_global`
prewarms only project A then targets project B: 38.0%, 8.35 tok/s, below the mixed
GLOBAL pool at 48.7%, 10.09 tok/s. Outputs remained correct because the target model
verified drafts. Contamination costs performance, not correctness, in this sample.

## Memory and CPU

- Installed `ngram-mod`: exactly 4,194,304 `int32_t` entries = 16 MiB per speculative
  context; occupancy/reset rules remain upstream.
- Microbenchmark: 5,000,000 lookups in 0.120749 s = 24.150 ns/lookup. Per-target
  lookup-only estimates are about 4.6–5.6 µs; server cumulative n-gram accounting is
  1.3–3.9 ms for representative cells. Verification forwards dominate.
- Measured recurrent checkpoint: 62.813 MiB. A prior isolated 256 MiB cache held two
  prompts at 255.9 MiB; its log recorded 5 creates, 1 exact restore, 4 invalidations.
- Prototype checkpoint quota is mandatory by count and bytes. Example safe research
  budget: 8 checkpoints and 512 MiB; it is not wired into production.
- Prototype speculative bounds: global 16 MiB, max 8 project pools, max 16 session
  pools, 256 MiB aggregate, LRU eviction.

## Upstream finding

Current `ngram-mod` uses one 16 MiB hash pool shared across sequences. It is updated
from each prompt and generated chunks, has automatic whole-pool reset, no save/load,
no public reset, and no internal mutex. `--lookup-cache-static/dynamic` belongs to
the distinct `ngram-cache` path. Source audit is detailed in
`docs/selective-speculative-memory.md`.

## Decision

**NO-GO for a llama.cpp multi-pool patch now.** PROJECT/SESSION did not repeatedly
beat GLOBAL, and acceptance alone did not predict throughput. **GO for the bounded
selector/checkpoint API prototype:** enable n-gram only for deterministic, repetitive
structured work; choose OFF for novel prose and uncertain metadata. Re-run with more
samples and GPU/offload before reconsidering the minimal separate-map `pool_id` patch.
