# Gmail — due PDF in bozza corso cinema, 11 ottobre 2026

## Esito verificato
- Controllo con Gmail `manage_email` indipendente da Bot-tazzi: `Labels: DRAFT`.
- Destinatario **Andrea Cagno**: `acagnoprod@gmail.com`. Mittente `fabio@tiremminnanz.com`.
- Oggetto: `Corso di cinema al Trotter – moduli di iscrizione e tesseramento Tiremm`.
- Draft ID `r-6066779218853180470`; Gmail message ID `1a12c75e83df3fc9`.
- Gmail mostra **Attachments (2)**:
  - `01_Iscrizione_corso_cinema_Trotter.pdf` (40.2 KB);
  - `02_Domanda_ammissione_Tiremm_ARCI_minori.pdf` (40.0 KB).
- Backend Bot-tazzi: `status=draft_only`, `sent=false`, `send_calls=0`, `writes=1`. Nessun messaggio inviato.

## Origine e validazione
- I PDF sono stati ricreati su Sibilla dal contenuto testuale delle versioni della conversazione, non trasferiti byte-per-byte dal sandbox. Hanno 2 pagine ciascuno.
- La quota del corso e le informative privacy definitive restano da verificare prima di usare i moduli.
- Correzione source Forgejo locale commit `431d9df9`, branch `fix/cinema-draft-two-pdf-20261011`; **16 test superati**.
- Rilascio runtime `bottazzi-cinema-two-pdfs-20261011-v11`, servizio `ralfloop-backend.service` attivo; rollback precedente `bottazzi-perrifarci-accogliersi-drafts-20261009-v10`.

## Bozze precedenti
- Per lo stesso destinatario sono rimaste bozze incomplete e di test con oggetto `Comunicazione Tiremm Innanz APS`.
- L'unica bozza da usare è Gmail message ID `1a12c75e83df3fc9`, verificata con oggetto specifico e 2 allegati. Nessun duplicato è stato eliminato.
