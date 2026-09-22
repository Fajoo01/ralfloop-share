from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ralfloop_agent.cli.session_store import SessionStore

from .contracts import PolicyClass


PENDING_DOMAINS = (
    "email", "whatsapp", "mailchimp", "jellyfin", "browser", "runts",
    "home", "infrastructure", "bandi", "clarification",
)
CONFIRM_WORDS = frozenset({
    "ok", "invia", "manda", "mandala", "sì invia", "si invia", "va bene",
    "confermo", "approvo", "procedi",
})
CANCEL_WORDS = frozenset({"annulla", "cancella", "no"})


PendingDomain = Literal[
    "email", "whatsapp", "mailchimp", "jellyfin", "browser", "runts",
    "home", "infrastructure", "bandi", "clarification",
]


class PendingAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pending_id: str = Field(pattern=r"^pending_[a-f0-9]{16}$")
    domain: PendingDomain
    action: str = Field(min_length=1, max_length=96)
    policy: PolicyClass
    payload: dict[str, Any]
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    displayed_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: int = Field(ge=1, le=1_000_000)
    created_at: int = Field(default_factory=lambda: int(time.time()), ge=0)
    expires_at: int = Field(default_factory=lambda: int(time.time()) + 3600, ge=0)
    approval_ref: str | None = Field(default=None, max_length=160)
    approved_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class PendingByDomain(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    email: PendingAction | None = None
    whatsapp: PendingAction | None = None
    mailchimp: PendingAction | None = None
    jellyfin: PendingAction | None = None
    browser: PendingAction | None = None
    runts: PendingAction | None = None
    home: PendingAction | None = None
    infrastructure: PendingAction | None = None
    bandi: PendingAction | None = None
    clarification: PendingAction | None = None


class ConversationState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["unified_conversation_v1"] = "unified_conversation_v1"
    last_intent: str | None = Field(default=None, max_length=96)
    last_entities: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    pending: PendingByDomain = Field(default_factory=PendingByDomain)


class ConfirmationResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "not_confirmation", "no_pending", "ambiguous", "resolved", "cancelled", "expired"
    ]
    pending: PendingAction | None = None
    domains: tuple[str, ...] = ()


class ConversationManager:
    def __init__(self, state: ConversationState | None = None) -> None:
        self.state = state or ConversationState()

    def remember(
        self, *, intent: str, domain: str, entities: tuple[str, ...] = ()
    ) -> ConversationState:
        last_entities = dict(self.state.last_entities)
        last_entities[domain] = tuple(entities[:8])
        self.state = self.state.model_copy(update={
            "last_intent": intent[:96],
            "last_entities": last_entities,
        })
        return self.state

    def stage(
        self,
        *,
        domain: str,
        action: str,
        policy: PolicyClass,
        payload: dict[str, Any],
        displayed_text: str,
        ttl_seconds: int = 3600,
    ) -> PendingAction:
        if domain not in PENDING_DOMAINS:
            raise ValueError("unsupported_pending_domain")
        previous = getattr(self.state.pending, domain)
        version = previous.version + 1 if previous else 1
        now = int(time.time())
        pending = PendingAction(
            pending_id="pending_" + uuid4().hex[:16],
            domain=domain,  # type: ignore[arg-type]
            action=action,
            policy=policy,
            payload=payload,
            payload_digest=_digest(payload),
            displayed_digest=_text_digest(displayed_text),
            version=version,
            created_at=now,
            expires_at=now + max(60, min(int(ttl_seconds), 86_400)),
        )
        self._set_pending(domain, pending)
        return pending

    def revise_email(
        self,
        *,
        body: str,
        displayed_text: str,
        risk: str | None = None,
        validation_state: str | None = None,
    ) -> PendingAction:
        current = self.state.pending.email
        if current is None:
            raise ValueError("email_draft_not_pending")
        payload = dict(current.payload)
        payload["body"] = body
        if risk is not None:
            payload["risk"] = risk
        if validation_state is not None:
            payload["validation_state"] = validation_state
        pending = PendingAction(
            pending_id="pending_" + uuid4().hex[:16],
            domain="email",
            action=current.action,
            policy=current.policy,
            payload=payload,
            payload_digest=_digest(payload),
            displayed_digest=_text_digest(displayed_text),
            version=current.version + 1,
            created_at=int(time.time()),
            expires_at=int(time.time()) + max(60, current.expires_at - current.created_at),
            approval_ref=None,
            approved_digest=None,
        )
        self._set_pending("email", pending)
        return pending

    def revise_whatsapp(
        self,
        *,
        body: str,
        displayed_text: str,
        risk: str | None = None,
        validation_state: str | None = None,
    ) -> PendingAction:
        current = self.state.pending.whatsapp
        if current is None:
            raise ValueError("whatsapp_draft_not_pending")
        payload = dict(current.payload)
        payload["body"] = body
        if risk is not None:
            payload["risk"] = risk
        if validation_state is not None:
            payload["validation_state"] = validation_state
        now = int(time.time())
        pending = PendingAction(
            pending_id="pending_" + uuid4().hex[:16],
            domain="whatsapp",
            action=current.action,
            policy=current.policy,
            payload=payload,
            payload_digest=_digest(payload),
            displayed_digest=_text_digest(displayed_text),
            version=current.version + 1,
            created_at=now,
            expires_at=now + max(60, current.expires_at - current.created_at),
            approval_ref=None,
            approved_digest=None,
        )
        self._set_pending("whatsapp", pending)
        return pending

    def bind_approval(
        self,
        *,
        domain: str,
        pending_id: str,
        payload_digest: str,
        approval_ref: str,
    ) -> PendingAction:
        current = getattr(self.state.pending, domain, None)
        if current is None or current.pending_id != pending_id:
            raise ValueError("pending_action_stale")
        if current.payload_digest != payload_digest:
            raise ValueError("pending_payload_hash_mismatch")
        bound = current.model_copy(update={
            "approval_ref": approval_ref,
            "approved_digest": payload_digest,
        })
        self._set_pending(domain, bound)
        return bound

    def attach_approval_request(
        self,
        *,
        domain: str,
        pending_id: str,
        payload_digest: str,
        approval_ref: str,
        created_at: int,
        expires_at: int,
    ) -> PendingAction:
        current = getattr(self.state.pending, domain, None)
        if current is None or current.pending_id != pending_id:
            raise ValueError("pending_action_stale")
        if current.payload_digest != payload_digest:
            raise ValueError("pending_payload_hash_mismatch")
        attached = current.model_copy(update={
            "approval_ref": approval_ref,
            "approved_digest": None,
            "created_at": int(created_at),
            "expires_at": int(expires_at),
        })
        self._set_pending(domain, attached)
        return attached

    def resolve_confirmation(
        self, text: str, *, domain_hint: str | None = None
    ) -> ConfirmationResolution:
        folded = " ".join(text.casefold().split()).strip(" .!?")
        if folded not in CONFIRM_WORDS | CANCEL_WORDS:
            return ConfirmationResolution(status="not_confirmation")
        active = [
            item for name in PENDING_DOMAINS
            if (item := getattr(self.state.pending, name)) is not None
            and (domain_hint is None or name == domain_hint)
        ]
        if not active:
            return ConfirmationResolution(status="no_pending")
        if len(active) > 1:
            return ConfirmationResolution(
                status="ambiguous", domains=tuple(sorted(item.domain for item in active))
            )
        pending = active[0]
        if folded in CANCEL_WORDS:
            self.clear(pending.domain)
            return ConfirmationResolution(status="cancelled", pending=pending)
        if pending.expires_at <= int(time.time()):
            self.clear(pending.domain)
            return ConfirmationResolution(status="expired", pending=pending)
        return ConfirmationResolution(status="resolved", pending=pending)

    def clear(self, domain: str) -> None:
        self._set_pending(domain, None)

    def last_entity(self, domain: str) -> str | None:
        entities = self.state.last_entities.get(domain, ())
        return entities[0] if len(entities) == 1 else None

    def _set_pending(self, domain: str, value: PendingAction | None) -> None:
        self.state = self.state.model_copy(update={
            "pending": self.state.pending.model_copy(update={domain: value})
        })


class SessionConversationAdapter:
    """Persists bounded state in the existing atomic SessionStore."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def load(self, session_id: str) -> ConversationManager:
        record = self.store.load(session_id)
        raw = record.get("assistant_state")
        return ConversationManager(
            ConversationState.model_validate(raw) if isinstance(raw, dict) else None
        )

    def save(self, session_id: str, manager: ConversationManager) -> None:
        record = self.store.load(session_id)
        record["assistant_state"] = manager.state.model_dump(mode="json")
        self.store.save(record)


def payload_matches(pending: PendingAction) -> bool:
    return pending.payload_digest == _digest(pending.payload)


def approval_matches(pending: PendingAction) -> bool:
    return (
        payload_matches(pending)
        and pending.approval_ref is not None
        and pending.approved_digest == pending.payload_digest
    )


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


__all__ = [
    "ConfirmationResolution",
    "ConversationManager",
    "ConversationState",
    "PendingAction",
    "SessionConversationAdapter",
    "approval_matches",
    "payload_matches",
]
