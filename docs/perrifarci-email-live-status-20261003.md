# PerRifArci — stato live bozze email 2026-10-03

Questo file registra lo stato operativo verificato. **Non autorizza alcun invio.**

## Stato definitivo

- Account Gmail: `fabio@tiremminnanz.com`
- Oggetto comune: `PerRifArci – richiesta lettera di sostegno`
- Modalità: **manual send only**
- Stato Gmail: **6 bozze DRAFT**
- Email PerRifArci in Sent negli ultimi 30 giorni: **0**
- Approval attive: **0**
- Execution: **0**
- Il vecchio helper PerRifArci per creare approval è stato messo in fail-closed: termina con `manual-send-only` e non crea request.
- Manifest live: `/home/bandi/perrifarci-staging-20261002/email-send-manifest.json`
- SHA256 manifest: `af44b8ec342a583758697a1353b9c34253e5155da4585d32355d552e775f3abb`

Le sei bozze sono state rifatte **in-place**, riutilizzando gli stessi Gmail draft ID; quindi non sono stati creati duplicati.

| Destinatario | Gmail draft ID | Gmail message ID attuale | Raw MIME SHA256 | Vecchia approval |
|---|---|---|---|---|
| miic8dd005@istruzione.it | r-3075609958872328161 | 1a1007c915dc87bd | 14e110948080d913b7a99a0797b342d7ade10e1258eb9b1aa5a8cbfcbd9cc5a3 | apr_G5FMKKD8 — expired |
| ED.UE10@comune.milano.it | r127658816653256721 | 1a1007c9d152bcd4 | b19260cdc4cb1d4bdc633a8dc93a91e886b4ece1e7b2fd64b0e5ef9cbeea28bb | apr_ZS9N4KCN — expired |
| miic8gh008@istruzione.it | r2520456484722217206 | 1a1007caa016a12c | 31a04ce4464f7c6580fb341ea5b0d08da4e139530a6ead23bcd1e2b41f61e763 | apr_R7543TMY — cancelled |
| info@amedi.it | r2403744326878782768 | 1a1007cbfa6a4d3a | ada5ecd9497abf5e736ba6194d2b529f7bdc1e441c52fb66c1d0c00e99576eaa | apr_3EKQG4FD — expired |
| pigreco@luogoideale.org | r-8570369634459486686 | 1a1007ccc209fd6f | 627033e8709d23c6507d690962361f21a015b2d271a137de452e69a7655597d3 | apr_TDRJXEFT — cancelled |
| amicitrotter@gmail.com | r1973480878599356126 | 1a1007cda7911245 | 3788b4699901fcac6e2993a03d0177d3f8af88113dd6b90a9b21a7d660531a30 | apr_RZYY2E9E — cancelled |

## Verifica Gmail

Read-back eseguito su tutte le 6 bozze:

- label `DRAFT`: 6/6
- destinatario corretto: 6/6
- oggetto corretto: 6/6
- corpo canonico corretto: 6/6
- 2 allegati: 6/6
- `PerRifArci_Abstract.pdf` presente: 6/6
- ricerca `in:drafts subject:PerRifArci`: 6 risultati
- ricerca `in:sent subject:PerRifArci newer_than:30d`: 0 risultati

## Allegati

Abstract originale: SHA256 `7ae315080bae587fa4c468baf45dadce777406455355bb5c9b282334a30b5955`.

Le sei lettere e l'abstract erano già stati verificati sul filesystem contro hash e dimensioni del manifest: 7/7 PASS.

## Vincolo operativo

Per questo dossier PerRifArci **Bot-tazzi prepara solo le bozze Gmail**. L'invio si fa manualmente dall'account Gmail dell'utente.

Le vecchie approval non sono eseguibili:
- 3 expired
- 3 cancelled
- 0 execution

Non rigenerare approval Telegram per queste email salvo nuova istruzione esplicita dell'utente.
