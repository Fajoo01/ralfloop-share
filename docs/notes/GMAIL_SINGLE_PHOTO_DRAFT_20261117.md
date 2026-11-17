# Bot-tazzi Gmail — singola fotografia in bozza (2026-11-17)

## Problema reale
L'utente ha richiesto una replica al Comune di Milano (AMSA, pratica GS0066477-2026) con una foto. Il percorso `manual_email_draft.py` accettava solo bundle documentali specifici (DOCX/PDF) o bozze senza allegati; il normale generatore è risultato bloccato da `semantic_judge_failed`. La bozza solo testo esiste (ID `r-994362734047347362`), ma va aggiornata e non inviata.

## Correzione generale
`create_manual_draft` riconosce automaticamente un solo allegato `.jpg/.jpeg/.png/.webp` e delega a `photo_email_draft.create_photo_draft` senza modificare il resto dei flussi, compresi ARCI, PerRifArci e cinema. Mantiene: contesto sorgente `ralf_terminal`, richiesta `email_manual_draft_only=true`, destinatario verificato una sola volta, corpo esatto con validazione `GenericEmailPipeline.validate_exact` già presente, CC esplicita/validata, foto dentro il workspace MCP (no symlink), file non vuoto <=9MB, signature MIME, hash SHA256 e filename presenti nell'istruzione.

Per sostituire in sicurezza una bozza precedente: `email_existing_draft_id` e `email_existing_draft_raw_sha256` devono essere entrambi presenti e ripetuti nel comando (`Bozza verificata: ...`, `SHA256 MIME precedente: ...`). **Non si esegue più PUT sul draft originario**: il broker Gmail `upsert_email_draft` crea una nuova bozza con foto, restituisce un nuovo ID e `sent=false`. La bozza precedente resta intatta fino a verifica indipendente della nuova. Non è previsto un sistema di eliminazione automatico: serve gestione separata di eventuali duplicati. L'impronta precedente è prova dichiarata, non precondizione atomica verificata dal broker. Nessun permesso di invio è introdotto.

## Collaudo e rilascio
- Unit/integration pytest su Sibilla: test `test_photo_email_draft.py` + `test_manual_email_draft.py` + `test_text_only_gmail_draft.py` + `test_cinema_dual_pdf_draft.py`.
- CI: Forgejo Actions self-hosted `.forgejo/workflows/gmail-single-photo-draft.yml`, canarie stdlib senza credenziali, compile.
- E2E: l'interfaccia `/assistant/v1/chat` deve restituire `gmail_draft_id`, `attachment_names`, `sent=false`; Google Workspace MCP `listDrafts` e `read` devono confermare foto e corpo; invio sempre vietato senza altra approvazione.
- Rollback production: preservare il target di `/home/sibilla-cumana/ralfloop-production/current` prima del cambio; riapplicarlo se health o draft guard falliscono. Nessun riavvio di router, altri servizi o GPU.

Questo ramo contiene soltanto codice, guardie, test e documentazione. Mai utilizzare UID fittizi o chiamare `send_email_draft` durante il test.


## Esecuzione reale 17/11/2026 — verifiche in Gmail
- Ramo su Forgejo locale `fix/gmail-single-photo-draft-20261117`, commit `5e99ac788fcd124d7be0449565e08bd75daba24d`.
- Test su Sibilla: 35 pytest PASS, 4 unittest stdlib PASS, compilazione PASS.
- Forgejo Actions self-hosted run **#3461 SUCCESS** sul commit esatto: checkout, 4 canarie, compilazione, 3/3 step.
- Tentativo con API `drafts.update` sul vecchio draft `r-994362734047347362`: `MCPError`; successivamente draft non più presente. Diagnosi: aggiornamento in-place non sicuro. Recupero senza invio creando draft testo `r5708497896604398104`.
- Creazione di NUOVA bozza Gmail con foto (non PUT): ID `r3204828384592282300`, message ID `1a1e930b2de58c6e`, `sent=false`, `attachment_names=['amsa_reclamo_20261117_photo.jpg']`, Google Workspace MCP READ conferma label `DRAFT`, un JPG 86,3 KB e corpo corretto.
- Verifica Gmail trova anche un **messaggio già inviato alle 11:53 del 17/11** al Sindaco (message ID `1a1e9242db735247`, label SENT, fotografia `1000055732.jpg` 1596 KB). È precedente alla nostra creazione di bozza fotografica; non attribuirlo al codice testato. Non inviare ulteriori copie senza nuova istruzione.
- Due bozze restano su Gmail (testo e foto); **nessuna eliminazione automatica**. La correzione permanente usa solo `POST drafts.create` per le foto e preserva tutte le bozze precedenti.
- Release production attuale `/home/sibilla-cumana/ralfloop-production/releases/agent-gmail-photo-safe-20261117-5e99ac78`: `MANIFEST.sha256` verificato, import fail-closed e guard anti-PUT verificata, `ralfloop-backend.service` active, `GET /health` e `GET /assistant/v1/status` OK. Rollback preparato verso `agent-standing-auth-20261114-db2f8c8`.
- **Non** modificati permessi di invio, GUP/REA, URI Gmail, credenziali, altri servizi; nessuna GitHub Action o ulteriore email inviata da questi collaudi.
