# HANDOFF — Bot-tazzi Shopping Intelligence

Repository: `Fajoo01/ralfloop-bottazzi`
Branch: `feat/shopping-intelligence-20260920`
Architecture commit: `142b739eaf482322e1b95dc259a11ffbf9a4caba`

Read first, in full:

`docs/SHOPPING-INTELLIGENCE-2026-09-20.md`

## User intent

Build a self-hostable shopping service that acts like an independent purchasing search engine:
- deep search across multiple e-commerce sources;
- include new, refurbished and used markets such as eBay/Vinted-like sources;
- compare real total price;
- keep historical prices like Keepa;
- warn when a searched/watchlisted item is genuinely at a good price;
- later extend to grocery shopping and optimize the cost of an entire basket.

## Important design decisions already made

1. Do not start by cloning Amazon checkout. Build search + normalization + history + alerts first.
2. Use isolated connectors so one marketplace change cannot break the service.
3. Product identity/entity resolution is a core subsystem, not an afterthought.
4. Prefer GTIN/EAN/MPN/model matching before semantic/fuzzy matching.
5. Persist observations over time; never keep only the current price.
6. Compare total acquisition cost, not only sticker price.
7. A `good deal` must be explainable through history/percentiles/condition/fees, not MSRP marketing.
8. Grocery mode should optimize the whole basket and unit prices, including delivery and multi-shop penalties.
9. Avoid hardcoding and avoid depending on undocumented APIs for the core.

## Immediate implementation target for the next chat

Use GitHub as technical diary. Work only on this branch unless a better dedicated worktree is created.

Implement the first vertical slice:

1. Inspect repository layout before changing anything.
2. Define canonical `Product`, `Offer`, `PriceObservation` and connector interface.
3. Add a minimal local service/API without disturbing existing Bot-tazzi runtime.
4. Implement one official/stable marketplace connector first (eBay is a good candidate if credentials/config permit).
5. Implement one generic structured-data shop parser (JSON-LD first).
6. Persist observations locally with a migration/schema suitable for moving to PostgreSQL if the repo currently prefers another lightweight test DB.
7. Add tests for normalization and entity resolution.
8. Expose `POST /search` returning normalized multi-source results.
9. Record decisions and test results in GitHub before handoff.

Do not begin with Vinted scraping, browser automation, checkout or grocery optimization. Those come after the first end-to-end search/history path works.

## Definition of done for first slice

A query can return normalized offers from at least two heterogeneous connector implementations; observations are persisted; repeated observations can form price history; tests cover product identity and price normalization; no existing Bot-tazzi service is broken.

## 2026-09-20 implementation update

The first vertical slice has now been implemented and validated.
Read next:

`docs/CHECKPOINT-SHOPPING-INTELLIGENCE-VERTICAL-SLICE-2026-09-20.md`

Current executable path:
- package: `ralfloop_agent/shopping_intelligence/`
- API: `ralfloop_agent.shopping_intelligence.api:app`
- tests: `tests/test_shopping_intelligence.py`

Do not replace the connector abstraction with site-specific core logic.
The next concrete external dependency is eBay Developer credentials for a live Browse API canary; absence of credentials must continue to degrade only that connector.
