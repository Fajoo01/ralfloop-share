# Ralf Skill, Jury, Collaboration, Verification

## Componenti

| Componente | Funzione |
| --- | --- |
| JuryPolicy | Decide quando serve revisione |
| CollaborationBackend | Decide come collaborano gli agenti |
| RecursiveMAS native | Hidden-state recursion con RecursiveLink |
| Text MAS proxy | Collaborazione tramite testo |
| VerificationPolicy | Verifica il risultato |
| LLM-as-Judge | Valutazione qualitativa opzionale |
| Human confirmation | Autorizza side effect |

## Stato reale

- `JuryPolicy`: implementazione reale di routing/policy.
- `CollaborationBackend`: contratto + selector reale.
- `text_mas_proxy`: implementazione reale di trace testuale, non RecursiveMAS nativo.
- `RecursiveMASNativeAdapter`: probe/preflight/load lazy predisposti; esecuzione nativa solo se abilitata e caricata.
- `VerificationPolicy`: contratto separato; verifica deterministica/rule/judge restano distinte dalla giuria.
- `jury_trace`: alias deprecato di `collaboration_trace`.
- Skill routing data-driven da `config/capability_routing.json`.
- Skill operative lette da `/home/sibilla-cumana/ralfloop_data/skills/*/skill.json`.
- Manifest skill in cache LRU per evitare rilettura JSON continua.

## Audit file

| File | Classificazione |
| --- | --- |
| `config/capability_routing.json` | contratto/policy |
| `src/models.py` | contratto |
| `src/routing_config.py` | implementazione reale config |
| `src/router.py` | routing/policy |
| `src/text_mas_proxy.py` | implementazione reale text proxy |
| `src/recursive_mas.py` | wrapper compatibile deprecato |
| `src/api.py` | payload/API MVP |
| `src/skills.py` | implementazione reale registry skill |
| `ralfloop_agent/integration/recursive_mas_native.py` | adapter/probe/preflight native |
| `ralfloop_agent/integration/collaboration_backend.py` | selector backend |
| `ralfloop_agent/models/result_envelope.py` | contratto payload |
| `ralfloop_agent/nodes/reasoning.py` | orchestrazione MVP |
| `ralfloop_agent/integration/capability_adapter.py` | compatibilita legacy |
| `tests/test_*` | test |

`src/recursive_mas.py` non esegue modelli e non esegue RecursiveLink: e' solo wrapper deprecato verso `text_mas_proxy`.

## Policy locale Ralfloop

Mapping pattern: policy locale, non regola del paper.

- `explicit_jury` -> `sequential`;
- `external_side_effect` -> `deliberation`;
- `patch_task` -> `sequential`;
- `multi_skill` -> `mixture`;
- `complexity_keyword` -> `sequential`;
- `style_selection_source=ralfloop_local_policy`.

Ordine side effect:

1. analisi e deliberazione;
2. verifica policy;
3. human confirmation;
4. esecuzione;
5. verifica risultato;
6. final review.

Nessun backend aggira conferma umana, sandbox o policy.

## RecursiveMAS native

Fonti primarie:

- <https://arxiv.org/abs/2604.25917>
- <https://github.com/RecursiveMAS/RecursiveMAS>
- <https://recursivemas.github.io/>

Native richiede:

- PyTorch/Transformers;
- input embeddings;
- ultimi hidden state;
- inner `Adapter`;
- outer `CrossModelAdapter`;
- ricorrenza tra agenti;
- nessuna decodifica testuale intermedia;
- decode solo finale.

Ollama HTTP `/api/generate` non espone input embeddings, hidden states o RecursiveLink. Quindi Ollama puo' essere solo `implementation_level=text_proxy`.

## Hardware

`sequential_light` include almeno:

- planner Qwen3 1.7B;
- critic Llama 3.2 1B;
- solver Qwen2.5 Math 1.5B;
- inner adapter;
- outer adapter;
- KV cache;
- attivazioni;
- overhead CUDA;
- margine operativo.

RTX 2070 8 GB e' probabilmente sotto soglia per `sequential_light` in CUDA, salvo ottimizzazioni non garantite. CPU puo' partire solo se RAM e checkpoint locali bastano. Nodo GPU remoto possibile, ma non attivato qui.

Probe corretto:

```bash
.venv/bin/python -m ralfloop_agent.integration.recursive_mas_native --probe --style sequential_light --device cuda
.venv/bin/python -m ralfloop_agent.integration.recursive_mas_native --probe --style sequential_light --device cpu
```

Il probe registra:

- `python_executable`;
- `virtualenv_active`;
- `dependency_versions`;
- `repository_status`;
- `checkpoint_status`;
- `cuda_probe_status`;
- RAM da `/proc/meminfo` con `MemFree` distinto da `MemAvailable`;
- `runtime_ready`.

`fits_cpu` usa `MemAvailable`, non `MemFree`. `fits_cuda` usa VRAM libera, non VRAM totale.

## LLM-as-Judge

Il judge non e' RecursiveMAS e non e' giuria.

Contratto minimo:

- `candidate_answer`;
- `user_goal`;
- `criteria`;
- `pass`;
- `score`;
- `reason`;
- `judge_provider`.

Ordine consigliato:

1. deterministic verifier;
2. rule verifier;
3. LLM-as-Judge solo per criteri qualitativi;
4. human confirmation per side effect;
5. transizione finale deterministica.

Il judge non autorizza side effect, non sostituisce test/exit code/soglie e non modifica policy.

## Runtime

- Nessuna attivazione nel runtime attivo.
- Nessun download automatico.
- Nessun modello caricato in import, route-only, startup FastAPI o manifest.
- Test reali con modelli: solo con `RALFLOOP_RECURSIVE_MAS_INTEGRATION=1`.
