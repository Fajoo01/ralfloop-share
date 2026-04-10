# Overview architettura Ralfloop + Cheshire

## Obiettivo
Usare Cheshire Cat come cervello principale per task tecnici, con tre ruoli:
- planner
- coder
- judge

Il backend esterno deve servire soprattutto come braccio operativo:
- sandbox
- exec/read/write/list
- probe_stream
- file temporanei

## Flusso corretto
1. L'utente scrive in chat.
2. Cheshire Cat intercetta la richiesta tramite plugin ralfloop_bridge.
3. Il plugin legge i settings locali:
   - planner_model_name
   - coder_model_name
   - judge_model_name
   - planner_rag_collection
   - coder_rag_collection
   - judge_rag_collection
4. Il plugin esegue tre passaggi LLM:
   - planner
   - coder
   - judge
5. Se serve esecuzione tecnica, il plugin usa backend OpenShell.
6. I file finali vengono salvati in una cartella temporanea host-visible.

## Stato desiderato
- LLM evocato dal plugin Cheshire, non dal backend
- RAG separata per ruolo
- File temporanei accessibili dall'host
- TTL 24h sui file temporanei

## Stato attuale voluto
- planner: modello generalista
- coder: modello coding
- judge: modello generalista
