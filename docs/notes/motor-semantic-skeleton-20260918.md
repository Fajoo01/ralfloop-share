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
