# Teacher red-team — fonti e decisioni 2026-09-18

## Fonti verificate

- TutorMoments / Ai2: replay di momenti reali, scaffolding vs rigor, controllo dell'over-scaffolding, action taxonomy, student trait congelato e misure cold/warm separate.
  - https://github.com/allenai/tutormoments
  - https://huggingface.co/datasets/allenai/tutormoments-preview
- MRBench / Unifying AI Tutor Evaluation: separa mistake identification, mistake location, revealing answer, guidance, actionability, coherence, tone e humanlikeness.
  - https://github.com/kaushal0494/UnifyingAITutorEvaluation
  - https://aclanthology.org/2025.naacl-long.57/
- MRBench V2 psychometrics (PMLR 2026): evitare di comprimere automaticamente tutte le dimensioni in un singolo score; alcune dimensioni non si comportano come previsto.
  - https://proceedings.mlr.press/v339/sharma26a.html
- SafeTutors: il danno pedagogico include over-disclosure, reinforcement della misconception e rinuncia allo scaffolding; i failure crescono fortemente nel multi-turno.
  - https://github.com/RadiantCrystal/SafeTutors
  - https://arxiv.org/abs/2603.17373

- MathDial: dialoghi ancorati a problemi, student incorrect solution e profili di misconception; utile per casi di regola meccanica, errore persistente e mosse Focus/Probing/Telling/Generic.
  - https://huggingface.co/datasets/eth-nlped/mathdial
- StudentSim: due proprietà da preservare nelle personas sintetiche: behavioral fidelity e guidance responsiveness.
  - https://github.com/microsoft/StudentSim
- llama.cpp: gli slot paralleli dividono il decode; il prefisso ripetuto va trattato come cache warm, non ricomputato concettualmente come nuovo test.
  - https://github.com/ggml-org/llama.cpp/discussions/22401
  - https://github.com/ggml-org/llama.cpp/discussions/24390

## Adattamento a Bot-tazzi

1. Corpus congelato e versionato, non student simulator generativo durante la baseline.
2. Persona stabile per tutto il caso, con misconception che non scompare magicamente dopo una risposta.
3. Conversazioni 3-4 turni: obiezione -> correzione -> secondo caso limite -> teach-back o errore residuo.
4. Rubrica multidimensionale; nessun singolo score usato come gate assoluto.
5. Failure deterministici prima: tool routing, turn classification, solution leakage, mode/model path, brevità.
6. Valutazione semantica dopo: targetedness, mistake location, scaffolding, actionability, continuity.
7. Cold start e turni warm annotati separatamente nei risultati di latenza.
8. Il corpus include discipline non matematiche per evitare overfitting a MathDial/MRBench.
9. Nessun fix pedagogico durante la baseline: i failure osservati diventano regression test solo nel ciclo successivo.

## Banco prova CPU: intoppi e soluzione

- Tentativo `llama-server --parallel 4 --threads 12`: con quattro richieste simultanee il prefill è sceso fino a pochi token/s per slot e le richieste hanno superato il timeout Teacher. Risultati scartati.
- Tentativo `--parallel 2 --threads 12`: stabile ma decode spesso 1-6 token/s; troppo lento.
- Microbenchmark sullo stesso GGUF già servito da `ralf-qwen-small` (`--threads 4 --parallel 1`):
  - prompt Teacher reale, 1438 token;
  - 80 token output: 7.5 s cold / 6.8 s warm;
  - decode osservato ~10.9 token/s;
  - cache hit 1432-1437/1438 prompt token.
- Configurazione baseline definitiva: due processi server indipendenti, stesso GGUF Qwen 2.5 3B, 4 thread e 1 slot ciascuno (`19110`, `19113`).
- Budget adattivo: fast 220 -> 320 solo se `finish_reason=length`; deep 320 -> 480. Nessuna modifica al prompt Teacher.
- `cache_prompt=true`; ogni lane ha il proprio slot/cache.
- Il runner tratta ora ogni `structuredContent.ok != true` come errore infrastrutturale, non come risposta pedagogica vuota.
- DS4 `19194`/`19196` mai terminati o sospesi.
