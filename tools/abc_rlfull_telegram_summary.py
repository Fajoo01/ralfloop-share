#!/usr/bin/env python3
"""Telegram-sized summary for the deterministic ABC Formula Loop."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")


def main() -> int:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [
            str(ROOT / ".venv/bin/python"),
            "-m",
            "openshell_backend.skills.abc_formula_loop",
            "score",
        ],
        cwd=str(ROOT),
        text=True,
        timeout=60,
        capture_output=True,
        env=env,
    )

    if proc.returncode != 0:
        msg = "RSC/ABC errore ABC Formula Loop"
        if proc.stderr:
            msg += "\n\nSTDERR:\n" + proc.stderr.strip()[-2500:]
        if proc.stdout:
            msg += "\n\nSTDOUT:\n" + proc.stdout.strip()[-1200:]
        print(msg[:3900])
        return 0

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(("RSC/ABC JSON non valido: " + repr(exc) + "\n\n" + proc.stdout[-2500:])[:3900])
        return 0

    flags = data.get("bias_flags") or []
    trace = data.get("trace") or []
    reply = (
        "RSC/ABC Formula Loop\n"
        f"Formula: {data.get('formula_version', 'n/d')}\n"
        f"RLFULL: {data.get('rlfull_current', 'n/d')}\n"
        f"Prudenziale: {data.get('prudential_score', 'n/d')}\n"
        f"Relcalc: {data.get('relcalc_score', 'n/d')}\n"
        f"Confidence: {data.get('confidence', 'n/d')}\n"
        f"Range: {data.get('operative_range', 'n/d')}\n"
        f"Azione: {data.get('action', 'n/d')}\n"
        f"Evidenze: {data.get('evidence_count', 0)}\n"
    )

    if flags:
        reply += "\nFlag: " + ", ".join(map(str, flags)) + "\n"

    if trace:
        top = trace[:3]
        reply += "\nTrace:\n"
        for item in top:
            reply += (
                f"- {item.get('kind')} {item.get('weight_key')} "
                f"({item.get('weight')}): {item.get('text')}\n"
            )

    reply += (
        "\nGaranzia: numeri calcolati da formula deterministica + pesi versionati; "
        "LLM escluso dallo scoring runtime. "
    )

    action = data.get("action")
    if action == "monitor_only":
        reply += "Lettura: monitorare, niente mosse."
    elif action == "light_open":
        reply += "Lettura: apertura leggera ammessa, senza pressione."
    elif action == "available_for_reconnection":
        reply += "Lettura: disponibilita alta, ma niente chiarimento romantico sotto soglia 85."
    else:
        reply += "Lettura: do_nothing_active; rispondere caldo/breve se apre lei."

    print(reply[:3900])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
