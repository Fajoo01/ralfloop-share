# RUNTS documentary gate — 2026-09-07

Continuation of `7bfc135`. No economic reclassification, source mutation,
deployment, upload, submission or email. All Suite SQLite connections use `mode=ro`.
Private artifacts remain outside Git.

## Verdict

`BLOCKED_REVIEW`, not `READY_FOR_HUMAN_APPROVAL` and never `EXECUTABLE`.
The current evidence does not justify regenerating a final PDF. The previous
review PDF is reused byte-for-byte, not relabelled as approved. Its incomplete
documentary sections remain explicitly unverified; zero is not a substitute for
missing evidence. The existing draft-only MCP permission/schema stays strict.

| Problem | Cause | Change | Verification |
|---|---|---|---|
| Duplicate ownership | Original upload does not identify the correct account | Owner evidence now requires matching instrument fingerprints plus source and mapping references | Chronology-only, different identifiers and conflicting owners stay blocked |
| Account balances | Account 1 mixes bank, PayPal and savings imports; account 2 is a wallet, not cash | Separate source-instrument audit and account-level evidence; no cash/wallet compensation | PayPal source reconciles all 782 intermediate balances; bank/cash remain explicitly incomplete |
| Tax presentation | Existing F24 expense is mapped to CE5, but no tax-breakdown document is linked | Preserve the expense and expose a source-backed presentation candidate | 2393 remains PDC_TASSE, -1020.06; no automatic standalone-tax reassignment or double count |
| Proposal lacked financial dossier | Previous proposal bound only the PDF and generic checks | Typed approved figures, independent account evidence, generator/DB hashes, channel and stale preconditions | Runtime retrieval/MCP PREPARE persists the enriched non-executable proposal |

## Ownership 22/23

Both original upload files match source SHA256
`b1ece5fb61fb041e20ac30cac0b44306a32703d93d2922749110864a2029a2fd`.
Import 22 precedes 23; source/raw/ledger equality and one projection occurrence
remain established. Production records are retained.

The cooperative statement header contains native identifiers, but none matches
the current account aliases or the aliases in the available database backups.
`accounts.owner_name` is absent for the relevant operational accounts.
`account_instruments` contains a wallet/card record, not a savings-to-account link.
The association name was not found in the statement header; this alone proves
neither personal nor association ownership. No raw identifiers or holder names
are copied to this document.

The original import handler accepts an account chosen by the caller; normalization
copies that assignment. There is no retained request evidence establishing why
different accounts were chosen. This is not evidence of a migration loss.
Preferred owner remains **null / OWNERSHIP_UNVERIFIED**. No automatic choice of
account 1 or 3 is made.

## Reimbursements

| Advance | Existing category / mode | Amount | Advance date | Recorded repayment date | Proven settlement IDs | Status |
|---|---|---:|---|---|---|---|
| 2544 | PDC_AFFILIAZIONE_ARCI / member_advance | 135.20 | 2025-10-02 | 2025-11-20 | none | REIMBURSEMENT_LINK_UNVERIFIED |
| 2576 | PDC_AFFILIAZIONE_ARCI / member_advance | 85.00 | 2025-11-14 | 2025-11-20 | none | REIMBURSEMENT_LINK_UNVERIFIED |

Recorded references 2557/2582 still fail direction/date/amount checks. In the
recorded-date search window, personal-account credit 2581 is +200.00; it is not
an exact 220.20 reimbursement and has no demonstrated matching entity debit.
Amounts or textual similarity alone do not authorize a link. Exact transfer
identity or reciprocal native account references remain mandatory. No expense
classification is blocked or changed by an unverified settlement link.

## Independent balances

| Scope | Opening | Source delta | Closing | Residual | Verdict |
|---|---:|---:|---:|---:|---|
| Bank source, import 4 | unknown | +857.00 | unknown | unknown | MISSING_BALANCE_EVIDENCE |
| Cash | 23.47, prior approved snapshot | unknown | unknown | unknown | MISSING_BALANCE_EVIDENCE |
| Savings source, once | 210.35 | -210.28 | 0.07 | 0.00 | RECONCILED; account owner unproven |
| PayPal source, import 6 | 1.21 | +4.04 | 5.25 | 0.00 | RECONCILED; ledger account assignment unproven |

The original bank XLSX contains transaction columns but no independent opening
or closing balance. The prior aggregate deposits figure 1819.13 is not blindly
assigned to that one source. No operational cash account/complete cash ledger is
present. Cash's missing delta is **null**, not zero.

PayPal's original CSV hash is verified and all 782 parsed rows equal the DB raw
rows. Opening is derived as first running balance minus first movement; every
successive running balance agrees, including final 5.25. This proves the source,
not a reassignment to account 2. Legacy account 1 delta after duplicate isolation
is +861.04 = bank +857.00 plus wallet +4.04; it is not a pure bank reconciliation.

## Capital and taxes

The effective operational classifications contain tax movement 2393, dated
2025-06-19, import 4, existing `PDC_TASSE -> CE5`, amount -1020.06. Its original
description explicitly references F24. Preserve this decision. The missing
document is the F24 detail/tax codes supporting its placement relative to the
ministerial standalone taxes row. Do not add the same amount twice.

No effective 2025 management row uses the existing investment/loan categories
72/73/74/149. This absence is **not** proof that all excluded financial flows are
zero capital flows. No blanket zero-section authorization is issued.

Two local `Bilancio 2025*.xlsx` files were inspected. Their 2025 model sheet shows
19950 income, 15380 expense, 6000 financing receipts, 1330 repayments and 2000
closing cash/deposits: these do not match the approved figures in this task.
They cannot replace the approved 2025 balance or prove zero capital sections.
Their prior-year sheet agrees with cash 23.47 / deposits 1819.13, but does not
establish the missing 2025 account closing balances.

## Approved invariants and artifacts

Before and after: income **16366.87**, expense **15654.17**, surplus **712.70**,
declared closing **2283.08**. Historic bridge residual remains **0.00**; this is
not an independent account reconciliation.

Private output: `/home/bandi/.local/share/bottazzi/runts/2603942/documentary-20260907/`.
`reconciliation.json` contains the source audit, preserved effective mappings,
per-advance evidence and independent scopes. `proposal.json` is produced through
the existing runtime / capability retrieval / semantic MCP / Memory path.

Review PDF (reused, not regenerated as final): `modello_d_review.pdf`.
SHA256: `6eb7a9afaed82d3a5be844f2e92ac790c0c234ed4e86c9f4ccab458ff49339fe`.
Practice 2603942; authoritative message 523278; channel RUNTS MESSAGGISTICA.

## Residual blockers — single authoritative list

1. `DUPLICATE_SOURCE_OWNER_UNVERIFIED`: authoritative instrument-to-account mapping missing.
2. `REIMBURSEMENT_LINK_UNVERIFIED`: exact entity/personal settlement evidence for 2544/2576 missing.
3. `ACCOUNT_BALANCE_EVIDENCE_REQUIRED`: independent bank opening/closing, cash ledger/closing and wallet ownership missing.
4. `CAPITAL_AND_TAX_PRESENTATION_REVIEW_REQUIRED`: F24 detail and evidence for the treatment of excluded financial flows; conflicting workbook is not approval evidence.
5. `FINAL_DOCUMENT_REVIEW_REQUIRED`: final complete PDF cannot be validated until the preceding evidence is resolved.

## Gate

Targeted + adjacent selection: 94 PASS; previous checkpoint's selection 85 PASS,
nine added cases, no new failures. No new claim about the historically red global
suite. Existing generator zero-row tests, approved-total guards and live PDF text
checks remain in force. New tests exercise ownership conflicts, intermediate
balance conflicts, independent account evidence, read-only DB hashes and proposal
context binding. Synthetic tests do not prove missing live documents.

Production DB SHA256 before/after:
`efa74ecb18fa0f5390477dd19a11fc9f75a16b673a0ba18f01bce63e0e48fa25`.
No services, Android project, production classifications or source rows changed.
WRITE count: **0**.
