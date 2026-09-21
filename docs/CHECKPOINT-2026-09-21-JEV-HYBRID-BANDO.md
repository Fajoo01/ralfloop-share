# Checkpoint — JEV/SemIf hybrid bando pipeline — 2026-09-21

Branch: `feat/motor-bando-pipeline-20260921`

## Architecture

Operational target:

`official regulation -> structured rule extraction -> deterministic hard checks -> SemIf/Qwen3.5 semantic triage -> DS4 Motor residual -> human review`

No stage authorizes external execution. `19194` remains untouched.

## Why chunk-level SemIf was rejected

The first fusion put SemIf in front of arbitrary SAFE text chunks. On the private ACT 2026 dossier it produced 30 chunks, 0 fast-PASS and 30 escalations at the frozen `P(PASS) >= 0.97` threshold.

The model itself was fast: roughly 0.29-0.38 s per chunk on RTX 2070 CUDA. The failure was architectural: isolated descriptive chunks do not contain all evidence needed for a final dossier verdict, so REVIEW is the conservative answer.
## Reuse of validated JEV path

Qwen3.5-4B Q4_K_M keeps the validated A/B/C one-token grammar scoring protocol and the conservative fast-PASS threshold 0.97. The prior frozen evaluation was 164/181 correct with 39 observed fast-PASS candidates and 0 observed false fast-PASS over 136 non-PASS examples.

The frozen held-out corpus is predominantly one-rule cases, so rule-level semantic checks match the intended decision shape better than arbitrary document chunks.

A live single-rule probe on ACT-style checks confirmed that semantic eligibility can reach a high-confidence PASS, while arithmetic comparisons are not reliable enough for semantic authority. Numeric/date/range comparisons therefore remain deterministic.

## Deterministic ACT preflight

The existing `BandoDomainBuilder` extracted 20 structured rules from the official ACT regulation. The hybrid preflight evaluated 21 checks including budget balance:

- SATISFIED: 5
- UNKNOWN / semantic residue: 14
- VIOLATED: 2
- hard violations: 1

The hard violation is the extracted minimum-contribution rule. The budget-balance mismatch is recorded as a non-hard review signal. Missing-document evidence is also review-only because a platform-generated document may not appear in the extracted application PDF.
For this dossier the deterministic hard gate returns `REJECT` and `next_stage=stop`, so neither Qwen nor DS4 is required for the final eligibility disposition. This agrees with the independent live DS4 finance canary recorded previously.

## Runtime / privacy

The Qwen CUDA canary used the isolated local model only. After measurement it was stopped and the prior experimental DS4 server on `127.0.0.1:19240` was restored from its captured command/environment.

Benchmark artifacts contain hashes, probabilities, rule IDs and metrics only; no application text or personal data is published.

## Promotion rule

Chunk-level SemIf remains research-only and is not the default runner. The promoted design uses deterministic structured gates first. SemIf may fast-PASS only read-only semantic checks meeting the frozen 0.97 threshold; everything else escalates to Motor/human review. Numeric and date arithmetic never depend on SemIf.
