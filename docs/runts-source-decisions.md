# RUNTS source hierarchy and versioned decisions

Operational scope: READ/PREPARE only. No source database mutation or deployment.

`DecisionFact` separates fact type, subject, practice, exercise, source reference,
source hash, observation time, authority level, version, conflicts and revocation.
Facts persist as immutable version entities plus a current-version head and
deduplicated Memory events. Reusing a superseded decision fails closed.

Authority levels: official source; approved document; primary financial document;
explicit human decision; existing recorded decision; raw DB; legacy note;
deterministic derivation; LLM. Ranking is fact-type-specific, not a confidence
score. Different legal/economic ownership facts coexist. Incompatible approved
totals fail with `APPROVED_TOTAL_CONFLICT`; lower-ranked disagreement is retained
as `SOURCE_CONFLICT`, never silently deleted. LLM-only facts cannot be promoted.

Account roles distinguish direct association assets, association assets held by
members, personal support accounts, private personal accounts and unknown owners.
Personal support/private account balances are not association assets. Specific
association expenses advanced through them retain their existing classification.

Practice-specific decisions are private structured data, not IDs embedded in the
general generator. For exercise 2025 the user explicitly resolved TARI to A5,
withholding to E5 rather than own-entity taxes, savings economic ownership and
inclusion within the approved deposits, entity identity and presentation zeros.
These supersede the earlier documentary blockers; they do not prove missing
technical import ownership or settlement pairing.

Source records are not SPID authentication or execution authorization. A human
classification decision never authorizes a RUNTS submission or an email.

Tests cover source ranking, explicit conflicts, ownership distinctions, immutable
versions, revocation/restart, idempotent events and personal-balance exclusion.
Production DB SHA256 remains
`efa74ecb18fa0f5390477dd19a11fc9f75a16b673a0ba18f01bce63e0e48fa25`.
