"""Deterministic ABC formula loop.

Runtime scoring is rule-based and reproducible:

- evidence is extracted from the current report by fixed lexical rules;
- evidence is cached with the report hash in `.ralf_run/last_evidence.json`;
- scores are calculated only from versioned weights;
- semantic extraction can propose bounded evidence atoms before scoring;
- no LLM/extractor can produce final scores, ranges, or actions.
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
EXTRACTOR_VERSION = "abc_rule_extractor_v2_semantic_atoms"
REPORT_SECTION = "## ABC Formula Loop"
LEGACY_SUMMARY_SECTION = "## ABC / Relcalc deterministic summary"
FORMULA_SCORING_INPUT_SECTION = "## formula_scoring_input"
RULEBOOK_DIR = Path(__file__).resolve().parent / "abc_rulebooks"
WEIGHTS_PATH = RULEBOOK_DIR / "evidence_weights_v1.json"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN_DIR = ROOT / ".ralf_run"
DEFAULT_REPORT = DEFAULT_RUN_DIR / "RSC_ABC_RAPPORTO_FULL_CURRENT.txt"
DEFAULT_MEMORY_DIR = ROOT / "abc_memory"
DEFAULT_EVIDENCE_CACHE = DEFAULT_RUN_DIR / "last_evidence.json"

SEMANTIC_ATOM_DEFAULT_WEIGHTS = {
    "observed_fact:autoinvito_familiare": 3.0,
    "observed_fact:permanenza_familiare_reale": 2.0,
    "observed_fact:gancio_futuro_domestico": 2.0,
    "observed_fact:conversione_logistica_in_presenza": 2.0,
    "boundary:boundary_limite_contatto_attivazione": -1.2,
}

EVIDENCE_KINDS = {
    "observed_fact",
    "inference",
    "contradiction",
    "external_signal",
    "operational_constraint",
    "boundary",
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
        "micro_riparazione_privata",
        "observed_fact",
        "observed_fact:micro_riparazione_privata",
        "Private repair after a social-exclusion wound; attenuates catastrophic reading without erasing the wound.",
        ("micro-riparazione privata", "riapre il canale privato", "riapertura del canale privato"),
        "relation_manual_v1:Attachment/Repair after rupture",
        0.78,
    ),
    EvidenceRule(
        "cena_1_1_domestica",
        "observed_fact",
        "observed_fact:cena_1_1_domestica",
        "Domestic 1:1 dinner after a logistical bridge; stronger than pure logistics, weaker than public inclusion.",
        ("cena 1:1 domestica", "cena a casa di fabio", "cena a casa tua"),
        "relation_manual_v1:Interdependence/Domestic 1:1",
        0.8,
    ),
    EvidenceRule(
        "autoinvito_familiare",
        "observed_fact",
        "observed_fact:autoinvito_familiare",
        "Arianna spontaneously inserts herself into the Fabio-mother family routine.",
        (),
        "relation_manual_v1:Interdependence/Family routine agency",
        0.82,
    ),
    EvidenceRule(
        "permanenza_familiare_reale",
        "observed_fact",
        "observed_fact:permanenza_familiare_reale",
        "Real domestic-family stay: about one hour, actual eating, relaxed family posture.",
        (),
        "relation_manual_v1:Interdependence/Real domestic permanence",
        0.8,
    ),
    EvidenceRule(
        "gancio_futuro_domestico",
        "observed_fact",
        "observed_fact:gancio_futuro_domestico",
        "Unnecessary future domestic hook, such as shared garden apples.",
        (),
        "relation_manual_v1:Future Orientation/Domestic continuity",
        0.76,
    ),
    EvidenceRule(
        "conversione_logistica_in_presenza",
        "observed_fact",
        "observed_fact:conversione_logistica_in_presenza",
        "A logistical request becomes accompaniment, dinner, and real presence.",
        (),
        "relation_manual_v1:Interdependence/Logistics-to-presence transition",
        0.8,
    ),
    EvidenceRule(
        "boundary_limite_contatto_attivazione",
        "boundary",
        "boundary:boundary_limite_contatto_attivazione",
        "Physical-contact limit appears situational under anger or stress; it reduces physical push, not the whole positive domestic delta.",
        (),
        "anti_bias_manual_v1:Body-boundary caution",
        0.78,
        ("contact_boundary",),
    ),

    EvidenceRule(
        "auto_invito_implicito_cibo",
        "observed_fact",
        "observed_fact:auto_invito_implicito_cibo",
        "Arianna converts Fabio's logistical bridge into a food/dinner opening by asking whether he has eaten and stating she only has fruit.",
        (),
        "relation_manual_v1:Agency/Implicit invitation",
        0.8,
    ),
    EvidenceRule(
        "logistica_convertita_in_convivialita",
        "observed_fact",
        "observed_fact:logistica_convertita_in_convivialita",
        "A logistical hook becomes a shared domestic meal through Arianna's own conversational move.",
        (),
        "relation_manual_v1:Interdependence/Logistics-to-intimacy transition",
        0.75,
    ),

    EvidenceRule(
        "contenuti_personali_1_1",
        "observed_fact",
        "observed_fact:contenuti_personali_1_1",
        "Personal content shared in a private low-pressure frame.",
        ("contenuti personali", "racconta contenuti personali", "contenuti personali del ritiro"),
        "relation_manual_v1:Attachment/Safe haven disclosure",
        0.72,
    ),
    EvidenceRule(
        "contatto_tollerato",
        "observed_fact",
        "observed_fact:contatto_tollerato",
        "Low-intensity body contact is tolerated rather than rejected.",
        ("contatto sul braccio non respinto", "contatto non respinto", "contatto fisico sul braccio", "non rifiutato", "non ha respinto il contatto", "toccarla un paio di volte sul braccio"),
        "relation_manual_v1:Attachment/Low-intensity contact",
        0.68,
    ),

    EvidenceRule(
        "soglia_prolungata",
        "observed_fact",
        "observed_fact:soglia_prolungata",
        "Threshold contact extends beyond minimum logistics without becoming escalation.",
        (),
        "relation_manual_v1:Attachment/Threshold tolerance",
        0.72,
    ),
    EvidenceRule(
        "comfort_silenzi_soglia",
        "observed_fact",
        "observed_fact:comfort_silenzi_soglia",
        "Silence at the threshold is tolerated; no rapid cold cut.",
        (),
        "relation_manual_v1:Attachment/Low pressure co-regulation",
        0.68,
    ),
    EvidenceRule(
        "deflazione_frame_camper",
        "operational_constraint",
        "operational_constraint:deflazione_frame_camper",
        "Camper/Poggio Rusco frame is deflated to practical geography, not a shared-vacation opening.",
        (),
        "anti_bias_manual_v1:No project escalation without explicit marker",
        0.75,
    ),
    EvidenceRule(
        "mancato_aggancio_mare_vacanza",
        "operational_constraint",
        "operational_constraint:mancato_aggancio_mare_vacanza",
        "No explicit hook into sea/vacation/couple-project frame.",
        (),
        "anti_bias_manual_v1:Micro-signal cap",
        0.8,
    ),
    EvidenceRule(
        "campo_bestia_agosto",
        "operational_constraint",
        "operational_constraint:campo_bestia_agosto",
        "August pet-care remains a practical future projection, not a romantic projection.",
        (),
        "anti_bias_manual_v1:Logistics are not romantic proof",
        0.7,
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


def select_scoring_text(text: str) -> str:
    clean = strip_generated_sections(text)
    marker_index = clean.find(FORMULA_SCORING_INPUT_SECTION)
    if marker_index < 0:
        return clean
    scoring_text = clean[marker_index + len(FORMULA_SCORING_INPUT_SECTION):].lstrip()
    end_positions = []
    stop_markers = (
        REPORT_SECTION,
        LEGACY_SUMMARY_SECTION,
        "## merged_current_context",
        "## current_facts",
        "## interpretation_after_merge",
        "## operational_rule",
    )
    for marker in stop_markers:
        pos = scoring_text.find(f"\n{marker}")
        if pos >= 0:
            end_positions.append(pos)
    if end_positions:
        scoring_text = scoring_text[: min(end_positions)]
    scoring_text = scoring_text.strip()
    return scoring_text + ("\n" if scoring_text else "")


def normalize_text(text: str) -> str:
    text = text.lower()
    text = text.replace("è", "e").replace("é", "e").replace("à", "a")
    text = text.replace("ù", "u").replace("ò", "o").replace("ì", "i")
    return re.sub(r"\s+", " ", text)


def load_weights(path: Path = WEIGHTS_PATH) -> dict[str, float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    weights = {str(key): float(value) for key, value in raw.items()}
    for key, value in SEMANTIC_ATOM_DEFAULT_WEIGHTS.items():
        weights.setdefault(key, float(value))
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


def _evidence_rule_by_id() -> dict[str, EvidenceRule]:
    return {rule.evidence_id: rule for rule in EVIDENCE_RULES}


def _json_candidate_blocks(text: str) -> Iterable[str]:
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL):
        yield match.group(1).strip()
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        yield stripped


def _iter_structured_evidence_items(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_structured_evidence_items(item)
        return
    if not isinstance(value, dict):
        return
    if "evidence_id" in value:
        yield value
    for key in ("evidence", "evidences", "structured_evidence", "interpretation_table", "items"):
        nested = value.get(key)
        if nested is not None:
            yield from _iter_structured_evidence_items(nested)


def _normalized_field(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if isinstance(value, (list, tuple)):
        value = " ".join(str(part) for part in value)
    return normalize_text(str(value or ""))


def _structured_confidence(item: dict[str, Any]) -> float:
    try:
        return float(item.get("confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _has_counter_evidence(item: dict[str, Any]) -> bool:
    counter = item.get("counter_evidence")
    if counter is None:
        return False
    if isinstance(counter, str):
        return bool(counter.strip())
    if isinstance(counter, Iterable):
        return any(bool(str(part).strip()) for part in counter)
    return bool(counter)


def _valid_auto_invito_food(item: dict[str, Any]) -> bool:
    if _structured_confidence(item) < 0.60 or _has_counter_evidence(item):
        return False
    quote = _normalized_field(item, "quote")
    speaker = _normalized_field(item, "speaker")
    context = _normalized_field(item, "context")
    interpretation = _normalized_field(item, "interpretation")
    notes = _normalized_field(item, "notes")
    combined = " ".join((quote, context, interpretation, notes))
    has_food_quote = any(marker in quote for marker in ("hai mangiato", "avesse gia mangiato", "cosa mangia", "cosa mangi", "domanda sul mangiare", "solo frutta"))
    has_speaker = "arianna" in speaker
    has_logistic_context = any(
        marker in context
        for marker in ("ombrello", "profumeria", "pioggia", "gancio logistico", "gancio", "logistica")
    )
    opens_food_frame = any(marker in interpretation for marker in ("cibo", "cena", "food", "dinner"))
    has_reinforcement = any(marker in combined for marker in ("solo frutta", "cena", "dinner", "condivisione cibo"))
    return has_food_quote and has_speaker and has_logistic_context and opens_food_frame and has_reinforcement


def _valid_logistics_to_conviviality(item: dict[str, Any]) -> bool:
    if _structured_confidence(item) < 0.60 or _has_counter_evidence(item):
        return False
    context = _normalized_field(item, "context")
    interpretation = _normalized_field(item, "interpretation")
    has_logistic_context = any(
        marker in context
        for marker in ("ombrello", "profumeria", "pioggia", "bestia", "gancio logistico", "gancio", "logistica")
    )
    has_conviviality = any(
        marker in interpretation
        for marker in ("cibo", "cena", "food", "dinner", "convivialita", "conviviality", "condivisione")
    )
    return has_logistic_context and has_conviviality


def _combined_structured_text(item: dict[str, Any]) -> str:
    return " ".join(
        _normalized_field(item, key)
        for key in ("quote", "context", "interpretation", "notes", "source_span")
    )


def _valid_structured_marker_evidence(item: dict[str, Any], required_groups: Sequence[Sequence[str]]) -> bool:
    if _structured_confidence(item) < 0.60 or _has_counter_evidence(item):
        return False
    text = _combined_structured_text(item)
    return all(any(marker in text for marker in group) for group in required_groups)


def _structured_evidence_item_is_valid(item: dict[str, Any]) -> bool:
    evidence_id = str(item.get("evidence_id") or "")
    if evidence_id == "auto_invito_implicito_cibo":
        return _valid_auto_invito_food(item)
    if evidence_id == "logistica_convertita_in_convivialita":
        return _valid_logistics_to_conviviality(item)
    if evidence_id == "soglia_prolungata":
        return _valid_structured_marker_evidence(
            item,
            (("soglia", "scala", "scale"), ("tratten", "prolung", "oltre il minimo", "non rapida", "non fredda")),
        )
    if evidence_id == "comfort_silenzi_soglia":
        return _valid_structured_marker_evidence(item, (("soglia", "scala", "scale"), ("silenzi", "silenzio")))
    if evidence_id == "deflazione_frame_camper":
        return _valid_structured_marker_evidence(
            item,
            (("camper", "poggio rusco"), ("deflaz", "pratico", "geografico", "non vacanza")),
        )
    if evidence_id == "mancato_aggancio_mare_vacanza":
        return _valid_structured_marker_evidence(
            item,
            (("mare", "vacanza", "camper"), ("non", "senza"), ("aggancio", "avvicinamento", "apre", "progetto")),
        )
    if evidence_id == "campo_bestia_agosto":
        return _valid_structured_marker_evidence(item, (("bestia",), ("agosto",), ("pratic", "gestione")))
    return False


def extract_structured_interpreted_evidence(
    report_text: str,
    weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    weights = weights or load_weights()
    rules = _evidence_rule_by_id()
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in _json_candidate_blocks(report_text):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        for source_item in _iter_structured_evidence_items(parsed):
            evidence_id = str(source_item.get("evidence_id") or "")
            if evidence_id in seen or not _structured_evidence_item_is_valid(source_item):
                continue
            rule = rules.get(evidence_id)
            if rule is None:
                continue
            item = _evidence_from_rule(rule, weights)
            item["confidence"] = min(rule.confidence, _structured_confidence(source_item))
            item["matched_terms"] = [str(source_item.get("quote") or source_item.get("source_span") or evidence_id)]
            item["structured_interpretation"] = {
                "quote": source_item.get("quote"),
                "speaker": source_item.get("speaker"),
                "context": source_item.get("context"),
                "interpretation": source_item.get("interpretation"),
                "confidence": source_item.get("confidence"),
                "counter_evidence": source_item.get("counter_evidence") or [],
                "notes": source_item.get("notes"),
            }
            seen.add(evidence_id)
            evidence.append(item)
    return evidence


def _has_any(text: str, markers: Sequence[str]) -> bool:
    return any(marker in text for marker in markers)


def _append_once(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _add_semantic_atom(
    atoms_by_id: dict[str, dict[str, Any]],
    *,
    evidence_id: str,
    kind: str,
    weight_suggestion: float,
    confidence: float,
    supporting_facts: Sequence[str],
    cap_interactions: Sequence[str] = (),
) -> None:
    if evidence_id in atoms_by_id:
        current = atoms_by_id[evidence_id]
        current["confidence"] = max(float(current["confidence"]), confidence)
        for fact in supporting_facts:
            _append_once(current["supporting_facts"], fact)
        for cap in cap_interactions:
            _append_once(current["cap_interactions"], cap)
        return
    atoms_by_id[evidence_id] = {
        "id": evidence_id,
        "kind": kind,
        "weight_suggestion": weight_suggestion,
        "confidence": confidence,
        "supporting_facts": list(supporting_facts),
        "cap_interactions": list(cap_interactions),
    }


def extract_llm_evidence_pre_scoring(report_text: str, weights: dict[str, float] | None = None) -> dict[str, Any]:
    """Return a bounded semantic-auditor schema without producing scores.

    The MVP adapter is deterministic so runtime tests remain reproducible. Its
    output shape is the contract expected from a future local LLM extractor.
    """
    weights = weights or load_weights()
    low = normalize_text(report_text)
    event_id = f"event_{sha256_text(report_text)[:12]}"
    facts: list[str] = []
    interpretations: list[str] = []
    counter_evidence: list[str] = []
    warnings: list[str] = []
    atoms_by_id: dict[str, dict[str, Any]] = {}

    window_call = _has_any(low, ("chiamata dalla finestra", "chiama dalla finestra", "dalla finestra"))
    print_bridge = _has_any(low, ("stampa", "stampare", "stampato"))
    accompaniment = _has_any(low, ("accompagnamento", "accompagna", "spedizione"))
    mother = _has_any(low, ("madre", "tua madre", "sua madre"))
    autoinvite = _has_any(low, ("autoinvito", "auto invito", "si inserisce spontaneamente"))
    dinner_frame = "cena" in low
    actual_eating = _has_any(low, ("mangia davvero", "mangiare davvero", "si convinto al mangiare", "si' convinto al mangiare"))
    dinner = dinner_frame or actual_eating
    real_stay = bool(re.search(r"\b20[:.]14\b.*\b21[:.]16\b", low)) or _has_any(
        low,
        ("permanenza 20:14-21:16", "permanenza 20.14-21.16", "resta circa un'ora", "circa un'ora", "permanenza 1h"),
    )
    family_relaxed = _has_any(low, ("scioltezza familiare", "sciolta", "sciolto", "routine familiare"))
    apples_hook = "mele" in low and _has_any(low, ("giardino", "proposta", "attivita futura", "attivita' futura"))
    contact_boundary = _has_any(
        low,
        (
            "limite corporeo",
            "limite contatto",
            "limite fisico",
            "non toccare",
            "contatto fisico peggiora",
            "contatto peggiora",
        ),
    ) and _has_any(low, ("arrabbiata", "stressata", "stress", "attivazione", "situazionale"))
    social_cap = _has_any(
        low,
        (
            "social_exclusion_cap",
            "non e stato invitato",
            "non invitato",
            "mancata inclusione sociale",
            "manca inclusione sociale esterna",
        ),
    )
    generic_domestic_label = _has_any(low, ("investimento familiare", "campo domestico", "logistica affettiva", "routine familiare"))

    if window_call:
        facts.append("Call from the window observed.")
    if print_bridge:
        facts.append("Print request observed.")
    if accompaniment:
        facts.append("Accompaniment or shipping movement observed.")
    if autoinvite and mother:
        facts.append("Self-invitation into Fabio-mother routine observed.")
    if actual_eating:
        facts.append("Actual eating or clear yes to eating observed.")
    if real_stay:
        facts.append("Real stay around one hour observed.")
    if family_relaxed:
        facts.append("Relaxed family posture observed.")
    if apples_hook:
        facts.append("Future domestic apples/garden hook observed.")
    if contact_boundary:
        facts.append("Physical-contact boundary under anger or stress observed.")
    if social_cap:
        counter_evidence.append("No external social inclusion observed; social_exclusion_cap remains active.")

    domestic_cap_interactions = ["social_exclusion_cap"] if social_cap else []
    if autoinvite and mother:
        _add_semantic_atom(
            atoms_by_id,
            evidence_id="autoinvito_familiare",
            kind="observed_fact",
            weight_suggestion=weights.get("observed_fact:autoinvito_familiare", 3.0),
            confidence=0.82,
            supporting_facts=[fact for fact in facts if "Self-invitation" in fact or "routine" in fact],
            cap_interactions=domestic_cap_interactions,
        )
    if real_stay and (actual_eating or family_relaxed) and (mother or family_relaxed):
        _add_semantic_atom(
            atoms_by_id,
            evidence_id="permanenza_familiare_reale",
            kind="observed_fact",
            weight_suggestion=weights.get("observed_fact:permanenza_familiare_reale", 2.0),
            confidence=0.8,
            supporting_facts=[fact for fact in facts if "stay" in fact or "eating" in fact or "Relaxed" in fact],
            cap_interactions=domestic_cap_interactions,
        )
    if apples_hook:
        _add_semantic_atom(
            atoms_by_id,
            evidence_id="gancio_futuro_domestico",
            kind="observed_fact",
            weight_suggestion=weights.get("observed_fact:gancio_futuro_domestico", 2.0),
            confidence=0.76,
            supporting_facts=[fact for fact in facts if "apples" in fact or "garden" in fact],
            cap_interactions=domestic_cap_interactions,
        )
    if print_bridge and accompaniment and (dinner or real_stay):
        _add_semantic_atom(
            atoms_by_id,
            evidence_id="conversione_logistica_in_presenza",
            kind="observed_fact",
            weight_suggestion=weights.get("observed_fact:conversione_logistica_in_presenza", 2.0),
            confidence=0.8,
            supporting_facts=[
                fact
                for fact in facts
                if "Print" in fact or "Accompaniment" in fact or "eating" in fact or "stay" in fact
            ],
            cap_interactions=domestic_cap_interactions,
        )
    if contact_boundary:
        _add_semantic_atom(
            atoms_by_id,
            evidence_id="boundary_limite_contatto_attivazione",
            kind="boundary",
            weight_suggestion=weights.get("boundary:boundary_limite_contatto_attivazione", -1.2),
            confidence=0.78,
            supporting_facts=[fact for fact in facts if "Physical-contact boundary" in fact],
            cap_interactions=["physical_push_reduction"],
        )
        counter_evidence.append("Body boundary reduces physical push; it does not erase domestic-family positives.")

    if atoms_by_id and social_cap:
        interpretations.append("Domestic-family positives are scored under social_exclusion_cap, not as external social inclusion.")
    if contact_boundary:
        interpretations.append("Contact limit is situational and should constrain physical escalation.")
    if generic_domestic_label and len(atoms_by_id) >= 2:
        warnings.append("event_underweighted_generic_domestic_label")
    if atoms_by_id and not any(atom["supporting_facts"] for atom in atoms_by_id.values()):
        warnings.append("semantic_atoms_need_fact_support")

    return {
        "event_id": event_id,
        "facts": facts,
        "interpretations": interpretations,
        "counter_evidence": counter_evidence,
        "evidence_atoms": list(atoms_by_id.values()),
        "bounded_delta_suggestion": {
            "prudential_delta": 0.0,
            "reason": "Extractor proposes evidence atoms only; deterministic formula owns final score, range, and action.",
        },
        "warnings": warnings,
    }


def _semantic_extraction_to_evidence(
    extraction: dict[str, Any],
    weights: dict[str, float],
) -> list[dict[str, Any]]:
    rules = _evidence_rule_by_id()
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for atom in extraction.get("evidence_atoms", []):
        if not isinstance(atom, dict):
            continue
        evidence_id = str(atom.get("id") or "")
        if evidence_id in seen:
            continue
        rule = rules.get(evidence_id)
        if rule is None:
            continue
        item = _evidence_from_rule(rule, weights)
        try:
            atom_confidence = float(atom.get("confidence", rule.confidence))
        except (TypeError, ValueError):
            atom_confidence = rule.confidence
        item["confidence"] = min(rule.confidence, atom_confidence)
        item["matched_terms"] = [str(fact) for fact in atom.get("supporting_facts", [])]
        item["llm_evidence_atom"] = {
            "id": evidence_id,
            "kind": atom.get("kind"),
            "weight_suggestion": atom.get("weight_suggestion"),
            "confidence": atom.get("confidence"),
            "supporting_facts": atom.get("supporting_facts", []),
            "cap_interactions": atom.get("cap_interactions", []),
        }
        seen.add(evidence_id)
        evidence.append(item)
    return evidence


def extract_evidence_rule_based(report_text: str, weights: dict[str, float] | None = None) -> list[dict[str, Any]]:
    weights = weights or load_weights()
    normalized = normalize_text(report_text)
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()

    for rule in EVIDENCE_RULES:
        normalized_terms = [normalize_text(term) for term in rule.terms]
        matched_terms = [
            term
            for term, norm in zip(rule.terms, normalized_terms)
            if norm in normalized and not _term_match_is_negated(rule.evidence_id, normalized, norm)
        ]
        if matched_terms:
            if rule.evidence_id in seen:
                continue
            seen.add(rule.evidence_id)
            item = _evidence_from_rule(rule, weights)
            item["matched_terms"] = matched_terms
            evidence.append(item)


    for item in extract_structured_interpreted_evidence(report_text, weights):
        evidence_id = str(item.get("id") or "")
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        evidence.append(item)

    semantic_extraction = extract_llm_evidence_pre_scoring(report_text, weights)
    for item in _semantic_extraction_to_evidence(semantic_extraction, weights):
        evidence_id = str(item.get("id") or "")
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        evidence.append(item)

    # ABC_POST_FILTER_AUTO_INVITO_NOT_LOGISTICA_PURA
    # If Arianna converts a logistical bridge into dinner/food, the episode must not remain
    # classified as pure logistics. This preserves the anti-bias distinction:
    # logistics accepted passively != logistics converted by Arianna into conviviality.
    ids = {item.get("id") for item in evidence}
    if {
        "auto_invito_implicito_cibo",
        "logistica_convertita_in_convivialita",
        "conversione_logistica_in_presenza",
    } & ids:
        evidence = [item for item in evidence if item.get("id") != "logistica_pura"]

    return evidence


def _term_match_is_negated(evidence_id: str, normalized_text: str, normalized_term: str) -> bool:
    if evidence_id != "opacita_terzo":
        return False
    negation_markers = (
        "non ha parlato di",
        "non parla di",
        "nessun",
        "nessuna",
        "senza",
        "non nominato",
        "non nominata",
        "non citato",
        "non citata",
        "non crea",
        "non aumenta",
        "non prova",
    )
    for match in re.finditer(re.escape(normalized_term), normalized_text):
        before = normalized_text[max(0, match.start() - 80):match.start()]
        after = normalized_text[match.end():match.end() + 80]
        window = f"{before}{normalized_term}{after}"
        if any(marker in before or marker in window for marker in negation_markers):
            return True
    return False


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
    if any(
        item.get("id") == "mancato_invito_sociale"
        or "social_exclusion_cap" in item.get("llm_evidence_atom", {}).get("cap_interactions", [])
        for item in evidence
    ):
        flags.add("social_exclusion_cap")
    if raw_score > 70 and observed_count == 0:
        flags.add("weak_evidence_high_score")
    if confidence < 0.7:
        flags.add("low_confidence")
    return sorted(flags)


def _contains_time_after_21(text: str) -> bool:
    for hour, _minute in re.findall(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", text):
        if int(hour) >= 21:
            return True
    return False


def _has_unnegated_marker(text: str, markers: Sequence[str], negations: Sequence[str] | None = None) -> bool:
    negations = negations or ("no", "non", "nessun", "nessuna", "assenza", "senza")
    for marker in markers:
        for match in re.finditer(re.escape(marker), text):
            before = text[max(0, match.start() - 45):match.start()]
            window = text[max(0, match.start() - 45):match.end() + 35]
            if any(neg in before or neg in window for neg in negations):
                continue
            return True
    return False


def _proper_name_third_presence(raw_text: str) -> bool:
    patterns = (
        r"\barriv\w*\b[^.\n]{0,90}\bcon\s+[A-ZÀ-Ö][A-Za-zÀ-ÖØ-öø-ÿ']{2,}\b",
        r"\bcon\s+lei\s+(?:e|è|era|risulta|appare)?\s*(?:presente\s+)?[A-ZÀ-Ö][A-Za-zÀ-ÖØ-öø-ÿ']{2,}\b",
        r"\b[A-ZÀ-Ö][A-Za-zÀ-ÖØ-öø-ÿ']{2,}\s+(?:e|è|era|risulta|appare)\s+(?:dietro|passegger\w+|presente)\b",
    )
    ignored = {"Arianna", "Fabio"}
    for pattern in patterns:
        for match in re.finditer(pattern, raw_text):
            snippet = match.group(0)
            if any(name in snippet for name in ignored):
                continue
            if any(marker in snippet.lower() for marker in ("teresa", "padre", "sposa")):
                continue
            return True
    return False


def _has_third_presence_phrase(text: str) -> bool:
    patterns = (
        r"(?:persona terza|terzo)[^.\n]{0,80}(?:presente|presenza|osservat|fisic|arriva|rientra|con lei)",
        r"(?:presente|presenza|osservat|fisic|arriva|rientra|con lei)[^.\n]{0,80}(?:persona terza|terzo)",
    )
    negating_context = (
        "non aumenta",
        "non crea",
        "senza evidenza",
        "senza evidence",
        "non competitor",
        "non sono competitor",
        "pressione terzo",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            window = text[max(0, match.start() - 60):match.end() + 60]
            if any(marker in window for marker in negating_context):
                continue
            return True
    return False


def assess_third_delta(report_text: str) -> dict[str, Any]:
    """Bounded audit for observed third-person factual deltas.

    This is deterministic guardrail logic, not manual scoring and not a claim that a
    full couple/third-dominant state exists. It only prevents non-trivial factual
    micro-deltas from disappearing silently when anti-bias rules block escalation.
    """
    raw = report_text
    low = normalize_text(report_text)
    third_presence_observed = (
        _has_third_presence_phrase(low)
        or _proper_name_third_presence(raw)
    )
    third_presence_late_evening = third_presence_observed and (
        _contains_time_after_21(low)
        or any(marker in low for marker in ("sera", "notte", "notturn", "rientro tardi", "tarda serata"))
    )
    third_omitted_from_prior_narrative = third_presence_observed and any(
        marker in low
        for marker in (
            "non aveva nominato",
            "non aveva citato",
            "non ha nominato",
            "non ha citato",
            "mancata menzione",
            "omissione selettiva",
            "compartimentazione",
        )
    )
    timeline_ambiguous = third_omitted_from_prior_narrative and any(
        marker in low
        for marker in (
            "vocale era precedente",
            "vocale precedente",
            "successivo al vocale",
            "timeline ambigua",
            "non e certo",
            "non e' certo",
        )
    )
    no_affection_observed = third_presence_observed and any(
        marker in low
        for marker in (
            "nessuna effusione",
            "assenza di effusioni",
            "non percepisce effusioni",
            "non emergono baci",
            "nessun bacio",
            "nessun abbraccio",
            "no effusioni",
        )
    )
    no_overnight = third_presence_observed and any(
        marker in low
        for marker in (
            "non dorme",
            "non rimane a dormire",
            "non pernotta",
            "non pernottamento",
            "assenza di pernottamento",
            "non risulta permanenza notturna",
            "non entra",
            "non resta",
        )
    )
    subject_self_driving_or_autonomous = third_presence_observed and any(
        marker in low
        for marker in (
            "alla guida",
            "guida lei",
            "guidava lei",
            "gestire lei",
            "gestisce lei",
            "soggetto autonomo",
            "lei autonoma",
            "lei autonomo",
        )
    )
    third_passive_or_low_support = third_presence_observed and any(
        marker in low
        for marker in (
            "passegger",
            "dietro",
            "passivo",
            "passivita",
            "al telefono",
            "guarda il telefono",
            "non appare particolarmente attivo",
            "non aiuta",
            "supporto logistico debole",
        )
    )
    support_logistics_weak = third_passive_or_low_support or (
        third_presence_observed and "supporto logistico debole" in low
    )
    next_morning_repair = third_presence_observed and any(
        marker in low
        for marker in (
            "mattina successiva",
            "riaggancio mattutino",
            "7:25",
            "07:25",
            "chiama fabio fuori",
            "chiama fuori",
            "spiegazione e l'abbraccio",
            "spiegazione e abbraccio",
        )
    )
    functional_explanation = third_presence_observed and any(
        marker in low
        for marker in (
            "spiega perche",
            "spiega perché",
            "questo spiega",
            "spiegazione funzionale",
            "non sapeva guidare",
            "non sapeva guidare la vespa",
            "ha recuperato la vespa",
            "recuperato la vespa",
            "guidasse la moto",
            "guidava la moto",
        )
    )
    accepted_hug_repair = third_presence_observed and any(
        marker in low
        for marker in (
            "accetta l'abbraccio",
            "abbraccio appare naturale",
            "abbraccio naturale",
            "abbraccio non respinto",
            "non respinge l'abbraccio",
            "contatto fisico di conforto",
            "contatto fisico di fiducia",
        )
    )
    partial_transparency = third_omitted_from_prior_narrative or any(
        marker in low for marker in ("trasparenza narrativa parziale", "trasparenza parziale")
    )
    compartmentalization = third_omitted_from_prior_narrative or "compartimentazione" in low
    affection_positive = (
        third_presence_observed
        and not no_affection_observed
        and _has_unnegated_marker(low, ("effusioni", "baci", "contatto fisico caldo"))
    )
    overnight_positive = (
        third_presence_observed
        and not no_overnight
        and _has_unnegated_marker(low, ("dorme", "pernotta", "rimane a dormire", "entra in casa", "entra dentro"))
    )
    third_hug_positive = (
        third_presence_observed
        and not no_affection_observed
        and not accepted_hug_repair
        and _has_unnegated_marker(low, ("abbracci", "abbraccio"))
    )
    affection_or_overnight_positive = affection_positive or overnight_positive or third_hug_positive

    effect = 0.0
    if third_presence_observed:
        effect -= 2.4
    if third_presence_late_evening:
        effect -= 0.7
    if third_omitted_from_prior_narrative:
        effect -= 0.7
    if affection_or_overnight_positive:
        effect -= 4.0
    if no_affection_observed:
        effect += 0.8
    if no_overnight:
        effect += 0.9
    if subject_self_driving_or_autonomous:
        effect += 0.7
    if third_passive_or_low_support:
        effect += 0.6
    if next_morning_repair:
        effect += 0.6
    if functional_explanation:
        effect += 0.6
    if accepted_hug_repair:
        effect += 0.6

    if affection_or_overnight_positive:
        effect = max(-6.0, min(-2.5, effect))
    elif third_presence_observed:
        effect = max(-1.8, min(-0.4, effect))
    else:
        effect = 0.0

    if not third_presence_observed:
        net_effect = "none"
        reason = "No observed third presence in scoring input."
    elif affection_or_overnight_positive:
        net_effect = "bounded_prudential_negative"
        reason = "Observed third presence includes affection or overnight markers; impact is stronger but still bounded."
    elif next_morning_repair or functional_explanation or accepted_hug_repair:
        net_effect = "small_prudential_negative_with_repair_offset"
        reason = "Third presence and omission remain negative, but next-morning repair, functional explanation, accepted contact, no affection/no overnight/self-driving/passive third cap the impact."
    elif effect < 0:
        net_effect = "small_prudential_negative"
        reason = "Presence and narrative omission are negative, but no affection/no overnight/self-driving/passive third cap the impact."
    else:
        net_effect = "offset"
        reason = "Observed third presence is fully offset by no affection/no overnight/autonomy/passive-third markers."

    return {
        "third_presence_observed": third_presence_observed,
        "third_presence_late_evening": third_presence_late_evening,
        "third_omitted_from_prior_narrative": third_omitted_from_prior_narrative,
        "timeline_ambiguous": timeline_ambiguous,
        "no_affection_observed": no_affection_observed,
        "no_overnight": no_overnight,
        "subject_self_driving_or_autonomous": subject_self_driving_or_autonomous,
        "third_passive_or_low_support": third_passive_or_low_support,
        "support_logistics_weak": support_logistics_weak,
        "next_morning_repair": next_morning_repair,
        "functional_explanation": functional_explanation,
        "accepted_hug_repair": accepted_hug_repair,
        "partial_transparency": partial_transparency,
        "compartmentalization": compartmentalization,
        "affection_or_overnight_positive": affection_or_overnight_positive,
        "classification": "partial_transparency_not_lie" if timeline_ambiguous else ("compartmentalization" if compartmentalization else "none"),
        "net_effect": net_effect,
        "prudential_delta": round(effect, 3),
        "reason": reason,
    }


def apply_bounded_delta_adjustment(scores: dict[str, Any], assessment: dict[str, Any]) -> dict[str, Any]:
    delta = float(assessment.get("prudential_delta") or 0.0)
    adjusted = dict(scores)
    if delta == 0.0:
        return adjusted
    prudential_score = clamp(float(scores["prudential_score"]) + delta)
    adjusted["prudential_score"] = round(prudential_score, 3)
    adjusted["relcalc_score"] = round((float(adjusted["rlfull_current"]) * 0.6) + (prudential_score * 0.4), 3)
    adjusted["operative_range"] = f"{int(clamp(prudential_score - 3))}-{int(clamp(prudential_score + 3))}"
    adjusted["action"] = select_action(prudential_score, float(adjusted["confidence"]))
    adjusted["bias_flags"] = sorted(set(adjusted.get("bias_flags", [])) | {"third_delta_bounded_assessment"})
    return adjusted


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
        "bounded_delta_adjustment": result.get("bounded_delta_adjustment"),
        "third_delta_assessment": result.get("third_delta_assessment"),
        "llm_evidence_atoms": [
            atom.get("id")
            for atom in result.get("llm_evidence_extraction", {}).get("evidence_atoms", [])
        ],
        "llm_evidence_warnings": result.get("llm_evidence_extraction", {}).get("warnings", []),
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
    scoring_text = select_scoring_text(report_text)
    llm_evidence_extraction = extract_llm_evidence_pre_scoring(scoring_text, weights)
    evidence, cache_meta = read_cached_or_extract_evidence(
        scoring_text,
        cache_path=evidence_cache_path,
        force_extract=force_extract,
        weights=weights,
    )
    base_scores = calculate_scores(evidence)
    third_delta_assessment = assess_third_delta(scoring_text)
    scores = apply_bounded_delta_adjustment(base_scores, third_delta_assessment)
    result = {
        "formula_version": FORMULA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        **scores,
        "evidence_count": len(evidence),
        "evidence": evidence,
        "trace": build_trace(evidence),
        "base_scores_before_bounded_delta": base_scores,
        "bounded_delta_adjustment": {
            "applied": bool(third_delta_assessment.get("prudential_delta")),
            "prudential_delta": third_delta_assessment.get("prudential_delta", 0.0),
            "applied_to": "prudential_score",
            "reason": third_delta_assessment.get("reason"),
        },
        "third_delta_assessment": third_delta_assessment,
        "source": {
            "report_path": str(report_path) if report_path else None,
            "report_sha256": cache_meta["report_sha256"],
            "evidence_cache_path": cache_meta.get("cache_path"),
            "evidence_cache_write_error": cache_meta.get("cache_write_error"),
        },
        "memory": read_memory_context(memory_dir),
        "llm_runtime_scoring": False,
        "llm_extraction_used": bool(llm_evidence_extraction.get("evidence_atoms")),
        "llm_evidence_extractor_mode": "deterministic_mvp_adapter",
        "llm_evidence_extraction": llm_evidence_extraction,
        "review_rules_status": "semantic_extractor_mvp_only",
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
