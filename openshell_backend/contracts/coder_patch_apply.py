from __future__ import annotations

from pathlib import Path
from typing import Any
import re
import subprocess


def _extract_code_block(text: str) -> str | None:
    s = str(text or "").strip()
    if not s:
        return None

    m = re.search(r"```(?:python)?\s*(.*?)```", s, flags=re.DOTALL | re.IGNORECASE)
    if m:
        code = m.group(1).strip()
        return code or None

    return s


def _extract_target_symbol_block(code: str, target_symbol: str) -> str | None:
    sym = re.escape(str(target_symbol or "").strip())
    if not sym:
        return None

    pat = rf"(^def\s+{sym}\s*\(.*?)(?=^def\s+|^class\s+|\Z)"
    m = re.search(pat, code, flags=re.DOTALL | re.MULTILINE)
    if not m:
        return None
    return m.group(1).rstrip() + "\n"


def _replace_top_level_symbol(src: str, target_symbol: str, new_block: str) -> str | None:
    sym = re.escape(str(target_symbol or "").strip())
    pat = rf"(^def\s+{sym}\s*\(.*?)(?=^def\s+|^class\s+|\Z)"
    m = re.search(pat, src, flags=re.DOTALL | re.MULTILINE)
    if not m:
        return None
    start, end = m.span(1)
    return src[:start] + new_block.rstrip() + "\n\n" + src[end:]


def _pytest_cmd_for_issue(issue_kind: str) -> list[str]:
    if issue_kind == "suspicious_apostrophe_token":
        return [
            ".venv/bin/python", "-m", "pytest", "-q",
            "tests/test_coder_patch_apply.py",
            "tests/test_coder_autofix_runner.py",
            "tests/test_coder_handoff_prompt.py",
            "tests/test_coder_patch_candidate.py",
            "tests/test_grammar_upstream_diagnoser.py",
            "tests/test_skill_response_grammar_upstream_autofix.py",
            "tests/grammar/test_grammar_diagnostics_api.py",
            "tests/grammar/test_self_heal_suspicious_apostrophe.py",
            "tests/grammar/test_validator.py",
        ]
    return [".venv/bin/python", "-m", "pytest", "-q", "tests/grammar"]


def maybe_apply_and_validate_coder_patch(
    *,
    repo_root: str,
    autofix_candidate: dict[str, Any],
    timeout_sec: int = 120,
) -> dict[str, Any] | None:
    af = dict(autofix_candidate or {})
    coder_text = str(af.get("coder_text") or "").strip()
    patch = af.get("coder_patch_candidate") or {}
    if not coder_text or not isinstance(patch, dict):
        return None

    target_file = str(patch.get("target_file") or "").strip()
    target_symbol = str(patch.get("target_symbol") or "").strip()
    issue_kind = str(patch.get("issue_kind") or "").strip()
    if not target_file or not target_symbol:
        return None

    repo = Path(repo_root).resolve()
    file_path = (repo / target_file).resolve()
    try:
        file_path.relative_to(repo)
    except Exception:
        return {
            "ok": False,
            "stage": "path_check",
            "error": "target_outside_repo",
            "target_file": target_file,
        }

    if not file_path.exists():
        return {
            "ok": False,
            "stage": "path_check",
            "error": "target_missing",
            "target_file": target_file,
        }

    raw_code = _extract_code_block(coder_text)
    if not raw_code:
        return {
            "ok": False,
            "stage": "extract_code",
            "error": "no_code_found",
        }

    new_block = _extract_target_symbol_block(raw_code, target_symbol)
    if not new_block:
        return {
            "ok": False,
            "stage": "extract_symbol",
            "error": "target_symbol_block_not_found",
            "target_symbol": target_symbol,
        }

    original = file_path.read_text(encoding="utf-8")
    patched = _replace_top_level_symbol(original, target_symbol, new_block)
    if not patched:
        return {
            "ok": False,
            "stage": "replace_symbol",
            "error": "target_symbol_not_found_in_file",
            "target_symbol": target_symbol,
        }

    file_path.write_text(patched, encoding="utf-8")
    try:
        cmd = _pytest_cmd_for_issue(issue_kind)
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    finally:
        file_path.write_text(original, encoding="utf-8")

    return {
        "ok": proc.returncode == 0,
        "stage": "validated",
        "target_file": target_file,
        "target_symbol": target_symbol,
        "issue_kind": issue_kind,
        "pytest_cmd": cmd,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "patched_preview": new_block[:1200],
    }


def apply_coder_patch_permanently(
    *,
    repo_root: str,
    autofix_candidate: dict[str, Any],
) -> dict[str, Any] | None:
    af = dict(autofix_candidate or {})
    validation_attempt = af.get("coder_validation_attempt") or {}
    if not isinstance(validation_attempt, dict) or not validation_attempt.get("ok"):
        return None

    coder_text = str(af.get("coder_text") or "").strip()
    patch = af.get("coder_patch_candidate") or {}
    if not coder_text or not isinstance(patch, dict):
        return None

    target_file = str(patch.get("target_file") or "").strip()
    target_symbol = str(patch.get("target_symbol") or "").strip()
    if not target_file or not target_symbol:
        return None

    repo = Path(repo_root).resolve()
    file_path = (repo / target_file).resolve()
    try:
        file_path.relative_to(repo)
    except Exception:
        return {
            "ok": False,
            "stage": "path_check",
            "error": "target_outside_repo",
            "target_file": target_file,
        }

    if not file_path.exists():
        return {
            "ok": False,
            "stage": "path_check",
            "error": "target_missing",
            "target_file": target_file,
        }

    raw_code = _extract_code_block(coder_text)
    if not raw_code:
        return {
            "ok": False,
            "stage": "extract_code",
            "error": "no_code_found",
        }

    new_block = _extract_target_symbol_block(raw_code, target_symbol)
    if not new_block:
        return {
            "ok": False,
            "stage": "extract_symbol",
            "error": "target_symbol_block_not_found",
            "target_symbol": target_symbol,
        }

    original = file_path.read_text(encoding="utf-8")
    patched = _replace_top_level_symbol(original, target_symbol, new_block)
    if not patched:
        return {
            "ok": False,
            "stage": "replace_symbol",
            "error": "target_symbol_not_found_in_file",
            "target_symbol": target_symbol,
        }

    if patched == original:
        return {
            "ok": False,
            "stage": "write",
            "error": "no_effect",
            "target_file": target_file,
        }

    file_path.write_text(patched, encoding="utf-8")
    return {
        "ok": True,
        "stage": "applied",
        "target_file": target_file,
        "target_symbol": target_symbol,
        "applied_preview": new_block[:1200],
    }
