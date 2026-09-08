# RUNTS upload PDF contract

Status: implemented/tested in worktree; **not deployed**. No portal operations,
approval changes, pending creation or retry are part of this change.

## Pipeline

`scripts/runts_modd_prepare.py` generates `modello_d_source.pdf`, then calls
`ralfloop_agent.unified_assistant.runts_upload_pdf.prepare_runts_upload_pdf` to
produce a new, exclusive `modello_d_review.pdf`. It never replaces an existing
final PDF. Hashing, golden Model D semantic checks, SourceRef, proposal and scope
all use the final file. Temporary conversion output is removed on failure.

All other PDF producers must call this same finalizer before constructing their
document reference. An unconverted/invalid artifact cannot pass the shared gates:

1. `prepare_document_review`: verifies before READY/persisting proposal.
2. `build_runts_pending_payload`: verifies before approval staging.
3. `RuntsAuthenticatedCdpWriteTransport._validate_scope`: verifies before CDP.

These three gates call **read-only** `verify_runts_upload_pdf`. They never convert,
repair permissions, or change already approved bytes. Old approvals are untouched;
an old invalid artifact is rejected, not silently converted under its approval.

## PDF/A implementation and evidence

Installed baseline: Ghostscript, Poppler, Java, ACL tools, Ghostscript sRGB ICC.
Production target is official veraPDF **1.30.2**, installed offline at
`/opt/ralfloop/verapdf/1.30.2/verapdf` by
`deploy/install_runts_verapdf.sh`. Expected executable SHA-256:
`ea4d7949a4c9e5939e3419d03f9610920f1d7b761bc5599a38facbddea28ae09`.
The reviewed installer archive used for this version was SHA-256
`6cc6341cb1af644044054b81f00a6590a7918abb18f762243de115258bcad838`.
The runtime executes `--version` and requires exactly `veraPDF 1.30.2`; it has
no download path or weak fallback.

Ghostscript uses PDF/A mode, sRGB OutputIntent, embedded fonts, SAFER and
`PDFACompatibilityPolicy=2` (abort incompatible output). An independent veraPDF
CLI validation must return exactly one compliant XML validation report. Missing
validator, invalid XML, failed process or noncompliance fails PREPARE closed.
No XMP-only/pdfinfo-only validation exists.

Target **PDF/A-2b**, configurable to 1b with `BOTTAZZI_RUNTS_PDFA_PART=1`.
Reason for deviating from preferred 1b: on the supplied real two-page source,
Ghostscript 1b conversion lost all extracted text, consistent with transparency
flattening/rasterization. The semantic gate rejected it. 2b passed veraPDF and
retained identical normalized text and page count. Synthetic 1b conversion was
also tested before selecting 2b for this source.

Real-source local conversion (no upload):

- Original raw SHA checked unchanged against supplied SHA.
- 2 pages before/after; normalized text SHA identical.
- Render comparison at 1200px: mean channel delta 0.01647 / 0.00329 on 0–255.
- Final raw SHA varies with metadata: never use a previous output's SHA.
- Final browser-user read/hash succeeded; temporary result removed, not staged.

The existing Model D golden/accounting verification remains after conversion.
General integrity checks compare page count and normalized full extracted text;
they are not a universal proof of visual equivalence (especially image-only PDFs).
Signed documents need a separate signature-preserving workflow: converting a signed
file is not a signature-preservation guarantee.

Primary references:

- [veraPDF CLI validation](https://docs.verapdf.org/cli/validation/)
- [Ghostscript PDF/A conversion](https://ghostscript.com/blog/zugferd.html)
- [Official RUNTS manual](https://servizi.lavoro.gov.it/Runts/Manuali/ManualeRUNTS.pdf)
  requires PDF/A without specifying a subpart in its attachment instructions.
  Actual portal acceptance of this output is **not tested** in this task.

## Access boundary

Final file is created exclusively with mode 0600, then named ACL `bandi:r--`.
Owned private ancestors receive only `bandi:--x`: traversal, not directory listing.
Already public-traversable or browser-owned directories are not changed.
Unowned ancestors are not changed. Existing extended ACL masks are not expanded:
that could reactivate unrelated masked permissions. Such ACLs must already allow
access or the actual probe rejects the file. No chmod777/chown production.

`verify_browser_readable` runs an actual complete file hash as UID `bandi`,
verifying both path traversal and readable bytes. Same UID works directly; root
drops UID/GID/supplementary groups in the subprocess. Other UIDs fail explicitly:
`runts_browser_identity_probe_unavailable`.

`ralf-runts-browser-probe.socket` / `.service` are the narrow browser-user
boundary. The socket is `0660 bandi:sibilla-cumana`; the service runs as `bandi`.
It receives exactly JSON `{path,expected_sha256}` and permits only regular files
below the canonical RUNTS root, rejects symlinks/traversal/oversize input, opens and
hashes as `bandi`, and returns only `{readable,sha256,size,error_code}`. It has no
shell, HTTP, mutating operation, arbitrary command or privilege escalation.
Backend probe failure remains fail-closed.

Actual two-user regression uses only temporary synthetic data: sibilla-owned
0700 directory / 0660 file fails initial bandi read; sibilla applies minimal ACL;
an independent privileged probe drops to bandi and verifies the bytes. The
unprivileged preparer itself remains fail-closed without a probe facility.

## Errors/configuration

- `runts_pdfa_conversion_failed`
- `runts_pdfa_validation_failed` (including missing validator)
- `runts_pdfa_icc_unavailable`, `runts_pdfa_profile_invalid`
- `runts_pdf_integrity_failed`, `runts_pdf_semantic_mismatch`
- `runts_browser_path_untraversable`, `runts_browser_file_unreadable`
- `runts_browser_identity_probe_unavailable`
- `runts_pdf_hash_mismatch`, `runts_pdf_final_already_exists`

Trusted configuration: `BOTTAZZI_RUNTS_VERAPDF` executable, optional
`BOTTAZZI_RUNTS_PDFA_ICC`, `BOTTAZZI_RUNTS_PDFA_PART` (1 or 2 only, default 2).
No command strings are interpolated into a shell.

## Controlled next E2E (not executed)

Verification: full relevant regression suite (RUNTS, unified runtime/Telegram,
DomainApproval, Mailchimp) plus isolated veraPDF conversion tests; root-only
two-UID ACL regression executed separately. `py_compile`, `git diff --check` and
heuristic secret scan of changed/new task files passed. No PDF, DB, session or
private binding was added to git; existing untracked `plugins/` was not touched.
The source RUNTS DB retains the user-supplied SHA. Last read of the live process
showed WRITE=1 and approval gate=1; this task did not deploy/restart or change flags.
Do not mistake zero writes by these tests for a disabled live write flag.

First provision/review the validator and least-privilege browser probe integration.
Keep RUNTS WRITE disabled and verify the live PID environment. On a private copy
of the read-only test binding, run the existing generator/golden integration test
with a temporary output directory and fake RUNTS providers:

```sh
BOTTAZZI_RUNTS_WRITE_ENABLED=0 \
BOTTAZZI_RUNTS_VERAPDF=/approved/path/verapdf \
BOTTAZZI_TEST_RUNTS_BINDING=/private/read-only-test-binding.json \
.venv/bin/pytest -q tests/test_runts_response_workflow.py \
  -k telegram_shadow_end_to_end_real_generator
```

Do not use this command until the cross-UID boundary is implemented. It must not
consume prior approvals or invoke a portal writer. A later real PREPARE must get a
new approval bound to the new PDF/A hash; no retry of the uncertain execution here.
