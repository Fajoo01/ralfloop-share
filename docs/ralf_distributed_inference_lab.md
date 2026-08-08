# Ralf distributed inference lab

Stato: laboratorio opt-in. Nessun runtime live sostituito.

## Decisione

- Baseline: Ollama + `qwen2.5:7b`; resta predefinita.
- Primo candidato locale: `llama-server`, già compilato con CUDA.
- Primo uso realistico di Temistocle: tool specialistico JSON a banda ridotta.
- Draft remoto: solo protocollo e metriche; non integrato nel verifier target.
- Activation transfer: solo design; non implementato.
- DwarfStar: fonte di principi, non sostituto drop-in.

Il benchmark B non è eseguibile senza un GGUF Qwen compatibile. Il benchmark C
non è eseguibile senza autenticazione già autorizzata verso l'endpoint VPN. D
richiede inoltre draft/target con tokenizer, vocabolario e chat template
compatibili. Nessun modello è stato scaricato o convertito.

## Audit 2026-07-14

### Sibilla

- CPU: Intel Xeon W-2133, 6 core / 12 thread.
- RAM: 62 GiB.
- GPU: NVIDIA RTX 2070, 8 GiB; driver 535.309.01, CUDA 12.2.
- VRAM osservata durante audit: 338 MiB occupati; Ollama senza modello caricato.
- Ollama: presente; `qwen2.5:7b` 4.7 GB e altri modelli locali presenti.
- `llama-server` / `llama-cli`: build 9542 `6b80c74f2`, CUDA disponibile.
- Python lab: 3.12.3; torch 2.5.1+cu121; transformers 4.46.3.
- GGUF target: nessun Qwen 2.5 7B usabile trovato. Un Qwen3 MoE trovato è vuoto;
  il file DeepSeek DwarfStar è circa 86 GB e incompatibile con questo target.

### Temistocle

Audit eseguito esclusivamente tramite `vpnpc`.

- Nodo: `temistocle-ThinkCentre-M710q`.
- CPU: Intel i5-6400T, 4 core / 4 thread.
- RAM: 7.6 GiB.
- GPU NVIDIA: assente.
- Ollama: presente; `gemma3:270m` 291 MB; nessun modello caricato.
- Assenti: `llama-server`, `llama-cli`, vLLM, SGLang, TabbyAPI, torch,
  transformers, ExLlamaV2.
- Endpoint VPN osservato: `10.252.14.12:9000`; `/health` restituisce HTTP 403.
  Nessun tentativo di aggirare autenticazione.

### VPN

- Ping, 100 campioni: p50 5.78 ms; p95 6.45 ms; jitter 0.436 ms; loss 0%.
- HTTP `/health`, 100 campioni: p50 13.085 ms; p95 15.692 ms; 100/100 HTTP 403.
- TCP applicativo via canale gestito da `vpnpc`: circa 12.6 MiB/s, 105.7 Mbit/s
  mediani su tre trasferimenti read-only da 16 MiB.
- `iperf3`: assente; nessuna porta aperta.

## Provider

Valori accettati:

```text
ollama
llama_cpp
remote_tool
speculative_local
speculative_remote
```

Default:

```sh
RALF_CHAT_PROVIDER=ollama
RALF_REMOTE_MINI_ENABLED=0
RALF_SPECULATIVE_ENABLED=0
```

Opt-in locale:

```sh
RALF_CHAT_PROVIDER=llama_cpp
RALF_LLAMA_CPP_BASE_URL=http://127.0.0.1:19091
RALF_LLAMA_CPP_MODEL=qwen2.5:7b
RALF_LLAMA_CPP_AUTOSTART=0
```

`llama_cpp` usa API OpenAI-compatible, streaming reale e fallback Ollama se il
motore fallisce prima del primo token. L'endpoint deve essere loopback. Il
manager user-space, il lock GPU e i comandi operativi sono documentati in
`docs/ralf_llama_cpp_fast_chat.md`.

Un server esterno può dichiarare speculative decoding locale attivo solo quando
entrambi sono espliciti:

```sh
RALF_SPECULATIVE_ENABLED=1
RALF_LLAMA_CPP_SPECULATIVE_CONFIRMED=1
```

Questo flag attesta un draft verificato dall'engine. Il manager fast-chat non
configura draft, `ngram-simple` o speculative decoding e non scarica modelli.

CLI:

```sh
ralf ask --provider llama_cpp "..."
ralf chat --provider remote_tool
ralf engine status
```

Comando interattivo: `/provider [NAME]`. Ogni provider non Ollama mostra
`EXPERIMENTAL PROVIDER`. `ralf agent` rifiuta provider sperimentali.

## Remote tool

Protocollo: `ralf-mini-v1`; endpoint `/v1/tool`.

- Input/output bounded; timeout; concorrenza 1 di default.
- Allowlist host VPN obbligatoria; endpoint pubblico rifiutato.
- Circuit breaker; massimo un retry, solo per richieste idempotenti.
- Nessuna cronologia completa: il prototipo invia solo la domanda corrente,
  troncata a 8192 caratteri.
- Campi secret rifiutati; log/manifest redatti.
- Risultato remoto non vincolante; comandi, autorizzazioni, capability e azioni
  protette rifiutati dallo schema.
- Il modello grande locale resta autore della risposta finale.
- Timeout, JSON invalido, endpoint lento/spento: risposta locale continua.

Configurazione disabilitata di default:

```sh
RALF_CHAT_PROVIDER=remote_tool
RALF_REMOTE_MINI_ENABLED=1
RALF_REMOTE_MINI_BASE_URL=http://VPN_HOST:PORT
RALF_REMOTE_MINI_ALLOWED_HOSTS=VPN_HOST
RALF_REMOTE_MINI_TIMEOUT=10
RALF_REMOTE_MINI_ROLE=tool
```

L'autenticazione deve arrivare dal meccanismo/sessione già autorizzata. Nessuna
nuova chiave è creata o memorizzata nel repository.

## Remote draft

Protocollo separato: `ralf-draft-v1`; endpoint `/v1/draft`.

- Scambia token ID, non suggerimenti testuali.
- Verifica hash tokenizer e vocabolario prima dell'accettazione.
- Massimo 1-8 token draft per round; default 4.
- Misura prodotti, accettati, acceptance rate, byte e latenza rete.
- Il client non è un verifier: senza integrazione target-side, il provider
  `speculative_remote` dichiara sempre `speculative_active=false` e usa decoding
  autoregressivo locale.

Qwen 2.5 7B target e `gemma3:270m` remoto sono incompatibili: tokenizer,
vocabolario e famiglia diversi. Temistocle non contiene un Qwen 2.5 0.5B/1.5B.
Acceptance rate quindi non misurabile. Il draft remoto non è stato eseguito.

## Costi remoti stimati

| Protocollo | Payload tipico | RTT per ciclo | Giudizio iniziale |
| --- | ---: | ---: | --- |
| Tool call | 1-16 KiB input, <=8 KiB JSON | 1 per compito | adatto |
| Draft batch | token context + 2-8 token | circa 1 per batch | rischio latenza seriale |
| Activation transfer | hidden states per layer/split | molti dati e sync | solo ricerca |

Con p50 HTTP 13.085 ms, 4 draft token per round aggiungono almeno circa 3.3 ms
di rete per token prima di serializzazione e calcolo remoto. Non prova uno
speedup: servono acceptance rate e benchmark end-to-end. Il tool a singolo round
è più robusto. Activation transfer richiede stesso runtime, partizione layer,
dtype/shape concordati, compressione e scheduling rete/calcolo.

## DwarfStar

Fonti primarie:

- <https://github.com/antirez/ds4>
- <https://github.com/ggml-org/llama.cpp/blob/master/docs/speculative.md>

Applicabile come principio:

- memoria esplicita e peak misurato;
- quantizzazione selettiva verificata contro vettori di correttezza;
- riduzione copie e overlap rete/calcolo;
- offload/caricamento per blocchi;
- trasmissione di attivazioni come design sperimentale.

Non applicabile direttamente:

- graph e GGUF specifici DeepSeek V4;
- routing/distribuzione esperti MoE su Qwen dense 7B;
- SSD streaming degli esperti;
- requisito 96/128+ GB;
- kernel custom e formato pesi dedicato.

DwarfStar non è stato trattato come sostituto generale di Ollama.

## Benchmark matrix

| ID | Configurazione | Stato |
| --- | --- | --- |
| A1/A2 | Ollama cold/warm | baseline nota 27-36 s TTFT; non rieseguita |
| B1/B2 | llama.cpp cold/warm | bloccato: GGUF target assente |
| C1 | Ollama + remote tool | bloccato: endpoint autorizzato non disponibile |
| C2 | llama.cpp + remote tool | bloccato dai due punti precedenti |
| D1 | llama.cpp + draft locale | bloccato: draft GGUF assente |
| D2 | llama.cpp + draft remoto | incompatibile con modello Temistocle attuale |
| E1 | prompt lookup / n-gram | supportato da llama.cpp; da misurare dopo GGUF |

La baseline non è stata rilanciata: avrebbe caricato il modello live e alterato
lo stato osservato. Nessun vincitore dichiarato senza 3 warm-up, 5 run misurate
e dataset completo. Primo benchmark consigliato: B contro A con GGUF autorizzato;
poi E1; infine C. D solo se un draft Qwen compatibile supera +15% end-to-end.

## Sicurezza invariata

- Chat normale non chiama `/tasks/run`.
- Gate Telegram resta deterministico e separato.
- Nessun auto-approve, auto-execute o capability executor nel lab.
- Remote tool non decide policy, approval, shell o autorizzazioni.
- Nessuna modifica a Meowgram, gate Telegram, systemd o servizi live.
