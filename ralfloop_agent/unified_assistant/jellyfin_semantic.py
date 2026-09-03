from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Mapping

from pydantic import Field

from .service_identity import SourceEvidence, StrictModel


class JellyfinHealth(StrictModel):
    available: bool
    server_id: str | None = Field(default=None, max_length=240)
    server_name: str | None = Field(default=None, max_length=240)
    version: str | None = Field(default=None, max_length=80)
    startup_wizard_completed: bool | None = None
    evidence: tuple[SourceEvidence, ...] = ()


class JellyfinLibrary(StrictModel):
    item_id: str = Field(min_length=1, max_length=240)
    name: str = Field(min_length=1, max_length=240)
    collection_type: str | None = Field(default=None, max_length=80)


class JellyfinItem(StrictModel):
    item_id: str = Field(min_length=1, max_length=240)
    name: str = Field(min_length=1, max_length=500)
    item_type: str = Field(min_length=1, max_length=80)
    series_name: str | None = Field(default=None, max_length=500)
    season_name: str | None = Field(default=None, max_length=500)
    index_number: int | None = None
    parent_index_number: int | None = None
    evidence: tuple[SourceEvidence, ...]


class JellyfinMediaStream(StrictModel):
    media_source_id: str = Field(min_length=1, max_length=240)
    index: int = Field(ge=0)
    type: str = Field(min_length=1, max_length=40)
    codec: str | None = Field(default=None, max_length=80)
    language: str | None = Field(default=None, max_length=40)
    display_title: str | None = Field(default=None, max_length=240)
    is_default: bool | None = None
    is_external: bool | None = None
    channels: int | None = Field(default=None, ge=0, le=128)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    bitrate: int | None = Field(default=None, ge=0)


class JellyfinProposalAction(StrEnum):
    USER_CREATE = "USER_CREATE"
    USER_LINK = "USER_LINK"
    USER_ENABLE = "USER_ENABLE"
    USER_DISABLE = "USER_DISABLE"


class JellyfinActionProposal(StrictModel):
    proposal_id: str = Field(pattern=r"^proposal\.jellyfin\.[a-f0-9]{24}$")
    action: JellyfinProposalAction
    arci_member_id: str = Field(min_length=1, max_length=240)
    jellyfin_user_id: str | None = Field(default=None, max_length=240)
    username: str | None = Field(default=None, max_length=240)
    reason: str = Field(min_length=1, max_length=500)
    evidence: tuple[SourceEvidence, ...]
    requires_approval: bool = True
    executable: bool = False


class JellyfinProposalService:
    def prepare(
        self, action: JellyfinProposalAction, *, arci_member_id: str,
        jellyfin_user_id: str | None = None, username: str | None = None,
        reason: str, evidence: tuple[SourceEvidence, ...],
    ) -> JellyfinActionProposal:
        if action in {JellyfinProposalAction.USER_LINK, JellyfinProposalAction.USER_ENABLE, JellyfinProposalAction.USER_DISABLE} and not jellyfin_user_id:
            raise ValueError("jellyfin_proposal_user_id_required")
        if action is JellyfinProposalAction.USER_CREATE and not username:
            raise ValueError("jellyfin_proposal_username_required")
        canonical = json.dumps({
            "action": action, "arci_member_id": arci_member_id,
            "jellyfin_user_id": jellyfin_user_id, "username": username,
            "reason": reason,
        }, sort_keys=True, separators=(",", ":"))
        return JellyfinActionProposal(
            proposal_id="proposal.jellyfin." + hashlib.sha256(canonical.encode()).hexdigest()[:24],
            action=action, arci_member_id=arci_member_id,
            jellyfin_user_id=jellyfin_user_id, username=username,
            reason=reason, evidence=evidence,
        )


def evidence_for_json(source_id: str, locator: str, payload: Mapping[str, Any]) -> SourceEvidence:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    from datetime import datetime, timezone
    return SourceEvidence(
        source_type="jellyfin_api", source_id=source_id, locator=locator,
        observed_at=datetime.now(timezone.utc),
        content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


__all__ = ["JellyfinActionProposal", "JellyfinHealth", "JellyfinItem", "JellyfinLibrary", "JellyfinMediaStream", "JellyfinProposalAction", "JellyfinProposalService", "evidence_for_json"]
