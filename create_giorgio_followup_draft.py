#!/usr/bin/env python3
"""Bot-tazzi: draft-only reply on verified Giorgio Trotter Gmail thread; NEVER send."""
import hashlib, json
from pathlib import Path
from urllib.request import Request,urlopen
ROOT=Path(__file__).parent
WORKSPACE=Path("/home/bandi/.local/share/google-workspace-mcp/workspace")
RESULT=ROOT/"perrifarci-giorgio-a2-draft-receipt-20261008.json"
if RESULT.exists() and json.loads(RESULT.read_text()).get("gmail_draft_id"):
    print("EXISTING_DRAFT_ID="+json.loads(RESULT.read_text())["gmail_draft_id"])
    raise SystemExit(0)
body=(ROOT/"docs/perrifarci/email_drafts/Giorgio_Trotter_solleticito_A2_regione_20261008.txt").read_text(encoding="utf-8").strip()
recipient="amicitrotter@gmail.com"
subject="Re: PerRifArci – scuse e Allegato A2 corretto per Amici del Trotter"
thread_id="1a11b26d538e3ca8"
attachments=[]
for suffix in (".docx",".pdf"):
    path=WORKSPACE/("A2_ARCI_07ott2026_Amici_Parco_Trotter_DA_FIRMARE"+suffix)
    if not path.is_file(): raise FileNotFoundError(path)
    attachments.append({"path":str(path),"name":path.name,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
message=(f"Prepara una email di rettifica a {recipient}. Destinatario verificato: {recipient}. "
         f"Thread verificato: {thread_id}. "
         "Il nuovo Allegato A2 Educativa per PerRifArci prevede cofinanziamento € 0,00. "
         "Riconosci esplicitamente i nostri errori precedenti e spiega la burocrazia della Regione Lombardia. "
         "Solo BOZZA GMAIL: non inviare.")
payload={
    "message":message,"mode":"auto","allow_tools":True,
    "session_id":"perrifarci-giorgio-followup-regione-a2-20261008",
    "context":{
        "email_exact_body":body,
        "email_exact_subject":subject,
        "email_reply_thread_id":thread_id,
        "email_attachments":attachments,
        "email_manual_draft_only":True
    }
}
req=Request("http://127.0.0.1:19090/assistant/v1/chat",
            data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type":"application/json"})
with urlopen(req,timeout=90) as response:
    res=json.load(response)
meta=res.get("metadata") or {}
if (res.get("route")!="unified" or meta.get("sent") is not False or
        meta.get("status")!="draft_only" or not meta.get("gmail_draft_id") or
        meta.get("gmail_thread_id") != thread_id):
    print(json.dumps({"error":"DRAFT_NOT_CONFIRMED","result":res},ensure_ascii=False)[:1900])
    raise SystemExit(2)
receipt={"recipient":recipient,"subject":subject,"thread_id":thread_id,
         "gmail_draft_id":meta["gmail_draft_id"],
         "attachment_names":meta.get("attachment_names",[]),
         "body_sha256":hashlib.sha256(body.encode()).hexdigest(),
         "raw_sha256":meta.get("gmail_raw_sha256"),
         "sent":False,"status":"draft_only"}
RESULT.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+"\n")
print(json.dumps(receipt,ensure_ascii=False))
