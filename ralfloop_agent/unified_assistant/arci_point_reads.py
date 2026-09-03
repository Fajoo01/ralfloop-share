from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence

from pydantic import Field, model_validator

from .contracts import StrictModel
from .platform import ErrorCode, SourceRef
from .service_identity import ArciMemberVerification, SourceEvidence, VerificationOutcome


_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,240}$")


class ArciPointReadError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ArciCardStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EXPELLED = "EXPELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ArciMember(StrictModel):
    id: str = Field(min_length=1, max_length=240)
    source: SourceRef


class ArciClub(StrictModel):
    id: str = Field(min_length=1, max_length=240)
    validity_year: int = Field(ge=2000, le=2200)
    source: SourceRef


class ArciCard(StrictModel):
    id: str = Field(min_length=1, max_length=240)
    user_id: str = Field(min_length=1, max_length=240)
    club_id: str = Field(min_length=1, max_length=240)
    number: str | None = Field(default=None, max_length=240)
    validity: int = Field(ge=2000, le=2200)
    status_raw: int
    status_normalized: ArciCardStatus
    expired: bool
    enabled_at: str | None = Field(default=None, max_length=80)
    disabled_at: str | None = Field(default=None, max_length=80)
    preregistration: bool | None = None
    consumer_movement_status: str | int | None = None
    source: SourceRef

    @model_validator(mode="after")
    def status_consistent(self) -> "ArciCard":
        expected = {
            10: ArciCardStatus.PENDING,
            20: ArciCardStatus.APPROVED,
            25: ArciCardStatus.EXPELLED,
            30: ArciCardStatus.REJECTED,
        }.get(self.status_raw, ArciCardStatus.UNKNOWN)
        if self.status_normalized is not expected:
            raise ValueError("arci_card_status_mismatch")
        return self


class ArciPointReadTransport(Protocol):
    """Fixed semantic reads. Implementations must not expose generic requests."""

    def get_member(self, user_id: str) -> Mapping[str, Any]: ...
    def get_card(self, card_id: str) -> Mapping[str, Any]: ...
    def list_member_cards(self, user_id: str) -> Sequence[Mapping[str, Any]]: ...
    def get_club(self, club_id: str) -> Mapping[str, Any]: ...


class ArciPointReadService:
    def __init__(self, transport: ArciPointReadTransport) -> None:
        self.transport = transport

    def get_member(self, user_id: str) -> ArciMember:
        native_id = _native_id(user_id)
        raw = _mapping(self.transport.get_member(native_id))
        _exact_id(raw, native_id)
        return ArciMember(id=native_id, source=_source("users", native_id, raw))

    def get_card(self, card_id: str) -> ArciCard:
        native_id = _native_id(card_id)
        raw = _mapping(self.transport.get_card(native_id))
        _exact_id(raw, native_id)
        return _card(raw, native_id)

    def get_club(self, club_id: str) -> ArciClub:
        native_id = _native_id(club_id)
        raw = _mapping(self.transport.get_club(native_id))
        _exact_id(raw, native_id)
        year = _year(raw.get("validity_year"), "club.validity_year")
        return ArciClub(id=native_id, validity_year=year, source=_source("clubs", native_id, raw))

    def list_member_cards(self, user_id: str) -> tuple[ArciCard, ...]:
        member = self.get_member(user_id)
        rows = self.transport.list_member_cards(member.id)
        if isinstance(rows, (str, bytes, Mapping)):
            raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, "member_cards_not_array")
        cards = tuple(_card(_mapping(row), str(row.get("id") or "")) for row in rows)
        if len({card.id for card in cards}) != len(cards):
            raise ArciPointReadError(ErrorCode.CONFLICT, "duplicate_card_id")
        if any(card.user_id != member.id for card in cards):
            raise ArciPointReadError(ErrorCode.CONFLICT, "card_member_relation_mismatch")
        return cards

    def verify_membership(self, user_id: str, club_id: str) -> ArciMemberVerification:
        stable_id = user_id or None
        try:
            member = self.get_member(user_id)
            club = self.get_club(club_id)
            cards = self.list_member_cards(member.id)
        except ArciPointReadError as exc:
            outcome = VerificationOutcome.NOT_FOUND if exc.code is ErrorCode.NOT_FOUND else VerificationOutcome.SOURCE_UNAVAILABLE
            return ArciMemberVerification(outcome=outcome, stable_member_id=stable_id, reason=f"{exc.code.value}:{exc}")
        except Exception:
            return ArciMemberVerification(
                outcome=VerificationOutcome.SOURCE_UNAVAILABLE,
                stable_member_id=stable_id,
                reason="SOURCE_UNAVAILABLE:point_read_transport_failed",
            )

        matching = tuple(card for card in cards if card.club_id == club.id)
        eligible = tuple(card for card in matching if (
            card.status_raw == 20 and not card.expired and card.validity >= club.validity_year
        ))
        evidence = tuple(_evidence(row.source) for row in (member, club, *matching))
        if eligible:
            chosen = sorted(eligible, key=lambda card: (card.validity, card.id), reverse=True)[0]
            return ArciMemberVerification(
                outcome=VerificationOutcome.VERIFIED_ELIGIBLE,
                stable_member_id=member.id,
                card_status=chosen.status_normalized.value,
                campaign_year=chosen.validity,
                organization_code=club.id,
                evidence=evidence,
                reason="exact_member_club_approved_unexpired_current_card",
            )
        return ArciMemberVerification(
            outcome=VerificationOutcome.VERIFIED_INELIGIBLE,
            stable_member_id=member.id,
            card_status=matching[0].status_normalized.value if matching else None,
            campaign_year=max((card.validity for card in matching), default=club.validity_year),
            organization_code=club.id,
            evidence=evidence or (_evidence(member.source),),
            reason="no_approved_unexpired_current_card_for_club",
        )


def _native_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, "invalid_native_id")
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, "response_not_object")
    return value


def _exact_id(raw: Mapping[str, Any], expected: str) -> None:
    actual = raw.get("id")
    if actual is None:
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, "response_id_missing")
    if str(actual) != expected:
        raise ArciPointReadError(ErrorCode.CONFLICT, "response_id_mismatch")


def _year(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, f"{field}_invalid")
    try:
        year = int(value)
    except (TypeError, ValueError) as exc:
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, f"{field}_invalid") from exc
    if not 2000 <= year <= 2200:
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, f"{field}_invalid")
    return year


def _source(kind: str, native_id: str, raw: Mapping[str, Any]) -> SourceRef:
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    return SourceRef(
        system="arci",
        native_id=native_id,
        locator=f"/{kind}/{native_id}",
        observed_at=datetime.now(timezone.utc).isoformat(),
        content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def _card(raw: Mapping[str, Any], expected_id: str) -> ArciCard:
    expected_id = _native_id(expected_id)
    _exact_id(raw, expected_id)
    try:
        status = int(raw["status"])
        expired = raw["expired"]
        if not isinstance(expired, bool):
            raise TypeError
        user_id = _native_id(str(raw["user_id"]))
        club_id = _native_id(str(raw["club_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, "card_contract_invalid") from exc
    normalized = {10: ArciCardStatus.PENDING, 20: ArciCardStatus.APPROVED, 25: ArciCardStatus.EXPELLED, 30: ArciCardStatus.REJECTED}.get(status, ArciCardStatus.UNKNOWN)
    return ArciCard(
        id=expected_id, user_id=user_id, club_id=club_id,
        number=str(raw["number"]) if raw.get("number") is not None else None,
        validity=_year(raw.get("validity"), "card.validity"),
        status_raw=status, status_normalized=normalized, expired=expired,
        enabled_at=str(raw["enabled_at"]) if raw.get("enabled_at") is not None else None,
        disabled_at=str(raw["disabled_at"]) if raw.get("disabled_at") is not None else None,
        preregistration=raw.get("preregistration"),
        consumer_movement_status=raw.get("consumer_movement_status"),
        source=_source("cards", expected_id, raw),
    )


def _evidence(source: SourceRef) -> SourceEvidence:
    return SourceEvidence(
        source_type="arci_mcp", source_id=source.native_id,
        locator=source.locator, observed_at=datetime.fromisoformat(source.observed_at),
        content_hash=source.content_hash or hashlib.sha256(source.locator.encode()).hexdigest(),
    )


__all__ = ["ArciCard", "ArciCardStatus", "ArciClub", "ArciMember", "ArciPointReadError", "ArciPointReadService", "ArciPointReadTransport"]
