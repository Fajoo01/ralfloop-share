# RUNTS persistent-ingress investigation — 2026-09-07

## Production boundary

Process cwd/release: `44c8fd09dfb732bf5835c12bef3821981a3139a1`.
Initial process had WRITE=1. Before tests, systemd override
`99-runts-read-prepare-only.conf` set `BOTTAZZI_RUNTS_WRITE_ENABLED=0`;
backend restarted on the **same release**. Actual process verified WRITE=0,
Telegram approval gate=1. No code deployment/commit/push. Leave WRITE disabled.

Meowgram `_explicit_bottazzi_goal` requires a leading Bottazzi prefix. The
reported request has none: `_call_ralfloop_natural_backend` posts `/tasks/run`
with `mode=route_only`, then `/tasks/run` without mode for `tool_backed_prepare`.
Context retains source, telegram_user_id/chat_id/message_id; installed client
does not supply telegram_chat_type. `/orchestrate` is only the check_only branch.

Job DB read-only: one total job, zero matching `2603942`; no matching job fields
or retry/cache entries exist. This is the non-job branch. Journal shows Meowgram
activity and POST at 17:08:30 CEST, followed by `/tasks/run` HTTP 200 at 17:09:24
on release44c8fd0. No shared request ID/response body is retained: historical
JSON attribution is not claimed from access logs alone. Current production
HTTP route-only probe returns tool_backed_prepare without provider execution.

## Reproduced first divergence

On SQLite `.backup()` copy of production Memory, decision-plan replay passes.
Reobserving the stored RUNTS practice raises:

`PecRuntsService._put_runts` → `_emit` → `MemoryService.append_event`
→ `ValueError("memory_event_conflict")`.

Old event ID depends on source/native ID/type/payload, but not observation time.
Fresh reads change observed_at and provenance.observed_at. Entity persistence
marks the observation fresh; event insert collides with the existing ID and
immutable comparison rejects different timestamps. MCP maps that ValueError
to MALFORMED_RESPONSE; execute_telegram_prepare renders the reported failure.
Empty temporary memory hides the bug. Approval staging is downstream.

Fix is confined to the PEC/RUNTS event producer plus a read-only get_event:
deduplicate identical source revisions ignoring local observation timestamps,
preserve first evidence, recognize legacy IDs, version changed source evidence
in both event ID and payload (both Memory uniqueness constraints matter).
Memory conflict checks and MCP error allowlist remain strict and unchanged.

## Environment/state

Backend binding and Memory paths match authorized paths. Unified assistant=1;
session-dir unset resolves under service user `.local/state/ralf/unified-sessions`.
Four session JSON files inspected: no practice2603942/pending RUNTS reply.
Production approval DB `/var/lib/ralfloop/domain-approvals.sqlite3` has no RUNTS
requests/executions. No prior approval reused. PEC provider auto has IMAP
configuration; no secrets printed. RUNTS uses authenticated provider factory.
PEC failure does not veto authoritative RUNTS PREPARE. Diagnosis opens production
Memory read-only; reproduction Memory/session/approval stores are private copies.

## Meowgram display

Installed formatter hides review text whenever approval_required=True.
`integrations/meowgram/runts-review-display.patch` preserves text only for
successful staged zero-write RUNTS PREPARE. Flags and other domains unchanged.
Test applies patch to temporary copy of installed source, then executes formatter.
Production Meowgram source/service untouched. Existing 3900-character limit
remains; patch does not send the PDF as a Telegram document.
Backend staging now explicitly displays the exact hash-bound payload body,
not only the preparer's draft; regression compares it with the pending payload.

## Scoped checkpoint audit

| Goal | Initial | Action | Final |
| --- | --- | --- | --- |
| Persistent replay failure | OPEN | Producer fix + HTTP regression | DONE working tree |
| Strict Memory validation | DONE | Preserve conflict checks | DONE |
| Hidden RUNTS review | OPEN | Tested companion patch, not applied live | Code complete; promotion requires human decision |
| Exact approved body displayed | OPEN | Display pending payload body, test equality | DONE working tree |
| Documentation says executor absent | OBSOLETE | Correct current status | DONE |
| Short practice-bound approval | DONE | Preserve syntax/hash-bound staging | DONE |
| Historical shadow-only evidence | DONE | Retain as historical, current report here | DONE |
| Generic document-caption TODO in Meowgram | OUT_OF_SCOPE | No unrelated handler change | OUT_OF_SCOPE |
| Deployment / actual Telegram delivery acceptance | REQUIRES_HUMAN_DECISION | No deployment authorized | REQUIRES_HUMAN_DECISION |
| Actual upload/send verification | REQUIRES_HUMAN_DECISION | Synthetic guarded tests only | REQUIRES_HUMAN_DECISION |

## Verification scope

Regression: Meowgram-compatible JSON → FastAPI route_only → PREPARE HTTP handler
→ unified routing → capability retrieval → semantic MCP → persistent Memory
→ proposal → hash-bound staging. Repeated observation with changed timestamps.
Synthetic artifact/preparer explicitly do not prove financial PDF rendering.
WRITE factory forbidden. Test fails with original _emit and passes with fix.
Additional tests cover source revisions and legacy production event identities.

Financial DB expected invariant:
`efa74ecb18fa0f5390477dd19a11fc9f75a16b673a0ba18f01bce63e0e48fa25`.
No real RUNTS/PEC write, financial mutation or approval execution.

Live-safe comparison used the real authenticated providers and Suite generator,
the same Meowgram-compatible FastAPI payload, and independent SQLite backups of
production Memory with temporary session/approval stores. Original _emit:
HTTP200, MALFORMED_RESPONSE. Working-tree _emit: HTTP200, READY_FOR_HUMAN_APPROVAL,
TRA, PEC/RUNTS MCP calls, pending runts_practice_reply, writes0.
Artifact (private, temporary, not a deployment):
`/tmp/runts-safe-ingress-gwtn9dud/artifacts/prepare-2603942-ieikms8k/modello_d_review.pdf`.
The full PREPARE was not posted into the production service, to preserve its
operational Memory and approval/session stores. Production HTTP was route-only.

Final targeted/adjacent gate: **69 passed, 2 skipped**, no new regression.
Selection: test_runts_persistent_ingress, test_meowgram_runts_review_patch,
test_runts_response_workflow, test_runts_write, test_domain_approval*,
test_pec_runts, test_pec_runts_telegram, test_runts_browser_adapter,
test_memory_service, test_event_router, test_unified_telegram_entrypoint.
The two skipped workflow tests require an explicit financial binding; the
separate live-safe ingress above ran the actual Suite generator.
Changed Python files compile; diff --check clean. Nine changed/new files scanned:
no added secret/PII/private artifact, no staged files. The email-pattern match in
runtime.py is unchanged baseline text, not introduced by this fix.
Final process flag=0, gate=1, release44c8fd0. Suite DB SHA before/after identical
to the invariant above. Live-safe PDF SHA256:
`f684be9bed30ba0c836a9118aa54e771c80959606b7cd4abb4786e0062c0ebaf`.

Final ingress refinement also executes the installed Meowgram
`_call_ralfloop_natural_backend` method with only HTTP transport redirected to
TestClient, exercising both requests without a Telegram send. The four new
ingress/revision/formatter tests pass after this refinement.
