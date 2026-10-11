"""Draft-only, deterministic Bot-tazzi Gmail handoff.

For explicitly user-authored email text. Still validates with existing domain
guards, verifies local document digests and uses only the UID-protected Gmail
DRAFT sync tool. There is deliberately no send/approve capability.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
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
    p7m_update = context.get("email_pigreco_p7m_update") is True
    accogliersi_docs = context.get("email_accogliersi_docs") is True
    if accogliersi_docs and (support_bundle or p7m_update):
        raise DraftPreparationError("accogliersi_bundle_conflict")
    if p7m_update and not support_bundle:
        raise DraftPreparationError("p7m_bundle_required")
    cc = str(context.get("email_exact_cc") or "").strip().lower()
    thread_id = str(context.get("email_reply_thread_id") or "").strip()
    if thread_id:
        verified_threads = {
            ("amicitrotter@gmail.com", "1a11b26d538e3ca8"):
                "Re: PerRifArci – scuse e Allegato A2 corretto per Amici del Trotter",
            ("pigreco@luogoideale.org", "1a11b26a4893e6ea"):
                "Re: PerRifArci – scuse e Allegato A2 corretto",
            ("franchini.giulio@gmail.com", "1a11c4a0973b970b"):
                "Re: PerRifArci – lettere A2 ricevute e aggiornamento scuole",
        }
        expected_subject = verified_threads.get((recipient, thread_id))
        if expected_subject is None:
            raise DraftPreparationError("reply_thread_unverified")
        if message.count("Thread verificato: " + thread_id) != 1:
            raise DraftPreparationError("reply_thread_not_in_objective")
        if subject != expected_subject:
            raise DraftPreparationError("reply_subject_mismatch")
    if support_bundle:
        if (recipient, cc) != (("franchini.giulio@gmail.com", "irene.conca@arcimilano.it") if p7m_update else ("irene.conca@arcimilano.it", "franchini.giulio@gmail.com")):
            raise DraftPreparationError("arci_recipients_unverified")
        if message.count("Cc verificata: " + cc) != 1:
            raise DraftPreparationError("arci_cc_unverified")
    elif cc:
        raise DraftPreparationError("cc_requires_explicit_bundle")
    if accogliersi_docs:
        if recipient != "associazioneaccogliersi@gmail.com" or subject != "Re: PerRifArci – richiesta di sostegno ad AccoglierSi":
            raise DraftPreparationError("accogliersi_recipient_subject_unverified")
        if "Conferma documenti AccoglierSi 20261009" not in message:
            raise DraftPreparationError("accogliersi_docs_confirmation_missing")
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
        allowed_suffixes = (".docx", ".pdf", ".jpg", ".p7m") if (support_bundle and p7m_update) else ((".docx", ".pdf", ".jpg") if support_bundle else (".docx", ".pdf"))
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
        if p7m_update:
            expected = {
                "A2_ARCI_07ott2026_PiGreco.pdf.p7m",
                "arci 2.pdf",
                "C.I Nuova A Calabria.pdf",
                "C.I Nuova B Calabria.pdf",
            }
        if {x["name"] for x in normalized} != expected:
            raise DraftPreparationError("arci_bundle_files_invalid")
    else:
        # The cinema enrollment and ARCI membership forms are two distinct PDFs.
        # Keep the legacy Word+PDF rule for every other request.
        cinema_pdf_pair = (
            recipient == "acagnoprod@gmail.com"
            and subject == "Corso di cinema al Trotter – moduli di iscrizione e tesseramento Tiremm"
            and not accogliersi_docs
            and {x["name"] for x in normalized} == {
                "01_Iscrizione_corso_cinema_Trotter.pdf",
                "02_Domanda_ammissione_Tiremm_ARCI_minori.pdf",
            }
        )
        if not cinema_pdf_pair and {Path(x["name"]).suffix for x in normalized} != {".docx", ".pdf"}:
            raise DraftPreparationError("word_and_pdf_required")
        if accogliersi_docs:
            expected_hashes = {
                "PerRifArci_Abstract.pdf": "7ae315080bae587fa4c468baf45dadce777406455355bb5c9b282334a30b5955",
                "NOME ENTE Allegato A2 Dichiarazione di adesione alla rete di sostegno - Educativa.docx": "f9cbbf06821ef03b6b028a8c5704b0f846a6dbdc4e2a513632a8f62104239d29",
            }
            if {x["name"] for x in normalized} != set(expected_hashes):
                raise DraftPreparationError("accogliersi_docs_invalid")
            if any(x["sha256"] != expected_hashes[x["name"]] for x in normalized):
                raise DraftPreparationError("accogliersi_docs_hash_mismatch")
        elif not cinema_pdf_pair and len({Path(x["name"]).stem for x in normalized}) != 1:
            raise DraftPreparationError("attachments_not_matching")

    if p7m_update:
        checked_hashes = {
            "A2_ARCI_07ott2026_PiGreco.pdf.p7m": "7a332c1136553509316c50c8d46af0aad6d7baf86640e06120139cbae436a76f",
            "arci 2.pdf": "cf22af26c3fe653b5334f95c5abf5722643cd5bf835355ab466ef2bd49871a85",
            "C.I Nuova A Calabria.pdf": "8d29cc8c5c70b53e39fcb2df427865a970767bee7e6316714af0fb87bd4bb7c6",
            "C.I Nuova B Calabria.pdf": "bc045a91117062fdf72d1bfaaf76405854a0ee4bb335fe9ac203386fe555a2a0",
        }
        for filename, known_good in checked_hashes.items():
            if hashlib.sha256((WORKSPACE / filename).read_bytes()).hexdigest() != known_good:
                raise DraftPreparationError("known_good_attachment_mismatch")
        signed = WORKSPACE / "A2_ARCI_07ott2026_PiGreco.pdf.p7m"
        try:
            subprocess.run(["openssl", "cms", "-verify", "-inform", "DER", "-in", str(signed),
                            "-noverify", "-out", "/dev/null"], capture_output=True, check=True, timeout=15)
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
            raise DraftPreparationError("p7m_invalid") from exc

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


def create_text_only_draft(
    message: str, context: Mapping[str, Any], *,
    sync_factory: Callable[[], Any] = GoogleWorkspaceGmailDraftSync.from_environment,
) -> dict[str, Any]:
    """Save an exact, explicitly requested Gmail draft without attachments or sending."""
    if context.get("email_manual_draft_only") is not True:
        raise DraftPreparationError("manual_draft_only_required")
    if str(context.get("source") or "") != "ralf_terminal":
        raise DraftPreparationError("terminal_source_required")
    match = ADDRESS.search(message)
    if match is None or message.count("Destinatario verificato:") != 1:
        raise DraftPreparationError("verified_recipient_required")
    recipient = match.group(1).lower()
    if context.get("email_attachments") not in (None, (), []):
        raise DraftPreparationError("text_only_attachments_denied")
    if str(context.get("email_reply_thread_id") or "").strip():
        raise DraftPreparationError("text_only_reply_denied")
    subject = str(context.get("email_exact_subject") or "").strip()
    body = str(context.get("email_exact_body") or "").strip()
    if not (0 < len(subject) <= 200 and 0 < len(body) <= 4000):
        raise DraftPreparationError("draft_shape_invalid")
    cc = str(context.get("email_exact_cc") or "").strip().lower()
    if cc:
        addresses = cc.split(",")
        if not (1 <= len(addresses) <= 4) or any(
            re.fullmatch(r"[a-zA-Z0-9.!#$%&'*+/=?^`{|}~-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", address) is None
            for address in addresses
        ):
            raise DraftPreparationError("text_only_cc_invalid")
        if message.casefold().count("cc verificata: " + cc) != 1:
            raise DraftPreparationError("text_only_cc_unverified")
    working = EmailWorkingMemoryBuilder(
        MemoryRouter(), UnifiedRegistryFacade().domain("email")
    ).build(objective=message, recipient=recipient, subject=subject)
    validation = GenericEmailPipeline().validate_exact(working, body)
    if validation.final_validator != "passed" or validation.hard_guard != "passed":
        raise DraftPreparationError("body_validation_failed:" + validation.hard_guard)
    synced = sync_factory().upsert(
        to=recipient, cc=cc, subject=subject, body=body, attachments=(),
    )
    if not synced.get("draftId") or synced.get("sent") is not False:
        raise DraftPreparationError("draft_not_confirmed")
    return {
        "status": "draft_only", "gmail_draft_id": str(synced["draftId"]),
        "gmail_raw_sha256": str(synced.get("rawSha256") or ""),
        "recipient": recipient, "cc": cc, "subject": subject,
        "attachment_names": [], "send_calls": 0, "writes": 1, "sent": False,
    }
