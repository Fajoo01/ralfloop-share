# PerRifArci — stato live bozze email 2026-10-03

Questo file registra lo stato operativo verificato. Non autorizza alcun invio.

## Stato

- Account Gmail: `fabio@tiremminnanz.com`
- Oggetto comune: `PerRifArci – richiesta lettera di sostegno`
- Corpo canonico verificato nelle 6 bozze.
- Ogni bozza contiene 2 allegati: lettera specifica + `PerRifArci_Abstract.pdf`.
- Ricerca Gmail `in:sent subject:PerRifArci newer_than:7d`: 0 risultati.
- Approval gate: enabled, auto_execute=false.
- Sei approval canoniche: pending.
- Decisioni: 0.
- Execution: 0.
- Sei notifiche control-plane Telegram consegnate.
- Una approval canary duplicata `apr_E9K8YSEM` è stata cancellata; execution=0.
- Manifest live: `/home/bandi/perrifarci-staging-20261002/email-send-manifest.json`
- SHA256 manifest: `095dba889562d9362bd612a2e1f86f90933510f278e9b7189ac463685a4e03e0`

| Destinatario | Gmail draft | Raw MIME SHA256 | Approval | Digest |
|---|---|---|---|---|
| miic8dd005@istruzione.it | r-3075609958872328161 | bcefa0ec3bb397eb9006488eca80f7c96f8e8e0dfd98e68bba853b36362bba3a | apr_G5FMKKD8 | 7153-3210 |
| ED.UE10@comune.milano.it | r127658816653256721 | 6060ada6c1b829ca883e9d44cd7ab8fdcd2e7d57494f916cae3b46d3192fc349 | apr_ZS9N4KCN | 7DA7-6306 |
| miic8gh008@istruzione.it | r2520456484722217206 | 94ce915b2c6028c5b3bc2d07647ceb7c8a20dc8146bf33894c71b400cc3eeea3 | apr_R7543TMY | B63A-8EDA |
| info@amedi.it | r2403744326878782768 | 490aa430efade20ecfc264880fa010959a1a315e506c57cbd58d10c9a64b9070 | apr_3EKQG4FD | 38ED-888C |
| pigreco@luogoideale.org | r-8570369634459486686 | 3402fdb6d2a1c170eedb4a0500292d2284199fa34138913d44e3ff95930b32d5 | apr_TDRJXEFT | 843F-8934 |
| amicitrotter@gmail.com | r1973480878599356126 | 6fbe1c79faf498690b006c8a8543d333242352865f0f6fe510d8302acab4f928 | apr_RZYY2E9E | AEA0-B6D4 |

## Allegati verificati

Abstract originale: SHA256 `7ae315080bae587fa4c468baf45dadce777406455355bb5c9b282334a30b5955`.

Le sei lettere sono state ricontrollate sul filesystem contro il manifest; hash e dimensione coincidono per 6/6.

## Vincolo

Nessuna email può essere inviata finché la specifica approval hash-bound non viene approvata. La pipeline `send_email_draft` rifà il controllo del raw MIME SHA256 prima dell'invio e fallisce closed se la bozza è cambiata.
