"""Single verified scanner PDF, using the existing backend-only Gmail draft path."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, UnixMCPTransport
from ralfloop_agent.unified_assistant.email import EmailWorkingMemoryBuilder
from ralfloop_agent.unified_assistant.email_pipeline import GenericEmailPipeline
from ralfloop_agent.unified_assistant.memory import MemoryRouter
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade
from .photo_email_draft import DraftPreparationError

WORKSPACE = Path("/home/bandi/.local/share/google-workspace-mcp/workspace")
STATE = Path.home()/".local/state/bottazzi/scanner-gmail-drafts"
FILENAME = re.compile(r"Scansione_Epson_[0-9]{4}-[0-9]{2}-[0-9]{2}(?:_[A-Za-z0-9_-]{1,40})?\.pdf")
HEX = re.compile(r"[0-9a-f]{64}")

def validate_attachment(message, context):
    rows=context.get("email_attachments")
    if not isinstance(rows,(list,tuple)) or len(rows)!=1 or not isinstance(rows[0],Mapping):
        raise DraftPreparationError("scanner_pdf_count_invalid")
    item=rows[0]
    name=str(item.get("name") or "")
    sha=str(item.get("sha256") or "")
    path=Path(str(item.get("path") or ""))
    if not FILENAME.fullmatch(name) or not HEX.fullmatch(sha):
        raise DraftPreparationError("scanner_pdf_metadata_invalid")
    if path != WORKSPACE/name or path.is_symlink() or not path.is_file():
        raise DraftPreparationError("scanner_pdf_outside_workspace")
    if not 5<=path.stat().st_size<=9_000_000:
        raise DraftPreparationError("scanner_pdf_size_invalid")
    data=path.read_bytes()
    if not data.startswith(b"%PDF-") or hashlib.sha256(data).hexdigest()!=sha:
        raise DraftPreparationError("scanner_pdf_hash_or_signature_invalid")
    if message.count("PDF verificato: "+name)!=1 or message.count("SHA256 PDF: "+sha)!=1:
        raise DraftPreparationError("scanner_pdf_evidence_missing")
    info=subprocess.run(["/usr/bin/pdfinfo",str(path)],capture_output=True,text=True,timeout=10,check=True)
    pages=re.search(r"^Pages:\s+(\d+)\s*$",info.stdout,re.M)
    if not pages or not 1<=int(pages[1])<=20:
        raise DraftPreparationError("scanner_pdf_pages_invalid")
    return {"path":str(path),"name":name,"sha256":sha},int(pages[1])

def provider_read(operation, **args):
    socket=os.getenv("RALF_GOOGLE_WORKSPACE_MCP_SOCKET","/run/ralf-google-workspace-mcp/mcp.sock")
    account=os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT","fabio@tiremminnanz.com")
    with MCPClientSession(UnixMCPTransport(socket),timeout=30,client_name="scanner-draft-verify") as c:
        c.list_tools()
        r=c.call_tool("manage_email",{"operation":operation,"email":account,**args})
    if r.get("isError"):
        raise DraftPreparationError("scanner_gmail_read_failed")
    return "\n".join(x.get("text","") for x in r.get("content",[]) if x.get("type")=="text")

def store(path, value):
    with tempfile.NamedTemporaryFile(mode="w",dir=path.parent,delete=False) as f:
        json.dump(value,f)
        f.flush()
        os.fsync(f.fileno())
        tmp=f.name
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def create_scanner_draft(message,context,recipient,sync_factory):
    if context.get("email_manual_draft_only") is not True or context.get("source")!="ralf_terminal":
        raise DraftPreparationError("scanner_draft_only_required")
    if any(context.get(x) for x in ("email_existing_draft_id","email_exact_cc","email_exact_bcc",
          "email_reply_thread_id","email_support_bundle","email_pigreco_p7m_update",
          "email_accogliersi_docs","email_membership_logo_update","email_photo_draft")):
        raise DraftPreparationError("scanner_draft_conflicting_workflow")
    subject=str(context.get("email_exact_subject") or "").strip()
    body=str(context.get("email_exact_body") or "").strip()
    if not (0<len(subject)<=200 and 0<len(body)<=4000) or any(c in subject for c in '\r\n"'):
        raise DraftPreparationError("scanner_draft_shape_invalid")
    item,pages=validate_attachment(message,context)
    working=EmailWorkingMemoryBuilder(MemoryRouter(),UnifiedRegistryFacade().domain("email")).build(
        objective=message,recipient=recipient,subject=subject)
    checked=GenericEmailPipeline().validate_exact(working,body)
    if checked.hard_guard!="passed" or checked.final_validator!="passed":
        raise DraftPreparationError("body_validation_failed:"+checked.hard_guard)
    key=hashlib.sha256(json.dumps([os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT","fabio@tiremminnanz.com"),
         recipient,subject,body,item["name"],item["sha256"]],ensure_ascii=False).encode()).hexdigest()
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    if STATE.is_symlink() or STATE.stat().st_uid!=os.getuid() or STATE.stat().st_mode&0o077:
        raise DraftPreparationError("scanner_draft_state_unsafe")
    with (STATE/(key+".lock")).open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        receipt=STATE/(key+".json")
        if receipt.exists():
            previous=json.loads(receipt.read_text())
            if previous.get("status")!="draft_only":
                raise DraftPreparationError("scanner_draft_outcome_uncertain_reconcile")
            drafts=provider_read("listDrafts",maxResults=50)
            if not re.search(r"(?<![A-Za-z0-9-])"+re.escape(previous["gmail_draft_id"])+r"(?![A-Za-z0-9-])",drafts):
                raise DraftPreparationError("scanner_draft_not_found_reconcile")
            return {**previous,"writes":0,"reused":True}
        query=f'in:drafts to:{recipient} subject:"{subject}" filename:{item["name"]}'
        if not provider_read("search",query=query,maxResults=50).startswith("No messages found for query:"):
            raise DraftPreparationError("scanner_draft_existing_or_search_uncertain")
        # Never retry an unacknowledged create; the durable marker survives restart.
        store(receipt,{"status":"pending_provider_outcome"})
        r=sync_factory().upsert(to=recipient,subject=subject,body=body,attachments=(item,))
        if r.get("sent") is not False or not r.get("draftId"):
            raise DraftPreparationError("scanner_draft_not_confirmed")
        result={"status":"draft_only","gmail_draft_id":str(r["draftId"]),
                "gmail_raw_sha256":str(r.get("rawSha256") or ""),
                "recipient":recipient,"subject":subject,"attachment_names":[item["name"]],
                "attachment_sha256":item["sha256"],"pages":pages,
                "send_calls":0,"writes":1,"sent":False}
        store(receipt,result)
        return result
