# Bottazzi project status

## Current milestone

Operational M1–Media preserved. RUNTS source hierarchy and exercise-scoped human decisions are now structured/versioned in Memory, not free-text prompt memory. The user resolved presentation and economic ownership for the approved 2025 Model D. Production remains READ/PREPARE without an operational RUNTS write executor; no deployment performed. See `docs/runts-source-decisions.md`. Historical checkpoints below remain evidence of earlier states, not current blockers after explicit decisions.

## Completed

- M1 repository inventory: `docs/bottazzi-platform-inventory.md`.
- M2 minimal common contracts: permission, promotion, normalized errors, source refs, capability descriptor/result.
- M3 central normalized in-process capability registry with fail-closed validation.
- M4 deterministic lexical/IDF retrieval core; permission, promotion, enabled and health filters; maximum 12 results; 104-case eval baseline.
- M5 SQLite core: append-only deduplicated events, structured practice projection, FTS5 document search, mandatory provenance.
- M5 semantic read-only Memory MCP: practice, open practices, timeline, document search.
- M6 Tiremm Admin v2: v1-compatible validation, persistent sources/practices, event timeline, restart restore, ten semantic MCP capabilities.
- M7-A: ARCI coverage matrix, mock-vs-provider gap, identity/status/pagination/error/provenance/write inventory documented in `docs/arci-mcp-m7-audit.md`.
- M7-B2: live read-only DataTables contracts captured and sanitized; deterministic complete paginator and three conditional semantic MCP tools implemented. ARCI write inventory documented; execution remains zero.
- M8: exact IdentityLink/Jellyfin shadow workflow, seven semantic reads, four non-executable proposals, legacy tool compatibility and Memory-backed practice timeline. Evidence: `docs/identity-jellyfin-m8.md`.
- M9: operational Bandi service using shared Memory, normalized contract, change timeline, deterministic eligibility, source catalog and eight semantic MCP tools. Evidence: `docs/bandi-platform-m9.md`.
- M10: bounded Web Research service/MCP reusing secure open/search primitives, assigned source IDs, comparison, evidence extraction, claim verification and Memory persistence. Evidence: `docs/web-research-m10.md`.
- M11: common deterministic Event Router for poller/webhook/scheduler/API/service/MCP events, shared Memory timeline, explicit workflow/wake gates and metrics. Evidence: `docs/event-router-m11.md`.
- Nightly Worker: persistent/idempotent Memory queue, Europe/Rome execution window, deterministic-first routing, configurable model tiers, bounded escalation and usage audit. Evidence: `docs/nightly-worker-m12.md`.
- Media Quality: PII-minimized ticket lifecycle, deterministic stream diagnosis, strict semantic MCP, non-executable fix proposals, complete library reconciliation and shared Event/Memory/Nightly flow. Evidence: `docs/media-quality-m13.md`.
- Operational eval/observability/safety audit: `docs/operational-gates-m14.md`.
- Shared local/shadow composition root: `docs/operational-runtime-m15.md`.
- PEC/RUNTS vertical core: strict DTO/provenance, shared Event/Memory/Nightly routing, exact RUNTSuite correlation, eight semantic tools and live authenticated PEC invocation. Evidence: `docs/pec-runts-vertical.md`.
- Telegram PEC/RUNTS decision path: exact-reference pre-route, Capability Registry retrieval, strict decision and real semantic MCP invocation; generic model-tool catalog bypassed only for this typed intent. Evidence: `docs/telegram-pec-runts-decision.md`.

## Frozen commits

- Capability platform/retrieval: `a40bc43` (`feat: add Bottazzi capability platform and retrieval`).
- Memory Service/MCP/status/baseline evidence: commit containing this status file (`feat: add persistent memory service and semantic MCP`).
- Regression baseline: `0242e79`; exact comparison in `docs/full-suite-baseline.md`.

## Tests

- `tests/test_platform_capabilities.py`: 5 passed.
- Targeted platform/Admin/identity regression: 74 passed.
- Memory Service/MCP: 3 passed.
- M6 targeted platform/Admin/identity regression: 78 passed.
- Relative full-suite gate through M6: GREEN. Current 2195 passed/65 failed/17 skipped/exit 139; baseline `0242e79` 2183 passed/65 failed/17 skipped/exit 139. Exact 65-test failure sets equal; zero new regressions; twelve new passes. Evidence: `docs/full-suite-baseline.md`.
- M7 combined targeted ARCI/identity suite: 59 passed. Live contract reconciliation: users 48/48; cards 50/50; `per_page=100`, `last_page=1`. Synthetic multi-page and failure-family coverage included. Production entrypoint unchanged.
- M8 targeted identity/Jellyfin/Memory/Admin suite: 32 passed. Live read-only smoke: Jellyfin 10.11.6, health true, 7 users, 3 libraries, exact item/playback contracts verified; writes 0.
- M9 targeted Bandi/Memory suite: 10 passed; legacy Bandi regressions included in milestone gate.
- M10 new + legacy Web Research/Bando research suite: 97 passed.
- M11 Event Router + Memory suite: 8 passed.
- Nightly Worker + Event Router + Memory suite: 13 passed.
- Media Quality + Nightly/Event Router/Memory/Jellyfin/Capability suite: 32 passed.
- Cross-domain observability/eval targeted gate: 33 passed.
- Final M7–Media relative regression selection: 176 passed; zero new failures.
- Post-hardening M7–Media relative regression selection: 180 passed; zero new failures.
- Operational runtime integration: 2 passed; shared persistence/routing/model-free deterministic path verified.
- Final M7–operational-runtime relative selection: 182 passed; zero new failures.
- PEC/RUNTS targeted runtime/regression selection: 35 passed; live Bottazzi PEC smoke: 10 read/persisted, provenance hashes 10/10, writes 0.
- Telegram PEC/RUNTS regression and adjacent orchestrator/chat suite: 58 passed; exact failure phrase validates and invokes `pec_find_by_runts_reference`, writes 0.
- 2026-09-06 relative targeted gate, same interpreter: detached `ac52bf4` 58 PASS; current selection plus Telegram regression 63 PASS, zero failures. This is not a new global-suite comparison.
- Live development Telegram-runtime check: decision valid, `pec_find_by_runts_reference`, MCP invoked, `AUTH_REQUIRED`, writes 0. No real-message success or production Telegram acceptance claimed.

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
- IdentityLink primary projection remains in-memory; workflow evidence is Memory-backed.
- Media library scan is intentionally fail-closed when the authoritative total exceeds the explicit 1000-item safety bound.
- `ffprobe` requires an operator-configured item-to-path resolver and filesystem-root allowlist; it is not wired to production paths.
- Optional test dependencies are incomplete in project metadata (`PyYAML`, `beautifulsoup4`).
- Aruba PEC INBOX live enumeration verified: 111 unique messages over three pages; reference search runs after complete reconciliation, not after the first 100 records.

## Blocked

- Global suite remains historically red; tracked separately. Dependency set is not locked in project metadata.
- PEC authenticated session belongs to `bandi`, available through existing loopback CDP 9236 on the development host. Exact Telegram phrase executed locally through semantic MCP: 3 PEC matches, READ success, WRITE 0. Production Telegram deployment/acceptance not performed.
- RUNTS authority proven for practice 2603942: message 523278 through observed `/api/v1/messaggio/2603942` (frontend parameter `idIstanza`). Official Model D attachment downloaded; binary SHA256 matches local file. Browser adapter/fixture work remains uncommitted and is preserved separately.
- Modello D checkpoint PARTIAL: development overlay corrects exact-label A6/A7 expense aliases and missing layout sections; running Suite unchanged. Real-data bridge explains 272.22 with zero arithmetic residual, but accounting classification remains unverified. Private review PDF and source/hash-bound `BLOCKED_REVIEW` ActionProposal generated through runtime/retrieval/MCP/Memory; no final filing claimed. See `docs/runts-modello-d-reconciliation.md`.
- Existing decisions recovered and preserved: 2488 is legacy personal debt; 2544/2576 remain ARCI affiliation + member_advance. No reclassification required. Parent 2248's apparent missing mapping was superseded by mapped splits. Source-backed reimbursement links remain unverified; historical note IDs are inconsistent.
- 2026-09-07 financial projection: imports 22/23 have identical source/raw/financial-ledger fingerprints. One owner-neutral source unit now reconciles 210.35 - 210.28 = 0.07 exactly once. Source ownership and independent bank/cash balances remain unproven. Approved income/expense/surplus/closing are explicitly checked and unchanged; private PDF/proposal regenerated, BLOCKED_REVIEW, WRITE 0. Details: `docs/runts-financial-projection.md`.
- Companion source recovered at `/home/bandi/work/android-companion`; no Android change/build/install yet. Sibilla has no adb available. Device handlers, QR handoff and Keystore authentication remain unverified.
- Auth Broker/resume, new OTP integration and practice 2603942 documentary preparation are not complete.

## Next step

- Documentary gate after `7bfc135`: BLOCKED_REVIEW confirmed, not ready for approval. Source header/current aliases/backup aliases do not prove import 22/23 ownership. Original PayPal CSV reconciles 782/782 raw rows and all intermediate balances (1.21 + 4.04 = 5.25), but account assignment remains unproven. Account 2 is a wallet, not cash; bank source delta is +857.00, independent closing absent. Cash opening 23.47 is preserved; cash delta/closing remain unknown. F24 2393 stays PDC_TASSE/CE5; detail needed for presentation. Conflicting local workbook figures are not used to alter the approved 2025 figures. See `docs/runts-documentary-gate.md` for the single residual blocker list.
- Enriched typed PREPARE dossier includes approved figures, independent account evidence, generator and production DB hashes, RUNTS MESSAGGISTICA, and stale checks. Previous review PDF reused byte-for-byte, not promoted or regenerated as final. Targeted/adjacent gate: 94 PASS, zero new failures; production DB hash unchanged; WRITE 0. Pre-existing untracked RUNTS browser work preserved separately.
- Obtain the missing instrument-to-account evidence, exact reimbursement evidence, independent bank/cash balances and F24/capital presentation support. No reclassification, production promotion, upload or send.

## Git status

- Branch: `codex/qwen35-runtime-research`.
- Baseline: `0242e79`.
- Remote target: `private`; `origin` is public legacy and must not receive project data.
- Nightly Worker: `8a41ac2`; Media Quality: `b8d3e89`; both pushed to `private`.
- Operational observability: `d153c68`, pushed to `private`.
- Media pagination/`ffprobe` hardening: commit containing this status.
- Operational shadow composition: commit containing this status.
