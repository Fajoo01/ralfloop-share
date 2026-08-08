# Ralf fast-chat con llama.cpp

`llama_cpp` è un provider opt-in per `POST /chat`, `POST /chat/stream`, `ralf`,
`ralf ask` e `ralf chat`. Il default iniziale resta `ollama`. `ralf agent`
resta sul workflow Ollama via `/tasks/run`.

Il provider usa direttamente il GGUF Qwen 2.5 7B Q4_K_M già posseduto da
Ollama. Non copia né converte il file. Configurazione predefinita:

```text
RALF_CHAT_PROVIDER=ollama
RALF_LLAMA_CPP_BASE_URL=http://127.0.0.1:19091
RALF_LLAMA_CPP_TIMEOUT=180
RALF_LLAMA_CPP_IDLE_TIMEOUT=60
RALF_LLAMA_CPP_CONTEXT=4096
RALF_LLAMA_CPP_GPU_LAYERS=24
RALF_LLAMA_CPP_THREADS=6
RALF_LLAMA_CPP_SLOTS=1
RALF_LLAMA_CPP_CACHE_PROMPT=1
RALF_LLAMA_CPP_AUTOSTART=0
RALF_LLAMA_CPP_FALLBACK=ollama
```

Il server user-space usa `127.0.0.1`, uno slot e prompt cache. Non abilita
`ngram-simple`, draft model o speculative decoding. Autostart è disabilitato:

```bash
ralf engine status
ralf engine start llama_cpp
ralf engine health llama_cpp
ralf ask --provider llama_cpp "Rispondi soltanto con LLAMA_CPP_RALF_OK"
ralf engine stop llama_cpp
```

Equivalente launcher:

```bash
scripts/ralf_llama_cpp_engine.sh status
scripts/ralf_llama_cpp_engine.sh start
scripts/ralf_llama_cpp_engine.sh health
scripts/ralf_llama_cpp_engine.sh stop
```

Il manager verifica hash GGUF, stato GPU Ollama, lock advisory, PID, ownership e
command line. Non arresta Ollama e termina soltanto il processo registrato.
Ollama e llama.cpp non devono tenere modelli contemporaneamente in VRAM. Prima
di usare `ralf agent`, fermare esplicitamente llama.cpp; non esiste kill
automatico.

Il fallback ammesso è `llama_cpp` fast-chat verso Ollama fast-chat, mai verso
`/tasks/run`. Non avviene dopo il primo token. Gate Telegram, capability e
approval restano deterministici ed esterni al modello.
