# NEXT_STEP_SHELL_EXACT_OUTPUT
# HOST_VISIBLE_BRANCH_EXPERIMENT
import re
import requests
import subprocess
from pathlib import Path

OLLAMA_BASE_URL = "http://127.0.0.1:11434"

def _ollama_generate(model: str, prompt: str, timeout_sec: int = 120) -> str:
    r = requests.post(
        f"{OLLAMA_BASE_URL}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
        },
        timeout=timeout_sec,
    )
    r.raise_for_status()
    data = r.json()
    return (data.get("response") or "").strip()

def _planner_prompt(user_text: str, skill_context: str, rag_text: str = "") -> str:
    return f"""You are the PLANNER.

User goal:
{user_text}

Skill context:
{skill_context}

RAG context:
{rag_text}

Decide the next step.
Return ONLY JSON with these keys:
- intent
- task_type
- needs_tool
- tool_kind
- coder_brief
- judge_criteria
- output_format

Allowed task_type values:
- python_code
- shell_commands
- calculation
- structured_text
- generic_text

Allowed output_format values:
- python
- shell
- plain_text
- json
- markdown

Rules:
- Be concise.
- Do not write code.
- Do not add explanations outside JSON.
- If the task mentions the host-visible temp folder of plugin ralfloop_bridge, remember the correct path is exactly:
  /ralfloop_tmp
"""

def _coder_prompt(user_text: str, planner_text: str, skill_context: str, rag_text: str = "") -> str:
    return f"""You are the CODER.

User goal:
{user_text}

Planner output:
{planner_text}

Skill context:
{skill_context}

RAG context:
{rag_text}

Rules:
- If the task requires Python, return ONLY raw executable Python code.
- No markdown fences.
- No explanations.
- No text before or after the code.
- No usage instructions.
- No titles.
  - Do not invent subfolders unless explicitly requested.
  - If the task mentions a specific path, use exactly that path.
  - If the task explicitly mentions the host-visible temp folder of plugin ralfloop_bridge, that path is exactly:
    /ralfloop_tmp
  - For generic shell temporary files inside the sandbox, prefer /tmp or: FILE=$(mktemp)
  - Never use /ralfloop_tmp for generic shell temp files unless the user explicitly requested that exact path.
  - If asked to save result.py, create or overwrite that file directly.
  - Do not use tempfile.gettempdir() in Python unless explicitly required.
  - Do not use os.environ["TEMP"] unless explicitly required.
  - Do not invent host-only or non-portable paths.
  - Do not copy result.py from an implicit source file unless the user explicitly asked for a copy operation.
"""

def _judge_prompt(user_text: str, planner_text: str, coder_text: str, rag_text: str = "") -> str:
    return f"""You are the JUDGE.

User goal:
{user_text}

Planner output:
{planner_text}

Coder output:
{coder_text}

RAG context:
{rag_text}

Strict validation rules:
- Return ONLY the final corrected raw executable Python code.
- No markdown fences.
- No explanations.
- No comments before or after the code.
  - If the task explicitly mentions the host-visible temp folder of plugin ralfloop_bridge, the correct host-visible path is EXACTLY:
    /ralfloop_tmp
  - For generic shell temp files inside the sandbox, prefer /tmp or mktemp.
  - Do not add invented subfolders such as host_visible, output, files, or similar unless explicitly requested.
  - Reject tempfile.gettempdir() only when it conflicts with an explicitly requested target path.
  - Reject os.environ["TEMP"] only when it conflicts with an explicitly requested target path.
  - Do not reject generic system temp paths for shell tasks.
  - If the coder used a host-only or non-portable path for a shell temp file, correct it to /tmp or mktemp.
- If the task asks to save result.py, the code must create or write that file directly, not copy it from an implicit source.
- Reject trivial empty-file solutions like file.write('') or equivalent unless the user explicitly asked for an empty file.
- Prefer meaningful example content such as print("ciao") when the user did not provide file contents.
- Reject shutil.copy(Path("result.py"), ...) unless the user explicitly asked for copying.
- Strip all prose completely.
- Output only the corrected final code.
"""

def _extract_signatures(text: str, max_lines: int = 80) -> str:
    lines = text.splitlines()
    out = []
    for line in lines:
        s = line.strip()
        if (
            s.startswith("def ")
            or s.startswith("class ")
            or s.startswith("async def ")
            or s.startswith("# ")
            or s.startswith("## ")
        ):
            out.append(line)
        if len(out) >= max_lines:
            break
    return "\n".join(out).strip()

def _trim_text(text: str, max_chars: int = 2200) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return head + "\n...\n" + tail




def _planner_field(planner_text: str, key: str) -> str:
    try:
        import json
        txt = planner_text.strip()
        if txt.startswith("```json"):
            txt = txt[len("```json"):].strip()
        elif txt.startswith("```"):
            txt = txt[len("```"):].strip()
        if txt.endswith("```"):
            txt = txt[:-3].strip()
        data = json.loads(txt)
        value = data.get(key, "")
        return value if isinstance(value, str) else ""
    except Exception:
        return ""

def _extract_python_only(text: str) -> str:
    text = text.strip()
    if text.startswith("```python"):
        text = text[len("```python"):].strip()
    elif text.startswith("```"):
        text = text[len("```"):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def _extract_shell_only(text: str) -> str:
    text = text.strip()
    if text.startswith("```shell"):
        text = text[len("```shell"):].strip()
    elif text.startswith("```bash"):
        text = text[len("```bash"):].strip()
    elif text.startswith("```sh"):
        text = text[len("```sh"):].strip()
    elif text.startswith("```"):
        text = text[len("```"):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text

def _should_validate_shell(user_text: str, planner_text: str, coder_text: str) -> bool:
    task_type = _planner_field(planner_text, "task_type").strip().lower()
    output_format = _planner_field(planner_text, "output_format").strip().lower()
    if task_type == "shell_commands" or output_format == "shell":
        return True

    txt = (user_text + "\n" + planner_text + "\n" + coder_text).lower()
    triggers = [
        "shell",
        "bash",
        "terminale",
        "comandi",
        '"task_type": "shell_commands"',
        '"output_format": "shell"',
    ]
    return any(x in txt for x in triggers)


def _run_shell_validation(task_dir: Path, shell_text: str, attempt: int = 1, sandbox_id: str | None = None) -> dict:
    out_dir = task_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"candidate_attempt_{attempt}.sh"
    script = "set -euo pipefail\n" + shell_text.strip() + "\n"
    candidate = out_dir / filename
    candidate.write_text(script, encoding="utf-8")

    created_here = sandbox_id is None
    sbid = sandbox_id or _create_sandbox()["id"]
    try:
        _post(f"/sandboxes/{sbid}/write", {
            "path": f"out/{filename}",
            "content": script,
        })
        data = _post(
            f"/sandboxes/{sbid}/exec",
            {"command": f"bash out/{filename}", "timeout_sec": 30},
        )
        return {
            "ok": data.get("exit_code", 1) == 0,
            "exit_code": data.get("exit_code", 1),
            "stdout": data.get("stdout", "") or "",
            "stderr": data.get("stderr", "") or "",
            "candidate_path": str(candidate),
        }
    finally:
        if created_here:
            _destroy_sandbox(sbid)

def _should_validate_python(user_text: str, planner_text: str, coder_text: str) -> bool:
    task_type = _planner_field(planner_text, "task_type").strip().lower()
    output_format = _planner_field(planner_text, "output_format").strip().lower()
    txt = (user_text + "\n" + planner_text + "\n" + coder_text).lower()

    shell_markers = [
        "```sh",
        "```bash",
        "```shell",
        " script shell",
        " script bash",
        " script sh",
        '"task_type": "shell"',
        '"output_format": "shell"',
    ]
    if any(x in txt for x in shell_markers):
        return False

    if task_type == "python_code" or output_format == "python":
        return True

    triggers = [
        "python",
        '"task_type": "python_code"',
        '"output_format": "python"',
    ]
    return any(x in txt for x in triggers)


def _sandbox_create_candidate(task_dir: Path, filename: str, content: str) -> Path:
    out_dir = task_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate = out_dir / filename
    candidate.write_text(content, encoding="utf-8")
    return candidate

def _sandbox_exec_local_fallback(cmd: list[str], cwd: Path) -> dict:
    r = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=8,
    )
    return {
        "ok": r.returncode == 0,
        "exit_code": r.returncode,
        "stdout": r.stdout,
        "stderr": r.stderr,
    }


def _sandbox_exec_via_openshell(filename: str, command: str, content: str, sandbox_id: str | None = None) -> dict:
    try:
        sandbox_root = "/tmp"
        write_payload = {
            "sandbox_id": sandbox_id,
            "path": f"{sandbox_root}/{filename}",
            "content": content,
        }
        wr = requests.post(
            f"{RALFLOOP_BASE_URL}/custom/ralfloop/write-file",
            json=write_payload,
            timeout=20,
        )
        wr.raise_for_status()

        exec_payload = {
            "sandbox_id": sandbox_id,
            "command": f"cd /tmp && {command}",
            "timeout_sec": 20,
        }
        ex = requests.post(
            f"{RALFLOOP_BASE_URL}/custom/ralfloop/exec",
            json=exec_payload,
            timeout=20,
        )
        ex.raise_for_status()
        data = ex.json()

        return {
            "ok": bool(data.get("ok_exec", data.get("ok", False))),
            "exit_code": int(data.get("exit_code", 0) or 0),
            "stdout": data.get("stdout", "") or "",
            "stderr": data.get("stderr", "") or "",
        }
    except Exception as e:
        return {
            "ok": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": f"OPENSHELL_ERROR: {e}",
        }


def _run_python_validation(task_dir: Path, code: str, attempt: int = 1, sandbox_id: str | None = None) -> dict:
    out_dir = task_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"candidate_attempt_{attempt}.py"
    content = code.strip() + "\n"
    candidate = out_dir / filename
    candidate.write_text(content, encoding="utf-8")

    created_here = sandbox_id is None
    sbid = sandbox_id or _create_sandbox()["id"]
    try:
        _post(f"/sandboxes/{sbid}/write", {
            "path": f"out/{filename}",
            "content": content,
        })
        data = _post(
            f"/sandboxes/{sbid}/exec",
            {"command": f"python3 out/{filename}", "timeout_sec": 30},
        )
        return {
            "ok": data.get("exit_code", 1) == 0,
            "exit_code": data.get("exit_code", 1),
            "stdout": data.get("stdout", "") or "",
            "stderr": data.get("stderr", "") or "",
            "candidate_path": str(candidate),
        }
    finally:
        if created_here:
            _destroy_sandbox(sbid)

def _extract_expected_exact_output(user_text: str) -> str | None:
    if not user_text:
        return None
    text = user_text.strip()

    patterns = [
        r"(?is)stampi\s+esattamente\s*:\s*(.+?)\s*$",
        r"(?is)stampa\s+esattamente\s*:\s*(.+?)\s*$",
        r"(?is)stampi\s+esattamente\s+(.+?)\s*$",
        r"(?is)stampa\s+esattamente\s+(.+?)\s*$",
        r"(?is)printi\s+esattamente\s+(.+?)\s*$",
    ]

    for pat in patterns:
        m = re.search(pat, text)
        if m:
            expected = m.group(1).strip()
            expected = expected.strip('`')
            if (
                (expected.startswith('"') and expected.endswith('"')) or
                (expected.startswith("'") and expected.endswith("'"))
            ):
                expected = expected[1:-1]
            return expected.strip()

    return None


def _keyword_score(text: str, words: list[str]) -> int:
    low = text.lower()
    return sum(low.count(w) for w in words if w)

def _cheshire_declarative_points() -> list[dict]:
    try:
        r = requests.get(f"{CAT_PUBLIC_BASE_URL}/memory/collections/declarative/points", timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            pts = data.get("points")
            if isinstance(pts, list):
                return pts
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        print(f"[RALFLOOP] cheshire declarative fetch failed: {e}", flush=True)
        return []

def _cheshire_role_chunks(role: str) -> list[dict]:
    out = []
    for p in _cheshire_declarative_points():
        payload = p.get("payload", {}) if isinstance(p, dict) else {}
        text = payload.get("page_content", "")
        if not isinstance(text, str) or not text.strip():
            continue
        head = text[:120].lower()
        if f"role: {role}" in head or text.lower().startswith(f"role: {role}"):
            out.append({
                "id": p.get("id"),
                "text": text,
                "source": (payload.get("metadata", {}) or {}).get("source", "")
            })
    return out

def _role_rag_text_cheshire(role: str, user_text: str, max_chunks: int = 4) -> str:
    words = [w for w in re.findall(r"[a-zA-Z0-9_]{3,}", user_text.lower()) if len(w) >= 3]
    chunks = _cheshire_role_chunks(role)
    if not chunks:
        return ""
    scored = []
    for c in chunks:
        text = c["text"]
        score = _keyword_score(text, words)
        low = user_text.lower()
        if role == "coder":
            if any(k in low for k in ["shell", "bash", "terminale", "comandi"]):
                if "mktemp" in text or "/tmp" in text:
                    score += 4
                if "/ralfloop_tmp" in text and "shell" in text.lower():
                    score -= 2
            if any(k in low for k in ["result.py", "host-visible", "artifact", "salva file"]):
                if "/ralfloop_tmp" in text:
                    score += 4
        scored.append((score, c["source"], text))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = scored[:max_chunks]
    if all(score == 0 for score, _, _ in picked):
        picked = scored[:1]
    parts = []
    for _, source, text in picked:
        body = text
        if role == "coder":
            body = _extract_signatures(text, max_lines=120) or text
        body = _trim_text(body, max_chars=2200)
        parts.append(f"### {source or 'memory'}\n{body}")
    return "\n\n".join(parts).strip()

def _role_rag_dir(role: str) -> Path:
    return Path("/app/cat/plugins/ralfloop_bridge/rag") / role

def _role_rag_text(cat, settings, role: str, user_text: str) -> str:
    cheshire = _role_rag_text_cheshire(role, user_text)
    if cheshire.strip():
        print(f"[RALFLOOP] using Cheshire RAG for role={role} len={len(cheshire)}", flush=True)
        return cheshire

    base = _role_rag_dir(role)
    if not base.exists():
        return ""

    words = [w for w in re.findall(r"[a-zA-Z0-9_]{3,}", user_text.lower()) if len(w) >= 3]
    candidates = []

    for fp in sorted(base.glob("*.md")):
        try:
            raw = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        score = _keyword_score(raw, words)
        if fp.name.startswith(("00_", "01_")):
            score += 2
        candidates.append((score, fp.name, raw))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: (-x[0], x[1]))
    picked = candidates[:3]
    if all(score == 0 for score, _, _ in picked):
        picked = candidates[:1]

    parts = []
    for _, name, raw in picked:
        body = raw
        if role == "coder":
            body = _extract_signatures(raw, max_lines=120) or raw
        body = _trim_text(body, max_chars=2200)
        parts.append(f"### {name}\n{body}")

    return "\n\n".join(parts).strip()




import uuid

def _load_plugin_settings(cat):
    try:
        return cat.mad_hatter.get_plugin().load_settings()
    except Exception:
        return {
            "planner_model_profile": "generalist",
            "coder_model_profile": "coder",
            "judge_model_profile": "generalist",
            "planner_rag_collection": "ralfloop_planner",
            "coder_rag_collection": "ralfloop_coder",
            "judge_rag_collection": "ralfloop_judge",
            "temp_output_dir": "/ralfloop_tmp",
        }

def _role_config(settings, role: str):
    if role == "planner":
        return {
            "model_profile": settings.get("planner_model_profile", "generalist"),
            "model_name": settings.get("planner_model_name", "qwen2.5:7b"),
            "rag_collection": settings.get("planner_rag_collection", "ralfloop_planner"),
        }
    if role == "coder":
        return {
            "model_profile": settings.get("coder_model_profile", "coder"),
            "model_name": settings.get("coder_model_name", "qwen2.5-coder:7b"),
            "rag_collection": settings.get("coder_rag_collection", "ralfloop_coder"),
        }
    return {
        "model_profile": settings.get("judge_model_profile", "generalist"),
        "model_name": settings.get("judge_model_name", "qwen2.5:7b"),
        "rag_collection": settings.get("judge_rag_collection", "ralfloop_judge"),
    }

def _make_task_dir(settings):
    base = Path(settings.get("temp_output_dir", "/ralfloop_tmp"))
    task_id = str(uuid.uuid4())
    task_dir = base / task_id
    (task_dir / "out").mkdir(parents=True, exist_ok=True)
    (task_dir / "meta").mkdir(parents=True, exist_ok=True)
    return task_id, task_dir

def _save_temp_artifact(task_dir: Path, name: str, content: str):
    out = task_dir / "out" / name
    out.write_text(content, encoding="utf-8")
    return str(out)



import os
from datetime import datetime

from cat.mad_hatter.decorators import hook

RALFLOOP_BASE_URL = os.getenv("RALFLOOP_BASE_URL", "http://127.0.0.1:19090").rstrip("/")
CAT_PUBLIC_BASE_URL = os.getenv("CAT_PUBLIC_BASE_URL", "http://127.0.0.1:1865").rstrip("/")

PLUGIN_DIR = Path(__file__).resolve().parent
GENERATED_DIR = PLUGIN_DIR / "generated_calcs"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
SKILLS_BASE_DIR = Path("/home/sibilla-cumana/ralfloop_data/skills")

TASK_SANDBOXES: dict[str, str] = {}


def _post(path: str, payload: dict | None = None):
    r = requests.post(f"{RALFLOOP_BASE_URL}{path}", json=payload or {}, timeout=90)
    r.raise_for_status()
    return r.json()


def _delete(path: str):
    r = requests.delete(f"{RALFLOOP_BASE_URL}{path}", timeout=60)
    r.raise_for_status()
    return r.json()


def _create_sandbox():
    return _post("/sandboxes")


def _destroy_sandbox(sid: str):
    return _delete(f"/sandboxes/{sid}")


def _persist_local_calc(code: str, prefix: str = "calc") -> tuple[str, str]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{ts}.py"
    path = GENERATED_DIR / filename
    path.write_text(code, encoding="utf-8")
    url = f"{CAT_PUBLIC_BASE_URL}/custom/ralfloop/download-calc/{filename}"
    return filename, url


def _run_python(code: str, prefix: str = "calc", sandbox_id: str | None = None) -> tuple[str, str]:
    _, url = _persist_local_calc(code, prefix=prefix)
    created_here = sandbox_id is None
    sbid = sandbox_id or _create_sandbox()["id"]
    try:
        _post(f"/sandboxes/{sbid}/write", {"path": "out/calc.py", "content": code})
        data = _post(
            f"/sandboxes/{sbid}/exec",
            {"command": "python3 out/calc.py", "timeout_sec": 30},
        )
        stdout = (data.get("stdout") or "").strip()
        stderr = (data.get("stderr") or "").strip()
        if data.get("exit_code", 1) != 0:
            return f"ERRORE\n{stderr or stdout}", url
        return stdout, url
    finally:
        if created_here:
            _destroy_sandbox(sbid)




def _load_skill_for_request(user_text: str) -> str:
    low = user_text.lower()
    skill_map = [
        (["stream_scraper", "mediaset", "resolver", "scraping tv", "scraper tv", "flusso tv", "canale5", "rai"], "tv/stream_scraper_debug.md"),
    ]
    for triggers, rel_path in skill_map:
        if any(t in low for t in triggers):
            path = SKILLS_BASE_DIR / rel_path
            if path.exists():
                try:
                    return path.read_text(encoding="utf-8")
                except Exception:
                    return ""
    return ""

def _extract_python_block(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"```python\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*(.*?)```", text, flags=re.DOTALL)
    if m:
        candidate = m.group(1).strip()
        if "print(" in candidate or "import " in candidate:
            return candidate
    return None


def _safe_prefix(text: str) -> str:
    t = re.sub(r"[^a-zA-Z0-9_]+", "_", text.lower()).strip("_")
    return t[:40] or "calc"


def _load_plugin_settings(cat):
    try:
        return cat.mad_hatter.get_plugin().load_settings()
    except Exception:
        return {
            "planner_model_profile": "generalist",
            "coder_model_profile": "coder",
            "judge_model_profile": "generalist",
        }


def _call_ralfloop_task(cat, user_text: str, skill_context: str = ""):
    settings = _load_plugin_settings(cat)
    payload = {
        "user_goal": user_text,
        "mode": "planner_coder_judge",
        "planner_model_profile": settings.get("planner_model_profile", "generalist"),
        "coder_model_profile": settings.get("coder_model_profile", "coder"),
        "judge_model_profile": settings.get("judge_model_profile", "generalist"),
        "planner_model_name": settings.get("planner_model_name", "qwen2.5:7b"),
        "coder_model_name": settings.get("coder_model_name", "qwen2.5:7b"),
        "judge_model_name": settings.get("judge_model_name", "qwen2.5:7b"),
        "planner_rag_collection": settings.get("planner_rag_collection", "ralfloop_planner"),
        "coder_rag_collection": settings.get("coder_rag_collection", "ralfloop_coder"),
        "judge_rag_collection": settings.get("judge_rag_collection", "ralfloop_judge"),
        "skill_context": skill_context or "",
        "extra_context": {},
    }
    r = requests.post(f"{RALFLOOP_BASE_URL}/tasks/run", json=payload, timeout=120)
    r.raise_for_status()
    return r.json()


@hook
def before_cat_reads_message(message, cat):
    try:
        text = (getattr(message, "text", None) or getattr(message, "content", None) or "").strip()
    except Exception:
        return message

    if not text.lower().startswith("calcola"):
        return message

    suffix = """

ISTRUZIONE SPECIALE PER QUESTA RICHIESTA:
- Usa solo il contesto recuperato dalla memoria/Rabbit Hole e i dati esplicitamente presenti nella richiesta.
- Non spiegare a parole.
- Rispondi SOLO con un singolo blocco di codice Python racchiuso in ```python ... ```.
- Il codice deve:
  1. essere minimale,
  2. calcolare il risultato richiesto,
  3. stampare solo il risultato finale con print(...).
- Se il contesto non basta, stampa con Python una stringa che inizi con "ERRORE:".
"""
    try:
        if hasattr(message, "text"):
            message.text = text + suffix
        elif hasattr(message, "content"):
            message.content = text + suffix
    except Exception:
        pass

    return message


@hook
def before_cat_sends_message(message, cat):
    print("[RALFLOOP] hook entered", flush=True)
    try:
        user_text = (cat.working_memory.user_message_json.get("text") or "").strip()
        print(f"[RALFLOOP] user_text={user_text!r}", flush=True)
    except Exception:
        return message

    low = user_text.lower()

    task_triggers = [
        "calcola",
        "risolvi",
        "equazione",
        "derivata",
        "integrale",
        "sistema",
        "percentuale",
        "media",
        "deviazione standard",
        "scrivi uno script",
        "scrivi un programma",
        "scrivi un endpoint",
        "scrivi un plugin",
        "genera codice",
        "fammi uno script",
        "crea uno script",
        "dammi i comandi",
        "dammi i comandi da shell",
        "dammi comandi shell",
        "comandi shell",
        "comandi bash",
        "dammi i comandi bash",
    ]

    if not any(low.startswith(t) for t in task_triggers):
        print("[RALFLOOP] trigger miss", flush=True)
        return message

    print("[RALFLOOP] trigger hit", flush=True)
    skill_context = ""
    try:
        skill_context = _load_skill_text(user_text)
    except Exception:
        skill_context = ""

    settings = _load_plugin_settings(cat)
    task_id, task_dir = _make_task_dir(settings)
    task_sandbox = _create_sandbox()
    TASK_SANDBOXES[task_id] = task_sandbox["id"]

    planner_cfg = _role_config(settings, "planner")
    coder_cfg = _role_config(settings, "coder")
    judge_cfg = _role_config(settings, "judge")

    audit_lines = [
        f"role::planner::profile::{planner_cfg['model_profile']}::rag::{planner_cfg['rag_collection']}",
        f"role::coder::profile::{coder_cfg['model_profile']}::rag::{coder_cfg['rag_collection']}",
        f"role::judge::profile::{judge_cfg['model_profile']}::rag::{judge_cfg['rag_collection']}",
    ]

    planner_rag = _role_rag_text(cat, settings, "planner", user_text)
    print(f"[RALFLOOP] planner_rag_len={len(planner_rag)}", flush=True)
    coder_rag = _role_rag_text(cat, settings, "coder", user_text)
    print(f"[RALFLOOP] coder_rag_len={len(coder_rag)}", flush=True)
    judge_rag = _role_rag_text(cat, settings, "judge", user_text)
    print(f"[RALFLOOP] judge_rag_len={len(judge_rag)}", flush=True)

    try:
        planner_text = _ollama_generate(
            planner_cfg["model_name"],
            _planner_prompt(user_text, skill_context, planner_rag),
            timeout_sec=120,
        )
        coder_text = _ollama_generate(
            coder_cfg["model_name"],
            _coder_prompt(user_text, planner_text, skill_context, coder_rag),
            timeout_sec=180,
        )

        exec1 = None
        exec2 = None
        exec_summary = ""
        promoted = None

        if _should_validate_python(user_text, planner_text, coder_text):
            code1 = _extract_python_only(coder_text)
            exec1 = _run_python_validation(task_dir, code1, attempt=1, sandbox_id=TASK_SANDBOXES.get(task_id))
            expected_exact_output = _extract_expected_exact_output(user_text)
            if expected_exact_output is not None:
                actual_stdout = (exec1.get("stdout") or "").strip()
                if actual_stdout != expected_exact_output:
                    exec1["ok"] = False
                    exec1["stderr"] = (
                        (exec1.get("stderr") or "") +
                        f"\nEXACT_OUTPUT_MISMATCH expected={expected_exact_output!r} actual={actual_stdout!r}"
                    ).strip()
            exec_summary += (
                "Execution attempt 1\n"
                f"ok={exec1['ok']}\n"
                f"exit_code={exec1['exit_code']}\n"
                f"stdout={exec1['stdout']}\n"
                f"stderr={exec1['stderr']}\n"
            )

            if not exec1["ok"]:
                retry_plan = planner_text + "\n\nExecution feedback:\n" + exec_summary
                coder_text = _ollama_generate(
                    coder_cfg["model_name"],
                    _coder_prompt(user_text, retry_plan, skill_context, coder_rag),
                    timeout_sec=180,
                )
                code2 = _extract_python_only(coder_text)
                exec2 = _run_python_validation(task_dir, code2, attempt=2, sandbox_id=TASK_SANDBOXES.get(task_id))
                expected_exact_output = _extract_expected_exact_output(user_text)
                if expected_exact_output is not None:
                    actual_stdout = (exec2.get("stdout") or "").strip()
                    if actual_stdout != expected_exact_output:
                        exec2["ok"] = False
                        exec2["stderr"] = (
                            (exec2.get("stderr") or "") +
                            f"\nEXACT_OUTPUT_MISMATCH expected={expected_exact_output!r} actual={actual_stdout!r}"
                        ).strip()
                exec_summary += (
                    "\nExecution attempt 2\n"
                    f"ok={exec2['ok']}\n"
                    f"exit_code={exec2['exit_code']}\n"
                    f"stdout={exec2['stdout']}\n"
                    f"stderr={exec2['stderr']}\n"
                )

        elif _should_validate_shell(user_text, planner_text, coder_text):
            shell1 = _extract_shell_only(coder_text)
            exec1 = _run_shell_validation(task_dir, shell1, attempt=1, sandbox_id=TASK_SANDBOXES.get(task_id))
            expected_exact_output = _extract_expected_exact_output(user_text)
            if expected_exact_output is not None:
                actual_stdout = (exec1.get("stdout") or "").strip()
                if actual_stdout != expected_exact_output:
                    exec1["ok"] = False
                    exec1["stderr"] = (
                        (exec1.get("stderr") or "") +
                        f"\nEXACT_OUTPUT_MISMATCH expected={expected_exact_output!r} actual={actual_stdout!r}"
                    ).strip()
            exec_summary += (
                "Execution attempt 1\n"
                f"ok={exec1['ok']}\n"
                f"exit_code={exec1['exit_code']}\n"
                f"stdout={exec1['stdout']}\n"
                f"stderr={exec1['stderr']}\n"
            )

            if not exec1["ok"]:
                retry_plan = planner_text + "\n\nExecution feedback:\n" + exec_summary
                coder_text = _ollama_generate(
                    coder_cfg["model_name"],
                    _coder_prompt(user_text, retry_plan, skill_context, coder_rag),
                    timeout_sec=180,
                )
                shell2 = _extract_shell_only(coder_text)
                exec2 = _run_shell_validation(task_dir, shell2, attempt=2, sandbox_id=TASK_SANDBOXES.get(task_id))
                expected_exact_output = _extract_expected_exact_output(user_text)
                if expected_exact_output is not None:
                    actual_stdout = (exec2.get("stdout") or "").strip()
                    if actual_stdout != expected_exact_output:
                        exec2["ok"] = False
                        exec2["stderr"] = (
                            (exec2.get("stderr") or "") +
                            f"\nEXACT_OUTPUT_MISMATCH expected={expected_exact_output!r} actual={actual_stdout!r}"
                        ).strip()
                exec_summary += (
                    "\nExecution attempt 2\n"
                    f"ok={exec2['ok']}\n"
                    f"exit_code={exec2['exit_code']}\n"
                    f"stdout={exec2['stdout']}\n"
                    f"stderr={exec2['stderr']}\n"
                )

        judge_plan = planner_text
        judge_code = coder_text
        calc_stdout = None
        if _planner_field(planner_text, "task_type").strip().lower() == "calculation" and exec1 and exec1.get("ok"):
            calc_stdout = (exec1.get("stdout") or "").strip()
        if exec_summary.strip():
            judge_plan = planner_text + "\n\nExecution summary:\n" + exec_summary
            judge_code = coder_text + "\n\nExecution summary:\n" + exec_summary

        if _should_validate_shell(user_text, planner_text, coder_text):
            chosen_shell = None
            if exec2 is not None and exec2.get("ok"):
                chosen_shell = _extract_shell_only(coder_text)
            elif exec1 is not None and exec1.get("ok"):
                chosen_shell = _extract_shell_only(coder_text)
            if chosen_shell:
                final = "```shell\n" + chosen_shell.strip() + "\n```"
            else:
                final = _ollama_generate(
                    judge_cfg["model_name"],
                    _judge_prompt(user_text, judge_plan, judge_code, judge_rag),
                    timeout_sec=120,
                )
        else:
            if calc_stdout:
                val = calc_stdout.strip()
                try:
                    f = float(val)
                    if f.is_integer():
                        final = str(int(f))
                    else:
                        final = val
                except Exception:
                    final = val
            else:
                try:
                    preferred_exec = None
                    if exec2 is not None and exec2.get("ok") and exec2.get("candidate_path"):
                        preferred_exec = exec2
                    elif exec1 is not None and exec1.get("ok") and exec1.get("candidate_path"):
                        preferred_exec = exec1

                    if preferred_exec is not None:
                        candidate_path = preferred_exec["candidate_path"]
                        from pathlib import Path as _Path
                        promoted = _Path(candidate_path).read_text(encoding="utf-8").strip()
                except Exception:
                    promoted = None

                if promoted:
                    final = promoted
                else:
                    final = _ollama_generate(
                        judge_cfg["model_name"],
                        _judge_prompt(user_text, judge_plan, judge_code, judge_rag),
                        timeout_sec=120,
                    )
    except Exception as e:
        final = f"ERRORE: {e}"
        try:
            _save_temp_artifact(task_dir, "error.txt", final)
        except Exception:
            pass
        sid = TASK_SANDBOXES.pop(task_id, None)
        if sid:
            try:
                _destroy_sandbox(sid)
            except Exception:
                pass
        return message

    if not final:
        sid = TASK_SANDBOXES.pop(task_id, None)
        if sid:
            try:
                _destroy_sandbox(sid)
            except Exception:
                pass
        return message

    role_history = ["planner", "coder", "judge"]
    stop_reason = "goal_completed"
    audit_summary = [
        f"role::planner::profile::{planner_cfg['model_profile']}::model::{planner_cfg['model_name']}::rag::{planner_cfg['rag_collection']}",
        f"role::coder::profile::{coder_cfg['model_profile']}::model::{coder_cfg['model_name']}::rag::{coder_cfg['rag_collection']}",
        f"role::judge::profile::{judge_cfg['model_profile']}::model::{judge_cfg['model_name']}::rag::{judge_cfg['rag_collection']}",
    ]
    try:
        if promoted:
            audit_summary.append("judge_bypassed::validated_candidate_promoted")
    except Exception:
        pass

    _save_temp_artifact(task_dir, "planner_rag.txt", planner_rag)
    _save_temp_artifact(task_dir, "coder_rag.txt", coder_rag)
    _save_temp_artifact(task_dir, "judge_rag.txt", judge_rag)
    print("[RALFLOOP] saving artifacts", flush=True)
    _save_temp_artifact(task_dir, "planner.txt", planner_text)
    _save_temp_artifact(task_dir, "coder.txt", coder_text)
    if exec1 is not None:
        _save_temp_artifact(task_dir, "exec_attempt_1.txt", (
            f"ok={exec1['ok']}\n"
            f"exit_code={exec1['exit_code']}\n"
            f"candidate_path={exec1['candidate_path']}\n"
            f"stdout={exec1['stdout']}\n"
            f"stderr={exec1['stderr']}\n"
        ))
    if exec2 is not None:
        _save_temp_artifact(task_dir, "exec_attempt_2.txt", (
            f"ok={exec2['ok']}\n"
            f"exit_code={exec2['exit_code']}\n"
            f"candidate_path={exec2['candidate_path']}\n"
            f"stdout={exec2['stdout']}\n"
            f"stderr={exec2['stderr']}\n"
        ))
    result_path = _save_temp_artifact(task_dir, "result.txt", final)
    meta_content = (
        f"task_id={task_id}\n"
        f"role_history={role_history}\n"
        f"stop_reason={stop_reason}\n"
        f"audit_lines={audit_lines}\n"
        f"audit_summary={audit_summary}\n"
    )
    if final.strip().startswith("```python") and final.strip().endswith("```"):
        py_body = final.strip()[9:-3].strip()
        _save_temp_artifact(task_dir, "result.py", py_body)

    meta_path = _save_temp_artifact(task_dir, "meta.txt", meta_content)

    final = final.strip()

    try:
        if isinstance(message, dict):
            message["text"] = final
            message["content"] = final
        else:
            if hasattr(message, "text"):
                message.text = final
            if hasattr(message, "content"):
                message.content = final
    except Exception:
        pass

    sid = TASK_SANDBOXES.pop(task_id, None)
    if sid:
        try:
            _destroy_sandbox(sid)
        except Exception:
            pass
    return message
