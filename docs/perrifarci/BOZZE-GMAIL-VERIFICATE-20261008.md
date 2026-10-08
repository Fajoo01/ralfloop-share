# Tre bozze Gmail A2 Educativa — verifica del 08/10/2026

Richiesta utente: preparare **solo bozze** email di scuse e documenti corretti PerRifArci. L'utente invia manualmente.

Tutte create dal backend Bot-tazzi sul percorso `/assistant/v1/chat`, con `email_manual_draft_only=true`, `email_exact_body`, `email_exact_subject` e allegati Word/PDF dal workspace autorizzato. Nessuna funzione di invio è stata chiamata.

| Destinatario | draftId Gmail | messageId riscontrato in Gmail | Stato | Allegati |
| --- | --- | --- | --- | --- |
| PiGreco `pigreco@luogoideale.org` | `r6362563097795089071` | `1a11b26a4893e6ea` | `DRAFT` | `A2_ARCI_07ott2026_PiGreco_DA_FIRMARE.docx`, `.pdf` |
| Amici del Parco Trotter `amicitrotter@gmail.com` | `r-4102696029579119646` | `1a11b26d538e3ca8` | `DRAFT` | `A2_ARCI_07ott2026_Amici_Parco_Trotter_DA_FIRMARE.docx`, `.pdf` |
| AMEDI `info@amedi.it` | `r-1919708680565138031` | `1a11b26ed80bd0e4` | `DRAFT` | `A2_ARCI_07ott2026_AMEDI_DA_FIRMARE.docx`, `.pdf` |

Rilettura indipendente `manage_email search in:drafts` + `manage_email read` per ciascuno: etichetta **DRAFT**, destinatario corretto, contenuto di scuse e quota **€ 0,00**, 2 allegati (PDF+DOCX). Riscontro vero di Gmail, non simulazione.

## Correzione software

- Nuovo componente `openshell_backend/manual_email_draft.py`: handoff deterministico della **sola bozza**, test della provenienza `ralf_terminal`, requisito di richiesta `email_manual_draft_only=true`, destinatario esplicito verificato, testo validato da `GenericEmailPipeline.validate_exact`, 2 allegati con SHA-256 e percorso vincolato al workspace, backend UID approvato e risposta `sent:false`.
- Ramo dedicato nel POST `/assistant/v1/chat` per `email.compose` **con testo esatto già disponibile**: niente generazione Ollama, niente autorizzazione di invio.
- Il percorso email ordinario resta invariato, come i controlli UID e l'impossibilità di inviare senza autorizzazione.
- Allestimento release tramite `stage_manual_gmail_release.py`; runtime attivo `/home/sibilla-cumana/ralfloop-production/releases/bottazzi-gmail-manual-drafts-20261008-v3` e servizio `ralfloop-backend.service` attivo.
- Test su sorgente e release di staging: **64 passed**, tra cui 3 bozze con FakeSync, errori di SHA e mancanza flag di sola bozza; nessun invio nei test.
- Ricevute strutturate: `perrifarci-draft-receipts-20261008.json`. Scrittura originale verificata tramite Gmail e integrità raw SHA-256.
- Da fare soltanto dall'utente: aprire Gmail → Bozze, verificare e fare clic su Invia se d'accordo. Non inviare automaticamente.

## Readback indipendente dopo il completamento (08/10/2026)

Verifica read-only eseguita senza modificare né inviare email:

- Repository locale Forgejo `fix/email-exact-body-no-ollama-20261008`: `HEAD` e `origin` coincidono al commit `06f72a830775459c3d76922448da801aa4895cef`, worktree pulito all'inizio del controllo.
- Runtime Sibilla: `ralfloop-backend.service` **active**, endpoint `/assistant/v1/status` **ok**, release effettiva `/home/sibilla-cumana/ralfloop-production/releases/bottazzi-gmail-manual-drafts-20261008-v3`.
- Lettura Gmail `manage_email.read` di `1a11b26a4893e6ea`: **DRAFT**, `pigreco@luogoideale.org`, due allegati (DOCX e PDF PiGreco), scuse e `€ 0,00`.
- Lettura Gmail `manage_email.read` di `1a11b26d538e3ca8`: **DRAFT**, `amicitrotter@gmail.com`, due allegati (DOCX e PDF Amici Trotter), scuse e `€ 0,00`.
- Lettura Gmail `manage_email.read` di `1a11b26ed80bd0e4`: **DRAFT**, `info@amedi.it`, due allegati (DOCX e PDF AMEDI), scuse e `€ 0,00`.
- Non sono stati eseguiti nuovi test o nuove build in questo readback: già 64 test superati nella fase precedente, nessuna modifica al codice. Nessuna invocazione di invio.

**Esito**: GOAL tecnico raggiunto; ultimo intervento umano ancora richiesto: revisione e invio manuale delle tre bozze, poi raccolta dei moduli completati e firmati con i documenti d'identità.
