#!/usr/bin/env python3
"""Host-only GLM/Colibri semantic judge benchmark; performs no external mutation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ralfloop_agent.semantic_judge import GlmColibriJudge
from ralfloop_agent.domains.email_reply import build_email_reply_domain


CONTEXT = {
    "source_email": {"subject": "Programmazione settembre", "body": "Vi invitiamo a partecipare a settembre.", "thread_context": []},
    "organization_context": {"relevant_facts": ["Tiremm Innanz APS svolge attivita educative."],
                             "signature": {"required": True, "name": "Fabio", "organization": "Tiremm Innanz"}},
    "user_intent": ["Vorremmo partecipare", "Speriamo di essere pronti per settembre"],
    "style": {"natural": True, "avoid_bureaucratic_language": True},
}
CONTEXT["email_reply_domain_v1"] = build_email_reply_domain(CONTEXT).model_dump(mode="json")
CASES = {
    "A": "Grazie per l'invito. Saremmo interessati a partecipare e speriamo di essere pronti per settembre. Fabio, Tiremm Innanz",
    "B": "Ti confermiamo che saremmo interessati a partecipare e speriamo di essere pronti per settembre. Fabio, Tiremm Innanz",
    "C": "L'iniziativa e coerente con i nostri valori. Confermiamo il costo di 500 euro e la scadenza del 10 agosto. Fabio, Tiremm Innanz",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark host-only del semantic judge GLM/Colibri")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--artifact-root", type=Path)
    args = parser.parse_args()
    if args.repetitions < 2:
        parser.error("--repetitions deve essere almeno 2")
    judge = GlmColibriJudge(timeout_sec=args.timeout_sec, artifact_root=args.artifact_root)
    rows = []
    for repetition in range(1, args.repetitions + 1):
        for case, draft in CASES.items():
            started = time.monotonic()
            try:
                result = judge.review(CONTEXT, draft)
                total = time.monotonic() - started
                rows.append({
                    "case": case, "repetition": repetition,
                    "startup_seconds": 0.0,
                    "startup_measurement": "included_in_review_seconds_by_existing_glm_launcher",
                    "review_seconds": round(result.latency_ms / 1000, 3),
                    "total_seconds": round(total, 3),
                    "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                    "verdict": result.review.verdict, "issues_count": len(result.review.issues),
                })
            except Exception as exc:
                total = time.monotonic() - started
                rows.append({"case": case, "repetition": repetition, "startup_seconds": 0.0,
                             "review_seconds": round(total, 3), "total_seconds": round(total, 3),
                             "input_tokens": None, "output_tokens": None, "verdict": "error",
                             "issues_count": 0, "error": f"{type(exc).__name__}:{exc}"})
    print(json.dumps({"provider": judge.provider, "model": judge.model, "rows": rows,
                      "host_benchmark_required": False}, ensure_ascii=False, indent=2))
    return 0 if all(row["verdict"] != "error" for row in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
