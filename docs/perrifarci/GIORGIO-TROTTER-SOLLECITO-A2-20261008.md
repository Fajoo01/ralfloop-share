# Sollecito modello A2 Educativa a Giorgio Calabria — 8 ottobre 2026

## Evidenza reale Gmail
- Nella risposta del 08/10/2026 16:45, Giorgio ha rimandato la stessa lettera libera `Arci.pdf` con i due documenti d'identità (fonte Gmail message ID `1a11bfaa8db812df`), non il modello regionale A2 Educativa già inviato alle 13:11.
- Nuova risposta **BOZZA e non inviata** creata in Gmail da Bot-tazzi, su thread originale `1a11b26d538e3ca8`: draft ID `r-7340047602358268583`, message ID `1a11c797a8f0db75`.
- `manage_email.search`, `manage_email.getThread` e `manage_email.read` indipendenti hanno confermato etichetta `DRAFT`, destinatario `amicitrotter@gmail.com`, appartenenza alla conversazione preesistente, **2 allegati** `A2_ARCI_07ott2026_Amici_Parco_Trotter_DA_FIRMARE.docx` e PDF.
- Testo esatto in `docs/perrifarci/email_drafts/Giorgio_Trotter_solleticito_A2_regione_20261008.txt`: scuse per la confusione, spiegazione adempimenti aggiuntivi di Regione Lombardia per questo bando, ribadisce € 0,00, chiede data/luogo/firma su modello ufficiale, conferma che i documenti d'identità sono già arrivati.
- **Nessun invio**: il broker Gmail risponde `sent:false`. Solo l'utente può approvare/inviare.

## Bot-tazzi e test
- Backend attivo: release immutabile `/home/sibilla-cumana/ralfloop-production/releases/bottazzi-gmail-trotter-thread-draft-20261008-v5` con ramo di sola bozza; sistema di invio non modificato.
- `email_reply_thread_id` accettato solo per il thread e indirizzo Gmail specificamente verificati, con oggetto esatto; nessuna iniezione di thread arbitrari.
- `pytest -q tests/test_manual_email_draft.py tests/test_unified_assistant.py tests/test_unified_email_send.py`: **69 passed**.
- Ricevuta macchina: `perrifarci-giorgio-a2-draft-receipt-20261008.json`.

## Stato residuo
Invio **manuale della bozza** a Giorgio; richiesta successiva del vero A2 firmato. Non confondere la lettera libera `Arci.pdf` con A2 conforme.
