#!/usr/bin/env python3
"""Prepare one Gmail DRAFT to ARCI via Bot-tazzi's backend. No email sends."""
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

root=Path(__file__).parent
workspace=Path("/home/bandi/.local/share/google-workspace-mcp/workspace")
receipt=root/"arci-perrifarci-draft-receipt-20261008.json"
if receipt.exists():
    previous=json.loads(receipt.read_text())
    if previous.get("gmail_draft_id"):
        print("EXISTING_DRAFT_ID="+previous["gmail_draft_id"])
        raise SystemExit(0)
names=[
"A2_ARCI_07ott2026_PiGreco_signed.pdf",
"A2_ARCI_07ott2026_AMEDI_FIRMATA.docx",
"CI Irene Pani fronte.jpg",
"CI Irene Pani retro.jpg",
]
attachments=[]
for name in names:
    p=workspace/name
    if not p.is_file(): raise FileNotFoundError(str(p))
    attachments.append({"path":str(p),"name":p.name,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()})
subject="PerRifArci – lettere A2 ricevute e aggiornamento scuole"
body=(root/"docs/perrifarci/email_drafts/ARCI_Irene_Giulio.txt").read_text(encoding="utf-8").strip()
msg=("Prepara una email a Irene e Giulio per PerRifArci. "
"Destinatario verificato: irene.conca@arcimilano.it. "
"Cc verificata: franchini.giulio@gmail.com. "
"PiGreco ha A2 firmato ma manca documento d'identità. "
"AMEDI ha A2 firmato e carta identità fronte retro. "
"Trotter ha inviato lettera generica non A2, scuole lente. "
"Solo BOZZA GMAIL, non inviare.")
data={
"message":msg,
"session_id":"perrifarci-arci-responsabili-20261008",
"mode":"auto",
"allow_tools":True,
"context":{
"email_manual_draft_only":True,
"email_support_bundle":True,
"email_exact_cc":"franchini.giulio@gmail.com",
"email_exact_subject":subject,
"email_exact_body":body,
"email_attachments":attachments}}
req=Request("http://127.0.0.1:19090/assistant/v1/chat",data=json.dumps(data,ensure_ascii=False).encode(),headers={"Content-Type":"application/json"})
with urlopen(req,timeout=80) as response:
    result=json.load(response)
meta=result.get("metadata") or {}
if result.get("route")!="unified" or meta.get("status")!="draft_only" or meta.get("sent") is not False or not meta.get("gmail_draft_id"):
    raise RuntimeError("GMAIL_DRAFT_NOT_CONFIRMED: "+json.dumps(result,ensure_ascii=False)[:450])
out={"recipient":meta["recipient"],"cc":meta["cc"],"subject":subject,"gmail_draft_id":meta["gmail_draft_id"],"status":"draft_only","sent":False,"attachment_names":meta["attachment_names"],"raw_sha256":meta.get("gmail_raw_sha256"),"body_sha256":hashlib.sha256(body.encode()).hexdigest()}
receipt.write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n")
print(json.dumps(out,ensure_ascii=False))
