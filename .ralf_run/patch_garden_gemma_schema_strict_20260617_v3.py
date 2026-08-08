#!/usr/bin/env python3
from pathlib import Path
import shutil
import datetime
import subprocess
import sys

DETECTOR = Path("/opt/bottazzi-garden/garden_person_detector.py")
DROPIN = Path("/etc/systemd/system/bottazzi-garden-detector.service.d/95-gemma-schema-strict.conf")
MARKER = "GARDEN_GEMMA_SCHEMA_STRICT_PATCH_20260617"

stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
bak = DETECTOR.with_name(f"garden_person_detector.py.bak_gemma_schema_strict_v3_{stamp}")
shutil.copy2(DETECTOR, bak)

s = DETECTOR.read_text(encoding="utf-8", errors="replace")

if MARKER not in s:
    old = '''        r = requests.post(
            OLLAMA_URL,
            json={
                "model": VISION_MODEL,
                "prompt": prompt,
                "images": [b64],
                "stream": False,
                "format": "json",
                "keep_alive": os.environ.get("BOTTAZZI_GARDEN_VISION_KEEP_ALIVE", "0s"),
                "options": {"temperature": 0, "num_predict": 120},
            },
            timeout=35,
        )
'''

    new = '''        # GARDEN_GEMMA_SCHEMA_STRICT_PATCH_20260617
        vision_json_schema = {
            "type": "object",
            "properties": {
                "human": {"type": "boolean"},
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["human", "confidence", "reason"],
            "additionalProperties": False,
        }

        r = requests.post(
            OLLAMA_URL,
            json={
                "model": VISION_MODEL,
                "prompt": prompt,
                "images": [b64],
                "stream": False,
                "format": vision_json_schema if os.environ.get("BOTTAZZI_GARDEN_VISION_JSON_SCHEMA", "1") != "0" else "json",
                "keep_alive": os.environ.get("BOTTAZZI_GARDEN_VISION_KEEP_ALIVE", "0s"),
                "options": {"temperature": 0, "num_predict": 120},
            },
            timeout=float(os.environ.get("BOTTAZZI_GARDEN_VISION_TIMEOUT_SECONDS", "70")),
        )
'''

    if old not in s:
        print("ERRORE: blocco requests.post Gemma non trovato")
        print("backup:", bak)
        sys.exit(1)

    s = s.replace(old, new, 1)

    DETECTOR.write_text(s, encoding="utf-8")

    rc = subprocess.run(["python3", "-m", "py_compile", str(DETECTOR)]).returncode
    if rc != 0:
        shutil.copy2(bak, DETECTOR)
        print("ERRORE py_compile, ripristinato:", bak)
        sys.exit(2)

DROPIN.parent.mkdir(parents=True, exist_ok=True)
DROPIN.write_text("""[Service]
Environment=BOTTAZZI_GARDEN_VISION_JSON_SCHEMA=1
Environment=BOTTAZZI_GARDEN_VISION_TIMEOUT_SECONDS=70
Environment=BOTTAZZI_GARDEN_VISION_KEEP_ALIVE=0s
Environment=BOTTAZZI_GARDEN_VISION_MODEL=gemma4:12b-it-qat
""", encoding="utf-8")

subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "restart", "bottazzi-garden-detector.service"], check=True)

print("OK:", MARKER)
print("backup:", bak)
print("dropin:", DROPIN)
