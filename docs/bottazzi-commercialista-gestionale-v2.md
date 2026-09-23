# Bot-tazzi Commercialista / Tiremm Gestionale — revisione v2

Status: development branch, READ + human-review proposals. No production mutation, filing, payment or fabricated supporting document.

## What was already present

This revision does not replace the earlier management prototype. It reuses:

- `TiremmAdminV2` as the source-bound administrative/practice layer;
- `runts_accounting.py` for existing accounting decisions and RUNTS projections;
- `runts_financial.py` for duplicate-source detection, statement reconciliation and reimbursement matching;
- `runts_document_prepare.py` for `BLOCKED_REVIEW` / `READY_FOR_HUMAN_APPROVAL` document proposals;
- the new `accounting.py` adapter as the unified Bot-tazzi Commercialista entry point.

Banco Beppe remains separate: it is an internal FIAT ledger and must not become the accounting source of truth.

## Revised model

The previous draft mixed, conceptually, three different questions. They are now explicit and independent:

1. **Economic movement** — did money actually enter/leave, and does the account reconcile?
2. **Documentary strength** — is there an original invoice/receipt, only bank evidence, or corroborating context?
3. **Human decision** — what should the association do when the original supporting document is missing?

A movement may therefore be arithmetically real and reconciled while the original receipt remains missing.

## Missing supporting documents

`accounting_review.py` implements the review queue.

Evidence states:

- `DOCUMENTED`: original supporting document exists;
- `CORROBORATED_MISSING_ORIGINAL`: payment evidence plus independent contextual evidence, original still missing;
- `PAYMENT_ONLY_MISSING_ORIGINAL`: payment evidence only;
- `UNSUPPORTED_MISSING_ORIGINAL`: no primary payment evidence.

Human decisions for a missing original:

- `approve_reconstruction`: keep/post the source-backed accounting reconstruction;
- `approve_nonreportable`: keep the economic movement but explicitly exclude it from external reporting use;
- `confirm_internal_transfer`: confirm a source-backed transfer pair and keep it outside expense reporting;
- `reject`: do not post the proposed reconstruction.

`approve_reconstruction` **never** changes `original_document_status=MISSING` and never creates a receipt/invoice. It also does not imply VAT deductibility, tax deductibility, grant eligibility or RUNTS documentary sufficiency. Those require a separate rule check against the applicable regime/source.

A missing-document case without primary payment evidence cannot be approved as a bookkeeping reconstruction by this engine; it stays `BLOCKED_NO_PRIMARY_EVIDENCE`.

## Human gate

Pending cases become ordinary Tiremm Admin `ActionProposal(action="propose_update")` objects. They carry exact evidence IDs and `requires_approval=True`. The proposal exposes four bounded decisions: approve reconstruction, approve as non-reportable, confirm a source-backed internal transfer, or reject.

This means Bot-tazzi may search for a plausible explanation and propose it, but the human remains the decision-maker and the evidence trail is retained.

## Reconciliation strategy

The system should try, in order:

1. exact original invoice/receipt;
2. bank/card/PayPal source movement;
3. matching email/order/booking/contract/vendor evidence;
4. transfer/reimbursement identity and account direction;
5. duplicate-source neutralisation;
6. prior approved accounting decision;
7. human review for the unresolved remainder.

No synthetic balancing movement is allowed merely to force a total. Arithmetic reconciliation and documentary/reporting eligibility are separate gates.

## Safety / execution boundary

Current Commercialista remains:

- `writes=0`;
- `sends=0`;
- `payments=0`;
- `filings=0`.

Future F24, filings, RUNTS submission, invoice issuance or bank actions must be separate approval-bound capabilities with post-action verification.

## Live RUNTS Suite source

The Commercialista can now consume the existing RUNTS Suite SQLite database through accounting_runts_live.py. The connection uses SQLite URI mode=ro plus PRAGMA query_only=ON; production rows, reviews and document links are never mutated. The path is injected with accounting.runts_db_path or BOTTAZZI_RUNTS_DB_PATH, not embedded in the Python implementation.

The live audit selects outgoing association movements after basic duplicate/transfer/exclusion gates, preserves movement/import provenance, detects formally linked project documents, and searches exact-amount/date-bounded document candidates without promoting them to originals. Unlinked candidates remain suggestions for human review.

Real canary on 2026-09-23: year 2025 produced 482 outgoing review candidates for EUR 23047.36, with zero formally linked originals in the current database; year 2026 produced two candidates for EUR 132.90, one formally documented and one requiring review. These are documentary-review candidates, not a claim that every amount is a final deductible/RUNTS management expense.

Private review snapshots are written outside Git under /home/bandi/.local/share/bottazzi/accounting/ and are not production accounting state.

## External evidence enrichment

The documentary audit can consume a cached external-evidence snapshot through `accounting.evidence_snapshot_path` or `BOTTAZZI_ACCOUNTING_EVIDENCE_SNAPSHOT`. Gmail is never rescanned on every accounting query: `accounting_external_evidence.py` partitions PayPal receipt searches by month, uses only Gmail READ operations, hydrates matching messages and stores a private snapshot outside Git.

PayPal receipt parsing extracts amount, merchant, message date, transaction id and payment-card suffix. These records are `payment_evidence`, explicitly `fiscal_document=false`. Matching requires exact amount, a bounded date distance, merchant similarity and a unique candidate. The same external provenance reference cannot support two movements; conflicts fail closed.

Existing `amazon_orders` are also reused read-only. An Amazon order is considered strong contextual evidence only for Amazon-labelled movements with exact amount and a bounded date distance. It never becomes an invoice or receipt automatically.

Bank-native evidence is also parsed deterministically from the existing statement row: F24, CBILL/document references, SDD/direct debits, CRO-bearing transfers with a meaningful causal, merchant/card rows and bank fees. These references can make a case ready for human bookkeeping confirmation but remain `fiscal_document=false`.

Exact replayed imports with the same source hash are represented once in the documentary review queue. This does not delete production movements and does not decide which account owns the source; conflicting account ownership remains explicit. PayPal `ADD TO BAL` debits are paired with exact opposite PayPal CSV credits when possible and proposed as `confirm_internal_transfer`, never excluded automatically.

Real 2025 canary on 2026-09-23: 121 PayPal receipt emails were collected from the authenticated Tiremm Gmail mailbox. The raw documentary candidate set is 482 rows / EUR 23047.36; 18 replayed source rows are neutralised in the review projection, leaving 464 unique-source cases / EUR 20836.36. The audit finds 107 unique PayPal email matches, 10 Amazon order matches, 79 PayPal balance-transfer pairs and 64 bank-native references. A hash-bound non-executing batch contains 250 cases / EUR 17128.58 ready for human decision: 171 suggested `approve_reconstruction` and 79 suggested `confirm_internal_transfer`. The remaining evidence-unresolved queue is 214 cases. `writes=0`; originals remain missing unless a real original is linked, and no tax/VAT/grant/RUNTS eligibility is inferred.

## Production review UI activation

The production review API and Bot-tazzi UI use a dedicated systemd `EnvironmentFile` (`/etc/ralfloop/accounting-review.env`) rather than embedding host-specific accounting paths in the service drop-in. The committed deployment template is `deploy/systemd/ralfloop-backend-accounting-review.conf`; `deploy/systemd/accounting-review.env.example` documents replaceable variables without committing private source paths or credentials.

Human review decisions are stored separately from RUNTS Suite in `BOTTAZZI_ACCOUNTING_REVIEW_DB`. Every write is bound to the current batch SHA-256 and item SHA-256; stale evidence fails closed. The review endpoint never mutates the RUNTS database, and a decision in this local review store is not itself a tax filing, payment, or RUNTS submission.

Production smoke on 2026-09-23 after activation: 250 ready cases, 214 evidence-unresolved cases and 18 replayed-source rows neutralised; zero RUNTS writes and zero human decisions pre-applied. The UI therefore starts with the full human decision backlog rather than silently accepting suggestions.

## Raw-source reconciliation hardening

The legacy importer had flattened important source semantics. The Commercialista now reads `movements_raw` in read-only mode and binds the preserved raw row back to the corresponding movement without rewriting the ledger.

For PayPal, `Blocco conto per autorizzazione aperta` rows whose raw state is `In sospeso` are treated as temporary authorization holds rather than documentary expenses. They are suppressed only from the review projection and remain untouched in RUNTS Suite. Settled raw PayPal transactions are accepted as strong payment evidence (`fiscal_document=false`) when type, completed state, balance impact and exact amount agree.

For `PAYPAL *ADD TO BAL`, the bank description contains the actual PayPal transaction date. Exact-date/amount transfer groups are accepted only when debit and credit group cardinalities balance. A 2↔2 or 3↔3 group is not converted into invented one-to-one pairs: the evidence explicitly records `pairing_identity_unresolved=true` and still requires human confirmation of the internal-transfer interpretation.

Completed Revolut raw rows (`Tipo=Pagamento`, `State=COMPLETATO`) are also strong payment evidence, but remain non-fiscal evidence. This makes payments such as `To Arci Milano` ready for human bookkeeping confirmation without inventing their accounting or tax purpose.

Real 2025 canary after this hardening: 126 pending PayPal authorization holds / EUR 394.32 are removed from the documentary-expense projection; 338 economic review cases / EUR 20442.04 remain. The evidence layer finds 118 settled PayPal raw matches, 79 direct PayPal balance-transfer pairs, 39 additional exact balanced transfer-group rows, 2 completed Revolut payments and 77 bank-native references. The hash-bound human batch contains 315 cases / EUR 17946.04 ready for a decision, leaving 23 unresolved cases: 18 cash withdrawals whose destination is not proven and 5 PayPal top-ups without a sufficiently strong counterpart. No cash withdrawal is auto-classified from the old `Fitti Passivi AIG` category because those rows remain `da_rivedere` with no manual review decision.
