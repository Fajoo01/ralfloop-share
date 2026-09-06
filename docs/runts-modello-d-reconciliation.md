# RUNTS Suite / Modello D review — 2026-09-06

## Proven authority (not re-discovered in this checkpoint)

Practice 2603942, message 523278, dated 2026-09-03. The observed frontend calls
`recuperaListaMessaggiApiUsingGET1(idIstanza, ...)`, yielding
`GET /api/v1/messaggio/2603942`. The administrative request is to correct the 2025
financial statement using the ministerial model, keep all required sections,
and reply through existing-practice messaging, **not a new deposit**.
The attached official Model D binary SHA256 is
`9f61a5e63b7b13d4cec0ea6481e3250180dcf82d6d9075837f1e3d7642ce31d0`.
No PEC notification is substituted for this authority.

## Real accounting evidence

`runts-suite.service` is active in `/home/sibilla-cumana/runts-suite`.
The observed API is `http://127.0.0.1:8001`:
`GET /runts/mod-d-draft?year=2025` and `GET /runts/deposit-documents`.
The latter points to locally staged deposit files, not proof of remote submission.
The database was opened read-only. The original report implementation was executed
against the read-only connection; its excluded/unmapped lists were captured in full,
not the API's truncated first-100 preview.

| Component | EUR | Evidence |
|---|---:|---|
| Opening cash | 23.47 | Approved 2024 snapshot |
| Opening bank | 1819.13 | Approved 2024 snapshot |
| Management surplus | +712.70 | 16366.87 receipts minus 15654.17 expenses |
| Excluded report items | -472.33 | 834 excluded entries, exact movement IDs retained privately |
| Unmapped debit | -20.09 | Movement 2488 |
| Reverse management expenses on non-operational personal account | +220.20 | Movements 2544 (-135.20) and 2576 (-85.00) |
| Reconstructed closing total | 2283.08 | Residual 0.00 |

Thus `-472.33 -20.09 +220.20 = -272.22`. This explains the arithmetic discrepancy,
but does **not** establish that these amounts belong to investments or financing.
No synthetic balancing movement was created.

Additional review evidence: movement 2121 is a -0.25 interest withholding marked
transfer; movement 2247 has two excluded splits, +81.28 and +146.72. Exclusion
flags are not independent evidence of the correct ministerial classification.

The production cash computation also masks negative cash:
account 3 annual delta -210.28 plus opening 23.47 gives **-186.81**;
account 1 annual delta +650.76 plus opening 1819.13 gives **2469.89**.
Its code moves the negative cash into bank and reports 0/2283.08. The review build
does not reproduce this unsupported reallocation. Raw imported statement evidence
goes further: import 23, raw rows 2754–2795, opening savings balance **210.35**
(211.31 minus first interest 0.96), closing **0.07**. Its 42 imported transactions
sum -210.28. The negative-cash projection is an artifact of applying the approved
physical-cash opening 23.47 to an account typed `libretto`, not evidence of actual
negative physical cash. The source-file hash is retained in the private report.
The proper savings-account/cash/bank classification and allocation of opening
balances must be verified; the review PDF does not present the projection as fact.

## Implemented checkpoint

The development overlay reuses/patches the existing generator, not a one-off PDF.
Expense A6 maps to A1 and A7 to A2 only when the exact official label agrees.
Unknown mappings fail. Income aliases are preserved. Management totals remain unchanged.
Capital, financing, taxes, summary and two-year figurative sections are represented;
unverified values remain explicitly unknown. Internal snapshot/debug labels are removed.

Private artifacts: `/home/bandi/.local/share/bottazzi/runts/2603942/review-20260906/`:
`reconciliation.json`, `modello_d_review.html`, `modello_d_review.pdf`, `proposal.json`,
and the existing Memory Service schema in `memory.sqlite` (not a new event store).
The report includes source snapshot, movement-input and report-code hashes.
The proposal binds practice, authoritative message, document SHA256, sources,
preconditions and postconditions. Actual Bottazzi runtime capability retrieval selected
`runts_prepare_document_review`; semantic MCP returned a persisted nonexecuting proposal.

## Gate and remaining boundary

PDF is parseable A4, with no JavaScript or custom metadata; visually inspected.
It is marked **BOZZA NON DEPOSITABILE**. Arithmetic reconciliation is PASS;
accounting classification, negative cash resolution and final filing validation
are **NOT PASS**. Unknown values were not replaced with zeros.

Required before a final document: verify the excluded movements against source
statements/supporting documents, resolve the unmapped debit and personal-advance
treatment, independently reconcile physical cash and bank, and validate tax/capital
classifications. Proposal status is `BLOCKED_REVIEW`; WRITE is 0.

No service restart/deployment, database mutation, upload, message send, submission,
PEC send or Android change. Production Telegram was not promoted; its installed
release remains distinct from this development branch. This checkpoint does not
claim full end-to-end Definition of Done.

Regression checkpoint: 85 PASS including 6 preserved RUNTS adapter work-in-progress
tests; 79 tests apply to the committed generator/adjacent selection. Identical
common-file selection: baseline ac52bf4 55 PASS, current 56 PASS, zero failures.
This is a targeted relative gate, not a new global-suite run. Historical global
failures/SIGSEGV were not reclassified. New regression list for this selection: empty.
`git diff --check` passed. Staged secret/PII pattern scan found zero private keys,
bearer tokens, GitHub tokens, emails, Italian tax codes or IBANs. No real PDF,
database or private evidence artifact was staged; artifact directory mode is 700,
proposal file mode 600. This is a bounded pattern scan, not a universal DLP guarantee.
