#!/usr/bin/env python3
"""Compatibility wrapper for the deterministic ABC formula loop."""

from __future__ import annotations

import json

from openshell_backend.skills.abc_formula_loop import DEFAULT_REPORT, format_trace, score_report


def main() -> int:
    result = score_report(update_report=True)

    legacy_calc = {
        "score": result["relcalc_score"],
        "confidence": result["confidence"],
        "bias_flags": result["bias_flags"],
        "next_safe_action": result["action"],
        "evidence_count": result["evidence_count"],
        "summary": f"formula={result['formula_version']}; action={result['action']}",
    }
    legacy_range = {
        "rlfull_current": result["rlfull_current"],
        "rlfull_prudential": result["prudential_score"],
        "relcalc_score": result["relcalc_score"],
        "operative_range": result["operative_range"],
        "prudential_range": result["operative_range"],
        "action": result["action"],
        "note": "ABC Formula Loop deterministic: evidence weights only, no runtime LLM scoring.",
    }

    print("=== SOURCE ===")
    print(result.get("source", {}).get("report_path") or DEFAULT_REPORT)
    print()

    print("=== ABC_FORMULA_LOOP ===")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print()

    print("=== EVIDENCE USED ===")
    for i, item in enumerate(result["trace"], 1):
        print(
            f"{i:02d}. {item['kind']:22s} weight={item['weight']:>5} "
            f"conf={item['confidence']:.2f} key={item['weight_key']}"
        )
        print("    " + str(item["text"]))
        print("    ref=" + str(item["manual_ref"]))
    print()

    print("=== ABC_RELCALC ===")
    print(json.dumps(legacy_calc, ensure_ascii=False, indent=2, sort_keys=True))
    print()

    print("=== OPERATIVE RANGE ===")
    print(json.dumps(legacy_range, ensure_ascii=False, indent=2, sort_keys=True))
    print()

    print("=== TRACE ===")
    print(format_trace(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
