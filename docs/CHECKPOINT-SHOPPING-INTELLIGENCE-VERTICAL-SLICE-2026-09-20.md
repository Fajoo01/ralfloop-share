# CHECKPOINT — Shopping Intelligence vertical slice

Date: 2026-09-20
Branch: `feat/shopping-intelligence-20260920`
Worktree: `/home/bandi/ralfloop-shopping-intelligence-20260920`
Issue: #25

## Implemented

A first executable vertical slice now exists under:

`ralfloop_agent/shopping_intelligence/`

It is isolated from the existing Bot-tazzi runtime and does not modify existing API routes.

Implemented components:
- canonical `Product`, `Offer`, `PriceObservation` models;
- connector interface with per-source failure isolation;
- eBay Browse API connector using OAuth client credentials;
- generic structured-data connector: JSON-LD/Schema.org first, OpenGraph fallback;
- SQLite persistence for products, current offers and append-only price observations;
- deterministic entity resolution: GTIN -> MPN -> model -> conservative fuzzy title fallback;
- `POST /search`;
- `GET /products/{product_id}/history`;
- `/health` connector status.

## Cost semantics

`total_cost` contains the known acquisition-cost components.
If shipping is not published by the source, `shipping_cost` is `null` and
`unknown_cost_components` contains `shipping`; the service does not silently
pretend unknown shipping is free.

## Configuration

eBay production defaults:
- `EBAY_CLIENT_ID`
- `EBAY_CLIENT_SECRET`
- `EBAY_MARKETPLACE_ID` (default `EBAY_IT`)
- `EBAY_ENVIRONMENT` (`production` or `sandbox`)

Storage:
- `SHOPPING_DB_PATH` (default `var/shopping-intelligence.sqlite`)

Generic URL fetches reject private/link-local destinations by default to reduce SSRF risk.
For deliberate local integration tests only, `SHOPPING_ALLOW_PRIVATE_URLS=1` disables that guard.

## Run

```bash
python -m uvicorn ralfloop_agent.shopping_intelligence.api:app --host 127.0.0.1 --port 19225
```

## Validation performed on Sibilla

Targeted automated tests:

```text
5 passed, 2 warnings
```

Covered:
- eBay response normalization with item price + shipping;
- JSON-LD Product/Offer extraction;
- GTIN-based multi-source resolution into one canonical product;
- repeated searches producing append-only price history;
- `POST /search` and history API;
- connector failure isolation;
- OpenGraph fallback with unknown-shipping disclosure.

Live smoke against a public Shopify product page returned one normalized offer:
`Sample Product`, `9.99 USD`, with `shipping` explicitly marked unknown.
The real FastAPI service was started on `127.0.0.1:19225`, queried twice, and history grew to two observations.

eBay live external search was not run because no eBay client credentials are configured in this worktree/environment. The connector contract itself is covered by the deterministic fake eBay HTTP test.

## Design references

Implementation choices were checked against:
- eBay Browse API documentation: item summary search + application OAuth token;
- Schema.org `Product`, `Offer`, `price` and shipping structured data;
- open-source price trackers that use structured metadata first, SQLite history and isolated source/plugin strategies.

No site-specific CSS selector, Playwright dependency, Vinted scraping or checkout automation was added.

## Next iteration

1. Configure eBay Developer credentials and run a real Italy-market Browse API canary.
2. Add source health/latency telemetry and request budgets.
3. Add 30/90/365-day history metrics and explainable good-price rules.
4. Add watchlists and deduplicated alerts.
5. Only then add more used-market connectors and grocery/EAN flows.
