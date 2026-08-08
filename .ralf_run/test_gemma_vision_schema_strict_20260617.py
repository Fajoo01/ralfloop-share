#!/usr/bin/env python3
import base64
import json
import time
import urllib.request
from pathlib import Path

imgs = sorted(
    Path("/opt/bottazzi-garden/snapshots").glob("*.jpg"),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)

if not imgs:
    raise SystemExit("NO_SNAPSHOTS_FOUND")

latest = imgs[0]

prompt = """Decide if the image contains a real human.

Return only fields allowed by the schema.

human=true only if a real human or clearly human body part is visible:
legs, feet, torso, head, arm, partially occluded person, person at edge.

human=false for video artifacts, vertical smear, corrupted frame, IR glare,
insects, rain, shadows, branches, leaves, furniture, Vespa, static objects,
compression, ghosting, light edges.
"""

schema = {
    "type": "object",
    "properties": {
        "human": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"}
    },
    "required": ["human", "confidence", "reason"],
    "additionalProperties": False
}

payload = {
    "model": "gemma4:12b-it-qat",
    "prompt": prompt,
    "images": [base64.b64encode(latest.read_bytes()).decode("ascii")],
    "stream": False,
    "format": schema,
    "keep_alive": "0s",
    "options": {
        "temperature": 0,
        "num_predict": 120
    },
}

t0 = time.time()

req = urllib.request.Request(
    "http://127.0.0.1:11434/api/generate",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)

with urllib.request.urlopen(req, timeout=90) as r:
    raw = r.read().decode("utf-8", errors="replace")

outer = json.loads(raw)
response = outer.get("response", "")
parsed = json.loads(response)

print("IMAGE:", latest)
print("SECONDS:", round(time.time() - t0, 2))
print("RAW_RESPONSE:", response)
print("JSON_OK: true")
print("PARSED:", json.dumps(parsed, ensure_ascii=False, indent=2))

missing = {"human", "confidence", "reason"} - set(parsed)
extra = set(parsed) - {"human", "confidence", "reason"}

print("MISSING:", sorted(missing))
print("EXTRA:", sorted(extra))

if missing or extra:
    raise SystemExit("SCHEMA_NOT_STRICT")
