#!/usr/bin/env python3
import base64
import json
import time
import urllib.request
from pathlib import Path

snap_dir = Path("/opt/bottazzi-garden/snapshots")
imgs = sorted(
    snap_dir.glob("*.jpg"),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)

if not imgs:
    raise SystemExit("NO_SNAPSHOTS_FOUND")

latest = imgs[0]

prompt = """Return ONLY valid JSON:
{"human": true, "confidence": 0.0, "reason": "..."}

human=true only if a real human or clearly human body part is visible: legs, feet, torso, head, arm, partially occluded person, person at edge.
human=false for video artifacts, vertical smear, corrupted frame, IR glare, insects, rain, shadows, branches, leaves, furniture, Vespa, static objects, compression, ghosting, light edges.
"""

payload = {
    "model": "gemma4:12b-it-qat",
    "prompt": prompt,
    "images": [base64.b64encode(latest.read_bytes()).decode("ascii")],
    "stream": False,
    "format": "json",
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

dt = time.time() - t0
outer = json.loads(raw)
response = outer.get("response", "")

print("IMAGE:", latest)
print("SECONDS:", round(dt, 2))
print("RAW_RESPONSE:", response)

parsed = json.loads(response)
print("JSON_OK: true")
print("PARSED:", json.dumps(parsed, ensure_ascii=False, indent=2))
