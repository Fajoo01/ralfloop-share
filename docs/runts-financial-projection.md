# Existing decisions and financial projection — 2026-09-07

Status: PARTIAL / BLOCKED_REVIEW. No source mutations or new economic classifications.

## Preserved decisions

2488 has the legacy `Debito personale` note and `member_advance`, with no economic
category assigned. The report ignored that note and called it unmapped; it is now
represented as an existing non-management personal-debt decision. No new cost code.
2544/2576 retain category 25 (`PDC_AFFILIAZIONE_ARCI`), existing RUNTS A2/Servizi,
and `member_advance`. Notes record reimbursement. No manual override rows were
found for these IDs in reviews/splits/external matches/expense allocations.
The same decisions exist in inspected May 2026 snapshots and the September 2
snapshot. Their original decision timestamp is unavailable; record hashes are
retained instead. RUNTS Suite's Git repository has no commits; no migration loss
is asserted. The marking API updates movement kind/notes, not RUNTS category.

The apparent missing mapping for parent movement 2248 was a projection bug:
its stored splits already have mappings. The new gate checks effective split
decisions, not the superseded parent's fallback. No classification was reopened.

## Duplicate source

- Original upload: import 22, 2026-04-14 13:22:33.385339, destination account 1.
- Replay: import 23, 2026-04-14 13:23:39.684190, destination account 3.
- Same parser `coop_libretto_pdf_v1`, source hash, 42 ordered raw rows, and ordered
  financial ledger tuples `(date, signed amount)`; native IDs/accounts differ.
- Source hash: `b1ece5fb61fb041e20ac30cac0b44306a32703d93d2922749110864a2029a2fd`.
- Group: `duplicate.67b163a20eed035b492749c0`.

The upload handler accepts `account_id` and inserts a new import without checking
for prior source hashes. Normalization propagates the import's destination and
sets `is_duplicate=False`. This explains the replay mechanism; there is no retained
request/audit evidence proving why the operator chose different destinations.
It is not evidence of an economic classification error.

Account 3 is typed `libretto` and explicitly selected by the second upload, but no
stable account identifier is available there to compare with the original statement.
Account 1's stable alias does not match that statement. Consequently preferred owner
is unset, status `BLOCKED_REVIEW`; account type alone does not prove ownership.

The projection neutralizes the duplicate by representing its source **once** in an
owner-neutral source unit. It removes both copies from account-level projected deltas,
without deleting anything or assigning a new owner. Libretto source reconciliation:
**210.35 - 210.28 = 0.07**, residual 0.00, one occurrence.
Accounts 1/2 still lack independent opening/closing evidence; account 3 awaits source
ownership. No cash/libretto compensation and no claim that every account reconciles.
Account 1's remaining projected delta is +861.04 (bank import +857.00, separate
CSV source +4.04, older text import net 0.00); it must not be equated with an
independently reconciled bank statement balance.

## Reimbursement matching

Recorded references 2557/2582 currently represent -20/-200 on the personal account,
not a demonstrated entity debit / personal credit; dates also differ. Their total
220.00 is not the advances' 220.20. The note is preserved, not silently deleted.

The bounded matcher ignores obsolete note IDs and requires exact amount, date window,
correct directions, entity/personal accounts, source hashes and either an exact shared
transfer reference or reciprocal native counterparty account identity. Amount/date
alone do not establish a link. Live data yielded no proven pair for the group:
`REIMBURSEMENT_LINK_UNVERIFIED`. This does not reverse the recorded reimbursement
decision; its financial references remain unverified. No expense is duplicated.

## Approved figures and artifacts

Explicit caller-supplied approved figures are checked before PDF generation:
income 16366.87, expenses 15654.17, surplus 712.70, approved closing 2283.08.
Any drift raises `APPROVED_TOTALS_CHANGED`. The original bridge remains
1842.60 + 712.70 - 472.33 - 20.09 + 220.20 = 2283.08 (residual 0.00).
This legacy bridge is not falsely relabelled as a complete per-account reconciliation.

Regenerated private artifacts:
`/home/bandi/.local/share/bottazzi/runts/2603942/projection-20260907/`.
Includes reconciliation JSON, projected management rows, PDF review, hash-bound proposal
and Memory persistence. Semantic PREPARE runs through the existing runtime/retrieval/MCP.
Financial source dedup never deletes an economic movement or changes approved totals.

Remaining blockers:

- `DUPLICATE_SOURCE_OWNER_UNVERIFIED`: document-backed native account ownership.
- `REIMBURSEMENT_LINK_UNVERIFIED`: exact source-backed repayment pairing.
- `ACCOUNT_BALANCE_EVIDENCE_REQUIRED`: independent bank/cash opening and closing balances.
- `CAPITAL_AND_TAX_PRESENTATION_REVIEW_REQUIRED`: existing expense labels such as
  `Tasse` do not establish whether amounts belong in the ministerial standalone tax
  row; capital/excluded financial flows lack an approved mapping to those display
  rows. Need the existing supporting decision/document, not a new expense classification
  or a balancing entry. Unknowns remain explicit, not fabricated zeros.

Proposal stays BLOCKED_REVIEW, executable=false. Production unchanged, WRITE=0.

Gate: 6 new financial tests + 79 adjacent tests = 85 PASS; the preceding accounting/
prepare tests were already green and were not repeated for rediscovery. No new
failures in this selection; global historical baseline not rerun. Production SQLite
SHA256 was identical before and after artifact generation. No APK/service change.
