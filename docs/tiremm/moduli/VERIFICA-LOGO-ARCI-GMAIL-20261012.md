# Verifica finale — modello originale con logo Tiremm (12/10/2026)

- Richiesta utente: non generare immagini; usare Bot-tazzi e **modulo esistente con logo**. Modello sorgente: `/home/bandi/teatro-moduli-corretti-20261006/01_Modulo_iscrizione_Tiremm_AMEDI_FINALE_20261006_v3.pdf`, logo originale con SHA256 `a9bc217343a46e37557b3331042803904d2691c25f7229df1db9a486e5fdb37f`.
- PDF domanda ARCI aggiornato con logo reale, intestazioni rosse e stesso stile, A4 **1 pagina**: `docs/tiremm/moduli/02_Domanda_ammissione_Tiremm_ARCI_minori_LOGO_20261012.pdf`; SHA256 `69df61ad4e84958687d8be321bbd11499835bf2122b9f315a9fbeb06df32ee31`.
- Il PDF iscrizione al corso è rimasto invariato: in precedenza includeva consenso immagini separato, che non deve essere perso. Nessuna immagine generata artificialmente.
- Solo il file `/home/bandi/.local/share/google-workspace-mcp/workspace/02_Domanda_ammissione_Tiremm_ARCI_minori.pdf` è stato sostituito. Backup: `/home/bandi/.cache/02_Domanda_ammissione_Tiremm_ARCI_minori_prelogo_20261012.pdf`.
- Bozza Gmail aggiornata **in posto** tramite Bot-tazzi backend autorizzato, `draftId=r-7136097804289011347`; destinatario `acagnoprod@gmail.com`, thread `1a127677cbfd26d7`, oggetto `Re: Corso di cinema al Trotter – IBAN e quota di iscrizione`. Il testo con richiesta di aggiornamento su Sara resta invariato.
- Lettura indipendente Gmail, nuovo messageId `1a12fb26b2bbf45e`: `Labels: DRAFT`; **Attachments (1)**: `02_Domanda_ammissione_Tiremm_ARCI_minori.pdf` (**54.2 KB**), non più 40.0 KB. `getThread` mostra due email, precedente inviata + bozza aggiornata.
- Backend: `status=draft_only`, `send_calls=0`, `sent=false`. Nessuna email inviata.
- Validatore applicato solo a destinatario/thread/draftId verificati; codice commit `0872bb8d`; **20 test superati**. Release attiva `bottazzi-cinema-membership-logo-20261012-v13`, rollback v12.
- Nota: informativa privacy completa del tesseramento deve essere consegnata insieme alla domanda prima di utilizzarla.
