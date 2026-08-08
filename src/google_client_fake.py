from __future__ import annotations

from typing import Any


class GoogleClientFake:
    def __init__(self, draft_only: bool = True) -> None:
        self.draft_only = draft_only
        self.drafts: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    def create_draft(self, to: str, subject: str, body: str) -> dict[str, Any]:
        draft = {
            "status": "draft_created",
            "draft_only": True,
            "to": to,
            "subject": subject,
            "body": body,
            "id": f"draft_{len(self.drafts)}",
        }
        self.drafts.append(draft)
        return draft

    def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        sent = {
            "status": "sent",
            "draft_only": False,
            "to": to,
            "subject": subject,
            "body": body,
            "id": f"sent_{len(self.sent)}",
        }
        self.sent.append(sent)
        return sent

    def is_configured(self) -> bool:
        return True
