role: coder
role: judge

# Anti-pattern host-visible result.py

## Anti-pattern 1
Non generare script che scrivono un altro result.py.

Sbagliato:
with open('/ralfloop_tmp/result.py', 'w') as file:
    file.write("print('ciao')")
print('/ralfloop_tmp/result.py')

Motivo:
- questo è un writer-script, non il contenuto finale di result.py

## Anti-pattern 2
Non scrivere testo descrittivo dentro result.py.

Sbagliato:
file.write("The solution is x = 7")

Motivo:
- result.py deve essere Python eseguibile

## Anti-pattern 3
Non considerare stabile il ramo host-visible nella baseline corrente.

Regola:
- ogni esperimento host-visible deve stare su copia separata del file
