"""Verified single-photo Gmail draft creation/update; never sends messages.

Preserves the protected Gmail broker/UID boundary and the existing exact-body
domain guard. Draft update requires a previously observed draft identifier and
raw MIME digest, both repeated in the operator's instruction.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from ralfloop_agent.unified_assistant.email import EmailWorkingMemoryBuilder
from ralfloop_agent.unified_assistant.email_pipeline import GenericEmailPipeline
from ralfloop_agent.unified_assistant.memory import MemoryRouter
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade

WORKSPACE = Path("/home/bandi/.local/share/google-workspace-mcp/workspace")
ADDRESS = re.compile(r"[a-zA-Z0-9.!#$%&'*+/=?^`{|}~-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
HEX = re.compile(r"[0-9a-f]{64}")
DRAFT_ID = re.compile(r"r-[0-9]{8,24}")

class DraftPreparationError(ValueError):
    """No write was attempted because an evidence guard failed."""


def _photo_data(context: Mapping[str, Any], message: str) -> dict[str, str]:
    items = context.get("email_attachments")
    if not isinstance(items, (list, tuple)) or len(items) != 1:
        raise DraftPreparationError("photo_count_invalid")
    item = items[0]
    if not isinstance(item, Mapping):
        raise DraftPreparationError("photo_metadata_invalid")
    name, digest = str(item.get("name") or ""), str(item.get("sha256") or "").lower()
    path = Path(str(item.get("path") or ""))
    if path.parent != WORKSPACE or path.name != name or path.is_symlink():
        raise DraftPreparationError("photo_outside_workspace")
    if not HEX.fullmatch(digest) or not path.is_file():
        raise DraftPreparationError("photo_missing_or_hash_invalid")
    if path.stat().st_size > 9_000_000 or path.stat().st_size < 12:
        raise DraftPreparationError("photo_size_invalid")
    data = path.read_bytes()
    kinds = {".jpg": data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"),
             ".jpeg": data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"),
             ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
             ".webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP"}
    if not kinds.get(path.suffix.lower(), False):
        raise DraftPreparationError("photo_mime_invalid")
    if hashlib.sha256(data).hexdigest() != digest:
        raise DraftPreparationError("photo_hash_mismatch")
    if message.count("Foto verificata: " + name) != 1:
        raise DraftPreparationError("photo_evidence_not_in_request")
    return {"path": str(path), "name": name, "sha256": digest}


def create_photo_draft(
    message: str, context: Mapping[str, Any], recipient: str,
    sync_factory: Callable[[], Any],
) -> dict[str, Any]:
    if context.get("email_manual_draft_only") is not True:
        raise DraftPreparationError("manual_draft_only_required")
    if context.get("source") != "ralf_terminal":
        raise DraftPreparationError("terminal_source_required")
    if any(context.get(x) for x in (
        "email_support_bundle", "email_pigreco_p7m_update",
        "email_accogliersi_docs", "email_membership_logo_update",
        "email_reply_thread_id",
    )):
        raise DraftPreparationError("photo_conflicting_workflow")
    subject = str(context.get("email_exact_subject") or "").strip()
    body = str(context.get("email_exact_body") or "").strip()
    if not (0 < len(subject) <= 200 and 0 < len(body) <= 4000):
        raise DraftPreparationError("draft_shape_invalid")
    cc = str(context.get("email_exact_cc") or "").lower().strip()
    if cc:
        addresses = cc.split(",")
        if len(addresses) > 4 or any(not ADDRESS.fullmatch(x) for x in addresses):
            raise DraftPreparationError("photo_cc_invalid")
        if message.casefold().count("cc verificata: " + cc) != 1:
            raise DraftPreparationError("photo_cc_not_verified")
    image = _photo_data(context, message)
    draft_id = str(context.get("email_existing_draft_id") or "").strip()
    previous_hash = str(context.get("email_existing_draft_raw_sha256") or "").lower().strip()
    if bool(draft_id) != bool(previous_hash):
        raise DraftPreparationError("photo_update_proof_incomplete")
    if draft_id:
        if not DRAFT_ID.fullmatch(draft_id) or not HEX.fullmatch(previous_hash):
            raise DraftPreparationError("photo_update_proof_invalid")
        if (message.count("Bozza verificata: " + draft_id) != 1
                or message.count("SHA256 MIME precedente: " + previous_hash) != 1):
            raise DraftPreparationError("photo_update_proof_not_in_request")
    working = EmailWorkingMemoryBuilder(MemoryRouter(), UnifiedRegistryFacade().domain("email")).build(
        objective=message, recipient=recipient, subject=subject,
    )
    checked = GenericEmailPipeline().validate_exact(working, body)
    if checked.hard_guard != "passed" or checked.final_validator != "passed":
        raise DraftPreparationError("body_validation_failed:" + checked.hard_guard)
    result = sync_factory().upsert(
        to=recipient, cc=cc, subject=subject, body=body, draft_id=draft_id,
        attachments=(image,),
    )
    if result.get("sent") is not False or not result.get("draftId"):
        raise DraftPreparationError("draft_not_confirmed")
    if draft_id and str(result["draftId"]) != draft_id:
        raise DraftPreparationError("draft_update_id_changed")
    return {
        "status": "draft_only", "gmail_draft_id": str(result["draftId"]),
        "gmail_raw_sha256": str(result.get("rawSha256") or ""),
        "recipient": recipient, "cc": cc, "subject": subject,
        "attachment_names": [image["name"]],
        "send_calls": 0, "writes": 1, "sent": False,
        "updated_existing": bool(draft_id),
    }
