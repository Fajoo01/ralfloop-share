# Bottazzi project status

## Current milestone

Milestone 6 — Tiremm Admin v2 integration.

## Completed

- M1 repository inventory: `docs/bottazzi-platform-inventory.md`.
- M2 minimal common contracts: permission, promotion, normalized errors, source refs, capability descriptor/result.
- M3 central normalized in-process capability registry with fail-closed validation.
- M4 deterministic lexical/IDF retrieval core; permission, promotion, enabled and health filters; maximum 12 results; 104-case eval baseline.
- M5 SQLite core: append-only deduplicated events, structured practice projection, FTS5 document search, mandatory provenance.
- M5 semantic read-only Memory MCP: practice, open practices, timeline, document search.

## Frozen commits

- Capability platform/retrieval: `a40bc43` (`feat: add Bottazzi capability platform and retrieval`).
- Memory Service/MCP/status/baseline evidence: commit containing this status file (`feat: add persistent memory service and semantic MCP`).
- Regression baseline: `0242e79`; exact comparison in `docs/full-suite-baseline.md`.

## Tests

- `tests/test_platform_capabilities.py`: 5 passed.
- Targeted platform/Admin/identity regression: 74 passed.
- Memory Service/MCP: 3 passed.
- Relative full-suite gate: GREEN. Current 2191 passed/65 failed/17 skipped/exit 139; baseline `0242e79` 2183 passed/65 failed/17 skipped/exit 139. Exact 65-test failure sets equal; zero new regressions; eight new passes. Evidence: `docs/full-suite-baseline.md`.

## Eval

- 104 deterministic cases from 13 realistic Tiremm intents × 8 phrasing variants.
- recall@3/task success: 1.0; precision@3: 0.7436; mean selected: 1.6346; irrelevant selections: 66 total.
- latency mean: 0.155 ms; p95: 0.271 ms.
- schema tokens: 5,538 selected vs 44,200 all-tools baseline (87.5% reduction); hallucinated tools: 0.

## Architecture decisions

- New platform layer wraps existing registries; no business logic moved or duplicated.
- Deterministic lexical retrieval first. Embeddings optional later.
- Unknown capability denied. Invalid permission/state combination rejected at load time.
- New capabilities remain disabled unless explicitly promoted.

## Open risks

- Existing registry schemas remain heterogeneous.
- IdentityLink primary projection remains in-memory. Tiremm Admin now has optional Memory Service persistence, not wired into runtime.
- ARCI individual member enumeration unavailable through current deployed MCP.
- Optional test dependencies are incomplete in project metadata (`PyYAML`, `beautifulsoup4`).

## Blocked

- Global suite remains historically red; tracked separately. Dependency set is not locked in project metadata.
- Live ARCI completeness/reconciliation requires read-only production connectivity; intentionally not used this run.

## Next step

- Split two atomic M1-M5 commits, then add production capability descriptors and wire Tiremm Admin v2 deterministic reads to Memory Service.

## Git status

- Branch: `codex/qwen35-runtime-research`.
- Baseline: `0242e79`.
- Remote target: `private`; `origin` is public legacy and must not receive project data.
- Local uncommitted Bottazzi platform files present. No push.
