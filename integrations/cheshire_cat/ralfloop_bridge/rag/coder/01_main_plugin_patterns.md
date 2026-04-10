# Pattern utili per main_plugin.py

## Helper principali
- _load_plugin_settings(cat)
- _role_config(settings, role)
- _make_task_dir(settings)
- _save_temp_artifact(task_dir, name, content)
- _ollama_generate(model, prompt)
- _planner_prompt(...)
- _coder_prompt(...)
- _judge_prompt(...)
- _role_rag_text(...)

## Pattern output file
Se output finale è testo:
- salva result.txt

Se output finale è un blocco python:
- salva result.txt
- estrai anche result.py

Se serve debugging:
- salva planner.txt
- salva coder.txt
- salva meta.txt

## Scrittura risposta finale
Preferire:
- message.text = final
mantenendo eventualmente anche content come fallback.

## Regola pratica
Il plugin deve generare il file utile direttamente in temp_output_dir host-visible.
Non affidarsi alla sandbox per i file finali se non serve.
