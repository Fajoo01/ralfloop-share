# Corso cinema Trotter — risposta collegata alla precedente email

## Stato Gmail verificato 11 ottobre 2026
- Email precedente inviata ad Andrea Cagno: message ID `1a127729eee19bb5`, soggetto `Corso di cinema al Trotter – IBAN e quota di iscrizione`, data 10 ottobre 2026, `Labels: SENT`.
- Thread originale verificato con Gmail `getThread`: `1a127677cbfd26d7`.
- Nuova bozza tramite Bot-tazzi: `Re: Corso di cinema al Trotter – IBAN e quota di iscrizione`, destinatario `acagnoprod@gmail.com`.
- Draft ID: `r-7136097804289011347`, Gmail message ID: `1a12c85e7259d687`.
- Verifica indipendente Gmail `getThread('1a127677cbfd26d7')`: **Thread (2 messages)**, originale 10 ottobre + risposta in bozza 11 ottobre. La risposta compare con oggetto `Re:` corretto.
- Lettura indipendente `manage_email.read('1a12c85e7259d687')`: `Labels: DRAFT`, nuovo corpo testuale approvato per sola bozza, **Attachments (2)**:
  - `01_Iscrizione_corso_cinema_Trotter.pdf`
  - `02_Domanda_ammissione_Tiremm_ARCI_minori.pdf`
- Bot-tazzi: `status=draft_only`, `sent=false`, `send_calls=0`, nessuna email inviata.
- Il nuovo testo fa riferimento alla mail precedente con quota e coordinate; non ripete i numeri in quanto già presenti nell'email del thread.

## Software
- Vincolo sul thread preciso e sul destinatario e oggetto approvato; nessun allargamento generico. Commit Forgejo `dc7ed6eb`.
- 18 test superati (inclusi thread corretto, thread errato bloccato, due PDF, hash).
- Release attiva: `bottazzi-cinema-reply-thread-20261011-v12`.
- Restano vecchie bozze non collegate al thread, incluse copie con oggetto diverso. Usare solo la nuova risposta ID `1a12c85e7259d687`. Nessuna eliminazione duplicati effettuata.
