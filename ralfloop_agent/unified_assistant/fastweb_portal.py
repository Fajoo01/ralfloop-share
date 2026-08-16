from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .browser_read_only import AccessibilitySnapshot, BrowserReadOnlyError, CdpReadOnlySnapshotClient, ax_name


class FastwebPortalFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    value: str
    certainty: Literal["verified", "unknown"] = "verified"
    provenance_ref: str


class FastwebInvoiceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invoice_ref: str
    due_date: str | None = None
    amount: str | None = None
    status: str | None = None
    provenance_ref: str


class FastwebPortalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["FOUND", "UNKNOWN", "AUTH_REQUIRED", "UNAVAILABLE"]
    session_authenticated: bool
    offer_name: str | None = None
    current_fee: str | None = None
    effective_date: str | None = None
    invoices: tuple[FastwebInvoiceSummary, ...] = Field(default_factory=tuple, max_length=3)
    notices: tuple[FastwebPortalFact, ...] = Field(default_factory=tuple, max_length=8)
    provenance: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    read_operations: tuple[str, ...] = Field(default_factory=tuple)
    write_operations: int = 0
    blocked_mutations: int = 0
    content_role: Literal["data"] = "data"
    response: str


class SnapshotReader(Protocol):
    def snapshot(self, *, expected_host: str, expected_path_prefix: str = "/") -> AccessibilitySnapshot: ...


class FastwebPortalReadOnly:
    def __init__(self, reader: SnapshotReader) -> None:
        self.reader = reader

    @classmethod
    def from_environment(cls) -> "FastwebPortalReadOnly":
        endpoint = os.getenv("RALFLOOP_FASTWEB_CDP_ENDPOINT", "http://127.0.0.1:9236")
        return cls(CdpReadOnlySnapshotClient(endpoint))

    def read(self) -> FastwebPortalResult:
        try:
            snapshot = self.reader.snapshot(expected_host="fastweb.it", expected_path_prefix="/myfastweb/")
        except BrowserReadOnlyError:
            return _result("UNAVAILABLE", False, response="MyFastPage non disponibile; nessun dato letto.")
        names = tuple(name for node in snapshot.nodes if (name := ax_name(node)))
        folded = "\n".join(names).casefold()
        authenticated_signal = any(
            term in folded for term in ("offerta attiva", "la mia offerta", "le mie fatture")
        )
        if not authenticated_signal and any(
            term in folded for term in ("inserisci la password", "codice otp", "autenticazione richiesta")
        ):
            return _result(
                "AUTH_REQUIRED", False, operations=snapshot.operations,
                response="Sessione MyFastPage non autenticata; accesso manuale richiesto.",
            )
        offer = _value_after(names, "OFFERTA ATTIVA", _valid_offer)
        fee = _labeled_value(names, ("CANONE ATTUALE", "CANONE MENSILE", "COSTO MENSILE"), _is_amount)
        effective = _labeled_value(names, ("DECORRENZA", "ATTIVA DAL", "VALIDA DAL"), _is_date)
        invoices = _extract_invoices(names)
        notices = _extract_notices(names)
        provenance = tuple(dict.fromkeys(
            (["fastweb_portal:myfastweb#offer"] if offer else [])
            + (["fastweb_portal:myfastweb#current_fee"] if fee else [])
            + (["fastweb_portal:myfastweb#effective_date"] if effective else [])
            + [item.provenance_ref for item in invoices]
            + [item.provenance_ref for item in notices]
        ))
        status = "FOUND" if any((offer, fee, effective, invoices, notices)) else "UNKNOWN"
        response = _response(offer, fee, effective, invoices, notices)
        return FastwebPortalResult(
            status=status,
            session_authenticated=True,
            offer_name=offer,
            current_fee=fee,
            effective_date=effective,
            invoices=invoices,
            notices=notices,
            provenance=provenance,
            read_operations=snapshot.operations,
            response=response,
        )


def _result(status, authenticated, *, operations=(), response):
    return FastwebPortalResult(
        status=status, session_authenticated=authenticated,
        read_operations=tuple(operations), response=response,
    )


def _value_after(names: tuple[str, ...], label: str, predicate) -> str | None:
    for index, value in enumerate(names):
        if value.casefold() != label.casefold():
            continue
        for candidate in names[index + 1:index + 8]:
            if predicate(candidate):
                return candidate[:160]
    return None


def _labeled_value(names: tuple[str, ...], labels: tuple[str, ...], predicate) -> str | None:
    for label in labels:
        value = _value_after(names, label, predicate)
        if value:
            return value
    return None


def _valid_offer(value: str) -> bool:
    folded = value.casefold()
    return 3 <= len(value) <= 160 and not any(
        term in folded for term in ("metodo di pagamento", "conto corrente", "fattur", "offerta attiva")
    )


def _is_amount(value: str) -> bool:
    return bool(re.fullmatch(r"(?:€\s*)?\d{1,4}[,.]\d{2}(?:\s*€)?(?:\s*/\s*mese)?", value.strip(), re.I))


def _is_date(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", value.strip()))


def _extract_invoices(names: tuple[str, ...]) -> tuple[FastwebInvoiceSummary, ...]:
    rows: list[FastwebInvoiceSummary] = []
    for index, value in enumerate(names):
        if value.casefold() != "scadenza":
            continue
        window = names[index + 1:index + 16]
        due = next((item for item in window if _is_date(item)), None)
        amount = next((item for item in window if _is_amount(item)), None)
        status = next((item for item in window if item.casefold() in {"pagata", "pagato", "emessa", "da pagare"}), None)
        if not due and not amount:
            continue
        digest = hashlib.sha256("\x00".join((due or "", amount or "", status or "")).encode()).hexdigest()[:16]
        ref = f"fastweb_portal:invoice:{digest}"
        candidate = FastwebInvoiceSummary(
            invoice_ref=digest, due_date=due, amount=amount, status=status, provenance_ref=ref,
        )
        if candidate not in rows:
            rows.append(candidate)
        if len(rows) == 3:
            break
    return tuple(rows)


def _extract_notices(names: tuple[str, ...]) -> tuple[FastwebPortalFact, ...]:
    rows: list[FastwebPortalFact] = []
    pattern = re.compile(r"\b(?:rimodulazion|variazion[ei] economica|modifica contrattuale|modifica delle condizioni)\b", re.I)
    for value in names:
        if pattern.search(value):
            digest = hashlib.sha256(value.encode()).hexdigest()[:16]
            rows.append(FastwebPortalFact(
                field="contract_notice", value=value[:500],
                provenance_ref=f"fastweb_portal:notice:{digest}",
            ))
        if len(rows) == 8:
            break
    return tuple(rows)


def _response(offer, fee, effective, invoices, notices) -> str:
    parts: list[str] = []
    if offer:
        parts.append(f"Offerta MyFastPage: {offer}.")
    parts.append(f"Canone corrente: {fee}." if fee else "Canone corrente non esposto in modo esplicito nella pagina letta.")
    if effective:
        parts.append(f"Decorrenza: {effective}.")
    if invoices:
        latest = invoices[0]
        parts.append(f"Ultima fattura visibile: {latest.amount or 'importo non trovato'}, scadenza {latest.due_date or 'non trovata'}.")
    if notices:
        parts.append(f"Avvisi contrattuali espliciti visibili: {len(notices)}.")
    return " ".join(parts) or "Nessun campo Fastweb verificabile trovato nella pagina corrente."


__all__ = ["FastwebPortalReadOnly", "FastwebPortalResult"]
