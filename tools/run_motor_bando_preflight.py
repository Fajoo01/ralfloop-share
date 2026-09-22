from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ralfloop_agent.domains.bando_domain_builder import (
    BandoDocumentClassification,
    BandoDomainBuilder,
)
from ralfloop_agent.integration.motor_bando_rule_gate import (
    budget_balance_check,
    evaluate_structured_rules,
    extract_bando_structured_evidence,
    hard_rule_violation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic bando preflight before SemIf/Motor")
    parser.add_argument("--regulation-text", type=Path, required=True)
    parser.add_argument("--application-text", type=Path, required=True)
    parser.add_argument("--budget-text", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = parse_args()
    regulation = args.regulation_text.read_text(encoding="utf-8")
    application = args.application_text.read_text(encoding="utf-8")
    budget = args.budget_text.read_text(encoding="utf-8") if args.budget_text else ""

    source = BandoDocumentClassification(
        source_id="local-regulation",
        title=args.regulation_text.stem,
        source_type="primary_official_document",
        issuer="",
        path=str(args.regulation_text),
        checksum=_sha256(args.regulation_text),
        binding_level="binding_official",
        text=regulation,
    )
    builder = BandoDomainBuilder()
    identity = builder.identify_bando([source])
    extracted = builder.extract_rules(identity, [source])
    evidence = extract_bando_structured_evidence(application, budget_text=budget)
    checks = list(evaluate_structured_rules(extracted.rules, evidence))
    checks.append(budget_balance_check(evidence))
    hard_violations = [check for check in checks if check.hard and check.status == "VIOLATED"]
    counts = {
        status: sum(check.status == status for check in checks)
        for status in ("SATISFIED", "UNKNOWN", "VIOLATED")
    }
    payload = {
        "schema_version": 1,
        "phase": "deterministic_preflight",
        "source_sha256": {
            "regulation": _sha256(args.regulation_text),
            "application": _sha256(args.application_text),
            "budget": _sha256(args.budget_text) if args.budget_text else None,
        },
        "bando_id": identity.bando_id,
        "rule_count": len(extracted.rules),
        "check_count": len(checks),
        "counts": counts,
        "hard_violation": hard_rule_violation(checks),
        "hard_violations": [check.as_dict() for check in hard_violations],
        "semantic_or_unknown_rule_ids": [
            check.rule_id for check in checks if check.status == "UNKNOWN"
        ],
        "budget_balance_status": next(
            check.status for check in checks if check.rule_id == "budget_balance"
        ),
        "decision": "REJECT" if hard_violations else "REQUEST_REVIEW",
        "next_stage": "stop" if hard_violations else "semif_rule_triage",
        "content_published": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
