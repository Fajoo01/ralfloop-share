#!/usr/bin/env python3
"""Draft-only PiGreco followup in verified Gmail thread; no email sends."""
import hashlib, json
from pathlib import Path
from urllib.request import Request, urlopen
ROOT=Path(__file__).parent
WORKSPACE=Path("/home/bandi/.local/share/google-workspace-mcp/workspace")
RESULT=ROOT/"perrifarci-pigreco-arci-resign-draft-receipt-20261008.json"
if RESULT.is_file():
    previous=json.loads(RESULT.read_text())
    if previous.get("gmail_draft_id"):
        print("EXISTING_DRAFT_ID="+previous["gmail_draft_id"])
        raise SystemExit(0)
body=(ROOT/"docs/perrifarci/email_drafts/PiGreco_reintegrazione_firma_Giulio_20261008.txt").read_text(encoding="utf-8").strip()
recipient="pigreco@luogoideale.org"
subject="Re: PerRifArci – scuse e Allegato A2 corretto"
thread_id="1a11b26a4893e6ea"
attachments=[]
for ext in ("docx","pdf"):
    path=WORKSPACE/("A2_ARCI_07ott2026_PiGreco_DA_FIRMARE."+ext)
    if not path.is_file(): raise FileNotFoundError(path)
    attachments.append({"path":str(path),"name":path.name,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
message=(f"Prepara una email di rettifica a {recipient}. Destinatario verificato: {recipient}. "
         f"Thread verificato: {thread_id}. "
         "Giulio Franchini di ARCI ha chiesto firma autografa più carta identità oppure firma digitale certificata .p7m. "
         "Il nuovo Allegato A2 Educativa per PerRifArci prevede cofinanziamento € 0,00. "
         "Riconosci esplicitamente i nostri errori precedenti. Solo BOZZA GMAIL: non inviare.")
payload={"message":message,"mode":"auto","allow_tools":True,
         "session_id":"perrifarci-pigreco-resign-arci-20261008",
         "context":{"email_exact_body":body,"email_exact_subject":subject,
                    "email_reply_thread_id":thread_id,"email_attachments":attachments,
                    "email_manual_draft_only":True}}
req=Request("http://127.0.0.1:19090/assistant/v1/chat",data=json.dumps(payload,ensure_ascii=False).encode(),
            headers={"Content-Type":"application/json"})
with urlopen(req,timeout=90) as response:
    result=json.load(response)
meta=result.get("metadata") or {}
if (result.get("route")!="unified" or meta.get("sent") is not False or
    meta.get("status")!="draft_only" or not meta.get("gmail_draft_id") or
    meta.get("gmail_thread_id")!=thread_id):
    print(json.dumps({"error":"GMAIL_DRAFT_NOT_CONFIRMED","result":result},ensure_ascii=False)[:1900])
    raise SystemExit(2)
receipt={"recipient":recipient,"subject":subject,"thread_id":thread_id,
         "gmail_draft_id":meta["gmail_draft_id"],
         "attachment_names":meta.get("attachment_names",[]),
         "body_sha256":hashlib.sha256(body.encode()).hexdigest(),
         "raw_sha256":meta.get("gmail_raw_sha256"),"sent":False,"status":"draft_only"}
RESULT.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+"\n")
print(json.dumps(receipt,ensure_ascii=False))
