# Ralf llama.cpp final deployment

All Sibilla operations use `/home/bandi/bin/vpnpc`. The templates contain no
secrets. Production installation requires `vpnpc sudo sibilla --non-interactive`.

Runtime policy:

- chat defaults to managed `llama_cpp` with lazy autostart;
- `/tasks/run` stops only the validated managed child, then uses Ollama;
- agent cleanup unloads only task models absent before the task;
- the next chat restarts llama.cpp lazily;
- `ngram-simple`, speculative decoding and remote drafting remain disabled.

`rollback.sh BACKUP_DIR` must be invoked as root through `vpnpc`. If no previous
environment existed, rollback writes only `RALF_CHAT_PROVIDER=ollama`, keeping
the dedicated drop-in so the source default cannot override rollback safety.
