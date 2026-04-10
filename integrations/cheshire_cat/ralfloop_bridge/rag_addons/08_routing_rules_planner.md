role: planner

# Routing rules planner

## Quando usare calculation
Usare task_type=calculation e output_format=python per:
- equazioni
- derivate
- integrali
- sistemi
- percentuali
- media
- deviazione standard
- calcoli verificabili via Python

## Quando usare shell_commands
Usare shell_commands per:
- richieste di comandi bash/shell
- file temporanei shell
- task CLI

## Quando NON forzare il loop
Non forzare planner/coder/judge per:
- chat generica
- spiegazioni normali
- scrittura libera non verificabile
