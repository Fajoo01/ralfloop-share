"""Draft-only, deterministic Bot-tazzi Gmail handoff.

For explicitly user-authored email text. Still validates with existing domain
guards, verifies local document digests and uses only the UID-protected Gmail
DRAFT sync tool. There is deliberately no send/approve capability.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from ralfloop_agent.unified_assistant.email import EmailWorkingMemoryBuilder
from ralfloop_agent.unified_assistant.email_pipeline import GenericEmailPipeline
from ralfloop_agent.unified_assistant.gmail_draft_sync import GoogleWorkspaceGmailDraftSync
from ralfloop_agent.unified_assistant.memory import MemoryRouter
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade

ADDRESS = re.compile(r"Destinatario verificato:\s*([a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", re.I)
WORKSPACE = Path("/home/bandi/.local/share/google-workspace-mcp/workspace")


class DraftPreparationError(ValueError):
    pass


def create_manual_draft(
    message: str, context: Mapping[str, Any], *,
    sync_factory: Callable[[], Any] = GoogleWorkspaceGmailDraftSync.from_environment,
) -> dict[str, Any]:
    if context.get("email_manual_draft_only") is not True:
        raise DraftPreparationError("manual_draft_only_required")
    if str(context.get("source") or "") != "ralf_terminal":
        raise DraftPreparationError("terminal_source_required")
    match = ADDRESS.search(message)
    if match is None:
        raise DraftPreparationError("verified_recipient_required")
    recipient = match.group(1).lower()
    if message.count("Destinatario verificato:") != 1:
        raise DraftPreparationError("ambiguous_recipient")
    subject = str(context.get("email_exact_subject") or "").strip()
    body = str(context.get("email_exact_body") or "").strip()
    attachments = context.get("email_attachments")
    support_bundle = context.get("email_support_bundle") is True
    cc = str(context.get("email_exact_cc") or "").strip().lower()
    thread_id = str(context.get("email_reply_thread_id") or "").strip()
    if thread_id:
        if recipient != "amicitrotter@gmail.com" or thread_id != "1a11b26d538e3ca8":
            raise DraftPreparationError("reply_thread_unverified")
        if message.count("Thread verificato: " + thread_id) != 1:
            raise DraftPreparationError("reply_thread_not_in_objective")
        if subject != "Re: PerRifArci – scuse e Allegato A2 corretto per Amici del Trotter":
            raise DraftPreparationError("reply_subject_mismatch")
    if support_bundle:
        if recipient != "irene.conca@arcimilano.it" or cc != "franchini.giulio@gmail.com":
            raise DraftPreparationError("arci_recipients_unverified")
        if message.count("Cc verificata: " + cc) != 1:
            raise DraftPreparationError("arci_cc_unverified")
    elif cc:
        raise DraftPreparationError("cc_requires_explicit_bundle")
    if not (0 < len(subject) <= 200 and 0 < len(body) <= 4000):
        raise DraftPreparationError("draft_shape_invalid")
    if not isinstance(attachments, (list, tuple)) or (len(attachments) != 4 if support_bundle else len(attachments) != 2):
        raise DraftPreparationError("attachment_count_invalid")
    normalized = []
    for item in attachments:
        if not isinstance(item, Mapping):
            raise DraftPreparationError("attachment_invalid")
        path = Path(str(item.get("path") or ""))
        name = str(item.get("name") or "")
        sha = str(item.get("sha256") or "").lower()
        allowed_suffixes = (".docx", ".pdf", ".jpg") if support_bundle else (".docx", ".pdf")
        if path.parent != WORKSPACE or path.name != name or path.suffix.lower() not in allowed_suffixes:
            raise DraftPreparationError("attachment_outside_workspace")
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or not path.is_file():
            raise DraftPreparationError("attachment_missing_or_hash_invalid")
        if path.stat().st_size > 9_000_000 or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise DraftPreparationError("attachment_hash_mismatch")
        normalized.append({"path": str(path), "name": name, "sha256": sha})
    if support_bundle:
        expected = {
            "A2_ARCI_07ott2026_PiGreco_signed.pdf",
            "A2_ARCI_07ott2026_AMEDI_FIRMATA.docx",
            "CI Irene Pani fronte.jpg",
            "CI Irene Pani retro.jpg",
        }
        if {x["name"] for x in normalized} != expected:
            raise DraftPreparationError("arci_bundle_files_invalid")
    else:
        if {Path(x["name"]).suffix for x in normalized} != {".docx", ".pdf"}:
            raise DraftPreparationError("word_and_pdf_required")
        if len({Path(x["name"]).stem for x in normalized}) != 1:
            raise DraftPreparationError("attachments_not_matching")

    builder = EmailWorkingMemoryBuilder(MemoryRouter(), UnifiedRegistryFacade().domain("email"))
    working = builder.build(objective=message, recipient=recipient, subject=subject)
    validation = GenericEmailPipeline().validate_exact(working, body)
    if validation.final_validator != "passed" or validation.hard_guard != "passed":
        raise DraftPreparationError("body_validation_failed:" + validation.hard_guard)

    synced = sync_factory().upsert(
        to=recipient, cc=cc, subject=subject, body=body,
        thread_id=thread_id, attachments=tuple(normalized),
    )
    if not synced.get("draftId") or synced.get("sent") is not False:
        raise DraftPreparationError("draft_not_confirmed")
    return {
        "status": "draft_only", "gmail_draft_id": str(synced["draftId"]),
        "gmail_raw_sha256": str(synced.get("rawSha256") or ""),
        "recipient": recipient, "cc": cc, "subject": subject,
        "gmail_thread_id": thread_id,
        "attachment_names": [x["name"] for x in normalized],
        "send_calls": 0, "writes": 1, "sent": False,
    }
