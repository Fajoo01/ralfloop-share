from __future__ import annotations

import json
import re
import requests

OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b"


def _repair_invalid_json_escapes(text: str) -> str:
    # Rende JSON più tollerante per stringhe LLM che contengono escape regex
    # tipo \s, \d, \w, \S, ecc. non validi in JSON standard.
    out = []
    i = 0
    in_str = False
    escaped = False

    valid_json_escapes = set('"\\/bfnrtu')

    while i < len(text):
        ch = text[i]

        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
                escaped = False
            i += 1
            continue

        # siamo dentro stringa JSON
        if escaped:
            # se l'escape non è JSON-valido, raddoppia il backslash
            if ch not in valid_json_escapes:
                out.append('\\')
            out.append(ch)
            escaped = False
            i += 1
            continue

        if ch == '\\':
            out.append(ch)
            escaped = True
            i += 1
            continue

        out.append(ch)
        if ch == '"':
            in_str = False
        i += 1

    return ''.join(out)


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None

    fenced = text
    if fenced.startswith("```json"):
        fenced = fenced[len("```json"):].strip()
    elif fenced.startswith("```"):
        fenced = fenced[len("```"):].strip()
    if fenced.endswith("```"):
        fenced = fenced[:-3].strip()

    repaired_fenced = _repair_invalid_json_escapes(fenced)
    repaired_text = _repair_invalid_json_escapes(text)

    for candidate in (fenced, repaired_fenced, text, repaired_text):
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass

    m = re.search(r'\{.*\}', repaired_fenced, flags=re.DOTALL)
    if not m:
        m = re.search(r'\{.*\}', repaired_text, flags=re.DOTALL)
    if not m:
        return None

    raw_obj = m.group(0)
    repaired_obj = _repair_invalid_json_escapes(raw_obj)

    for candidate in (raw_obj, repaired_obj):
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass

    return None

def llm_judge_reusability(user_goal: str, final_answer: str, coder_text: str = "", model: str = DEFAULT_MODEL) -> dict | None:
    prompt = f"""You are a skill generalization judge and proposer.

Given:
- user request
- final answer
- optional code used to solve it

Decide whether the task is reusable as a general skill.

Return ONLY JSON with keys:
- is_reusable
- problem_type
- executor
- input_names
- match_regex
- template
- safety
- reason

Hard rules:
- Prefer true only for short deterministic reusable tasks.
- The task may still be reusable even if CODE_USED is empty.
- If the user request clearly describes a repeatable file/text/data transformation, you may propose a template from the request alone.
- No network access.
- No destructive commands.
- No system modification.
- Prefer python_inline for reusable transformations and calculations.
- Prefer false if the task is mostly explanation, opinion, browsing, or one-off context.

Good positive examples:
1) "scrivi uno script python che legge un file json e stampa le chiavi top-level"
2) "scrivi uno script python che somma i numeri in un file, uno per riga"

Additional hard rules for reusable file-processing tasks:
- Do NOT wrap template in markdown fences.
- Template MUST contain print(...).
- Do NOT hardcode placeholder paths like input.txt or path/to/your/file.txt.
- A request that contains a concrete file path is STILL reusable if the operation is generic.
- For file-processing tasks with a concrete path in the request, prefer:
  - input_names = ["file_path"]
  - match_regex = ".*file\\s+(\\S+)"
  - template uses "{{file_path}}" and NOT literal hardcoded filenames.
- If the request has no concrete filename and no filename placeholder, prefer is_reusable=false rather than inventing fake captures.
- match_regex groups count must equal input_names length exactly.

Good reusable example with concrete path:
- "somma i numeri nel file /tmp/numeri_test.txt"
  -> reusable because the operation is generic and the path is just a parameter

Good negative examples:
1) "spiegami la fotosintesi"
2) "trova lo stream di un canale tv"
3) "analizza questo testo e dimmi cosa ne pensi"

For reusable tasks:
- produce a concrete problem_type
- produce a usable match_regex
- produce input_names
- produce a short working template
- template must be consistent with FINAL_ANSWER or with the clearly requested reusable task

If not reusable:
- set is_reusable=false
- leave other fields empty or minimal

USER_REQUEST:
{user_goal}

FINAL_ANSWER:
{final_answer}

CODE_USED:
{coder_text}
"""
    try:
        r = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0,
                    "top_p": 1,
                    "repeat_penalty": 1
                }
            },
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        parsed = _extract_json(data.get("response") or "")
        return parsed
    except Exception as e:
        return None
