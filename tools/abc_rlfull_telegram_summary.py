#!/usr/bin/env python3
"""Telegram-sized summary for the deterministic ABC Formula Loop."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(os.environ.get("RALFLOOP_ROOT", Path(__file__).resolve().parents[1])).resolve()
RUN_DIR = Path(os.environ.get("RALFLOOP_RUN_DIR", ROOT / ".ralf_run")).resolve()
MEMORY_DIR = Path(os.environ.get("ABC_MEMORY_DIR", ROOT / "abc_memory")).resolve()


def configure_extractor() -> None:
    use_llm = os.environ.get("ABC_USE_LLM_EXTRACTOR", "").lower() in {"1", "true", "yes", "on"}
    if not use_llm:
        os.environ.setdefault("ABC_DISABLE_LLM_EXTRACTOR", "1")


def main() -> int:
    try:
        import sys

        configure_extractor()
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from openshell_backend.skills import abc_formula_loop, abc_memory

        context = abc_memory.build_current_context(MEMORY_DIR, RUN_DIR)
        scoring_text = context.get("formula_scoring_input") or context.get("merged_current_context") or ""
        data = abc_formula_loop.score_text(
            scoring_text,
            report_path=None,
            memory_dir=MEMORY_DIR,
            evidence_cache_path=RUN_DIR / "last_evidence.json",
            force_extract=False,
        )
    except Exception as exc:
        print(("RSC/ABC errore ABC Formula Loop: " + repr(exc))[:3900])
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
        priority_ids = {
            "micro_riparazione_privata",
            "cena_1_1_domestica",
            "auto_invito_implicito_cibo",
            "logistica_convertita_in_convivialita",
            "soglia_prolungata",
            "comfort_silenzi_soglia",
            "deflazione_frame_camper",
            "mancato_aggancio_mare_vacanza",
            "campo_bestia_agosto",
            "contenuti_personali_1_1",
            "contatto_tollerato",
            "mancato_invito_sociale",
            "autoinvito_familiare",
            "permanenza_familiare_reale",
            "gancio_futuro_domestico",
            "conversione_logistica_in_presenza",
            "boundary_limite_contatto_attivazione",
            "relazione_aperta_rifiutata_da_arianna",
            "rottura_narrativa_antonluca",
            "svalutazione_terzo_esplicita",
            "third_degradation",
            "rebound_risk_high",
        }
        priority = [item for item in trace if item.get("id") in priority_ids]
        rest = [item for item in trace if item.get("id") not in priority_ids]
        top = (priority + rest)[:12]
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
