from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from openshell_backend.skill_generalizer import llm_judge_reusability
from openshell_backend.skills.registry import try_direct_skill
from openshell_backend.skills.validators import is_skill_output_sufficient, validate_skill_output
from openshell_backend.skills.responses import build_skill_insufficient_response


def skill_cache_dir() -> Path:
    return Path("/home/sibilla-cumana/ralfloop_agent_scaffold/openshell_backend/cache_skills")


def run_python_inline(code: str) -> str | None:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code + "\n")
        path = f.name
    try:
        r = subprocess.run(
            ["python3", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if out:
            return out.strip()
        if err:
            return None
        return None
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def candidate_skill_dir() -> Path:
    return Path("/home/sibilla-cumana/ralfloop_agent_scaffold/openshell_backend/cache_skills_candidates")


def _safe_skill_id(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", (text or "").lower()).strip("_")
    return text[:80] or "skill"


def _all_skill_dirs() -> list[Path]:
    return [skill_cache_dir(), candidate_skill_dir()]




def _candidate_to_stable_path(candidate_path: Path, candidate_obj: dict) -> Path:
    problem_type = str(candidate_obj.get("problem_type") or "")
    stable_base = skill_cache_dir()
    if "cubic_root" in problem_type:
        return stable_base / "math" / "cubic_root.auto.promoted.json"
    if "square_root" in problem_type:
        return stable_base / "math" / "square_root.auto.promoted.json"
    if "binary_arithmetic" in problem_type:
        return stable_base / "math" / "binary_arithmetic.auto.promoted.json"
    if "file_processing" in problem_type:
        return stable_base / "file_processing" / candidate_path.name
    return stable_base / "python" / candidate_path.name




def _dedupe_skill_path(problem_type: str, executor: str, match_regex: str, template: str) -> str | None:
    try:
        for base in _all_skill_dirs():
            if not base.exists():
                continue
            for path in sorted(base.rglob("*.json")):
                try:
                    obj = json.loads(path.read_text(encoding="utf-8"))
                    if (obj.get("problem_type") or "") != problem_type:
                        continue
                    if (obj.get("executor") or "") != executor:
                        continue
                    if (obj.get("match", {}).get("regex") or "") != match_regex:
                        continue
                    if (obj.get("template") or "") != template:
                        continue
                    return str(path)
                except Exception:
                    continue
        return None
    except Exception:
        return None

def maybe_promote_candidate_file(candidate_path: Path) -> str | None:
    try:
        obj = json.loads(candidate_path.read_text(encoding="utf-8"))
        if not validate_skill_candidate(obj):
            return None

        hit_count = int(obj.get("hit_count") or 0)
        promote_threshold = int(obj.get("promote_threshold") or 2)
        if hit_count < promote_threshold:
            return None

        stable_path = _candidate_to_stable_path(candidate_path, obj)
        stable_path.parent.mkdir(parents=True, exist_ok=True)

        promoted = dict(obj)
        promoted["source"] = "candidate_promoted"
        promoted["promoted_from_candidate"] = str(candidate_path)
        promoted["validated"] = True

        if not stable_path.exists():
            stable_path.write_text(json.dumps(promoted, ensure_ascii=False, indent=2), encoding="utf-8")

        return str(stable_path)
    except Exception:
        return None


def record_candidate_hit(candidate_path: Path) -> tuple[int, str | None]:
    try:
        obj = json.loads(candidate_path.read_text(encoding="utf-8"))
        obj["hit_count"] = int(obj.get("hit_count") or 0) + 1
        candidate_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        promoted = maybe_promote_candidate_file(candidate_path)
        return obj["hit_count"], promoted
    except Exception:
        return 0, None

def skill_cache_answer(user_goal: str) -> str | None:
    q = (user_goal or "").strip().lower()

    for base in _all_skill_dirs():
        if not base.exists():
            continue

        for path in sorted(base.rglob("*.json")):
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                pat = obj.get("match", {}).get("regex")
                if not pat:
                    continue
                m = re.search(pat, q)
                if not m:
                    continue
                names = obj.get("input_names", [])
                vals = list(m.groups())
                if len(names) != len(vals):
                    continue
                code = (obj.get("template") or "").format(**dict(zip(names, vals)))
                if obj.get("executor") != "python_inline":
                    continue

                out = run_python_inline(code)
                if out is None:
                    continue

                if "cache_skills_candidates" in str(path):
                    hit_count, promoted = record_candidate_hit(path)
                    print(f"[COMMON_ROUTER] candidate_hit path={path} hit_count={hit_count} promoted={promoted}", flush=True)

                return out
            except Exception:
                continue

    return None





def _strip_code_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```python"):
        t = t[len("```python"):].strip()
    elif t.startswith("```"):
        t = t[len("```"):].strip()
    if t.endswith("```"):
        t = t[:-3].strip()
    return t

def _normalize_llm_candidate_fields(problem_type: str, executor: str, match_regex: str, template: str, input_names):
    problem_type = str(problem_type or "").strip()
    executor = str(executor or "").strip().lower()
    match_regex = str(match_regex or "").strip()
    template = _strip_code_fences(template)

    if isinstance(input_names, str):
        input_names = [x.strip() for x in input_names.split(",") if x.strip()]
    if not isinstance(input_names, list):
        input_names = []
    input_names = [str(x).strip() for x in input_names if str(x).strip()]

    # normalizza nomi tipici
    input_names = ["file_path" if x in {"filename", "file", "path"} else x for x in input_names]

    # se non ci sono gruppi regex ma c'è un solo input file_path, crea regex permissiva senza gruppi
    # e poi fallback: niente candidate se non coerente
    return problem_type, executor, match_regex, template, input_names



def candidate_from_judged(user_goal: str, final_answer: str, judged: dict) -> dict | None:
    if not isinstance(judged, dict):
        return None
    if not bool(judged.get("is_reusable")):
        return None

    problem_type, executor, match_regex, template, input_names = _normalize_llm_candidate_fields(
        judged.get("problem_type"),
        judged.get("executor"),
        judged.get("match_regex"),
        judged.get("template"),
        judged.get("input_names"),
    )

    if not problem_type:
        return None
    if not executor:
        executor = "python_inline"
    if executor != "python_inline":
        return None
    if not match_regex or not template or not input_names:
        return None
    if len(template) > 2000:
        return None

    low_template = template.lower()
    if "print(" not in low_template:
        return None
    if "input.txt" in low_template or "path/to/your/file" in low_template:
        return None
    if "input_names[" in template or "input_names[" in low_template:
        return None

    low_goal = (user_goal or "").lower()

    # normalizzazione pragmatica per task file-based con path concreto nella richiesta
    if " file " in f" {low_goal} " and executor == "python_inline" and len(input_names) == 1:
        input_names = ["file_path"]

        try:
            import re as _re
            compiled = _re.compile(match_regex)
            group_count = compiled.groups
        except Exception:
            return None

        if group_count == 0:
            match_regex = r'.*file\s+(\S+)'

        template = template.replace("open('filename')", 'open("{file_path}")')
        template = template.replace('open("filename")', 'open("{file_path}")')
        template = template.replace("open('filename', 'r')", 'open("{file_path}", "r")')
        template = template.replace('open("filename", "r")', 'open("{file_path}", "r")')
        template = template.replace("open('file_path')", 'open("{file_path}")')
        template = template.replace('open("file_path")', 'open("{file_path}")')
        template = template.replace("open('file_path', 'r')", 'open("{file_path}", "r")')
        template = template.replace('open("file_path", "r")', 'open("{file_path}", "r")')

    try:
        import re as _re
        group_count = _re.compile(match_regex).groups
    except Exception:
        return None

    if group_count != len(input_names):
        return None

    return {
        "id": "auto_llm_" + _safe_skill_id(user_goal),
        "problem_type": _safe_skill_id(problem_type),
        "match": {
            "regex": match_regex
        },
        "executor": executor,
        "input_names": input_names,
        "template": template,
        "source": "autopromote_llm",
        "validated": False,
        "hit_count": 0,
        "promote_threshold": 2,
        "example_input": user_goal,
        "example_output": final_answer,
    }

def propose_skill_candidate_llm(user_goal: str, final_answer: str, planner_text: str = "", coder_text: str = "") -> dict | None:
    judged = llm_judge_reusability(user_goal, final_answer, coder_text)
    return candidate_from_judged(user_goal, final_answer, judged)

def propose_skill_candidate(user_goal: str, final_answer: str, planner_text: str = "", coder_text: str = "") -> dict | None:
    judged = llm_judge_reusability(user_goal, final_answer, coder_text)
    return candidate_from_judged(user_goal, final_answer, judged)

def validate_skill_candidate(candidate: dict) -> bool:
    try:
        pat = candidate.get("match", {}).get("regex")
        names = candidate.get("input_names") or []
        ex_in = candidate.get("example_input") or ""
        ex_out = str(candidate.get("example_output") or "").strip()
        template = candidate.get("template") or ""
        executor = candidate.get("executor") or ""

        if not pat or not names or not ex_in or not ex_out or not template:
            return False
        if executor != "python_inline":
            return False

        m = re.search(pat, ex_in.lower())
        if not m:
            return False

        vals = list(m.groups())
        if len(vals) != len(names):
            return False

        code = template.format(**dict(zip(names, vals)))
        got = run_python_inline(code)
        if got is None:
            return False

        return str(got).strip() == ex_out
    except Exception:
        return False


def store_candidate_skill(candidate: dict) -> str | None:
    try:
        candidate = dict(candidate)
        dup = _dedupe_skill_path(
            candidate.get("problem_type") or "",
            candidate.get("executor") or "",
            candidate.get("match", {}).get("regex") or "",
            candidate.get("template") or "",
        )
        if dup:
            return dup

        out = candidate_skill_dir() / "python" / f"{candidate['id']}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        candidate["validated"] = True
        out.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(out)
    except Exception:
        return None



def maybe_autopromote_candidate(user_goal: str, final_answer: str, planner_text: str = "", coder_text: str = "") -> str | None:
    q = (user_goal or "").strip().lower()
    ans = (final_answer or "").strip()
    code = (coder_text or "").strip()

    if not q or not ans:
        return None

    if code:
        if "print(" not in code:
            return None
        if len(code) > 400:
            return None

    candidate = propose_skill_candidate(user_goal, final_answer, planner_text, coder_text)
    if not candidate:
        return None

    if not validate_skill_candidate(candidate):
        return None

    candidate["validated"] = True
    return store_candidate_skill(candidate)

def fast_math_answer(user_goal: str) -> str | None:
    q = (user_goal or "").strip().lower()

    def _num(x: str) -> float:
        return float(x.replace(",", "."))

    try:
        m = re.search(r'radice cubica di\s+(-?\d+(?:[.,]\d+)?)', q)
        if m:
            n = _num(m.group(1))
            x = n ** (1/3) if n >= 0 else -((-n) ** (1/3))
            return str(round(x, 6))

        m = re.search(r'radice quadrata di\s+(-?\d+(?:[.,]\d+)?)', q)
        if m:
            n = _num(m.group(1))
            if n < 0:
                return None
            return str(round(n ** 0.5, 6))

        m = re.search(r'(?:quanto fa|calcola)\s+(-?\d+(?:[.,]\d+)?)\s*([\+\-\*x/])\s*(-?\d+(?:[.,]\d+)?)', q)
        if m:
            a = _num(m.group(1))
            op = m.group(2)
            b = _num(m.group(3))
            if op == '+':
                return str(a + b)
            if op == '-':
                return str(a - b)
            if op in ('*', 'x'):
                return str(a * b)
            if op == '/':
                if b == 0:
                    return None
                return str(a / b)
    except Exception:
        return None

    return None


def route_common(user_goal: str, skill_context: str = "") -> dict | None:
    cached_answer = skill_cache_answer(user_goal or "")
    if cached_answer is not None:
        return {
            "ok": True,
            "mode": "skill_cache",
            "stop_reason": "goal_completed",
            "final_answer": cached_answer,
            "current_role": "skill_cache",
            "role_history": ["skill_cache"],
        }

    direct_skill = try_direct_skill(user_goal or "")
    if direct_skill is not None:
        skill_name, skill_answer = direct_skill
        validation = validate_skill_output(skill_name, skill_answer)

        if is_skill_output_sufficient(skill_name, skill_answer):
            return {
                "ok": True,
                "mode": f"skill::{skill_name}",
                "stop_reason": "goal_completed",
                "final_answer": skill_answer,
                "current_role": f"skill::{skill_name}",
                "role_history": [f"skill::{skill_name}"],
                "audit_summary": [f"fastpath::skill::{skill_name}"],
                "autofix_candidate": {},
            }

        return build_skill_insufficient_response(
            skill_name=skill_name,
            user_goal=user_goal or "",
            final_answer=skill_answer,
            validation=validation,
            include_runtime_fields=False,
        )

    fast_answer = fast_math_answer(user_goal or "")
    if fast_answer is not None:
        promoted_skill_path = maybe_autopromote_candidate(user_goal or "", fast_answer, "", "")
        return {
            "ok": True,
            "mode": "fast_math",
            "stop_reason": "goal_completed",
            "final_answer": fast_answer,
            "autopromoted_skill": promoted_skill_path,
            "current_role": "fast_math",
            "role_history": ["fast_math"],
        }

    return None
