# MD / Goodify — read-only protocol MCP checkpoint

Date: 2026-09-20
Branch: `feat/md-goodify-mcp-20260920`

## Goal completed

The reverse-engineered MD Goodify protocol is now represented in Bot-tazzi
without implementing any live `purchasedonation` request.

Added a pure protocol module:

- `ralfloop_agent/unified_assistant/md_goodify_api.py`

It performs no network I/O. It can:

- parse the APK response shape `payload[0].Donation[]`;
- normalize only the 15 verified Gson fields;
- build the local `getdonation` body `{token}`;
- build the local `purchasedonation` body `{token, qr_code}`;
- produce a redacted MCP preview without receiving the access token.

## MCP surface

Added two semantic tools:

- `md_goodify_parse_api_response`
- `md_goodify_build_purchase_payload`

`md_goodify_build_purchase_payload` accepts only `qr_code`; it returns a
redacted template with `token_source=runtime_access_token`. Raw access tokens
are therefore not passed through the MCP argument/result path.

## Safety boundary

There is no HTTP client or submit method in the protocol module. The
`purchasedonation` URL is documented as protocol metadata only.

No donation, purchase or redemption was performed during development.
No phone interaction or traffic capture was required.

## Validation

Targeted tests: `14 passed`.
The production MCP runtime was not restarted or changed in this checkpoint.
