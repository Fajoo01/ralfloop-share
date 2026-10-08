#!/usr/bin/env python3
"""Apply reviewed draft-only handoff to a copied release; never edit live source."""
from pathlib import Path
import sys
dst=Path(sys.argv[1])
repo=Path(__file__).parent
app=dst/"openshell_backend/assistant_v1_api.py"
src=repo/"openshell_backend/assistant_v1_api.py"
source=src.read_text()
start=source.index("    if route is not None:\n        # Explicitly authored")
end=source.index("        try:\n            result = unified_runner(",start)
inject=source[start:end]
original=app.read_text()
anchor="    if route is not None:\n        try:\n            result = unified_runner("
assert original.count(anchor)==1, original.count(anchor)
patched=original.replace(anchor,inject+"        try:\n            result = unified_runner(",1)
app.chmod(0o644)
app.write_text(patched)
app.chmod(0o444)
target=dst/"openshell_backend/manual_email_draft.py"
target.write_bytes((repo/"openshell_backend/manual_email_draft.py").read_bytes())
target.chmod(0o444)
print("DRAFT_ONLY_RELEASE_PATCH_OK")
