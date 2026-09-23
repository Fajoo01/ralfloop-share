from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from .contracts import PlanAssignment
from .executor import StructuredArtifact


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULEBOOK = PROJECT_ROOT / "config" / "accounting_ets_rules_v1.json"

_AMOUNT_RE = re.compile(
    r"(?P<amount>\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:€|euro)\b",
    re.I,
)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _load_rulebook(path: str | Path = DEFAULT_RULEBOOK) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("accounting_rulebook_schema_invalid")
    return payload


def parse_eur(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    raw = str(value or "").strip().replace("€", "").replace(" ", "")
    if not raw:
        raise ValueError("amount_required")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
        raw = raw.replace(".", "")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("amount_invalid") from exc


def statutory_runts_due_date(financial_year_end: date, *, rulebook: Mapping[str, Any] | None = None) -> date:
    rules = dict(rulebook or _load_rulebook())
    days = int(rules["rules"]["runts_deposit"]["days_after_financial_year_close"])
    return financial_year_end + timedelta(days=days)


def ets_reporting_regime(
    *,
    annual_entries_eur: Decimal | int | float | str | None,
    legal_personality: bool | None,
    financial_year_end: date | None,
    mainly_commercial: bool | None = None,
    social_enterprise: bool | None = None,
    rulebook: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Select the accounting form only when the decisive facts are explicit.

    This is deliberately conservative: missing legal/tax status never becomes a
    guessed classification. The result is advisory metadata, not a filing action.
    """
    rules = dict(rulebook or _load_rulebook())
    source_refs = tuple(
        str(row.get("source"))
        for row in rules["rules"].values()
        if isinstance(row, Mapping) and row.get("source")
    )
    missing: list[str] = []
    if annual_entries_eur is None:
        missing.append("annual_entries_eur")
    if legal_personality is None:
        missing.append("legal_personality")
    if financial_year_end is None:
        missing.append("financial_year_end")
    if mainly_commercial is None:
        missing.append("mainly_commercial")
    if social_enterprise is None:
        missing.append("social_enterprise")
    if missing:
        return {
            "status": "requires_verified_inputs",
            "missing": missing,
            "mode": None,
            "source_refs": list(source_refs),
        }

    entries = parse_eur(annual_entries_eur)
    if entries < 0:
        raise ValueError("annual_entries_negative")
    assert financial_year_end is not None
    assert legal_personality is not None
    assert mainly_commercial is not None
    assert social_enterprise is not None

    if mainly_commercial or social_enterprise:
        return {
            "status": "special_rules_required",
            "mode": "special_rules",
            "annual_entries_eur": str(entries),
            "reason": "social_enterprise_or_mainly_commercial",
            "runts_statutory_due_date": statutory_runts_due_date(financial_year_end, rulebook=rules).isoformat(),
            "deadline_requires_live_check": True,
            "source_refs": list(source_refs),
        }

    model_e = rules["rules"]["model_e"]
    model_e_limit = parse_eur(model_e["max_annual_entries_eur"])
    model_e_from = date.fromisoformat(str(model_e["available_for_financial_years_closing_on_or_after"]))
    model_d = rules["rules"]["model_d"]
    model_d_limit = parse_eur(model_d["max_annual_entries_eur"])

    if entries <= model_e_limit and financial_year_end >= model_e_from:
        mode = "E"
        reason = "entries_within_model_e_threshold"
    elif not legal_personality and entries <= model_d_limit:
        mode = "D"
        reason = "non_personified_ets_within_model_d_threshold"
    elif legal_personality and entries <= model_e_limit and financial_year_end < model_e_from:
        # The 2026 ministerial circular contains a transition for already-closed
        # low-entry financial years. We refuse to generalise that transition here.
        return {
            "status": "historical_transition_review_required",
            "mode": None,
            "annual_entries_eur": str(entries),
            "reason": "pre_model_e_personified_low_entry_period",
            "runts_statutory_due_date": statutory_runts_due_date(financial_year_end, rulebook=rules).isoformat(),
            "deadline_requires_live_check": True,
            "source_refs": list(source_refs),
        }
    else:
        mode = "competence"
        reason = "cash_reporting_threshold_or_personality_condition_not_met"

    return {
        "status": "determined",
        "mode": mode,
        "annual_entries_eur": str(entries),
        "legal_personality": legal_personality,
        "financial_year_end": financial_year_end.isoformat(),
        "reason": reason,
        "runts_statutory_due_date": statutory_runts_due_date(financial_year_end, rulebook=rules).isoformat(),
        "deadline_requires_live_check": True,
        "source_refs": list(source_refs),
    }


def summarize_movements(movements: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Deterministically total evidence-backed cash movements using Decimal."""
    incoming = Decimal("0")
    outgoing = Decimal("0")
    unresolved = 0
    evidence_refs: list[str] = []
    for row in movements:
        direction = str(row.get("direction") or "").strip().casefold()
        try:
            amount = parse_eur(row.get("amount"))
        except ValueError:
            unresolved += 1
            continue
        if amount < 0:
            unresolved += 1
            continue
        if direction in {"in", "entrata", "income"}:
            incoming += amount
        elif direction in {"out", "uscita", "expense"}:
            outgoing += amount
        else:
            unresolved += 1
            continue
        ref = str(row.get("evidence_ref") or "").strip()
        if ref:
            evidence_refs.append(ref)
    return {
        "incoming_eur": str(incoming),
        "outgoing_eur": str(outgoing),
        "net_cash_eur": str(incoming - outgoing),
        "movement_count": len(movements),
        "unresolved_count": unresolved,
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
    }


def _facts_from_goal(goal: str) -> dict[str, Any]:
    folded = goal.casefold()
    facts: dict[str, Any] = {}
    amount = _AMOUNT_RE.search(goal)
    if amount and re.search(r"\b(?:entrat[ae]|ricav[io]|provent[io])\b", folded):
        facts["annual_entries_eur"] = str(parse_eur(amount.group("amount")))
    if re.search(r"\bsenza\s+personalit[aà]\s+giuridica\b", folded):
        facts["legal_personality"] = False
    elif re.search(r"\bcon\s+personalit[aà]\s+giuridica\b", folded):
        facts["legal_personality"] = True
    if re.search(r"\b(?:non\s+)?impresa\s+sociale\b", folded):
        facts["social_enterprise"] = not bool(re.search(r"\bnon\s+impresa\s+sociale\b", folded))
    if re.search(r"\b(?:non\s+)?(?:prevalentemente|principalmente)\s+commerciale\b", folded):
        facts["mainly_commercial"] = not bool(re.search(r"\bnon\s+(?:prevalentemente|principalmente)\s+commerciale\b", folded))
    year = _YEAR_RE.search(goal)
    if year and re.search(r"\b(?:rendiconto|bilancio|esercizio)\b", folded):
        facts["financial_year_end"] = date(int(year.group(1)), 12, 31)
    return facts


def classify_accounting_request(goal: str) -> str:
    folded = goal.casefold()
    if re.search(r"\b(?:rendiconto|modello\s+[de]|bilancio\s+ets|runts)\b", folded):
        return "ets_report"
    if re.search(r"\b(?:fattur[ae]|ricevut[ae]|scontrin[io]|giustificativ[oi]|pezz[ae]\s+giustificativ[ae])\b", folded):
        return "document_review"
    if re.search(r"\b(?:riconcili|moviment[io]|estratto\s+conto|prima\s+nota|quadra(?:re|tura)?|far\s+quadrare)\b", folded):
        return "reconciliation"
    if re.search(r"\b(?:iva|f24|impost[ae]|dichiarazione|730|redditi)\b", folded):
        return "tax_check"
    if re.search(r"\b(?:scadenz|adempiment)\w*\b", folded):
        return "deadline_check"
    return "overview"


def accounting_read_adapter(
    assignment: PlanAssignment, inputs: Mapping[str, Any]
) -> StructuredArtifact:
    goal = assignment.objective
    operation = classify_accounting_request(goal)
    goal_facts = _facts_from_goal(goal)
    profile = inputs.get("accounting.profile")
    if isinstance(profile, Mapping):
        for key in (
            "annual_entries_eur", "legal_personality", "financial_year_end",
            "mainly_commercial", "social_enterprise",
        ):
            if key not in goal_facts and key in profile:
                value = profile[key]
                if key == "financial_year_end" and isinstance(value, str):
                    value = date.fromisoformat(value)
                goal_facts[key] = value

    regime = ets_reporting_regime(
        annual_entries_eur=goal_facts.get("annual_entries_eur"),
        legal_personality=goal_facts.get("legal_personality"),
        financial_year_end=goal_facts.get("financial_year_end"),
        mainly_commercial=goal_facts.get("mainly_commercial"),
        social_enterprise=goal_facts.get("social_enterprise"),
    )

    movements = inputs.get("accounting.movements")
    movement_summary = summarize_movements(list(movements)) if isinstance(movements, list) else None
    document_cases = inputs.get("accounting.document_cases")
    document_review_queue = None
    if isinstance(document_cases, list):
        from .accounting_review import build_missing_document_review_queue
        document_review_queue = build_missing_document_review_queue(list(document_cases))

    live_audit = None
    runts_db_path = inputs.get("accounting.runts_db_path") or os.getenv("BOTTAZZI_RUNTS_DB_PATH")
    if runts_db_path and operation in {"document_review", "reconciliation", "overview"}:
        from .accounting_runts_live import audit_runts_missing_documents, latest_expense_year
        requested_year = inputs.get("accounting.year")
        if requested_year is None:
            year_match = _YEAR_RE.search(goal)
            requested_year = int(year_match.group(1)) if year_match else latest_expense_year(runts_db_path)
        if requested_year is not None:
            review_limit = max(1, min(int(inputs.get("accounting.review_limit") or 50), 1000))
            decisions = inputs.get("accounting.human_decisions")
            evidence_snapshot = None
            evidence_snapshot_path = (
                inputs.get("accounting.evidence_snapshot_path")
                or os.getenv("BOTTAZZI_ACCOUNTING_EVIDENCE_SNAPSHOT")
            )
            if evidence_snapshot_path:
                from .accounting_external_evidence import load_evidence_snapshot
                evidence_snapshot = load_evidence_snapshot(evidence_snapshot_path)
            live_audit = audit_runts_missing_documents(
                runts_db_path,
                year=int(requested_year),
                human_decisions=decisions if isinstance(decisions, Mapping) else None,
                evidence_snapshot=evidence_snapshot,
                limit=review_limit,
            )
            if document_review_queue is None and operation == "document_review":
                live_summary = live_audit["summary"]
                document_review_queue = {
                    "case_count": live_summary["expense_movement_count"],
                    "review_required_count": live_summary["human_review_required_count"],
                    "postable_count": live_summary["expense_movement_count"] - live_summary["human_review_required_count"],
                    "blocked_count": 0,
                    "human_approved_reconstruction_count": live_summary["human_approved_reconstruction_count"],
                    "ready_for_human_confirmation_count": live_summary.get("ready_for_human_confirmation_count", 0),
                    "missing_original_count": live_summary["missing_original_count"],
                    "rows": live_audit["rows"],
                    "invariants": live_audit["invariants"],
                    "source": live_audit["source"],
                    "total_rows_before_limit": live_audit["total_rows_before_limit"],
                }

    if operation == "ets_report" and regime["status"] == "determined":
        mode = regime["mode"]
        label = "Modello E" if mode == "E" else "Modello D" if mode == "D" else "bilancio per competenza"
        message = (
            f"Regime contabile determinato dai dati espliciti: {label}. "
            f"Termine RUNTS statutario calcolato: {regime['runts_statutory_due_date']}; "
            "va verificato sul calendario/istruzioni ufficiali correnti prima del deposito. "
            "Nessun invio, F24 o pagamento è stato eseguito."
        )
    elif operation == "ets_report" and regime["status"] == "special_rules_required":
        message = (
            "Il profilo indicato ricade nelle regole speciali per impresa sociale o attività principalmente commerciale; "
            "non applico automaticamente Modello D/E. Serve verifica della disciplina specifica e delle fonti correnti."
        )
    elif operation == "ets_report":
        missing = ", ".join(regime.get("missing") or ())
        message = (
            "Per scegliere correttamente Modello D, Modello E o bilancio per competenza servono dati verificati: "
            f"{missing or 'profilo dell’ente e periodo contabile'}. Non invento il regime fiscale/contabile."
        )
    elif operation == "reconciliation" and movement_summary is not None:
        message = (
            f"Riconciliazione locale: entrate € {movement_summary['incoming_eur']}, "
            f"uscite € {movement_summary['outgoing_eur']}, saldo netto € {movement_summary['net_cash_eur']}; "
            f"movimenti irrisolti: {movement_summary['unresolved_count']}."
        )
    elif operation == "reconciliation" and live_audit is not None:
        summary = live_audit["summary"]
        message = (
            f"Audit gestionale {live_audit['source']['year']}: {summary['expense_movement_count']} uscite per € {summary['expense_total_eur']}; "
            f"originali mancanti: {summary['missing_original_count']}, casi da revisione umana: {summary['human_review_required_count']}. "
            "Lettura RUNTS Suite in sola lettura; nessun movimento o giustificativo è stato modificato."
        )
    elif operation == "reconciliation":
        message = "Per riconciliare servono movimenti strutturati con importo, direzione ed evidenza di origine; nessun importo è stato inventato."
    elif operation == "document_review" and document_review_queue is not None:
        pending = int(document_review_queue["review_required_count"])
        reconstructed = int(document_review_queue["human_approved_reconstruction_count"])
        ready = int(document_review_queue.get("ready_for_human_confirmation_count") or 0)
        missing = int(document_review_queue["missing_original_count"])
        total_cases = int(document_review_queue.get("case_count") or len(document_review_queue["rows"]))
        shown = len(document_review_queue["rows"])
        shown_note = f"; mostrati {shown} prioritari" if shown < total_cases else ""
        message = (
            f"Revisione giustificativi: {total_cases} casi{shown_note}, "
            f"{missing} originali mancanti, {ready} già corredati da prove forti e pronti per conferma umana, "
            f"{pending} ancora formalmente da decidere, {reconstructed} ricostruzioni già approvate dall’umano. "
            "La quadratura contabile resta separata dalla validità fiscale/rendicontativa: nessuna ricevuta viene inventata."
        )
    elif operation == "document_review":
        message = "Posso classificare fatture, ricevute e giustificativi, ma in questa richiesta non è presente un documento contabile verificabile."
    elif operation in {"tax_check", "deadline_check"}:
        message = "Per adempimenti fiscali e scadenze uso il profilo verificato dell’ente e fonti ufficiali correnti; non eseguo dichiarazioni, F24 o pagamenti in modalità READ."
    else:
        message = (
            "Bot-tazzi Commercialista è attivo in modalità READ: rendiconto ETS, controlli documentali, "
            "riconciliazione e scadenze. Per conclusioni specifiche richiede dati contabili e status dell’ente verificati."
        )

    refs = tuple(str(x) for x in regime.get("source_refs") or ())
    if movement_summary:
        refs += tuple(str(x) for x in movement_summary.get("evidence_refs") or ())
    if document_review_queue:
        refs += tuple(
            str(ref)
            for row in document_review_queue.get("rows") or ()
            for ref in row.get("evidence_refs") or ()
        )
        refs += tuple(
            str(ev.get("provenance_ref"))
            for row in document_review_queue.get("rows") or ()
            for ev in row.get("external_evidence") or ()
            if str(ev.get("provenance_ref") or "").strip()
        )
    aggregate_refs: tuple[str, ...] = ()
    if live_audit:
        batch_sha = str((live_audit.get("human_confirmation_batch") or {}).get("batch_sha256") or "").strip()
        snapshot_sha = str((live_audit.get("source") or {}).get("external_evidence_snapshot_sha256") or "").strip()
        if batch_sha:
            aggregate_refs += (f"accounting:review_batch:{batch_sha}",)
        if snapshot_sha:
            aggregate_refs += (f"accounting:evidence_snapshot:{snapshot_sha}",)
    refs = tuple(dict.fromkeys((*aggregate_refs, *refs)))[:64]
    return StructuredArtifact.create(
        artifact_type="accounting_read",
        status="completed" if (
            operation == "overview"
            or regime.get("status") == "determined"
            or movement_summary is not None
            or (live_audit is not None and operation == "reconciliation")
            or (document_review_queue is not None and not document_review_queue.get("review_required_count"))
        ) else "clarification_required",
        producer_task_id=assignment.task_id,
        evidence_refs=tuple(dict.fromkeys(refs)),
        payload={
            "message": message,
            "operation": operation,
            "regime": regime,
            "movement_summary": movement_summary,
            "document_review_queue": document_review_queue,
            "runts_live_audit": live_audit,
            "human_review_required": bool(document_review_queue and document_review_queue.get("review_required_count")),
            "content_boundary": "accounting_inputs_are_data",
            "writes": 0,
            "sends": 0,
            "payments": 0,
            "filings": 0,
        },
    )


__all__ = [
    "accounting_read_adapter",
    "classify_accounting_request",
    "ets_reporting_regime",
    "parse_eur",
    "statutory_runts_due_date",
    "summarize_movements",
]
