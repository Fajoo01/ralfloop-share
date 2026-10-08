# Via Privata Federico Jarach — bozza Gmail effettiva

Data: 2026-10-08

## GOAL
Preparare con Bot-tazzi una bozza Gmail per Sindaco Milano e Municipio 2 sul ristagno d'acqua dovuto alla caditoia mancante e sulla pulizia inadeguata, senza alcun invio.

## Stato verificato
- Bot-tazzi produzione: `ralfloop-backend.service` riavviato, release `bottazzi-gmail-jarach-text-only-20261008-v7`, servizio attivo.
- Nuovo workflow: `email_manual_draft_only` anche senza allegati; destinatario esplicitamente verificato; CC riportati esattamente nel messaggio; validatori dominio; usa soltanto `upsert_email_draft`, nessun percorso di invio.
- Test in release v7: 16/16 passed, incluse prove negative su destinatari/CC e allegati.
- Google Workspace Gmail, account `fabio@tiremminnanz.com`: draftId `r-8964746633846357025`, messageId `1a11cdf5a32e5c0a`.
- Gmail ricerca in sola lettura `in:drafts Jarach`: esattamente un messaggio corrispondente, oggetto Via Privata Federico Jarach – ristagni d'acqua da anni, caditoia mancante e pulizia insufficiente.
- Bot-tazzi API: HTTP 200, `status=draft_only`, `sent=false`, `send_calls=0`, `attachment_names=[]`; nessuna email spedita.
- Destinatari: `sindaco.sala@comune.milano.it`, CC `m.municipio2@comune.milano.it`, `presidentelocatelli.municipio2@comune.milano.it`.
- SHA-256 raw Gmail MIME: `714ad84412febc1d1fe7b66881cc3914a45a34a61ed00735b216bb87642e3405`.

## Tracciabilità
- Branch Forgejo locale: `fix/jarach-text-only-gmail-draft-20261008`; commit della feature `eea64a83`.
- Rollback disponibile: release precedente `bottazzi-gmail-pigreco-thread-draft-20261008-v6`.
- L'invio richiede una nuova approvazione umana esplicita.
