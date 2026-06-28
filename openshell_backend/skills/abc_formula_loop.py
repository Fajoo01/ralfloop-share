"""Deterministic ABC formula loop.

Runtime scoring is rule-based and reproducible:

- evidence is extracted from the current report by fixed lexical rules;
- evidence is cached with the report hash in `.ralf_run/last_evidence.json`;
- scores are calculated only from versioned weights;
- LLM use is limited to the future `review-rules` flow, never live scoring.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


FORMULA_VERSION = "abc_formula_loop_v1"
EXTRACTOR_VERSION = "abc_rule_extractor_v1"
REPORT_SECTION = "## ABC Formula Loop"
LEGACY_SUMMARY_SECTION = "## ABC / Relcalc deterministic summary"
RULEBOOK_DIR = Path(__file__).resolve().parent / "abc_rulebooks"
WEIGHTS_PATH = RULEBOOK_DIR / "evidence_weights_v1.json"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN_DIR = ROOT / ".ralf_run"
DEFAULT_REPORT = DEFAULT_RUN_DIR / "RSC_ABC_RAPPORTO_FULL_CURRENT.txt"
DEFAULT_MEMORY_DIR = ROOT / "abc_memory"
DEFAULT_EVIDENCE_CACHE = DEFAULT_RUN_DIR / "last_evidence.json"

EVIDENCE_KINDS = {
    "observed_fact",
    "inference",
    "contradiction",
    "external_signal",
    "operational_constraint",
}


@dataclass(frozen=True)
class EvidenceRule:
    evidence_id: str
    kind: str
    weight_key: str
    text: str
    terms: tuple[str, ...]
    manual_ref: str
    confidence: float = 1.0
    tags: tuple[str, ...] = ()


EVIDENCE_RULES: tuple[EvidenceRule, ...] = (
    EvidenceRule(
        "secure_base_pre_nodo",
        "observed_fact",
        "observed_fact:secure_base_pre_nodo",
        "Concrete object used as a light pre-node bridge.",
        ("casco prima", "dato a fabio il casco", "affida il casco", "lascia le chiavi"),
        "relation_manual_v1:Attachment/Secure Base",
        0.9,
    ),
    EvidenceRule(
        "notte_negata_alternativa",
        "observed_fact",
        "observed_fact:notte_negata_alternativa",
        "Alternative night or competing option is denied/shortened.",
        ("notte negata", "ha negato la notte", "non dorme da", "alternativa negata"),
        "relation_manual_v1:Interdependence",
        0.9,
    ),
    EvidenceRule(
        "rientro_immediato_post_nodo",
        "observed_fact",
        "observed_fact:rientro_immediato_post_nodo",
        "Return or callback soon after a node.",
        ("rientro immediato", "torna subito", "rientro alle 18", "tornata intorno alle 18"),
        "relation_manual_v1:Attachment/Post-node return",
        0.85,
    ),
    EvidenceRule(
        "investimento_familiare",
        "observed_fact",
        "observed_fact:investimento_familiare",
        "Shared domestic routine or affective logistics beyond a single errand.",
        ("investimento familiare", "routine condivisa", "gestione comune", "campo domestico", "logistica affettiva"),
        "relation_manual_v1:Interdependence/Investment",
        0.82,
    ),
    EvidenceRule(
        "logistica_pura",
        "observed_fact",
        "observed_fact:logistica_pura",
        "Pure logistical hook without independent emotional marker.",
        ("bestia", "gancio logistico", "logistica della bestia", "commissioni non emotive"),
        "anti_bias_manual_v1:Micro-signal cap",
        0.75,
    ),
    EvidenceRule(
        "marking_territoriale",
        "observed_fact",
        "observed_fact:marking_territoriale",
        "Light public/default inclusion marker.",
        ("marking territoriale", "default sociale", "incluso di default", "territoriale"),
        "relation_manual_v1:Differentiation",
        0.75,
    ),
    EvidenceRule(
        "progetti_futuri",
        "observed_fact",
        "observed_fact:progetti_futuri",
        "Concrete future plan with behavioral marker.",
        ("progetto futuro", "progetti futuri", "data fissata", "abbiamo fissato"),
        "relation_manual_v1:Future Orientation",
        0.72,
    ),
    EvidenceRule(
        "due_chiacchiere",
        "observed_fact",
        "observed_fact:due_chiacchiere",
        "Explicit lightweight talk invitation.",
        ("due chiacchiere",),
        "relation_manual_v1:Attachment/Spontaneous contact",
        0.8,
    ),
    EvidenceRule(
        "contatto_spontaneo",
        "observed_fact",
        "observed_fact:contatto_spontaneo",
        "Unprompted contact not required by logistics.",
        ("chiama fabio", "chiamato fabio", "ha chiamato fabio", "chiamata delle 22:06", "contatto spontaneo", "messaggio spontaneo", "ha scritto", "riaperto il canale"),
        "relation_manual_v1:Attachment/Spontaneous contact",
        0.82,
    ),
    EvidenceRule(
        "messaggio_leggero",
        "observed_fact",
        "observed_fact:messaggio_leggero",
        "Light non-pressing message exchange.",
        ("ancora viva", "mi porti fuori stasera", "messaggio leggero"),
        "relation_manual_v1:Attachment/Low pressure signal",
        0.65,
    ),
    EvidenceRule(
        "tempo_dedicato",
        "observed_fact",
        "observed_fact:tempo_dedicato",
        "Time is dedicated without pressure.",
        ("tempo dedicato", "resta a parlare", "rimane a parlare"),
        "relation_manual_v1:Interdependence/Time investment",
        0.78,
    ),
    EvidenceRule(
        "campo_quotidiano",
        "external_signal",
        "external_signal:campo_quotidiano",
        "Daily-field signal from context.",
        ("campo quotidiano", "micro-contatti", "campo domestico"),
        "relation_manual_v1:Interdependence/Context signal",
        0.65,
    ),
    EvidenceRule(
        "terzi_neutri",
        "external_signal",
        "external_signal:terzi_neutri",
        "Third-party neutral signal.",
        ("teresa ha detto", "detto da teresa", "informazione da terzi"),
        "relation_manual_v1:Differentiation/Third-party signal",
        0.55,
    ),
    EvidenceRule(
        "silenzio_diretto_su_nodo",
        "contradiction",
        "contradiction:silenzio_diretto_su_nodo",
        "Silence on the direct relational node.",
        ("silenzio diretto", "non parla del nodo", "non chiarisce"),
        "anti_bias_manual_v1:Contradiction confidence cut",
        0.85,
    ),
    EvidenceRule(
        "mancato_invito_sociale",
        "contradiction",
        "contradiction:mancato_invito_sociale_esterno",
        "Social event without Fabio despite domestic proximity.",
        ("senza invitare fabio", "non invitato a evento", "non e stato invitato", "non invitato", "nama senza invitare"),
        "anti_bias_manual_v1:Social exclusion cap",
        0.9,
        ("social_exclusion",),
    ),
    EvidenceRule(
        "opacita_terzo",
        "contradiction",
        "contradiction:opacita_terzo",
        "Third-person opacity remains active.",
        ("opacita terzo", "terzo opaco", "vincenzo", "antonluca"),
        "relation_manual_v1:Bowen triangulation",
        0.7,
    ),
    EvidenceRule(
        "promessa_senza_data",
        "contradiction",
        "contradiction:promessa_senza_data",
        "Warm future phrase without date or follow-through.",
        ("promessa senza data", "dice di voler festeggiare ma non fissa", "non fissa data"),
        "anti_bias_manual_v1:No current intention without marker",
        0.75,
    ),
    EvidenceRule(
        "mind_reading_non_supportato",
        "inference",
        "inference:mind_reading_non_supportato",
        "Mind-reading cue without observable confirmation.",
        ("lei pensa che", "sta male per", "lo fa per", "vuole farmi", "controlla quando accedo"),
        "anti_bias_manual_v1:Mind reading stays inference",
        0.45,
        ("mind_reading",),
    ),
    EvidenceRule(
        "tono_ambiguo",
        "inference",
        "inference:tono_ambiguo",
        "Tone interpretation remains weak inference.",
        ("tono non allegro", "tono agitato", "tono freddo", "tono strano"),
        "anti_bias_manual_v1:Tone is not proof",
        0.45,
        ("mind_reading", "tone"),
    ),
    EvidenceRule(
        "gelosia_non_provata",
        "inference",
        "inference:gelosia_non_provata",
        "Jealousy interpretation without direct marker.",
        ("gelosia", "gelosa", "geloso"),
        "anti_bias_manual_v1:Inference cap",
        0.4,
        ("mind_reading",),
    ),
    EvidenceRule(
        "non_chiedere_chiarimento",
        "operational_constraint",
        "operational_constraint:non_chiedere_chiarimento",
        "Operational constraint: do not ask for clarification.",
        ("non chiedere chiarimento", "non chiedere del cinema", "non chiedere conferme"),
        "anti_bias_manual_v1:Action Bias Guard",
        1.0,
    ),
    EvidenceRule(
        "non_inseguire",
        "operational_constraint",
        "operational_constraint:non_inseguire",
        "Operational constraint: do not chase.",
        ("non inseguire", "non pressare", "do nothing active"),
        "anti_bias_manual_v1:Action Bias Guard",
        1.0,
    ),
)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def weights_sha256(weights: dict[str, float]) -> str:
    payload = json.dumps(weights, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(payload)


def strip_generated_sections(text: str) -> str:
    clean = text.rstrip()
    for marker in (REPORT_SECTION, LEGACY_SUMMARY_SECTION):
        pattern = re.compile(rf"\n\n{re.escape(marker)}\n.*?(?=\n\n## |\Z)", re.S)
        clean = pattern.sub("", clean).rstrip()
        if clean.startswith(marker):
            clean = ""
    return clean.rstrip() + ("\n" if clean else "")


def normalize_text(text: str) -> str:
    text = text.lower()
    text = text.replace("è", "e").replace("é", "e").replace("à", "a")
    text = text.replace("ù", "u").replace("ò", "o").replace("ì", "i")
    return re.sub(r"\s+", " ", text)


def load_weights(path: Path = WEIGHTS_PATH) -> dict[str, float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    weights = {str(key): float(value) for key, value in raw.items()}
    validate_weights(weights)
    return weights


def validate_weights(weights: dict[str, float]) -> None:
    max_fact = max(abs(value) for key, value in weights.items() if key.startswith("observed_fact:"))
    for key, value in weights.items():
        kind = key.split(":", 1)[0]
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"Unknown evidence kind in {key!r}")
        if key.startswith("inference:") and abs(value) > max_fact * 0.30:
            raise ValueError(f"Inference weight violates 30% cap: {key}={value}")
        if key in {"contradiction:mancato_invito_sociale", "contradiction:mancato_invito_sociale_esterno"} and value < -7:
            raise ValueError("Social non-invite cap violated")


def max_observed_fact_weight(weights: dict[str, float] | None = None) -> float:
    weights = weights or load_weights()
    return max(abs(value) for key, value in weights.items() if key.startswith("observed_fact:"))


def _evidence_from_rule(rule: EvidenceRule, weights: dict[str, float]) -> dict[str, Any]:
    return {
        "id": rule.evidence_id,
        "kind": rule.kind,
        "text": rule.text,
        "weight_key": rule.weight_key,
        "weight": weights[rule.weight_key],
        "confidence": rule.confidence,
        "manual_ref": rule.manual_ref,
        "matched_terms": list(rule.terms),
        "tags": list(rule.tags),
    }


def extract_evidence_rule_based(report_text: str, weights: dict[str, float] | None = None) -> list[dict[str, Any]]:
    weights = weights or load_weights()
    normalized = normalize_text(report_text)
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()

    for rule in EVIDENCE_RULES:
        normalized_terms = [normalize_text(term) for term in rule.terms]
        if any(term in normalized for term in normalized_terms):
            if rule.evidence_id in seen:
                continue
            seen.add(rule.evidence_id)
            item = _evidence_from_rule(rule, weights)
            item["matched_terms"] = [term for term, norm in zip(rule.terms, normalized_terms) if norm in normalized]
            evidence.append(item)

    return evidence


def read_cached_or_extract_evidence(
    report_text: str,
    *,
    cache_path: Path | None = DEFAULT_EVIDENCE_CACHE,
    force_extract: bool = False,
    weights: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    report_hash = sha256_text(report_text)
    weights = weights or load_weights()
    weights_hash = weights_sha256(weights)

    if cache_path and cache_path.exists() and not force_extract:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cached = {}
        if (
            cached.get("report_sha256") == report_hash
            and cached.get("weights_sha256") == weights_hash
            and cached.get("formula_version") == FORMULA_VERSION
            and cached.get("extractor_version") == EXTRACTOR_VERSION
            and isinstance(cached.get("evidence"), list)
        ):
            return list(cached["evidence"]), {
                "report_sha256": report_hash,
                "cache_path": str(cache_path),
                "cache_hit": True,
            }

    evidence = extract_evidence_rule_based(report_text, weights)
    cache_payload = {
        "formula_version": FORMULA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "weights_sha256": weights_hash,
        "report_sha256": report_hash,
        "evidence": evidence,
    }
    cache_write_error = None
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(cache_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            cache_write_error = repr(exc)

    return evidence, {
        "report_sha256": report_hash,
        "cache_path": str(cache_path) if cache_path else None,
        "cache_hit": False,
        "cache_write_error": cache_write_error,
    }


def _bias_flags(evidence: Sequence[dict[str, Any]], confidence: float, raw_score: float) -> list[str]:
    flags: set[str] = set()
    if not evidence:
        flags.add("no_evidence")
    contradiction_count = sum(1 for item in evidence if item.get("kind") == "contradiction")
    inference_count = sum(1 for item in evidence if item.get("kind") == "inference")
    observed_count = sum(1 for item in evidence if item.get("kind") == "observed_fact")
    if contradiction_count:
        flags.add("contradiction_present")
    if inference_count > observed_count:
        flags.add("too_many_inferences")
    if any("mind_reading" in item.get("tags", []) for item in evidence):
        flags.add("mind_reading_risk")
    if any(item.get("id") == "mancato_invito_sociale" for item in evidence):
        flags.add("social_exclusion_cap")
    if raw_score > 70 and observed_count == 0:
        flags.add("weak_evidence_high_score")
    if confidence < 0.7:
        flags.add("low_confidence")
    return sorted(flags)


def select_action(prudential_score: float, confidence: float) -> str:
    if confidence < 0.7:
        return "monitor_only" if prudential_score < 40 else "do_nothing_active"
    if prudential_score >= 80 and confidence > 0.9:
        return "available_for_reconnection"
    if prudential_score >= 70 and confidence > 0.8:
        return "light_open"
    if prudential_score < 40:
        return "monitor_only"
    return "do_nothing_active"


def calculate_scores(evidence: Sequence[dict[str, Any]]) -> dict[str, Any]:
    weights_sum = sum(float(item.get("weight", 0.0)) for item in evidence)
    raw_score = 50.0 + weights_sum
    contradiction_count = sum(1 for item in evidence if item.get("kind") == "contradiction")
    inference_count = sum(1 for item in evidence if item.get("kind") == "inference")
    mind_reading_seen = any("mind_reading" in item.get("tags", []) for item in evidence)
    confidence = max(0.0, 1.0 - (contradiction_count * 0.1 + inference_count * 0.05 + (0.05 if mind_reading_seen else 0.0)))
    rlfull_current = clamp(raw_score * confidence)
    prudential_score = clamp(raw_score * min(confidence, 0.8))
    relcalc_score = round((rlfull_current * 0.6) + (prudential_score * 0.4), 3)
    operative_range = f"{int(clamp(prudential_score - 3))}-{int(clamp(prudential_score + 3))}"
    action = select_action(prudential_score, confidence)
    flags = _bias_flags(evidence, confidence, raw_score)

    if "mind_reading_risk" in flags and action in {"light_open", "available_for_reconnection"}:
        action = "do_nothing_active"
        flags = sorted(set(flags) | {"action_downgraded_for_mind_reading"})

    return {
        "raw_score": round(raw_score, 3),
        "rlfull_current": round(rlfull_current, 3),
        "prudential_score": round(prudential_score, 3),
        "relcalc_score": relcalc_score,
        "confidence": round(confidence, 3),
        "operative_range": operative_range,
        "action": action,
        "bias_flags": flags,
    }


def build_trace(evidence: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": item.get("id"),
            "kind": item.get("kind"),
            "text": item.get("text"),
            "weight_key": item.get("weight_key"),
            "weight": item.get("weight"),
            "confidence": item.get("confidence"),
            "manual_ref": item.get("manual_ref"),
        }
        for item in evidence
    ]


def read_memory_context(memory_dir: Path = DEFAULT_MEMORY_DIR) -> dict[str, Any]:
    if not memory_dir.exists():
        return {"memory_dir": str(memory_dir), "available": False, "files": []}
    files = sorted(path.name for path in memory_dir.iterdir() if path.is_file())
    return {"memory_dir": str(memory_dir), "available": True, "files": files[:20]}


def build_formula_section(result: dict[str, Any]) -> str:
    summary = {
        "formula_version": result["formula_version"],
        "rlfull_current": result["rlfull_current"],
        "prudential_score": result["prudential_score"],
        "relcalc_score": result["relcalc_score"],
        "confidence": result["confidence"],
        "operative_range": result["operative_range"],
        "action": result["action"],
        "bias_flags": result["bias_flags"],
        "trace": result["trace"][:8],
    }
    return REPORT_SECTION + "\n\n```json\n" + json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"


def update_report_section(report_path: Path, result: dict[str, Any]) -> None:
    if not report_path.exists():
        return
    text = strip_generated_sections(report_path.read_text(encoding="utf-8", errors="replace")).rstrip()
    section = build_formula_section(result)
    text = text + "\n\n" + section if text else section
    report_path.write_text(text.rstrip() + "\n", encoding="utf-8")


def score_text(
    report_text: str,
    *,
    report_path: Path | None = None,
    memory_dir: Path = DEFAULT_MEMORY_DIR,
    evidence_cache_path: Path | None = DEFAULT_EVIDENCE_CACHE,
    force_extract: bool = False,
) -> dict[str, Any]:
    weights = load_weights()
    scoring_text = strip_generated_sections(report_text)
    evidence, cache_meta = read_cached_or_extract_evidence(
        scoring_text,
        cache_path=evidence_cache_path,
        force_extract=force_extract,
        weights=weights,
    )
    scores = calculate_scores(evidence)
    result = {
        "formula_version": FORMULA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        **scores,
        "evidence_count": len(evidence),
        "evidence": evidence,
        "trace": build_trace(evidence),
        "source": {
            "report_path": str(report_path) if report_path else None,
            "report_sha256": cache_meta["report_sha256"],
            "evidence_cache_path": cache_meta.get("cache_path"),
            "evidence_cache_write_error": cache_meta.get("cache_write_error"),
        },
        "memory": read_memory_context(memory_dir),
        "llm_runtime_scoring": False,
        "llm_extraction_used": False,
        "review_rules_status": "stub_only",
    }
    return result


def score_report(
    report_path: Path = DEFAULT_REPORT,
    *,
    memory_dir: Path = DEFAULT_MEMORY_DIR,
    evidence_cache_path: Path = DEFAULT_EVIDENCE_CACHE,
    force_extract: bool = False,
    update_report: bool = True,
) -> dict[str, Any]:
    if not report_path.exists():
        raise FileNotFoundError(f"ABC report not found: {report_path}")
    text = report_path.read_text(encoding="utf-8", errors="replace")
    result = score_text(
        text,
        report_path=report_path,
        memory_dir=memory_dir,
        evidence_cache_path=evidence_cache_path,
        force_extract=force_extract,
    )
    if update_report:
        try:
            update_report_section(report_path, result)
        except OSError as exc:
            result["source"]["report_update_error"] = repr(exc)
    return result


def format_trace(result: dict[str, Any]) -> str:
    lines = [
        f"formula_version={result.get('formula_version')}",
        f"report_sha256={result.get('source', {}).get('report_sha256')}",
    ]
    for item in result.get("trace", []):
        lines.append(
            " | ".join(
                [
                    str(item.get("id")),
                    str(item.get("kind")),
                    f"weight={item.get('weight')}",
                    f"confidence={item.get('confidence')}",
                    str(item.get("manual_ref")),
                    str(item.get("text")),
                ]
            )
        )
    return "\n".join(lines)


def review_rules_stub() -> dict[str, Any]:
    return {
        "status": "not_implemented",
        "formula_version": FORMULA_VERSION,
        "llm_allowed_scope": "proposal_only",
        "runtime_scoring_allowed": False,
        "required_flow": [
            "generate proposed rulebook/weight diff",
            "generate or update calibration tests",
            "human approval",
            "version bump",
            "deterministic test run",
        ],
    }


def _path_arg(value: str | None, default: Path) -> Path:
    return Path(value).expanduser().resolve() if value else default


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ABC deterministic formula loop")
    sub = parser.add_subparsers(dest="command", required=True)

    score_parser = sub.add_parser("score")
    score_parser.add_argument("--report")
    score_parser.add_argument("--memory-dir")
    score_parser.add_argument("--evidence-cache")
    score_parser.add_argument("--force-extract", action="store_true")
    score_parser.add_argument("--no-update-report", action="store_true")

    trace_parser = sub.add_parser("trace")
    trace_parser.add_argument("--report")
    trace_parser.add_argument("--memory-dir")
    trace_parser.add_argument("--evidence-cache")
    trace_parser.add_argument("--force-extract", action="store_true")

    sub.add_parser("review-rules")

    args = parser.parse_args(argv)

    if args.command == "review-rules":
        print(json.dumps(review_rules_stub(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    report = _path_arg(args.report, DEFAULT_REPORT)
    memory_dir = _path_arg(args.memory_dir, DEFAULT_MEMORY_DIR)
    evidence_cache = _path_arg(args.evidence_cache, DEFAULT_EVIDENCE_CACHE)
    result = score_report(
        report,
        memory_dir=memory_dir,
        evidence_cache_path=evidence_cache,
        force_extract=args.force_extract,
        update_report=not getattr(args, "no_update_report", False) and args.command == "score",
    )

    if args.command == "trace":
        print(format_trace(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
