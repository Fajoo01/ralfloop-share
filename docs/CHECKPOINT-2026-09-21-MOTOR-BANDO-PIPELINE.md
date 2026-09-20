# Motor bando pipeline — checkpoint 2026-09-21

Branch: `feat/motor-bando-pipeline-20260921`
Base: `6a390b7` (`research/motor-semantic-equivalence-20260920`)

Implemented fused path:
`bando facts -> semantic chunker -> SAFE skeleton -> Motor Judge -> deterministic aggregator`.

Safety properties:
- exact rendered DS4 token budget per chunk;
- default cap 360 tokens, below the observed 379-token successful band;
- no blind truncation: an atomic unit over budget fails closed;
- rules and candidate actions are repeated in every chunk;
- chunk disagreement, incomplete runtime, or PASS with a blocked gate => final `UNCERTAIN`;
- aggregation never authorizes execution;
- serialized reports contain hashes/metrics, not source dossier text.

Live canary on `19196`: 2 chunks, 359 and 244 tokens; both runtime complete; no CUDA/prefill errors.
Both chunk verdicts were PASS/LOW but gates blocked (missing evidence / low confidence), so final result is correctly `UNCERTAIN` with human review required.
`19194` was never touched. `19240` was temporarily stopped and restored with its saved command/environment.
Tests: 44 passed.
