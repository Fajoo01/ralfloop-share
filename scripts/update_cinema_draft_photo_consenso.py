#!/usr/bin/env python3
"""Update ONLY the verified existing Gmail DRAFT with two minimal cinema PDFs; never send."""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, '/home/sibilla-cumana/ralfloop-production/current')
from ralfloop_agent.unified_assistant.email_search import GoogleWorkspaceReadContext
from ralfloop_agent.unified_assistant.gmail_draft_sync import GoogleWorkspaceGmailDraftSync

DRAFT_ID = 'r-7136097804289011347'
MESSAGE_ID = '1a12c85e7259d687'
THREAD_ID = '1a127677cbfd26d7'
RECIPIENT = 'acagnoprod@gmail.com'
SUBJECT = 'Re: Corso di cinema al Trotter – IBAN e quota di iscrizione'
ROOT = Path('/home/bandi/.local/share/google-workspace-mcp/workspace')
FILES = ('01_Iscrizione_corso_cinema_Trotter.pdf', '02_Domanda_ammissione_Tiremm_ARCI_minori.pdf')
BODY = '''Ciao Andrea,

come seguito alla mail con le coordinate bancarie, ti mando solo i due moduli essenziali per il corso di cinema al Parco Trotter:

1. Iscrizione al corso, sul modello del doposcuola. Nella seconda pagina ci sono le scelte facoltative e separate per immagini, riprese e pubblicazione: chi non dà il consenso può comunque partecipare.
2. Domanda di ammissione a Tiremm Innanz APS per la tessera ARCI, che resta distinta dall'iscrizione al corso.

Puoi farli avere alle famiglie interessate e raccogliere le copie compilate e firmate.

Grazie, Fabio
Tiremm Innanz APS'''

with GoogleWorkspaceReadContext('/run/ralf-google-workspace-mcp/mcp.sock', 'fabio@tiremminnanz.com', 22) as gateway:
    current = gateway.invoke('read', messageId=MESSAGE_ID).get('message') or {}
    assert current.get('to') == RECIPIENT, 'wrong recipient'
    assert current.get('subject') == SUBJECT, 'wrong thread subject'
    assert 'DRAFT' in str(current.get('labels')), 'draft no longer editable'
    assert all(name in str(current.get('body')) for name in FILES), 'attachments not as expected'
attachments = []
for name in FILES:
    p = ROOT / name
    blob = p.read_bytes()
    assert blob.startswith(b'%PDF-')
    attachments.append({'path': str(p), 'name': name, 'sha256': hashlib.sha256(blob).hexdigest()})
result = GoogleWorkspaceGmailDraftSync.from_environment().upsert(
    to=RECIPIENT, subject=SUBJECT, body=BODY,
    draft_id=DRAFT_ID, thread_id=THREAD_ID, attachments=tuple(attachments))
assert result['draftId'] == DRAFT_ID and result['sent'] is False
print({'status': 'DRAFT_UPDATED_ONLY', 'draft_id': DRAFT_ID,
       'files': FILES, 'hashes': [x['sha256'] for x in attachments], 'sent': False})
