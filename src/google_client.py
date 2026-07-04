from __future__ import annotations

import base64
from email.message import EmailMessage
from pathlib import Path
from typing import Any


SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
]


class GoogleAuthError(RuntimeError):
    pass


class GoogleClient:
    def __init__(
        self,
        client_secrets_path: str | Path | None,
        token_path: str | Path | None,
        draft_only: bool = True,
    ) -> None:
        self.client_secrets_path = Path(client_secrets_path).expanduser() if client_secrets_path else None
        self.token_path = Path(token_path).expanduser() if token_path else None
        self.draft_only = draft_only

    def is_configured(self) -> bool:
        return bool(
            self.client_secrets_path
            and self.token_path
            and self.client_secrets_path.exists()
            and self.token_path.exists()
        )

    def create_draft(self, to: str, subject: str, body: str) -> dict[str, Any]:
        try:
            service = self._build_service()
            raw = self._build_raw_message(to, subject, body)
            result = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
            return {"status": "draft_created", "draft_only": True, "id": result.get("id"), "result": result}
        except GoogleAuthError:
            raise
        except Exception as exc:
            raise GoogleAuthError(f"Google draft creation failed: {exc}") from exc

    def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        try:
            service = self._build_service()
            raw = self._build_raw_message(to, subject, body)
            result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            return {"status": "sent", "draft_only": False, "id": result.get("id"), "result": result}
        except GoogleAuthError:
            raise
        except Exception as exc:
            raise GoogleAuthError(f"Google email send failed: {exc}") from exc

    def _build_service(self) -> Any:
        if not self.is_configured():
            raise GoogleAuthError("Google OAuth paths are not configured")
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except Exception as exc:  # pragma: no cover - depends on optional deployment deps
            raise GoogleAuthError(f"Google client libraries unavailable: {exc}") from exc

        assert self.token_path is not None
        creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
        if not creds.valid:
            raise GoogleAuthError("Google OAuth token is invalid or expired")
        return build("gmail", "v1", credentials=creds)

    @staticmethod
    def _build_raw_message(to: str, subject: str, body: str) -> str:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
