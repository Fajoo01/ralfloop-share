# Convenzioni file output

## Struttura
<temp_output_dir>/<task-id>/out/

## File tipici
- result.txt -> output finale mostrabile
- result.py -> solo se il risultato è un blocco python
- planner.txt -> output planner
- coder.txt -> output coder
- meta.txt -> metadati task

## Contenuto meta.txt
- task_id
- role_history
- stop_reason
- audit_lines
- audit_summary

## Regole
- se il coder produce codice python in blocco markdown, estrarre il body in result.py
- se il risultato è json, considerare anche result.json
- se il risultato è html, considerare anche result.html
- mantenere sempre result.txt come fallback umano leggibile

## Obiettivo
L'utente deve poter recuperare subito il file utile dall'host senza docker cp.

## Example for tasks asking to save result.py
If the user asks for a script that saves result.py, the script should usually:
- create /ralfloop_tmp if needed
- write meaningful Python code into result.py
- print the resulting file path

Bad:
with open('/ralfloop_tmp/result.py', 'w') as file:
    file.write('')

Good:
from pathlib import Path

temp_dir = Path("/ralfloop_tmp")
temp_dir.mkdir(parents=True, exist_ok=True)

file_path = temp_dir / "result.py"
file_path.write_text('print("ciao")\\n', encoding="utf-8")

print(file_path)

## Formatting quality for generated file content
When generating Python code to be written into result.py:
- prefer runnable content
- include a final newline
- avoid empty placeholder content when the user expects a meaningful example
