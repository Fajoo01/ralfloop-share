from pathlib import Path
from datetime import datetime
import subprocess
import os
import sys

ROOT = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
RUN = ROOT / ".ralf_run"
OUT = RUN / "RSC_ABC_RAPPORTO_ESTESO_CURRENT.txt"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from openshell_backend.skills import abc_memory

MEM = Path(os.environ.get("ABC_MEMORY_DIR", str(RUN / "abc_memory")))
latest = abc_memory.latest_patch_record(MEM, RUN)
if not latest:
    raise SystemExit("ERRORE: nessuna patch RL_ABC_PATCH*.txt trovata")
patch = Path(str(latest["patch_file"]))
patch_text = patch.read_text(encoding="utf-8", errors="replace")
freshness = abc_memory.freshness(MEM, RUN, patch)

runner = RUN / "run_rsc_clean_abc_current.py"
if not runner.exists():
    raise SystemExit("ERRORE: runner current mancante")

env = os.environ.copy()
env["RSC_ABC_PATCH_FILE"] = str(patch)
env["RL_ABC_PATCH_FILE"] = str(patch)
env["RSC_ABC_MODE"] = "current"

subprocess.run(
    ["python3", str(runner)],
    cwd=str(ROOT),
    env=env,
    check=True,
    timeout=120,
)

body = OUT.read_text(encoding="utf-8", errors="replace") if OUT.exists() else ""
if not body.strip():
    raise SystemExit("ERRORE: helper non ha prodotto RSC_ABC_RAPPORTO_ESTESO_CURRENT.txt")

bad_markers = [
    "RSC_FULL_REPORT_OVERRIDE_20260608",
    "caso abc aggiornato al 2026-06-08",
    "PATCH 2026-06-11 SERA / NOTTE",
    "RL_ABC_PATCH_20260611_NOTTE.txt",
    "RSC_ABC_CLEAN_20260611_NOTTE.md",
]
for marker in bad_markers:
    if marker in body:
        raise SystemExit(f"ERRORE: output contiene ancora hardcode vecchio: {marker}")

header = f"""RSC ABC — RAPPORTO ESTESO TXT
Generato: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Fonte: abc_memory structured freshness, no semantic hardcode
Runner: {runner.name}
Patch dominante: {patch.name}
STALE_INPUT={str(bool(freshness.get("stale_input"))).lower()}
Patch source: {freshness.get("source", "unknown")}
Message id: {freshness.get("message_id", "")}
Payload hash: {freshness.get("payload_hash", "")}
Age hours: {freshness.get("age_hours", "n/d")}

"""

OUT.write_text(header + body.strip() + "\n", encoding="utf-8")
print(OUT)
