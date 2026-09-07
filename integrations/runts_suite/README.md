# RUNTS Suite development overlay — NOT deployed

Copied from the active RUNTS Suite generator and template, then patched locally.
Targets, only after a separate promotion/review decision:

- `app/services/runts_modd_builder.py`
- `templates/runts_mod_d_pdf.html`

This is not an independent accounting service. It reuses the existing report function
against the existing database with SQLite `mode=ro`. The running service and its
database, mappings, movements and templates are unchanged.

General fixes: exact-label legacy A6/A7 expense normalization, invalid-side/code
rejection, Decimal arithmetic, corrected B1 income label, capital/financing rows,
tax and overall-result sections, two-year figurative rows, no internal snapshot
metadata, no invented cash balances. Cash/bank must be supplied independently.

The updated template requires explicit capital/tax/figurative verification context.
It is **not a drop-in production replacement** for the old `_build_context`.
The development runner supplies this context; unknown figures display
`Da verificare`, not invented zeros. A final context adapter and verified accounting
classifications are still required before promotion.

Run `scripts/runts_modd_prepare.py` using the development interpreter with the
existing Suite dependency directory on `PYTHONPATH`. This avoids installing anything
into the production environment. The runner imports the original Suite reporting
function, reads the prior-year approved snapshot directly (avoiding the legacy
GET helper that calls `_ensure_table`), renders private review artifacts, and
invokes `runts_prepare_document_review` via Bottazzi runtime/retrieval/MCP/Memory.

The runner now requires explicit `--approved-income`, `--approved-expense`,
`--approved-surplus` and `--approved-closing`; changes fail before rendering.
Existing manual/split decisions are projected independently of payment kind and
financial account. Exact duplicate financial sources are represented once without
inventing account ownership; repayment pairing requires source identity, not merely
matching amounts. See `docs/runts-financial-projection.md`.

Outputs must be outside this repository. The generated PDF is a **review draft**,
not an approved ministerial filing. The semantic proposal remains `BLOCKED_REVIEW`
and `executable=false`. There is no submission executor in this integration.
