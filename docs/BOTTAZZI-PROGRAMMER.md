# Bot-tazzi Programmatore

Stato: candidato locale verificato il 2026-09-23.

## Scopo

Bot-tazzi Programmatore trasforma un ticket di manutenzione software in una patch candidata verificata, senza dare al modello autorita su commit, push o deploy.

Pipeline:

`ticket -> preflight Git -> coding worker -> diff bounded -> validator deterministico -> risk gate -> candidate_ready/fail_closed`

## Vincoli di sicurezza

- accetta solo la radice di un Git worktree collegato e consentito;
- per default richiede worktree pulito e nessuna operazione Git in corso;
- il worker non riceve `bash` per default: solo `read,edit,write`;
- il prompt vieta commit, push, deploy e modifica della storia Git;
- il supervisore verifica che `HEAD` sia identico prima e dopo il worker;
- test e validator sono eseguiti dal supervisore, non affidati al modello;
- nessun push/deploy e implicito;
- stato/audit locale e persistito con permessi 0600;
- se il worker fallisce senza produrre modifiche, chiude `fail_closed` senza invocare il semantic judge.

## Componenti

- `ralfloop_agent/programmer/core.py`: orchestratore e preflight;
- `ralfloop_agent/coding_harness/harness.py`: worker, fallback, diff, validator, risk gate;
- `tools/run_bottazzi_programmer.py`: CLI;
- `openshell_backend/app.py`: routing `patch_allowed + local_maintenance` verso il Programmatore;
- `tests/test_programmer.py`: contratto di isolamento e no-commit;
- `tests/test_coding_harness_core.py`: fallback, errori semantici del worker e validator.

## Configurazione

Variabili principali:

- `RALF_CODE_WORKTREE_ROOTS`: root locali ammesse, separate da `:`;
- `RALF_PROGRAMMER_STATE_ROOT`: directory audit locale;
- `RALF_CODE_WORKER_USER`: utente del coding worker;
- `RALF_CODE_PROVIDER` / `RALF_CODE_MODEL`: worker primario;
- `RALF_CODE_FALLBACK_PROVIDER` / `RALF_CODE_FALLBACK_MODEL`: fallback;
- `RALF_PI_BIN`: percorso del client `pi`;
- `RALF_CODE_WORKER_SHELL=1`: abilita esplicitamente `bash` al worker; default disabilitato.

Il validator predefinito e `git diff --check`. Viene eseguito direttamente con Git, pager disabilitato e `--no-ext-diff --no-textconv`, cosi resta disponibile anche se il parser AST opzionale dello Shell Judge non e installato nel worktree.

Validator diversi continuano a passare dal Shell Judge e falliscono chiusi se non verificabili.

## Stati

- `candidate_ready`: patch prodotta e gate deterministici superati;
- `blocked`: preflight non soddisfatto (root non ammessa, worktree sporco, merge/rebase in corso, ecc.);
- `failed`: harness concluso senza candidato valido;
- `fail_closed`: errore del worker/judge o violazione del contratto, inclusa modifica di HEAD.

## Provider `pi`

Il wrapper `pi` puo terminare con exit code 0 anche quando la sessione JSON riporta `stopReason=error` e `auto_retry_end success=false`. Il coding harness interpreta ora questi eventi come errore sintetico `rc=70`, cosi il fallback viene realmente attivato.

## Smoke reale 2026-09-23

Worktree usa-e-getta sotto `/home/bandi/.ralf-work` con:

```python
def add(a, b):
    return a - b
```

Ticket: correggere esclusivamente il bug e restituire `a + b`.

Risultato:

- `llamacpp-code-local / qwen2.5-coder-7b`: connection error, classificato `rc=70`;
- fallback `agentcpm-local / AgentCPM-Explore`: patch prodotta;
- file finale: `return a + b`;
- `git diff --check`: verde;
- diff: 1 file, 2 linee;
- rischio: low;
- decisione: `deterministic_fast_path`;
- stato: `candidate_ready`;
- `HEAD` prima/dopo invariato;
- `ds4_invoked=false`, `repair_invoked=false`.

`deepseek-local / deepseek-v4-flash` e risultato non raggiungibile nello smoke precedente; non e necessario al percorso funzionante perche AgentCPM e operativo.

## Rapporto con Autorepair

Autorepair (issue #36) puo in futuro creare automaticamente ticket per il Programmatore. Non deve pero promuovere direttamente una patch in produzione: Programmatore produce il candidato, mentre canary/promozione/rollback restano gate separati.
