# Operational eval, observability and safety gate

Date: 2026-09-03. Production unchanged.

## Evidence

- M7–Media cross-domain relative regression after observability completion: 176 passed; zero new failures.
- Post-pagination/`ffprobe` hardening selection: 180 passed; zero new failures.
- Media deterministic eval: 7/7 scenarios pass (English-only audio, mislabeled Italian, low resolution, client-only complaint, missing subtitles, duplicate, provider failure).
- Capability retrieval eval remains recall@3/task success 100%, hallucinated tools 0. Media query isolation selects ticket/scan/stream capabilities only.
- Required counters now cover Event Router, Bandi polling/change/filter/escalation, Web Research query/source/failure, Nightly job/model/escalation/failure and Media ticket/detection/diagnosis/escalation/resolution.

## Safety assertions

- ARCI writes 0; external Jellyfin writes 0; Media execution/delete/acquisition 0.
- Media proposals are approval-required and non-executable.
- Source outages fail closed; incomplete library enumeration returns `INCOMPLETE_SOURCE`.
- Recursive Nightly secret rejection; Media report email/phone minimization.
- No generic REST/shell tool; no production deployment.
