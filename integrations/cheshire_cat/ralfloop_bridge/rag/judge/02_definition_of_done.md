# Definition of done

Un task è davvero finito quando:

1. Il ruolo planner ha prodotto un piano sensato.
2. Il ruolo coder ha prodotto un output tecnico coerente.
3. Il ruolo judge ha pulito e validato il risultato.
4. Il file finale è salvato in una directory host-visible.
5. Se il risultato è codice Python, esiste result.py.
6. meta.txt contiene audit_summary e role_history.
7. Il modello giusto è visibile nell'audit per ogni ruolo.
8. L'utente può recuperare il file senza entrare nel container.
9. Il file temporaneo è sotto una directory con TTL 24h.
10. Non ci sono spiegazioni inutili se l'utente vuole solo il file o il codice.

## Example of acceptable final code for "save result.py"
A good final answer is not an empty file writer.
It should:
- use exactly /ralfloop_tmp
- create the directory if needed
- write meaningful Python content into result.py
- print the final file path

Example:

from pathlib import Path

temp_dir = Path("/ralfloop_tmp")
temp_dir.mkdir(parents=True, exist_ok=True)

file_path = temp_dir / "result.py"
file_path.write_text('print("ciao")\n', encoding="utf-8")

print(file_path)

## Execution-based validation
For Python code tasks:
- prefer code that actually runs
- use execution feedback if available
- reject code that fails at runtime when a corrected version can be produced
- prefer a second corrected attempt over a pretty but broken answer
