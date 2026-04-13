# Handoff grammar RAG scaffold

## Stato
Scaffold minimale funzionante per analisi grammaticale guidata da manuale locale tipo RAG.

## File
- openshell_backend/rag_manual.py
- openshell_backend/skill_grammar_rag.py
- openshell_backend/rag_manuals/grammatica_italiana/manuale_base.md
- hook in openshell_backend/common_router.py

## Richieste intercettate
- Fai l'analisi grammaticale della frase: ...
- Analisi grammaticale: ...
- Analizza grammaticalmente: ...

## Output
Array JSON con:
- token
- categoria

## Test riusciti
- Il gatto nero corre veloce.
- La bambina legge un libro.

## Limiti attuali
- retrieval locale molto semplice
- non entra ancora nel sistema candidate/promoted
- matching richieste ancora stretto
- output JSON, non ancora formattazione user-friendly

## Prossimi passi
1. test via Telegram
2. allargare i pattern di linguaggio naturale
3. decidere se questa famiglia deve restare route-specializzata o entrare nel sistema skill candidate
4. aggiungere formatter user-friendly sopra il JSON
