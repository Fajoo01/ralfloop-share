# Checkpoint — Bandi MCP cross-chat

Date: 2026-09-16
Base: `release/atm-teacher-integration-20260910` @ `4fdbbcc`
Target coordination branch: `codex/bandi-mcp-integration-20260916`

## This branch

- Adds a strict read-only Bandi MCP with 4 tools: fresh research, latest report, report search, opportunity detail.
- Routes natural Bot-tazzi requests such as `cercami bandi adatti a Tiremm` to `bandi.research`.
- Routes specific compatibility checks to `bandi.eligibility`.
- Reuses the existing M9/M10 Bandi research pipeline instead of duplicating it.
- Adds direct official listing discovery so Regione Lombardia detail pages are actually reached.
- Hardens status, document classification and beneficiary-class filtering to avoid false positives such as Pro Loco-only calls.
- Keeps the connector read-only: `writes=0`, `sends=0`.
- Bandi broker is active at `/tmp/ralf-bandi-mcp/mcp.sock`.

## Other chat / GitHub branch

Related work already exists on:

- `codex/arci-grant-email-intake-20260914`
- commit `f0266e22cecb24bfc0cfa8cc2db2de27d8141bfd`
- worktree `/home/bandi/ralfloop-arci-grant-intake-20260914`

That branch adds ARCI grant email intake, extraction of a Bando reference from Gmail evidence, Gmail inbox trigger and approval-bound reply preparation.

The two lines of work are complementary. Do not replace either with the other. Integration must preserve both:

1. Gmail/ARCI intake can supply a specific bando reference or source email.
2. Bandi MCP performs fresh discovery, official-source verification and opportunity/eligibility analysis.
3. Planner/runtime should retain both email-source chaining and the new `bandi.research` route.
4. External application/submission remains outside this read-only MCP.

## Verification

Targeted suite: `58 passed` across Bandi MCP, weekly research, semantic retrieval and unified runtime tests.

Live MCP research produced verified Regione Lombardia opportunities, including:

- `Terzo settore Triennio 2026-2028`, deadline 2026-10-16.
- `Proposte di educazione ambientale e alla sostenibilità – 2026`, deadline 2026-10-22.

A Pro Loco-only call is now classified as incompatible for Tiremm rather than receiving a high compatibility score.

Before production deployment, merge/cherry-pick the ARCI intake branch deliberately, rerun the combined planner/runtime tests, then build the immutable release and canary the normal Bot-tazzi entrypoint.
