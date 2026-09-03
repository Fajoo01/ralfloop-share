# Media Quality Ticket System

Date: 2026-09-03. Shadow/local only. Production unchanged. External writes: 0.

## Workflow

User report or Jellyfin metadata → PII-minimized idempotent ticket → deterministic stream rules → diagnosis → non-executable fix proposal → approval/execution outside this milestone → fresh-evidence verification → close.

Tickets and proposals use shared Memory entities. Event Router receives ticket, quality/language detection, fix proposal and verified-fix events; `NightlyEventSink` queues hard media cases without inline model wake. Duplicate item/type reports return the existing ticket.

Deterministic rules inspect Jellyfin video/audio/subtitle metadata before any LLM: Italian track presence, subtitle presence/language, resolution and audio bitrate. Provider outage is `SOURCE_UNAVAILABLE`, never an ineligible/healthy result. Jellyfin library enumeration uses stable ordering, 100-row internal pages, duplicate/stall/total-change guards and a 1000-item safety bound. Library scans return complete only when unique received IDs equal the authoritative total; otherwise `INCOMPLETE_SOURCE`.

`FFprobeMediaReader` is the optional local deep-probe adapter: fixed argv, no shell, configured filesystem-root allowlist, 30-second default timeout, bounded JSON output and no media path in its result. It normalizes streams/duration and flags invalid streams, missing language tags, unusual duration and empty probes before model escalation.

## Semantic MCP

`media_ticket_create`, `media_ticket_get`, `media_ticket_list_open`, `media_ticket_add_evidence`, `media_ticket_diagnose`, `media_ticket_prepare_fix`, `media_ticket_verify_fix`, `media_ticket_close`, `media_scan_item`, `media_scan_library`, `media_get_streams`, `media_get_playback_context`.

All schemas reject extra fields. Fix proposals always have `requires_approval=true`, `executable=false`; MCP results report `writes=0`. No delete, acquisition, generic REST or generic shell tool exists. Catalog-dependent reads fail closed when the catalog is absent.

Metrics: `media_tickets_open`, `media_ticket_duplicates`, `media_auto_detected`, `media_probe_failures`, `media_diagnosed`, `media_fix_proposed`, `media_escalated`, `media_resolved`.

Targeted Media/Nightly/Event Router/Memory/Jellyfin/Capability gate: 32 passed.

Eval scenarios: only-English audio, Italian audio with wrong language metadata, low resolution, client-only playback complaint with healthy media, missing subtitles, duplicate ticket and probe failure. All seven deterministic expectations pass.
