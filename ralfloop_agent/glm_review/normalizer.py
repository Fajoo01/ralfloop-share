from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
import re
from typing import Any, Iterable


HISTORICAL_GRANT_FIELDS = (
    "goal",
    "constraints",
    "draft",
    "requirements",
    "evidence",
    "budget_summary",
    "known_gaps",
    "requested_review",
)
STRUCTURED_GRANT_FIELDS = (
    "age_range",
    "participant_count",
    "duration_minutes",
    "same_group",
    "free_event",
    "budget_total",
    "budget_items",
    "deadline",
    "event_window",
    "organization_status",
)
MONTHS = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
NUMBER_WORDS = {
    "un": 1,
    "uno": 1,
    "una": 1,
    "due": 2,
    "tre": 3,
    "quattro": 4,
    "cinque": 5,
    "sei": 6,
    "sette": 7,
    "otto": 8,
    "nove": 9,
    "dieci": 10,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
DATE_TEXT = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}\s+(?:" + "|".join(MONTHS) + r")\s+\d{4})"


def normalize_grant_review(
    value: dict[str, Any],
    *,
    source_file: str = "memory",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select and normalize raw, historical-artifact, and structured grant inputs."""
    if not isinstance(value, dict):
        raise ValueError("grant_input_must_be_object")
    selected, selected_path, warnings = _select_payload(value)
    normalized = deepcopy(selected)
    derived: dict[str, dict[str, Any]] = {}
    provenance = deepcopy(normalized.get("field_provenance")) if isinstance(normalized.get("field_provenance"), dict) else {}

    proposal = normalized.get("proposal") if isinstance(normalized.get("proposal"), dict) else {}
    for name in (*HISTORICAL_GRANT_FIELDS, *STRUCTURED_GRANT_FIELDS, "objectives", "activities", "kpis"):
        if normalized.get(name) in (None, "", []) and proposal.get(name) not in (None, "", []):
            _derive(normalized, provenance, derived, name, deepcopy(proposal[name]), f"{selected_path}.proposal.{name}")

    corpus = list(_text_corpus(normalized, selected_path))
    _derive_alias(normalized, provenance, derived, "participant_count", ("participants", "beneficiaries_count"), selected_path)
    _derive_alias(normalized, provenance, derived, "same_group", ("same_participant_group",), selected_path)
    _derive_alias(normalized, provenance, derived, "free_event", ("free_participation", "free", "gratuita"), selected_path)

    if normalized.get("age_range") in (None, "", []):
        found = _search(corpus, (
            re.compile(r"\b(?:da(?:gli|i)?|tra)\s+(\d{1,2})\s+(?:a|ai|e)\s+(\d{1,2})\s+anni\b", re.I),
            re.compile(r"\b(\d{1,2})\s*[-–—/]\s*(\d{1,2})\s+(?:anni|years?)\b", re.I),
        ))
        if found:
            match, path, evidence_ref = found
            _derive(normalized, provenance, derived, "age_range", f"{match.group(1)}-{match.group(2)}", path, evidence_ref)

    if normalized.get("participant_count") in (None, "", []):
        found = _search(corpus, (
            re.compile(r"\b(?:almeno|minimo(?:\s+di)?|minimum(?:\s+of)?)?\s*(\d{1,4})\s+(?:partecipanti|participants?|giovani|youths?)\b", re.I),
        ))
        if found:
            match, path, evidence_ref = found
            _derive(normalized, provenance, derived, "participant_count", int(match.group(1)), path, evidence_ref)

    if normalized.get("duration_minutes") in (None, "", []):
        total = _search(corpus, (re.compile(r"\b(?:durata\s+totale|totale|total(?:\s+duration)?)\s*[:=]?\s*(\d{2,4})\s*(?:minuti|min\.?|minutes?)\b", re.I),))
        if total:
            match, path, evidence_ref = total
            _derive(normalized, provenance, derived, "duration_minutes", int(match.group(1)), path, evidence_ref)
        else:
            sessions = _search(corpus, (re.compile(
                r"\b(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")\s+(?:incontri|sessioni|sessions?|meetings?)\s+(?:da|di|of|x)\s*(\d{1,3})\s*(?:minuti|min\.?|minutes?)\b",
                re.I,
            ),))
            if sessions:
                match, path, evidence_ref = sessions
                count = int(match.group(1)) if match.group(1).isdigit() else NUMBER_WORDS[match.group(1).casefold()]
                _derive(normalized, provenance, derived, "duration_minutes", count * int(match.group(2)), path, evidence_ref)

    if normalized.get("same_group") in (None, "", []):
        found = _search(corpus, (re.compile(r"\b(?:stesso gruppo|medesimo gruppo|same group)\b", re.I),))
        if found:
            _, path, evidence_ref = found
            _derive(normalized, provenance, derived, "same_group", True, path, evidence_ref)

    if normalized.get("free_event") in (None, "", []):
        found = _search(corpus, (re.compile(r"\b(?:partecipazione\s+)?gratuit[oaie]\b|\bfree(?:\s+(?:event|participation))?\b", re.I),))
        if found:
            _, path, evidence_ref = found
            _derive(normalized, provenance, derived, "free_event", True, path, evidence_ref)

    _derive_budget(normalized, provenance, derived, selected_path)

    if normalized.get("deadline") in (None, "", []):
        found = _search(corpus, (
            re.compile(r"\b(?:deadline|scadenza|entro(?:\s+il)?|fino\s+al)\b.{0,80}?(" + DATE_TEXT + r")", re.I | re.S),
            re.compile(r"\bpropost[ea]\b.{0,180}?\b(?:al|fino\s+al)\s+(" + DATE_TEXT + r")", re.I | re.S),
        ))
        if found:
            match, path, evidence_ref = found
            parsed = _parse_date(match.group(1))
            if parsed:
                _derive(normalized, provenance, derived, "deadline", parsed, path, evidence_ref)

    if normalized.get("event_window") in (None, "", []):
        found = _search(corpus, (re.compile(
            r"\b(?:periodo(?:\s+(?:degli\s+)?eventi)?|eventi|evento)\b.{0,80}?\b(?:tra|dal|da)\s+(?:il\s+)?(" + DATE_TEXT + r")\s+(?:e|al|a)\s+(?:il\s+)?(" + DATE_TEXT + r")",
            re.I | re.S,
        ),))
        if found:
            match, path, evidence_ref = found
            start, end = _parse_date(match.group(1)), _parse_date(match.group(2))
            if start and end:
                _derive(normalized, provenance, derived, "event_window", {"start": start, "end": end}, path, evidence_ref)

    if normalized.get("organization_status") in (None, "", []):
        found = _search(corpus, (
            re.compile(r"\b(?:Tiremm(?:\s+Innanz)?\s+)?(APS|ETS|ODV|ONLUS)\b", re.I),
            re.compile(r"\b(organizzazione\s+non\s+profit|nonprofit organization)\b", re.I),
        ))
        if found:
            match, path, evidence_ref = found
            _derive(normalized, provenance, derived, "organization_status", match.group(1).upper(), path, evidence_ref)

    if provenance:
        normalized["field_provenance"] = provenance
    source_draft = _source_draft(selected)
    mapping_error = bool(source_draft.strip()) and not str(normalized.get("draft") or "").strip()
    if mapping_error:
        warnings.append("input_mapping_error:source_draft_became_empty")
    preserved = [
        name for name in HISTORICAL_GRANT_FIELDS
        if name in selected and normalized.get(name) == selected.get(name)
    ]
    missing = [
        name for name in (*HISTORICAL_GRANT_FIELDS, *STRUCTURED_GRANT_FIELDS)
        if normalized.get(name) in (None, "", [], {})
    ]
    report = {
        "schema_version": 1,
        "source_file": source_file,
        "input_keys": sorted(str(key) for key in value),
        "selected_source_path": selected_path,
        "selected_keys": sorted(str(key) for key in selected),
        "normalized_keys": sorted(str(key) for key in normalized),
        "fields_preserved": preserved,
        "fields_derived": derived,
        "fields_missing": missing,
        "warnings": warnings,
        "mapping_error": mapping_error,
        "source_hashes": {
            "input_sha256": _hash_json(value),
            "selected_sha256": _hash_json(selected),
            "normalized_sha256": _hash_json(normalized),
        },
    }
    return normalized, report


def source_draft_present(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    selected, _, _ = _select_payload(value)
    return bool(_source_draft(selected).strip())


def _select_payload(value: dict[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
    selected = value
    path = "$"
    warnings: list[str] = []
    for _ in range(4):
        nested = selected.get("payload")
        if not isinstance(nested, dict):
            break
        outer_grant_keys = set(selected) & set(HISTORICAL_GRANT_FIELDS)
        nested_grant_keys = set(nested) & (set(HISTORICAL_GRANT_FIELDS) | {"proposal"})
        artifact_wrapper = isinstance(selected.get("source"), dict) and "schema_version" in selected
        if not nested_grant_keys or (outer_grant_keys and not artifact_wrapper):
            break
        selected = nested
        path += ".payload"
        warnings.append("historical_input_artifact_unwrapped")
    return selected, path, warnings


def _source_draft(value: dict[str, Any]) -> str:
    raw = value.get("draft")
    if isinstance(raw, str):
        return raw
    proposal = value.get("proposal")
    if isinstance(proposal, dict) and isinstance(proposal.get("draft"), str):
        return proposal["draft"]
    return ""


def _text_corpus(value: dict[str, Any], path: str) -> Iterable[tuple[str, str, str | None]]:
    for name in ("draft", "project_description", "description"):
        raw = value.get(name)
        if isinstance(raw, str) and raw.strip():
            yield raw, f"{path}.{name}", None
    proposal = value.get("proposal")
    if isinstance(proposal, dict):
        for name in ("draft", "project_description", "description"):
            raw = proposal.get(name)
            if isinstance(raw, str) and raw.strip():
                yield raw, f"{path}.proposal.{name}", None
    for name in ("requirements", "constraints", "known_gaps"):
        rows = value.get(name)
        if isinstance(rows, list):
            for index, raw in enumerate(rows):
                if isinstance(raw, str) and raw.strip():
                    yield raw, f"{path}.{name}[{index}]", None
    evidence = value.get("evidence")
    if isinstance(evidence, list):
        for index, row in enumerate(evidence):
            if not isinstance(row, dict):
                continue
            text = "\n".join(str(row.get(name) or "") for name in ("claim", "excerpt", "text"))
            if text.strip():
                source_id = str(row.get("source_id") or row.get("chunk_id") or "") or None
                yield text, f"{path}.evidence[{index}]", source_id
    for name in ("goal",):
        raw = value.get(name)
        if isinstance(raw, str) and raw.strip():
            yield raw, f"{path}.{name}", None


def _search(
    corpus: Iterable[tuple[str, str, str | None]],
    patterns: Iterable[re.Pattern[str]],
) -> tuple[re.Match[str], str, str | None] | None:
    rows = list(corpus)
    for text, path, evidence_ref in rows:
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return match, path, evidence_ref
    return None


def _derive_alias(
    value: dict[str, Any],
    provenance: dict[str, Any],
    derived: dict[str, dict[str, Any]],
    target: str,
    aliases: tuple[str, ...],
    path: str,
) -> None:
    if value.get(target) not in (None, "", []):
        return
    for alias in aliases:
        if value.get(alias) not in (None, "", []):
            _derive(value, provenance, derived, target, deepcopy(value[alias]), f"{path}.{alias}")
            return


def _derive(
    value: dict[str, Any],
    provenance: dict[str, Any],
    derived: dict[str, dict[str, Any]],
    name: str,
    result: Any,
    source_path: str,
    evidence_ref: str | None = None,
) -> None:
    value[name] = result
    origin: dict[str, Any] = {"source_path": source_path}
    if evidence_ref:
        origin["evidence_ref"] = evidence_ref
    provenance[name] = origin
    derived[name] = origin


def _derive_budget(
    value: dict[str, Any],
    provenance: dict[str, Any],
    derived: dict[str, dict[str, Any]],
    path: str,
) -> None:
    summary = value.get("budget_summary")
    if not isinstance(summary, dict):
        return
    raw_lines = summary.get("items") if isinstance(summary.get("items"), list) else summary.get("lines")
    lines = _budget_lines(raw_lines)
    if value.get("budget_items") in (None, "", []) and lines:
        key = "items" if isinstance(summary.get("items"), list) else "lines"
        _derive(value, provenance, derived, "budget_items", lines, f"{path}.budget_summary.{key}")
    total = _number(summary.get("total"))
    if total is None and lines and all(isinstance(row.get("amount"), (int, float)) for row in lines):
        total = sum(float(row["amount"]) for row in lines)
    if value.get("budget_total") in (None, "", []) and total is not None:
        total_value: int | float = int(total) if total.is_integer() else total
        _derive(value, provenance, derived, "budget_total", total_value, f"{path}.budget_summary.total")


def _budget_lines(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        row = deepcopy(raw)
        amount = _number(row.get("amount"))
        if amount is not None:
            row["amount"] = int(amount) if amount.is_integer() else amount
        output.append(row)
    return output


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9,.-]", "", str(value)).strip()
    if not text:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date(value: str) -> str | None:
    text = " ".join(str(value).strip().casefold().split())
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", text)
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "HISTORICAL_GRANT_FIELDS",
    "STRUCTURED_GRANT_FIELDS",
    "normalize_grant_review",
    "source_draft_present",
]
