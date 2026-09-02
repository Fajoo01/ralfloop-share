from __future__ import annotations

import json
from typing import Any
import urllib.error
import urllib.request

from pydantic import BaseModel, ConfigDict, Field


class RuntsuiteMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: str
    runtsuite_identity_id: str | None = None
    external_member_id: str | None = None
    active: bool | None = None


class RuntsuiteReadOnlyAdapter:
    """Explicit GET-only adapter for the observed RUNTSuite 0.1.0 API."""

    ENDPOINTS = {
        "projects": "/projects/",
        "funding_calls": "/funding-calls/",
        "meetings": "/meetings/",
        "attendance": "/attendance/",
        "member_cards": "/member-cards/list",
        "member_account_links": "/member-account-links/",
        "review_queue": "/runts/review-queue",
        "members": "/members/",
    }

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_member(self, external_member_id: str) -> RuntsuiteMember:
        stable_id = external_member_id.strip()
        if not stable_id or len(stable_id) > 240:
            raise ValueError("runtsuite_external_member_id_invalid")
        rows = self._get("members")
        if not isinstance(rows, list):
            raise RuntimeError("runtsuite_members_malformed")
        matches = [row for row in rows if isinstance(row, dict) and str(row.get("external_member_id") or "") == stable_id]
        if len(matches) > 1:
            return RuntsuiteMember(status="AMBIGUOUS", external_member_id=stable_id)
        if not matches:
            return RuntsuiteMember(status="NOT_FOUND", external_member_id=stable_id)
        row = matches[0]
        identity = row.get("member_id")
        if not isinstance(identity, int) or identity < 1:
            raise RuntimeError("runtsuite_member_malformed")
        return RuntsuiteMember(
            status="FOUND", runtsuite_identity_id=str(identity),
            external_member_id=stable_id, active=bool(row.get("active")),
        )

    def list_projects(self): return self._get("projects")
    def list_funding_calls(self): return self._get("funding_calls")
    def list_meetings(self): return self._get("meetings")
    def list_attendance(self): return self._get("attendance")
    def list_member_cards(self): return self._get("member_cards")
    def list_member_account_links(self): return self._get("member_account_links")
    def list_review_queue(self): return self._get("review_queue")

    def _get(self, capability: str) -> Any:
        if capability not in self.ENDPOINTS:
            raise ValueError("runtsuite_capability_not_allowlisted")
        request = urllib.request.Request(
            self.base_url + self.ENDPOINTS[capability],
            method="GET", headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            raise RuntimeError("runtsuite_source_unavailable") from exc


__all__ = ["RuntsuiteMember", "RuntsuiteReadOnlyAdapter"]
