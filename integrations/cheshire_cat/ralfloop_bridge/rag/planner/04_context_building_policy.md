# Context building policy

## Obiettivo
Costruire un contesto breve, mirato e utile per ruolo.

## Regole
1. Prima overview, poi dettaglio locale.
2. Non mandare file interi se bastano firme e chunk rilevanti.
3. Mettere i vincoli importanti all'inizio del prompt.
4. Limitare il contesto totale a pochi blocchi buoni.
5. Preferire:
   - file planner: architettura e strategia
   - file coder: pattern tecnici e path reali
   - file judge: checklist e anti-pattern

## Priorità contenuto
- richiesta utente
- skill_context
- estratti RAG del ruolo
- output del ruolo precedente

## Anti-pattern
- dump completo del repo
- log enormi
- duplicati
- storia intera della chat
