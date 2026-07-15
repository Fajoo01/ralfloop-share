from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable


CATEGORIES = (
    "strategic_assessment",
    "conflicting_sources",
    "recommendation_required",
    "incomplete_rules",
    "evidence_synthesis",
    "domain_validation",
)
DOMAINS = {
    "synthetic_public_grant": ("graduatoria sperimentale", "punteggio", "commissione"),
    "synthetic_education_project": ("laboratorio scolastico", "partecipazione", "collegio"),
    "synthetic_system_architecture": ("servizio isolato", "latenza", "revisore tecnico"),
    "synthetic_association_governance": ("assemblea simulata", "quorum", "consiglio"),
    "synthetic_procurement": ("acquisto dimostrativo", "offerta", "responsabile"),
    "synthetic_nonprofit_policy": ("programma volontari", "copertura", "comitato"),
    "synthetic_project_risk": ("progetto pilota", "rischio", "risk owner"),
    "synthetic_data_governance": ("dataset sintetico", "completezza", "data steward"),
}

PLANNER_FIELDS = {
    "questions",
    "facts_selected",
    "rules_selected",
    "sources_selected",
    "interpretations",
    "missing_information",
}
CRITIC_FIELDS = {
    "criticisms",
    "contradictions",
    "rule_application_errors",
    "source_provenance_errors",
    "strongest_counterargument",
    "unresolved_issues",
}
SOLVER_FIELDS = {
    "position",
    "supporting_arguments",
    "counterarguments",
    "rule_application",
    "evidence_used",
    "uncertainties",
    "alternative_interpretations",
    "recommendation",
    "confidence",
    "human_decision_required",
}


def generate_dataset() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cases: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    for category in CATEGORIES:
        for domain_index, (domain_id, labels) in enumerate(DOMAINS.items()):
            for variant in range(5):
                case = _build_case(category, domain_id, labels, domain_index, variant)
                cases.append(case)
                traces.append(_build_traces(case))
    validate_dataset(cases, traces)
    return cases, traces


def _build_case(
    category: str,
    domain_id: str,
    labels: tuple[str, str, str],
    domain_index: int,
    variant: int,
) -> dict[str, Any]:
    subject, measure, authority = labels
    serial = domain_index * 5 + variant + 1
    prefix = f"{category[:4]}_{domain_index + 1:02d}_{variant + 1:02d}"
    fact_a = f"F_{prefix}_A"
    fact_b = f"F_{prefix}_B"
    fact_irrelevant = f"F_{prefix}_X"
    rule_a = f"R_{prefix}_A"
    rule_b = f"R_{prefix}_B"
    rule_irrelevant = f"R_{prefix}_X"
    source_a = f"S_{prefix}_A"
    source_b = f"S_{prefix}_B"
    source_irrelevant = f"S_{prefix}_X"
    threshold = 55 + ((serial * 7) % 31)
    observed = threshold + (4 if variant % 2 == 0 else -3)
    conflict = category == "conflicting_sources" or (category == "domain_validation" and variant == 0)
    insufficient = category == "incomplete_rules" or (category == "recommendation_required" and variant < 2)
    category_instruction = {
        "strategic_assessment": "Valuta due strategie e indica condizioni, rischi e alternativa più prudente.",
        "conflicting_sources": "Valuta fonti discordanti senza scegliere arbitrariamente quella favorevole.",
        "recommendation_required": "Formula una raccomandazione operativa ma non vincolante.",
        "incomplete_rules": "Spiega cosa manca e formula solo una raccomandazione condizionata.",
        "evidence_synthesis": "Combina evidenze pertinenti separando dato, regola e inferenza.",
        "domain_validation": "Valuta coerenza del dominio sintetico e segnala regole non applicabili.",
    }[category]
    question = (
        f"Caso sintetico {serial}: il {subject} deve essere valutato rispetto a {measure}. "
        f"{category_instruction} Quale posizione motivata dovrebbe adottare {authority}?"
    )
    facts = [
        {"fact_id": fact_a, "kind": "fact", "statement": f"La misura osservata è {observed} su 100 nel caso {serial}."},
        {"fact_id": fact_b, "kind": "fact", "statement": f"Il vincolo di costo sintetico è {10 + serial} unità e non può essere superato."},
        {"fact_id": fact_irrelevant, "kind": "irrelevant", "statement": f"Il colore del fascicolo dimostrativo è variante {variant + 1}."},
    ]
    rules = [
        {"rule_id": rule_a, "statement": f"Una raccomandazione favorevole richiede {measure} almeno pari a {threshold}."},
        {"rule_id": rule_b, "statement": "In presenza di evidenza contraddittoria o incompleta la conclusione deve essere condizionata."},
        {"rule_id": rule_irrelevant, "statement": "La numerazione grafica degli allegati non modifica la valutazione sostanziale."},
    ]
    source_b_value = observed - 18 if conflict else observed - 1
    sources = [
        {"source_id": source_a, "statement": f"Registro sintetico A rileva {measure}={observed}; affidabilità alta."},
        {"source_id": source_b, "statement": f"Nota sintetica B rileva {measure}={source_b_value}; affidabilità {'media' if conflict else 'alta'}."},
        {"source_id": source_irrelevant, "statement": "Catalogo grafico privo di dati valutativi."},
    ]
    contradictions = []
    if conflict:
        contradictions.append(
            {
                "contradiction_id": f"C_{prefix}_A",
                "source_ids": [source_a, source_b],
                "statement": "Le due fonti riportano valori incompatibili per la stessa misura.",
            }
        )
    if insufficient:
        missing = f"Manca la verifica indipendente del valore {measure} per il caso {serial}."
    elif conflict:
        missing = "Serve riconciliare le due fonti prima di una conclusione certa."
    else:
        missing = "Nessuna lacuna critica; resta da monitorare il vincolo di costo."
    relevant_rules = [rule_a, rule_b] if conflict or insufficient else [rule_a]
    relevant_sources = [source_a, source_b] if conflict else [source_a]
    required_counterarguments = [f"Il valore della fonte {source_b} può invalidare la lettura favorevole."] if conflict else ["Il margine rispetto alla soglia può non essere stabile nel tempo."]
    required_uncertainties = [missing] if conflict or insufficient else ["La misura futura può differire dal dato osservato."]
    forbidden_claims = [
        f"{source_irrelevant} dimostra l'esito",
        f"{rule_irrelevant} autorizza una decisione",
        "approvazione automatica",
    ]
    reason_codes = [category]
    return {
        "id": f"domain_adapter_{prefix}",
        "category": category,
        "domain_id": domain_id,
        "domain_version": "synthetic-v1",
        "question": question,
        "facts": facts,
        "rules": rules,
        "sources": sources,
        "constraints": [
            "Nessuna azione esterna.",
            "Nessuna approval implicita.",
            f"Non superare {10 + serial} unità di costo sintetico.",
        ],
        "known_contradictions": contradictions,
        "irrelevant": {"fact_id": fact_irrelevant, "rule_id": rule_irrelevant, "source_id": source_irrelevant},
        "objection": required_counterarguments[0],
        "reason_codes": reason_codes,
        "gold": {
            "relevant_rules": relevant_rules,
            "relevant_sources": relevant_sources,
            "required_counterarguments": required_counterarguments,
            "required_uncertainties": required_uncertainties,
            "forbidden_claims": forbidden_claims,
            "required_rules": relevant_rules,
            "required_sources": relevant_sources,
            "contradiction_required": bool(conflict),
            "conditional_required": bool(conflict or insufficient),
            "forbidden": forbidden_claims,
        },
        "rubric": {
            "schema": 1,
            "rule_provenance": 1,
            "source_provenance": 1,
            "counterargument": 1,
            "uncertainty": 1,
        },
    }


def _build_traces(case: dict[str, Any]) -> dict[str, Any]:
    gold = case["gold"]
    fact_ids = [case["facts"][0]["fact_id"], case["facts"][1]["fact_id"]]
    planner = {
        "questions": [case["question"]],
        "facts_selected": fact_ids,
        "rules_selected": list(gold["relevant_rules"]),
        "sources_selected": list(gold["relevant_sources"]),
        "interpretations": [
            {"id": "I1", "summary": "Applicare la soglia alla misura osservata.", "refs": [gold["relevant_rules"][0], gold["relevant_sources"][0]]},
            {"id": "I2", "summary": "Rinviare la conclusione se evidenza o regole non bastano.", "refs": [gold["relevant_rules"][-1]]},
        ],
        "missing_information": list(gold["required_uncertainties"]),
    }
    critic = {
        "criticisms": [{"text": gold["required_counterarguments"][0], "refs": list(gold["relevant_sources"])}],
        "contradictions": [item["contradiction_id"] for item in case["known_contradictions"]],
        "rule_application_errors": [],
        "source_provenance_errors": [],
        "strongest_counterargument": gold["required_counterarguments"][0],
        "unresolved_issues": list(gold["required_uncertainties"]),
    }
    conditional = bool(gold["conditional_required"])
    recommendation = (
        "Se le evidenze vengono riconciliate e i vincoli restano rispettati, adottare l'opzione conforme; altrimenti rinviare."
        if conditional
        else "Adottare l'opzione conforme, mantenendo monitoraggio del vincolo e revisione umana."
    )
    solver = {
        "position": "Opinione condizionata basata esclusivamente su fatti, regole e fonti sintetiche fornite.",
        "supporting_arguments": [
            {"text": "La misura va confrontata con la soglia applicabile.", "kind": "inference", "refs": [gold["relevant_rules"][0], fact_ids[0]]}
        ],
        "counterarguments": [
            {"text": gold["required_counterarguments"][0], "kind": "inference", "refs": list(gold["relevant_sources"])}
        ],
        "rule_application": [
            {"rule_id": rule_id, "inference": "Regola applicata condizionatamente ai fatti e alle fonti selezionate."}
            for rule_id in gold["relevant_rules"]
        ],
        "evidence_used": [
            {"kind": "fact", "id": fact_id} for fact_id in fact_ids
        ] + [{"kind": "source", "id": source_id} for source_id in gold["relevant_sources"]],
        "uncertainties": list(gold["required_uncertainties"]),
        "alternative_interpretations": ["I1: applicazione diretta della soglia.", "I2: conclusione sospesa o condizionata."],
        "recommendation": recommendation,
        "confidence": 0.58 if conditional else 0.78,
        "human_decision_required": bool(conditional),
    }
    domain_opinion = {"domain_id": case["domain_id"], "question": case["question"], **solver}
    return {
        "case_id": case["id"],
        "planner_target": planner,
        "critic_target": critic,
        "solver_target": solver,
        "domain_opinion_target": domain_opinion,
    }


def deterministic_splits(cases: list[dict[str, Any]]) -> dict[str, list[str]]:
    split = {"train": [], "validation": [], "test": []}
    for category in CATEGORIES:
        rows = [case for case in cases if case["category"] == category]
        rows.sort(key=lambda row: hashlib.sha256(f"recursive-domain-v1:{row['id']}".encode()).hexdigest())
        split["train"].extend(row["id"] for row in rows[:28])
        split["validation"].extend(row["id"] for row in rows[28:34])
        split["test"].extend(row["id"] for row in rows[34:40])
    return {name: sorted(ids) for name, ids in split.items()}


def validate_dataset(cases: list[dict[str, Any]], traces: list[dict[str, Any]]) -> dict[str, Any]:
    if len(cases) != 240 or len({case["id"] for case in cases}) != 240:
        raise ValueError("domain_adapter_dataset_must_have_240_unique_cases")
    counts = {category: sum(case["category"] == category for case in cases) for category in CATEGORIES}
    if counts != {category: 40 for category in CATEGORIES}:
        raise ValueError("domain_adapter_category_distribution_invalid")
    if {case["domain_id"] for case in cases} != set(DOMAINS):
        raise ValueError("domain_adapter_domains_invalid")
    trace_by_id = {trace["case_id"]: trace for trace in traces}
    if set(trace_by_id) != {case["id"] for case in cases}:
        raise ValueError("domain_adapter_trace_ids_invalid")
    for case in cases:
        required = {"domain_id", "question", "facts", "rules", "sources", "constraints", "known_contradictions", "gold"}
        if required - set(case):
            raise ValueError(f"domain_adapter_case_schema_invalid:{case['id']}")
        gold_required = {"relevant_rules", "relevant_sources", "required_counterarguments", "required_uncertainties", "forbidden_claims"}
        if gold_required - set(case["gold"]):
            raise ValueError(f"domain_adapter_gold_schema_invalid:{case['id']}")
        trace = trace_by_id[case["id"]]
        if set(trace["planner_target"]) != PLANNER_FIELDS:
            raise ValueError(f"planner_target_schema_invalid:{case['id']}")
        if set(trace["critic_target"]) != CRITIC_FIELDS:
            raise ValueError(f"critic_target_schema_invalid:{case['id']}")
        if set(trace["solver_target"]) != SOLVER_FIELDS:
            raise ValueError(f"solver_target_schema_invalid:{case['id']}")
    splits = deterministic_splits(cases)
    if {name: len(ids) for name, ids in splits.items()} != {"train": 168, "validation": 36, "test": 36}:
        raise ValueError("domain_adapter_split_sizes_invalid")
    if set(splits["train"]) & set(splits["validation"]) or set(splits["train"]) & set(splits["test"]) or set(splits["validation"]) & set(splits["test"]):
        raise ValueError("domain_adapter_split_overlap")
    signatures: dict[str, str] = {}
    for case in cases:
        signature = _content_signature(case)
        if signature in signatures:
            raise ValueError(f"domain_adapter_duplicate:{signatures[signature]}:{case['id']}")
        signatures[signature] = case["id"]
    return {"case_count": len(cases), "category_counts": counts, "split_sizes": {name: len(ids) for name, ids in splits.items()}, "duplicates": 0}


def write_dataset(output_dir: Path) -> dict[str, Any]:
    cases, traces = generate_dataset()
    splits = deterministic_splits(cases)
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_text = _jsonl(cases)
    traces_text = _jsonl(traces)
    split_text = json.dumps(splits, indent=2, sort_keys=True) + "\n"
    (output_dir / "recursive_domain_adapter_cases.jsonl").write_text(dataset_text, encoding="utf-8")
    (output_dir / "recursive_domain_adapter_traces.jsonl").write_text(traces_text, encoding="utf-8")
    (output_dir / "recursive_domain_adapter_splits.json").write_text(split_text, encoding="utf-8")
    manifest = {
        "format": "recursive-domain-adapter-v1",
        "case_count": 240,
        "category_counts": {category: 40 for category in CATEGORIES},
        "domain_count": 8,
        "split_sizes": {name: len(ids) for name, ids in splits.items()},
        "dataset_sha256": hashlib.sha256(dataset_text.encode()).hexdigest(),
        "traces_sha256": hashlib.sha256(traces_text.encode()).hexdigest(),
        "splits_sha256": hashlib.sha256(split_text.encode()).hexdigest(),
        "duplicate_count": 0,
        "synthetic_only": True,
        "contains_free_chain_of_thought": False,
    }
    (output_dir / "recursive_domain_adapter_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _content_signature(case: dict[str, Any]) -> str:
    text = " ".join(
        [case["question"]]
        + [str(item.get("statement") or "") for field in ("facts", "rules", "sources") for item in case[field]]
    ).lower()
    text = re.sub(r"\b[frscm]_[a-z0-9_]+\b", "", text)
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def _jsonl(rows: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(write_dataset(args.output_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
