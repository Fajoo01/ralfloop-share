# Bot-tazzi Gmail — singola fotografia in bozza (2026-11-17)

## Problema reale
L'utente ha richiesto una replica al Comune di Milano (AMSA, pratica GS0066477-2026) con una foto. Il percorso `manual_email_draft.py` accettava solo bundle documentali specifici (DOCX/PDF) o bozze senza allegati; il normale generatore è risultato bloccato da `semantic_judge_failed`. La bozza solo testo esiste (ID `r-994362734047347362`), ma va aggiornata e non inviata.

## Correzione generale
`create_manual_draft` riconosce automaticamente un solo allegato `.jpg/.jpeg/.png/.webp` e delega a `photo_email_draft.create_photo_draft` senza modificare il resto dei flussi, compresi ARCI, PerRifArci e cinema. Mantiene: contesto sorgente `ralf_terminal`, richiesta `email_manual_draft_only=true`, destinatario verificato una sola volta, corpo esatto con validazione `GenericEmailPipeline.validate_exact` già presente, CC esplicita/validata, foto dentro il workspace MCP (no symlink), file non vuoto <=9MB, signature MIME, hash SHA256 e filename presenti nell'istruzione.

Per aggiornare anziché duplicare: `email_existing_draft_id` e `email_existing_draft_raw_sha256` devono essere entrambi presenti, avere formato valido ed essere ripetuti nel comando con `Bozza verificata: ...` e `SHA256 MIME precedente: ...`. Il connettore Gmail `upsert_email_draft` conserva ID e restituisce `sent=false`; una differenza di ID causa blocco. L'impronta della bozza precedente è una prova dichiarata dall'operatore, **non è una condizione atomica verificata dal broker**: rileggere la bozza corrente prima di effettuare modifiche concorrenti. Nessun permesso di invio è introdotto.

## Collaudo e rilascio
- Unit/integration pytest su Sibilla: test `test_photo_email_draft.py` + `test_manual_email_draft.py` + `test_text_only_gmail_draft.py` + `test_cinema_dual_pdf_draft.py`.
- CI: Forgejo Actions self-hosted `.forgejo/workflows/gmail-single-photo-draft.yml`, canarie stdlib senza credenziali, compile.
- E2E: l'interfaccia `/assistant/v1/chat` deve restituire `gmail_draft_id`, `attachment_names`, `sent=false`; Google Workspace MCP `listDrafts` e `read` devono confermare foto e corpo; invio sempre vietato senza altra approvazione.
- Rollback production: preservare il target di `/home/sibilla-cumana/ralfloop-production/current` prima del cambio; riapplicarlo se health o draft guard falliscono. Nessun riavvio di router, altri servizi o GPU.

Questo ramo contiene soltanto codice, guardie, test e documentazione. Mai utilizzare UID fittizi o chiamare `send_email_draft` durante il test.
