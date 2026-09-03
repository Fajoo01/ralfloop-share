# M10 Web Research Service/MCP

Date: 2026-09-03. Production unchanged.

Existing secure bounded web search/open implementation remains intact and is reused through adapters. New service provides six semantic capabilities: search, authoritative-source discovery, assigned-source open, comparison, evidence extraction and claim verification.

Properties:

- source IDs assigned before open; no arbitrary URL accepted by MCP open;
- SSRF-safe, redirect-validated, size-bounded reader reused;
- every result carries URL, title, observation time, optional publication time/hash and confidence;
- opened evidence persists through shared Memory Service documents;
- deterministic lexical evidence/negation checks; conflicting or absent evidence stays explicit;
- not used as Bandi base crawler or model memory;
- no write/send/generic browser tool.

Eval covers authoritative source, secondary-only filtering, conflicting sources and no evidence. Stale-evidence policy remains a later Event Router freshness concern.
