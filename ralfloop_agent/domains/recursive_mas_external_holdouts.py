from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .recursive_mas_domain_dataset import CATEGORIES
from .recursive_mas_qwen3_heldout_benchmark import input_payload, ngram_similarity, stable_sha256


SCHEMA_VERSION = "recursive-domain-external-holdout-v1"
AUTHORING_SEED = 20260715
CONCLUSION_BY_CATEGORY = {
    "strategic_assessment": "favorable",
    "conflicting_sources": "conditional",
    "recommendation_required": "favorable",
    "incomplete_rules": "conditional",
    "evidence_synthesis": "contrary",
    "domain_validation": "contrary",
}


@dataclass(frozen=True)
class DomainSpec:
    code: str
    domain_id: str
    entity: str
    authority: str
    decision: str
    metric: str
    observed: int
    threshold: int
    condition: str
    alternative: str
    context: str


FINAL_DOMAINS = (
    DomainSpec("wash", "fa_shared_laundry_rotation", "lavanderia cooperativa Aurora", "turno di quartiere", "estendere le fasce serali", "utilizzo settimanale", 74, 68, "presidio volontario nelle ultime due ore", "prenotazione a finestre mobili", "servizio comunitario di lavaggio condiviso"),
    DomainSpec("rain", "fa_rain_garden_stewardship", "giardino di pioggia Selce", "gruppo di manutenzione", "ampliare il bacino filtrante", "riduzione del deflusso", 63, 60, "test stagionale del drenaggio profondo", "aiuole modulari distribuite", "programma ambientale per acque meteoriche"),
    DomainSpec("book", "fa_mobile_library_circulation", "biblioteca itinerante Cometa", "cabina di programmazione", "aggiungere una fermata periferica", "prestiti per fermata", 41, 36, "copertura del turno di rientro", "punto di ritiro presso edicola vuota", "servizio culturale mobile"),
    DomainSpec("mesh", "fa_civic_sensor_exchange", "rete civica di sensori Brina", "custodi del protocollo", "pubblicare aggregati orari", "copertura dei consensi", 82, 85, "verifica della revoca per i nodi ospitati", "rilascio di soli indici settimanali", "scambio sintetico di misure ambientali"),
    DomainSpec("bike", "fa_cargo_bike_rotation", "flotta cargo-bike Cedro", "coordinamento delle stazioni", "aprire una stazione aggiuntiva", "corse utili per mezzo", 28, 24, "disponibilità di ricovero notturno", "stazione mobile due giorni a settimana", "mobilità condivisa per piccoli carichi"),
    DomainSpec("hall", "fa_rehearsal_hall_allocation", "sala prove Prisma", "tavolo di calendario", "riservare una fascia ai gruppi emergenti", "ore effettivamente occupate", 57, 52, "collaudo della barriera acustica", "rotazione mensile delle fasce", "gestione di uno spazio culturale"),
)

RESERVE_DOMAINS = (
    DomainSpec("food", "rb_food_rescue_circuit", "circuito recupero pasti Timo", "cellula logistica", "aggiungere un punto di raccolta", "consegne integre", 91, 88, "verifica della catena termica serale", "ritiro alternato tra due punti", "redistribuzione sintetica di eccedenze alimentari"),
    DomainSpec("poll", "rb_pollinator_corridor", "corridoio impollinatori Lume", "squadra botanica", "collegare due microhabitat", "presenze di specie indicatrici", 19, 22, "assenza di trattamento nelle particelle intermedie", "isole fiorite non contigue", "cura locale della biodiversità"),
    DomainSpec("tool", "rb_repair_toolpool", "banco attrezzi Quarzo", "collegio dei facilitatori", "estendere il prestito specialistico", "restituzioni puntuali", 78, 75, "certificazione interna degli utensili rotanti", "sessioni assistite senza prestito", "laboratorio comunitario di riparazione"),
    DomainSpec("dock", "rb_floating_market_dock", "banchina mercato Galena", "presidio di allestimento", "consentire un turno serale", "uscite concluse entro finestra", 33, 30, "ispezione dell'illuminazione di emergenza", "turno anticipato con copertura mobile", "mercato sintetico su pontile temporaneo"),
)


def _rotate(values: Sequence[dict[str, Any]], amount: int) -> list[dict[str, Any]]:
    index = amount % len(values)
    return [dict(item) for item in (*values[index:], *values[:index])]


def _case(spec: DomainSpec, category: str, dataset: str, index: int) -> dict[str, Any]:
    namespace = "FA" if dataset == "FINAL_A" else "RB"
    prefix = f"{namespace}_{spec.code.upper()}"
    conclusion = CONCLUSION_BY_CATEGORY[category]
    category_code = {
        "strategic_assessment": "STRA",
        "conflicting_sources": "CONF",
        "recommendation_required": "RECO",
        "incomplete_rules": "INCO",
        "evidence_synthesis": "EVID",
        "domain_validation": "DOMA",
    }[category]
    facts = [
        {"fact_id": f"{prefix}_F01", "kind": "fact", "statement": f"Il conteggio indipendente attribuisce a {spec.entity} {spec.observed} punti di {spec.metric} nell'ultima finestra completa."},
        {"fact_id": f"{prefix}_F02", "kind": "fact", "statement": f"La soglia deliberata per {spec.decision} è {spec.threshold} punti verificati."},
        {"fact_id": f"{prefix}_F03", "kind": "fact", "statement": f"Il vincolo ancora aperto riguarda {spec.condition}."},
        {"fact_id": f"{prefix}_F04", "kind": "fact", "statement": f"Una ricognizione separata colloca la stessa misura a {max(0, spec.observed - 13)} punti, senza verbale di riconciliazione."},
        {"fact_id": f"{prefix}_F05", "kind": "fact", "statement": f"L'alternativa «{spec.alternative}» è attuabile con le risorse simulate già disponibili."},
        {"fact_id": f"{prefix}_F06", "kind": "fact", "statement": f"Il prossimo controllo completo è previsto dopo due cicli operativi di {spec.context}."},
        {"fact_id": f"{prefix}_F07", "kind": "irrelevant", "statement": f"Il cartello identificativo usa una cornice color indaco per {spec.code}."},
        {"fact_id": f"{prefix}_F08", "kind": "irrelevant", "statement": "Il registro delle sedie pieghevoli riporta sette unità, senza relazione con la decisione."},
    ]
    rules = [
        {"rule_id": f"{prefix}_R01", "statement": f"La proposta può ricevere valutazione favorevole soltanto con {spec.metric} verificato almeno pari a {spec.threshold}."},
        {"rule_id": f"{prefix}_R02", "statement": f"Se manca la verifica di {spec.condition}, ogni posizione deve restare condizionata e richiedere decisione umana."},
        {"rule_id": f"{prefix}_R03", "statement": "Quando due rilevazioni pertinenti divergono oltre otto punti, occorre esplicitare il conflitto e non scegliere la più favorevole senza riconciliazione."},
        {"rule_id": f"{prefix}_R04", "statement": f"Una raccomandazione deve confrontare la proposta con «{spec.alternative}» quando entrambe rispettano i vincoli disponibili."},
        {"rule_id": f"{prefix}_R05", "statement": "Una condizione essenziale non documentata impedisce una conclusione definitiva, anche se altri indicatori superano la soglia."},
        {"rule_id": f"{prefix}_R06", "statement": "Colori, arredi mobili e ordine tipografico non costituiscono evidenza della fattibilità sostanziale."},
    ]
    sources = [
        {"source_id": f"{prefix}_S01", "statement": f"Rapporto di campo firmato da due osservatori: {spec.metric}={spec.observed}; metodo replicabile; affidabilità alta."},
        {"source_id": f"{prefix}_S02", "statement": f"Diario operativo non controfirmato: {spec.metric}={max(0, spec.observed - 13)}; affidabilità media; periodo parzialmente sovrapposto."},
        {"source_id": f"{prefix}_S03", "statement": f"Scheda di capacità conferma che «{spec.alternative}» usa risorse già disponibili; affidabilità alta ma non decide la proposta principale."},
        {"source_id": f"{prefix}_S04", "statement": f"Promemoria provvisorio segnala che {spec.condition} non è ancora verificato; provenienza interna, affidabilità media."},
        {"source_id": f"{prefix}_S05", "statement": f"Rassegna contestuale descrive {spec.context}, senza misurare la soglia del caso."},
        {"source_id": f"{prefix}_S06", "statement": "Inventario decorativo elenca colori e sedie; fonte autentica ma irrilevante per la decisione."},
    ]
    required = {
        "strategic_assessment": ([1, 4], [1, 3], False, False),
        "conflicting_sources": ([1, 3], [1, 2], True, True),
        "recommendation_required": ([1, 2, 4], [1, 3, 4], False, False),
        "incomplete_rules": ([2, 5], [1, 4], False, True),
        "evidence_synthesis": ([1, 3, 5], [1, 2, 4], True, True),
        "domain_validation": ([2, 5], [2, 4], False, True),
    }[category]
    rule_ids = [f"{prefix}_R{number:02d}" for number in required[0]]
    source_ids = [f"{prefix}_S{number:02d}" for number in required[1]]
    contradiction_ids = [f"{prefix}_C01"] if required[2] else []
    uncertainty_required = bool(required[3])
    category_instruction = {
        "strategic_assessment": f"Confronta l'estensione proposta con «{spec.alternative}» e valuta robustezza e reversibilità.",
        "conflicting_sources": "Tratta esplicitamente la divergenza tra le due rilevazioni senza media aritmetica automatica.",
        "recommendation_required": "Formula una raccomandazione concreta ma non esecutiva, indicando chi deve decidere.",
        "incomplete_rules": f"Stabilisci cosa è lecito concludere finché manca {spec.condition}.",
        "evidence_synthesis": "Distingui misura, vincolo, conflitto e inferenza prima della conclusione.",
        "domain_validation": "Verifica se una conclusione definitiva sarebbe coerente con le condizioni documentate.",
    }[category]
    question_openers = (
        f"Per {spec.entity}, {category_instruction} Conviene {spec.decision}?",
        f"{category_instruction} Quale posizione deve assumere {spec.authority} sulla scelta di {spec.decision}?",
        f"È in esame la decisione di {spec.decision} per {spec.entity}. {category_instruction}",
    )
    question = question_openers[index % len(question_openers)]
    counterargument = (
        f"La rilevazione da {max(0, spec.observed - 13)} punti e il vincolo su {spec.condition} riducono la forza della lettura favorevole."
    )
    uncertainty = f"Resta da riconciliare la misura e verificare {spec.condition}."
    if conclusion == "favorable":
        position = f"La proposta per {spec.entity} è sostenibile con monitoraggio e confronto dell'alternativa."
        recommendation = f"Raccomandare a {spec.authority} una prosecuzione reversibile, verificando {spec.condition} prima dell'attuazione definitiva."
        confidence = 0.74
    elif conclusion == "contrary":
        position = f"Le evidenze disponibili non sostengono una decisione definitiva di {spec.decision}."
        recommendation = f"Non procedere ora; {spec.authority} rivaluti dopo riconciliazione delle misure e verifica di {spec.condition}."
        confidence = 0.68
    else:
        position = f"La posizione su {spec.entity} deve restare condizionata alla verifica mancante."
        recommendation = f"Se la misura viene riconciliata e {spec.condition} risulta verificato, procedere in modo reversibile; altrimenti scegliere «{spec.alternative}»."
        confidence = 0.56
    human = uncertainty_required or conclusion != "favorable"
    gold_output = {
        "domain_id": spec.domain_id,
        "question": question,
        "position": position,
        "supporting_arguments": [{"text": f"La misura e le condizioni applicabili vanno lette insieme per {spec.context}."}],
        "counterarguments": [{"text": counterargument}],
        "rule_application": [{"rule_id": rule_id, "application": "selected_by_planner_and_reviewed_by_critic"} for rule_id in rule_ids],
        "evidence_used": [{"kind": "source", "id": source_id} for source_id in source_ids],
        "uncertainties": [uncertainty] if uncertainty_required else [f"Il dato futuro di {spec.metric} può variare."],
        "alternative_interpretations": [f"Adottare «{spec.alternative}» come opzione reversibile."],
        "recommendation": recommendation,
        "confidence": confidence,
        "human_decision_required": human,
    }
    forbidden = [
        f"{prefix}_S06 dimostra la fattibilità",
        f"{prefix}_R06 autorizza automaticamente la decisione",
        "approvazione automatica",
    ]
    return {
        "id": f"external_{dataset.lower()}_{spec.code}_{category_code.lower()}",
        "case_id": f"external_{dataset.lower()}_{spec.code}_{category_code.lower()}",
        "dataset": dataset,
        "category": category,
        "domain_id": spec.domain_id,
        "domain_version": "external-synthetic-v1",
        "question": question,
        "facts": _rotate(facts, index),
        "rules": _rotate(rules, index * 2),
        "sources": _rotate(sources, index * 3),
        "constraints": ["Nessuna azione esterna.", "Nessuna approval implicita.", "La decisione finale resta umana.", f"Valutare soltanto {spec.context} con gli elementi forniti."],
        "known_contradictions": [{"contradiction_id": f"{prefix}_C01", "source_ids": [f"{prefix}_S01", f"{prefix}_S02"], "statement": "Le due rilevazioni pertinenti divergono oltre otto punti."}],
        "expected_relevant_rule_ids": rule_ids,
        "expected_relevant_source_ids": source_ids,
        "expected_contradiction_ids": contradiction_ids,
        "expected_counterargument_requirements": [counterargument],
        "expected_uncertainty_requirement": uncertainty_required,
        "expected_recommendation_type": conclusion,
        "human_decision_requirement": human,
        "forbidden_claims": forbidden,
        "gold_structured_output": gold_output,
        "gold": {
            "relevant_rules": rule_ids,
            "relevant_sources": source_ids,
            "required_rules": rule_ids,
            "required_sources": source_ids,
            "required_counterarguments": [counterargument],
            "required_uncertainties": gold_output["uncertainties"],
            "expected_contradiction_ids": contradiction_ids,
            "expected_recommendation_type": conclusion,
            "human_decision_required": human,
            "contradiction_required": bool(required[2]),
            "conditional_required": conclusion == "conditional" or uncertainty_required,
            "forbidden_claims": forbidden,
            "forbidden": forbidden,
        },
    }


def generate_external_holdouts() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    final = [_case(domain, category, "FINAL_A", domain_index * len(CATEGORIES) + category_index) for domain_index, domain in enumerate(FINAL_DOMAINS) for category_index, category in enumerate(CATEGORIES)]
    reserve = [_case(domain, category, "RESERVE_B", domain_index * len(CATEGORIES) + category_index) for domain_index, domain in enumerate(RESERVE_DOMAINS) for category_index, category in enumerate(CATEGORIES)]
    validate_external_holdouts(final, reserve)
    return final, reserve


def validate_external_holdouts(final: Sequence[Mapping[str, Any]], reserve: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    _validate_set(final, "FINAL_A", 36, 6)
    _validate_set(reserve, "RESERVE_B", 24, 4)
    final_domains = {str(case["domain_id"]) for case in final}
    reserve_domains = {str(case["domain_id"]) for case in reserve}
    final_ids = {str(case["id"]) for case in final}
    reserve_ids = {str(case["id"]) for case in reserve}
    final_rules = _ids(final, "rules", "rule_id")
    reserve_rules = _ids(reserve, "rules", "rule_id")
    final_sources = _ids(final, "sources", "source_id")
    reserve_sources = _ids(reserve, "sources", "source_id")
    if final_domains & reserve_domains or final_ids & reserve_ids or final_rules & reserve_rules or final_sources & reserve_sources:
        raise ValueError("external_holdout_disjointness_invalid")
    final_inputs = {stable_sha256(input_payload(case)) for case in final}
    reserve_inputs = {stable_sha256(input_payload(case)) for case in reserve}
    final_gold = {stable_sha256(case["gold_structured_output"]) for case in final}
    reserve_gold = {stable_sha256(case["gold_structured_output"]) for case in reserve}
    if final_inputs & reserve_inputs or final_gold & reserve_gold:
        raise ValueError("external_holdout_hash_overlap")
    return {
        "final_count": len(final),
        "reserve_count": len(reserve),
        "domain_overlap": 0,
        "case_overlap": 0,
        "rule_overlap": 0,
        "source_overlap": 0,
        "input_hash_overlap": 0,
        "gold_hash_overlap": 0,
    }


def _validate_set(cases: Sequence[Mapping[str, Any]], dataset: str, size: int, domain_count: int) -> None:
    if len(cases) != size or len({case["id"] for case in cases}) != size:
        raise ValueError(f"{dataset.lower()}_size_invalid")
    if len({case["domain_id"] for case in cases}) != domain_count:
        raise ValueError(f"{dataset.lower()}_domain_count_invalid")
    categories = Counter(str(case["category"]) for case in cases)
    if categories != {category: domain_count for category in CATEGORIES}:
        raise ValueError(f"{dataset.lower()}_category_balance_invalid")
    conclusions = Counter(str(case["expected_recommendation_type"]) for case in cases)
    if conclusions != {"favorable": size // 3, "contrary": size // 3, "conditional": size // 3}:
        raise ValueError(f"{dataset.lower()}_conclusion_balance_invalid")
    for case in cases:
        if len(case["facts"]) != 8 or len(case["rules"]) != 6 or len(case["sources"]) != 6:
            raise ValueError(f"external_case_cardinality_invalid:{case['id']}")
        if sum(item.get("kind") == "irrelevant" for item in case["facts"]) < 2:
            raise ValueError(f"external_case_distractors_invalid:{case['id']}")
        if not case["gold_structured_output"].get("counterarguments") or not case["gold_structured_output"].get("recommendation"):
            raise ValueError(f"external_case_gold_invalid:{case['id']}")


def _ids(cases: Sequence[Mapping[str, Any]], field: str, key: str) -> set[str]:
    return {str(item[key]) for case in cases for item in case[field]}


def contamination_report(
    final: Sequence[Mapping[str, Any]], reserve: Sequence[Mapping[str, Any]], previous: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    previous_case_ids = {str(case["id"]) for case in previous}
    previous_domain_ids = {str(case["domain_id"]) for case in previous}
    previous_inputs = {stable_sha256(input_payload(case)) for case in previous}
    previous_gold = {stable_sha256(case.get("gold_structured_output") or case["gold"]) for case in previous}
    previous_rules = _ids(previous, "rules", "rule_id")
    previous_sources = _ids(previous, "sources", "source_id")
    output = {}
    for name, cases in (("FINAL_A", final), ("RESERVE_B", reserve)):
        rows = []
        for case in cases:
            similarities = [(ngram_similarity(case, other, 5), str(other["id"])) for other in previous]
            max_similarity, nearest = max(similarities, default=(0.0, ""))
            fact_similarity = max((_statement_similarity(case["facts"], other["facts"]) for other in previous), default=0.0)
            rule_similarity = max((_statement_similarity(case["rules"], other["rules"]) for other in previous), default=0.0)
            row = {
                "case_id": case["id"],
                "domain_id": case["domain_id"],
                "case_id_overlap": case["id"] in previous_case_ids,
                "domain_id_overlap": case["domain_id"] in previous_domain_ids,
                "input_hash_overlap": stable_sha256(input_payload(case)) in previous_inputs,
                "gold_hash_overlap": stable_sha256(case["gold_structured_output"]) in previous_gold,
                "rule_id_overlap": bool({item["rule_id"] for item in case["rules"]} & previous_rules),
                "source_id_overlap": bool({item["source_id"] for item in case["sources"]} & previous_sources),
                "max_5gram_similarity": max_similarity,
                "nearest_previous_case_id": nearest,
                "max_normalized_fact_similarity": fact_similarity,
                "max_normalized_rule_similarity": rule_similarity,
            }
            row["exact_duplicate"] = any(row[key] for key in ("case_id_overlap", "input_hash_overlap", "gold_hash_overlap"))
            row["contaminated"] = row["exact_duplicate"] or row["domain_id_overlap"] or row["rule_id_overlap"] or row["source_id_overlap"]
            rows.append(row)
        output[name] = {
            "contaminated": any(row["contaminated"] for row in rows),
            "max_5gram_similarity": max(row["max_5gram_similarity"] for row in rows),
            "max_normalized_fact_similarity": max(row["max_normalized_fact_similarity"] for row in rows),
            "max_normalized_rule_similarity": max(row["max_normalized_rule_similarity"] for row in rows),
            "above_5gram_review_threshold": [row["case_id"] for row in rows if row["max_5gram_similarity"] >= 0.55],
            "rows": rows,
        }
    return output


def _statement_similarity(left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]) -> float:
    def tokens(items: Sequence[Mapping[str, Any]]) -> set[str]:
        value = " ".join(str(item.get("statement") or "") for item in items).casefold()
        value = re.sub(r"\b(?:fa|rb|f|r|s|c)_[a-z0-9_]+\b", " ", value)
        value = re.sub(r"\d+", " ", value)
        return set(re.findall(r"[a-zà-ÿ]{4,}", value))
    a, b = tokens(left), tokens(right)
    return len(a & b) / len(a | b) if a or b else 1.0


def jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows).encode()


def dataset_manifest(
    final: Sequence[Mapping[str, Any]],
    reserve: Sequence[Mapping[str, Any]],
    *,
    runner_frozen_manifest_hash: str,
    contamination: Mapping[str, Any],
) -> dict[str, Any]:
    final_raw, reserve_raw = jsonl_bytes(final), jsonl_bytes(reserve)
    disjointness = validate_external_holdouts(final, reserve)
    return {
        "schema_version": SCHEMA_VERSION,
        "authoring_seed": AUTHORING_SEED,
        "creation_timestamp": datetime.now(timezone.utc).isoformat(),
        "runner_frozen_manifest_hash": runner_frozen_manifest_hash,
        "FINAL_A": {
            "case_count": len(final),
            "domains": sorted({case["domain_id"] for case in final}),
            "categories": dict(Counter(case["category"] for case in final)),
            "conclusions": dict(Counter(case["expected_recommendation_type"] for case in final)),
            "case_hashes": {case["id"]: stable_sha256(case) for case in final},
            "dataset_sha256": hashlib.sha256(final_raw).hexdigest(),
            "gold_sha256": stable_sha256([case["gold_structured_output"] for case in final]),
        },
        "RESERVE_B": {
            "case_count": len(reserve),
            "domains": sorted({case["domain_id"] for case in reserve}),
            "categories": dict(Counter(case["category"] for case in reserve)),
            "conclusions": dict(Counter(case["expected_recommendation_type"] for case in reserve)),
            "case_hashes": {case["id"]: stable_sha256(case) for case in reserve},
            "dataset_sha256": hashlib.sha256(reserve_raw).hexdigest(),
            "gold_sha256": stable_sha256([case["gold_structured_output"] for case in reserve]),
            "executed": False,
        },
        "disjointness_report": disjointness,
        "contamination_report": contamination,
        "frozen": True,
    }
