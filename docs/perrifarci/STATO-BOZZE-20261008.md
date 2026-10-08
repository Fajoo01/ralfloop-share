# PerRifArci — tre bozze Gmail, checkpoint 08/10/2026

## Richiesta utente
SOLO bozze Gmail (invio manuale dell'utente) a:
- PiGreco `pigreco@luogoideale.org`
- Amici del Parco Trotter `amicitrotter@gmail.com`
- AMEDI `info@amedi.it`

Tono di scuse esplicite per ripetute lettere errate; allegare per ciascuno Word+PDF del nuovo Allegato A2 **Educativa** modello ARCI 07/10, quota `€ 0,00`, nessuna dichiarazione già firmata. Testi pronti in `docs/perrifarci/email_drafts/*.txt`. Allegati nel workspace Google MCP di Sibilla, `A2_ARCI_07ott2026_{PiGreco,Amici_Parco_Trotter,AMEDI}_DA_FIRMARE.{docx,pdf}`.

## Codice testato
- Branch Forgejo `fix/email-exact-body-no-ollama-20261008`, commit `72ea43d6`, push verificato.
- Correzione core/runtime Bot-tazzi: usare `email_exact_body` validato senza rigenerarlo via Ollama; `email_manual_draft_only` impedisce richieste di invio/approvazione automatica.
- `66 passed` su `test_unified_assistant.py`, `test_unified_email_pipeline.py`, `test_unified_email_send.py`, sia branch sia copia del runtime di produzione.
- Tre corpi reali superano `GenericEmailPipeline.validate_exact` (`passed/passed`), senza date artificiali.
- Test diretto come utente non autorizzato arriva a `draft_sync_failed/MCPError` e NON scrive in Gmail: guardia UID funzionante.
- Runtime attivo su Sibilla: `/home/sibilla-cumana/ralfloop-production/current` -> `releases/bottazzi-email-exactbody-20261008-v1`; `ralfloop-backend.service` attivo. Logging diagnostico temporaneo v2 disattivato con rollback a v1.

## Stato finale delle bozze: NON CONFERMATE
- Richiesta reale di sola bozza PiGreco via `POST /assistant/v1/chat` dopo il deploy ha dato ancora **503**, senza `draftId`; NON affermare bozza creata. Questa eccezione non è stata isolata completamente.
- Dopo il deploy il sistema remoto ha bloccato un ulteriore tentativo *prima dell'esecuzione* tramite controlli OpenAI; non aggirare.
- Ricerche Gmail `in:drafts to:amicitrotter@gmail.com` e `in:drafts to:info@amedi.it`: nessuna bozza. Per PiGreco la ricerca precedente non aveva trovato bozze; il controllo ulteriore è stato bloccato dallo strumento, quindi assenza non verificabile all'ultimo istante.
- Nessun invio eseguito o autorizzato. Non eseguire `send_email_draft`, `manage_email send`, `reply` o invio via interfaccia.
- Follow-up tecnico: isolare errore backend nel percorso effettivo Gmail draft-only, preferibilmente test nel canale di Bot-tazzi autorizzato e non con client shell non autorizzato; verificare ID e allegati di ogni bozza con letture Gmail; mai rilanciare una create senza prima verificare assenza di bozza, per evitare duplicati.
