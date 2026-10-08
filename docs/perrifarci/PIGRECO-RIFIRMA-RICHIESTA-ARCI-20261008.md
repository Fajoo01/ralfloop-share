# PiGreco — richiesta ARCI di nuova firma A2 (8 ottobre 2026)

Giulio Franchini ha risposto alle 20:09 via Gmail, messaggio `1a11cb4f649d4306`: il PDF PiGreco precedente non è accettato ai fini della loro pratica come firma digitale certificata. Chiede esplicitamente **firma autografa (olografa) sul modello A2 e copia del documento d'identità**, oppure firma digitale certificata `.p7m`. Non attribuire questa richiesta in modo generico alla Regione: è l'integrazione domandata da Giulio per questa consegna.

Bot-tazzi ha predisposto **soltanto una BOZZA Gmail** nella conversazione esistente, non ha inviato nulla:
- destinatario `pigreco@luogoideale.org`
- thread ID `1a11b26a4893e6ea`
- draft ID `r8415529406854926643`
- message ID `1a11ccab82904fc3`, letto indipendentemente tramite Gmail `manage_email.read` con etichetta **DRAFT**
- allegati effettivi: `A2_ARCI_07ott2026_PiGreco_DA_FIRMARE.docx` e `A2_ARCI_07ott2026_PiGreco_DA_FIRMARE.pdf`
- testo verificato: `docs/perrifarci/email_drafts/PiGreco_reintegrazione_firma_Giulio_20261008.txt`; contiene scuse per reiterazione, entrambe le modalità e cofinanziamento €0,00.

Per chiarire l'apparente contraddizione tecnica: il PDF PiGreco già ricevuto possiede firma crittografica CAdES valida sul contenuto (verifica `pdfsig`), ma il trust store del sistema non certifica l'emittente, mentre ARCI ha esplicitato un requisito di consegna distinto; non dichiarare che il documento è privo di firma.

## Runtime/test
- Versione: `bottazzi-gmail-pigreco-thread-draft-20261008-v6`; whitelist di indirizzo, thread e oggetto per reply protetto.
- Test: `71 passed` con `tests/test_manual_email_draft.py tests/test_unified_assistant.py tests/test_unified_email_send.py`.
- Ricevuta: `perrifarci-pigreco-arci-resign-draft-receipt-20261008.json`.

**Resta:** utente controlla e invia la bozza, quindi attendere l'A2 rifirmato e la carta d'identità (se sceglie la firma autografa). Nessuna email inviata automaticamente.
