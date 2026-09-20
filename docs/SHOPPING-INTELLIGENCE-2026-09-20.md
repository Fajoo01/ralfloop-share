# Bot-tazzi Shopping Intelligence

Date: 2026-09-20
Branch: `feat/shopping-intelligence-20260920`

## Goal

Build an independent shopping-intelligence service: a single search that compares new, refurbished and used offers across multiple e-commerce sources, keeps price history, detects genuinely good prices and sends alerts. Later extend the same engine to grocery shopping and whole-basket optimization.

This is not intended to start as a full Amazon clone. The MVP is a search/price/alert engine that can later grow into an assisted purchasing layer.

## Core user flows

1. Search for a product by free text, GTIN/EAN, model, URL or eventually image.
2. Resolve different listings to the same underlying product.
3. Search multiple sources for new/refurbished/used offers.
4. Normalize total cost: item price + shipping + fees - coupons/cashback when known.
5. Persist price observations over time.
6. Show history and context instead of fake list-price discounts.
7. Alert on a user threshold or when the current price is statistically attractive versus recent history.
8. For groceries, compare unit price and optimize the whole shopping basket, including delivery/travel costs and substitutions.

## Initial source strategy

Prefer official/stable APIs where possible, with connectors isolated behind a common interface.

Candidate sources:
- Amazon historical/context data through Keepa API where licensed/available.
- eBay official Browse API for new and used listings.
- Generic web shops via structured data first: JSON-LD, Microdata, OpenGraph, then site-specific selectors only when necessary.
- Vinted/Subito/other used marketplaces as separate connectors; do not make the core dependent on undocumented APIs.
- Open Food Facts / Open Prices for grocery identity and open product/price data.

Each source connector must fail independently without breaking the rest of the search.

## Product identity / entity resolution

This is the hardest part and should be treated as a first-class subsystem.

Preferred keys, in order of confidence:
- GTIN/EAN/UPC
- MPN / manufacturer part number
- exact model + variant
- normalized attributes such as capacity, color, size, bundle contents
- fuzzy/semantic matching only when structured identifiers are missing

Never merge offers when a materially different variant is possible. Used listings must retain condition, missing accessories, warranty and seller metadata.

## Normalized offer model

Minimum fields:
- source
- source_listing_id
- canonical_product_id
- title
- url
- condition
- seller
- seller_reputation if available
- item_price
- shipping_cost
- marketplace_fees
- known_discount
- total_cost
- currency
- stock/availability
- observed_at
- raw_source_payload reference/hash

## Price history

Persist observations, not only latest values.

Useful derived metrics:
- current total cost
- 30/90/365 day median
- recent minimum
- all-time observed minimum
- percentile of current price within selected history window
- volatility
- source-specific versus market-wide history

A useful presentation is factual, e.g.:

`Today 249 EUR; 90-day median 315 EUR; 12-month minimum 239 EUR; current price is near the bottom of the observed range.`

## Good-price detection

Do not base recommendations on MSRP/list-price discount alone.

Initial detector should use:
- current total cost
- rolling median/history
- current percentile
- shipping and fees
- condition
- warranty/returns where available
- seller reliability signal

Expose the evidence used to classify an offer rather than hiding it behind an opaque score.

## Alerts

Two initial modes:
- explicit threshold: `alert me below 260 EUR`
- history-aware: alert only when the offer enters a configurable low percentile / satisfies a rule

Alerts should be deduplicated and have cooldowns to avoid notification spam.

## Grocery mode

Grocery comparison is a basket-optimization problem, not only a per-item price lookup.

Normalize:
- package quantity
- measurement unit
- unit price (EUR/kg, EUR/l, EUR/item)
- exact product versus acceptable substitute
- loyalty/promotional constraints
- delivery fees/minimum order

Optimization objective can initially minimize total monetary cost, then allow penalties for extra shops, delivery time and substitutions.

Example output:
- Shop A basket: 72.40 EUR
- Shop B basket: 68.10 EUR
- Split A+B: products 61.80 EUR, extra delivery 6.90 EUR, effective total 68.70 EUR

The engine should therefore be able to conclude that the apparently cheapest split basket is not actually cheaper after acquisition costs.

## Proposed architecture

```text
User query / watchlist
        |
Search planner
        |
Product resolver ----------------------+
        |                              |
Connector fan-out                      |
  | Keepa/Amazon                       |
  | eBay                               |
  | generic shops                      |
  | used marketplaces                  |
  | grocery/open data                  |
        |                              |
Offer normalization -------------------+
        |
PostgreSQL / price observations
        |
Price intelligence + basket optimizer
        |
API / Bot-tazzi capability / UI / alerts
```

## Suggested stack

Keep it replaceable and avoid hardcoding sources into the core.

- API/service: Python FastAPI is acceptable for initial iteration; C/Rust components may be introduced later if profiling justifies it.
- Storage: PostgreSQL; TimescaleDB optional, not required for MVP.
- Jobs: simple worker/queue first; avoid premature orchestration.
- Browser fallback: Playwright only where structured HTTP retrieval is insufficient and legally/technically appropriate.
- Product matching: deterministic identifiers first, then normalized text/attributes, then embeddings as fallback.
- UI: lightweight web UI initially; Bot-tazzi can expose the same API as a conversational front end.

## MVP phases

### Phase 1
- canonical product model
- connector interface
- eBay connector
- generic-shop structured-data connector
- Keepa connector abstraction/configuration
- PostgreSQL observations
- basic search API

### Phase 2
- historical charts/data endpoints
- threshold alerts
- history-aware deal detection
- source health / connector failure isolation

### Phase 3
- used-marketplace connectors
- condition/warranty/seller normalization
- stronger entity resolution

### Phase 4
- grocery/EAN flow
- Open Food Facts/Open Prices connector
- unit-price normalization
- basket optimizer

### Phase 5
- assisted purchase workflows only after search, identity, history and alerts are reliable

## Non-goals for the first implementation

- Do not build a checkout/payment platform.
- Do not scrape dozens of sites before the canonical model is correct.
- Do not make Vinted or any undocumented interface a hard dependency.
- Do not invent a proprietary deal score without exposing the underlying facts.
- Do not hardcode site behavior into the product core.

## First technical milestone

Deliver a local API capable of:

`POST /search`

with a product query and returning normalized offers from at least two heterogeneous sources, plus persisted observations.

Then add:

`GET /products/{id}/history`

and:

`POST /watchlists`

The milestone is complete only when repeated searches create a usable historical series and the same product from different sources resolves to one canonical identity with confidence/evidence recorded.
