# Teacher Bot-tazzi — red-team pedagogico hardened 2026-09-18

## Esecuzione
- casi: 25; turni: 77
- modello di controllo: qwen2.5:3b su inferenza locale CPU-only controllata
- superficie: Teacher MCP reale; DB temporaneo; Core MCP C temporaneo; Grammar MCP reale
- tool esposti: 13 (attesi 13)
- backend produzione osservato e non modificato: /home/sibilla-cumana/ralfloop-production/releases/13ac8d427d3d2f53260b6f875d8d43093df11bcb

## Rubrica automatica

| Dimensione | Pass | Totale | % |
|---|---:|---:|---:|
| actionable_feedback | 75 | 77 | 97.4% |
| brevity | 77 | 77 | 100.0% |
| clarity | 77 | 77 | 100.0% |
| conceptual_correctness | 77 | 77 | 100.0% |
| corrects_prior_simplification | 77 | 77 | 100.0% |
| counterexample_handling | 77 | 77 | 100.0% |
| diagnostic_capacity | 71 | 77 | 92.2% |
| level_adaptation | 77 | 77 | 100.0% |
| misconception_recognition | 77 | 77 | 100.0% |
| model_routing | 77 | 77 | 100.0% |
| multi_turn_continuity | 77 | 77 | 100.0% |
| no_early_solution | 77 | 77 | 100.0% |
| no_unnecessary_repetition | 77 | 77 | 100.0% |
| pedagogical_policy | 77 | 77 | 100.0% |
| responds_to_point | 77 | 77 | 100.0% |
| scaffolding | 77 | 77 | 100.0% |

## Failure per classe

| Classe | Occorrenze |
|---|---:|

## Casi con failure reali

| Caso | Persona | Turni falliti | Esempio |
|---|---|---:|---|

## Dettaglio dei failure più informativi

## Limiti del run hardened

- Le metriche lessicali sono conservative: segnalano candidati failure, non sostituiscono una revisione pedagogica umana.
- Il modello di prova è qwen2.5:3b CPU; il routing fast/deep viene verificato dalla decisione pedagogica, ma entrambi i percorsi usano la stessa classe di replica CPU in questo run isolato.
- Il corpus è avversariale e piccolo: serve come regression suite, non come stima della qualità media.
- La UI non è esercitata in questo blocco: il test passa dalla superficie MCP studente.
