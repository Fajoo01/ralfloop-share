#!/usr/bin/env python3
"""Create Gmail DRAFTS through the production Bot-tazzi backend only. Never sends."""
import hashlib
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path("/home/bandi/.local/share/google-workspace-mcp/workspace")
CONFIG = [
    ("PiGreco", "pigreco@luogoideale.org", "PerRifArci – scuse e Allegato A2 corretto"),
    ("Amici_Parco_Trotter", "amicitrotter@gmail.com", "PerRifArci – scuse e Allegato A2 corretto per Amici del Trotter"),
    ("AMEDI", "info@amedi.it", "PerRifArci – scuse e Allegato A2 corretto per AMEDI"),
]
RESULTS = ROOT / "perrifarci-draft-receipts-20261008.json"
known = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
only = set(sys.argv[1:]) or {name for name, *_ in CONFIG}
for name, email, subject in CONFIG:
    if name not in only:
        continue
    if known.get(name, {}).get("draft_id"):
        print(json.dumps({"name":name,"state":"already_recorded","draft_id":known[name]["draft_id"]}))
        continue
    body = (ROOT / "docs/perrifarci/email_drafts" / (name + ".txt")).read_text(encoding="utf-8").strip()
    attachments = []
    for ext in ("docx","pdf"):
        filename = "A2_ARCI_07ott2026_" + name + "_DA_FIRMARE." + ext
        path = WORKSPACE / filename
        if not path.is_file(): raise FileNotFoundError(path)
        attachments.append({"path":str(path),"name":filename,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
    session = "draft-perrifarci-arci-20261008-" + name.lower().replace("_","-")
    message = (f"Prepara una email di rettifica a {email}. Destinatario verificato: {email}. "
               "Il nuovo Allegato A2 Educativa per PerRifArci prevede cofinanziamento € 0,00. "
               "Riconosci esplicitamente i nostri errori precedenti. Solo BOZZA GMAIL: non inviare.")
    payload = {
        "message":message,
        "session_id":session,
        "mode":"auto",
        "allow_tools":True,
        "context":{
            "email_exact_body":body,
            "email_exact_subject":subject,
            "email_attachments":attachments,
            "email_manual_draft_only":True
        },
    }
    request=Request("http://127.0.0.1:19090/assistant/v1/chat",
        data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type":"application/json"})
    with urlopen(request,timeout=90) as reply:
        result=json.load(reply)
    metadata=result.get("metadata") or {}
    entry={"recipient":email,"subject":subject,"route":result.get("route"),"status":metadata.get("status"),
           "draft_id":metadata.get("gmail_draft_id"),"attachments":[x["name"] for x in attachments],
           "raw_sha256":metadata.get("gmail_raw_sha256"),"sent":False}
    if result.get("route") != "unified" or not entry["draft_id"]:
        print(json.dumps({"error":"draft_not_confirmed","name":name,"response":result},ensure_ascii=False)[:3500],flush=True)
        raise SystemExit(2)
    known[name]=entry
    RESULTS.write_text(json.dumps(known,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps({"created":name,**entry},ensure_ascii=False),flush=True)
