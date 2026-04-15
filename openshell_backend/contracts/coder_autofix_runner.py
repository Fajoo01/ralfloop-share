from __future__ import annotations

from typing import Any

import requests


def run_coder_handoff_prompt(
    *,
    prompt: str,
    model_name: str,
    base_url: str = "http://127.0.0.1:11434",
    timeout_sec: int = 60,
) -> str | None:
    p = str(prompt or "").strip()
    m = str(model_name or "").strip()
    if not p or not m:
        return None

    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={
                "model": m,
                "prompt": p,
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=timeout_sec,
        )
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        out = str(data.get("response") or "").strip()
        return out or None
    except Exception:
        return None


def maybe_attach_coder_text(
    *,
    payload: dict[str, Any],
    model_name: str,
    base_url: str = "http://127.0.0.1:11434",
    timeout_sec: int = 60,
) -> dict[str, Any]:
    out = dict(payload or {})
    af = dict(out.get("autofix_candidate", {}) or {})
    prompt = str(af.get("coder_handoff_prompt") or "").strip()
    if not prompt:
        return out

    coder_text = run_coder_handoff_prompt(
        prompt=prompt,
        model_name=model_name,
        base_url=base_url,
        timeout_sec=timeout_sec,
    )
    if coder_text:
        af["coder_text"] = coder_text
        out["autofix_candidate"] = af
    return out
