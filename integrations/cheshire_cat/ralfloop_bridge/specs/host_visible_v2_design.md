# Host-visible v2 design

## Problema attuale
Nel ramo host-visible la LLM continua a generare writer-script:
- open('/ralfloop_tmp/result.py', 'w')
- file.write(...)
- Path(...).write_text(...)

Questo produce:
- candidate semanticamente sbagliati
- retry che non converge
- result.py host-visible assente o non affidabile

## Principio v2
La LLM NON deve più generare codice che scrive result.py.
La LLM deve generare SOLO il contenuto finale del file result.py.
Il plugin deve:
1. ricevere il candidate python finale
2. salvarlo in /home/sibilla-cumana/ralfloop_tmp/result.py
3. salvare candidate_attempt_N.py negli artifact
4. restituire stdout con il path finale
5. opzionalmente validare sintassi/import base

## Contratto nuovo
### Input utente
"scrivi uno script python ... e salvi result.py in modo host-visible stampando il path finale"

### Output del coder
Solo contenuto finale di result.py, per esempio:
from sympy import symbols, Eq, solve
x = symbols('x')
equation = Eq(2*x + 5, 19)
solution = solve(equation, x)
print(solution[0])

### Output del plugin
- scrive quel contenuto nel file host-visible
- stdout: /home/sibilla-cumana/ralfloop_tmp/result.py

## Regole del gate
Per task host-visible, candidate da bocciare se contiene:
- /ralfloop_tmp/result.py
- with open(
- .write_text(
- file.write(
- os.makedirs('/ralfloop_tmp') se non serve
- testo descrittivo invece di Python finale

## Refactor minimo richiesto
1. nuovo helper:
   _extract_final_python_content_for_resultpy(...)
2. _run_python_validation_host_visible:
   - scrive direttamente il candidate nel file host-visible
   - non esegue writer-script
3. _needs_success_retry:
   - boccia ogni writer-script
4. coder prompt:
   - ribadire che il candidate è il contenuto finale del file

## Criteri di successo
- candidate_attempt_1.py senza writer-script
- se candidate 1 è writer-script, retry attivo
- candidate_attempt_2.py senza writer-script
- /home/sibilla-cumana/ralfloop_tmp/result.py presente
- result.py uguale al candidate finale
