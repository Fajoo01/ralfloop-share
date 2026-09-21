# MD / Goodify reconciliation — 2026-09-21

## Live finding

Two real QR scans reached MD at 01:46:10 and 01:46:34 and were recorded in `getdonation` history. No QR was retried.

The apparent `AMBIGUOUS_PURCHASE` was primarily a parser bug: MD returns `Goodify_UrldonationId` as `https://me.goodify.com/view/<redirectId>`, while our parser accepted only `/donation/<donationId>`.

The Goodify `/view/:id` Nuxt component uses the official GraphQL mutation `redeemRedirect(id)` and caches the returned donation ID before navigating to `/donation/<id>`.
