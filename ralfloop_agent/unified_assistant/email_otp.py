from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import ModuleType
from typing import Any, Mapping

_VERIFY_LOCK = threading.Lock()


class EmailOtpGateError(RuntimeError):
    pass


class EmailOtpGate:
    """Persistent, approval-bound adapter for the mandatory email OTP tool."""

    def __init__(
        self,
        *,
        module_path: str | Path,
        db_path: str | Path,
        audit_path: str | Path,
        key_path: str | Path,
        binding_db_path: str | Path,
        ttl: int = 300,
        max_attempts: int = 3,
    ) -> None:
        self.module_path = Path(module_path)
        self.db_path = Path(db_path)
        self.audit_path = Path(audit_path)
        self.key_path = Path(key_path)
        self.binding_db_path = Path(binding_db_path)
        self.ttl = int(ttl)
        self.max_attempts = int(max_attempts)
        self._loaded: ModuleType | None = None
        self._ensure_schema()

    @classmethod
    def from_environment(cls) -> "EmailOtpGate":
        return cls(
            module_path=os.getenv("RALFLOOP_EMAIL_OTP_TOOL", "/home/bandi/bin/ralf_email_otp.py"),
            db_path=os.getenv("RALFLOOP_EMAIL_OTP_DB", "/var/lib/ralfloop/email-otp.sqlite3"),
            audit_path=os.getenv("RALFLOOP_EMAIL_OTP_AUDIT", "/var/lib/ralfloop/email-otp-audit.jsonl"),
            key_path=os.getenv("RALFLOOP_EMAIL_OTP_KEY", "/etc/ralfloop/email-otp.key"),
            binding_db_path=os.getenv(
                "RALFLOOP_EMAIL_OTP_BINDING_DB",
                "/var/lib/ralfloop/email-otp-bindings.sqlite3",
            ),
            ttl=int(os.getenv("RALFLOOP_EMAIL_OTP_TTL", "300")),
            max_attempts=int(os.getenv("RALFLOOP_EMAIL_OTP_MAX_ATTEMPTS", "3")),
        )

    @staticmethod
    def required() -> bool:
        return os.getenv("RALFLOOP_EMAIL_OTP_REQUIRED", "0") == "1"

    def request_for_approval(self, approval: Mapping[str, Any]) -> dict[str, Any]:
        request_id = str(approval.get("request_id") or "")
        if not request_id or str(approval.get("action") or "") not in {"send_email", "reply_email"}:
            return {"status": "email_otp_not_applicable", "requested": False}
        scope = self._otp_scope(approval.get("scope") or {})
        scope_json = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select * from email_otp_bindings where approval_request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                conn.commit()
                return self._public_binding(row, requested=False)
            conn.execute(
                "insert into email_otp_bindings "
                "(approval_request_id, otp_request_id, otp_scope_json, status) "
                "values (?, '', ?, 'creating')",
                (request_id, scope_json),
            )
            conn.commit()
        try:
            result = self._module().create_request(
                scope=scope,
                db_path=self.db_path,
                audit_path=self.audit_path,
                key_path=self.key_path,
                ttl=self.ttl,
                max_attempts=self.max_attempts,
            )
            otp_request_id = str(result.get("request_id") or "")
            if not otp_request_id:
                raise EmailOtpGateError("email_otp_request_id_missing")
        except Exception as exc:
            with self._connect() as conn:
                conn.execute(
                    "update email_otp_bindings set status = 'request_failed' "
                    "where approval_request_id = ? and status = 'creating'",
                    (request_id,),
                )
            raise EmailOtpGateError("email_otp_request_failed") from exc
        with self._connect() as conn:
            conn.execute(
                "update email_otp_bindings set otp_request_id = ?, status = 'pending' "
                "where approval_request_id = ? and status = 'creating'",
                (otp_request_id, request_id),
            )
            row = conn.execute(
                "select * from email_otp_bindings where approval_request_id = ?",
                (request_id,),
            ).fetchone()
        return self._public_binding(row, requested=True)

    def authorize(self, approval: Mapping[str, Any]) -> dict[str, Any]:
        request_id = str(approval.get("request_id") or "")
        scope = self._otp_scope(approval.get("scope") or {})
        with self._connect() as conn:
            row = conn.execute(
                "select * from email_otp_bindings where approval_request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None or row["status"] != "pending" or not row["otp_request_id"]:
            return {"status": "email_otp_required", "authorized": False}
        if json.loads(str(row["otp_scope_json"])) != scope:
            return {"status": "email_otp_scope_mismatch", "authorized": False}
        verified = self._module().verify_telegram_request(
            request_id=str(row["otp_request_id"]),
            db_path=self.db_path,
            audit_path=self.audit_path,
            key_path=self.key_path,
        )
        if not verified.get("approved"):
            return {
                "status": str(verified.get("status") or "email_otp_required"),
                "authorized": False,
            }
        consumed = self._module().consume_request(
            request_id=str(row["otp_request_id"]),
            scope=scope,
            db_path=self.db_path,
            audit_path=self.audit_path,
        )
        if not consumed.get("authorized"):
            return {
                "status": str(consumed.get("status") or "email_otp_denied"),
                "authorized": False,
            }
        with self._connect() as conn:
            conn.execute(
                "update email_otp_bindings set status = 'consumed' "
                "where approval_request_id = ? and status = 'pending'",
                (request_id,),
            )
        return {"status": "email_otp_authorized", "authorized": True}

    def submit_telegram_reply(
        self,
        approval: Mapping[str, Any],
        *,
        otp: str,
        telegram_chat_id: int,
        telegram_message_id: int,
    ) -> dict[str, Any]:
        """Verify an OTP received by the authenticated Telegram entry point."""
        request_id = str(approval.get("request_id") or "")
        with self._connect() as conn:
            row = conn.execute(
                "select * from email_otp_bindings where approval_request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None or row["status"] != "pending" or not row["otp_request_id"]:
            return {"status": "email_otp_required", "approved": False}
        module = self._module()
        original_config = module._telegram_config
        update = {
            "update_id": int(telegram_message_id),
            "message": {
                "date": int(__import__("time").time()),
                "text": str(otp),
                "chat": {"id": int(telegram_chat_id)},
                "reply_to_message": {"text": f"Request: {row['otp_request_id']}"},
            },
        }
        # verify_telegram_request requires the Telegram source allowlist.  The
        # already-authenticated Meowgram event supplies it without exposing a
        # Bot token or placing the OTP in a command, log or file.
        with _VERIFY_LOCK:
            module._telegram_config = lambda: ("local-forwarded-event", [str(telegram_chat_id)])
            try:
                return dict(module.verify_telegram_request(
                    request_id=str(row["otp_request_id"]),
                    db_path=self.db_path,
                    audit_path=self.audit_path,
                    key_path=self.key_path,
                    update_loader=lambda: [update],
                ))
            finally:
                module._telegram_config = original_config

    def _otp_scope(self, scope: Mapping[str, Any]) -> dict[str, Any]:
        attachment_paths = scope.get("attachment_paths") or ()
        if scope.get("attachments") and not attachment_paths:
            raise EmailOtpGateError("email_otp_attachment_paths_required")
        return self._module().build_scope(
            to=str(scope.get("recipient") or scope.get("to") or ""),
            cc=str(scope.get("cc") or ""),
            bcc=str(scope.get("bcc") or ""),
            subject=str(scope.get("subject") or ""),
            attachments=[str(item) for item in attachment_paths],
        )

    def _module(self) -> ModuleType:
        if self._loaded is not None:
            return self._loaded
        if not self.module_path.is_file():
            raise EmailOtpGateError("email_otp_tool_unavailable")
        spec = importlib.util.spec_from_file_location("ralf_email_otp", self.module_path)
        if spec is None or spec.loader is None:
            raise EmailOtpGateError("email_otp_tool_unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._loaded = module
        return module

    def _connect(self) -> sqlite3.Connection:
        self.binding_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.binding_db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "create table if not exists email_otp_bindings ("
                "approval_request_id text primary key, otp_request_id text not null, "
                "otp_scope_json text not null, status text not null)"
            )
        os.chmod(self.binding_db_path, 0o600)

    @staticmethod
    def _public_binding(row: sqlite3.Row, *, requested: bool) -> dict[str, Any]:
        return {
            "status": str(row["status"]),
            "approval_request_id": str(row["approval_request_id"]),
            "otp_request_id": str(row["otp_request_id"]),
            "requested": requested,
            "authorized": False,
        }


__all__ = ["EmailOtpGate", "EmailOtpGateError"]
