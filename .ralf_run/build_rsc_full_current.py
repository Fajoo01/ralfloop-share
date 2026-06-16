from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('/home/sibilla-cumana/ralfloop_agent_scaffold')
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from openshell_backend.skills import abc_memory
RUN = ROOT / '.ralf_run'
MEM = Path(os.environ.get('ABC_MEMORY_DIR', str(RUN / 'abc_memory')))
OUT_FULL = RUN / 'RSC_ABC_RAPPORTO_FULL_CURRENT.txt'
OUT_EXT = RUN / 'RSC_ABC_RAPPORTO_ESTESO_CURRENT.txt'
CORR_JSON = RUN / 'RSC_ABC_LLM_CORRECTION_CURRENT.json'
MAX_EXT_CHARS = 200_000
MAX_PATCH_CHARS = 80_000


def read_limited(path: Path, limit: int, missing: list[str]) -> str:
    if not path.exists():
        missing.append(str(path))
        return ''
    with path.open('r', encoding='utf-8', errors='replace') as fh:
        return fh.read(limit + 1)[:limit]


def parse_created_at(text: str) -> float | None:
    m = re.search(r'^CREATED_AT\s*=\s*([^\n\r]+)', text, re.I | re.M)
    if not m:
        return None
    raw = m.group(1).strip().replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return None


def latest_patch() -> Path | None:
    latest = abc_memory.latest_patch_record(MEM, RUN)
    if latest:
        return Path(str(latest['patch_file']))
    return None


def load_correction(warnings: list[str]) -> dict[str, Any]:
    if not CORR_JSON.exists():
        warnings.append(f'WARNING: missing correction JSON: {CORR_JSON}')
        return {}
    try:
        return json.loads(CORR_JSON.read_text(encoding='utf-8', errors='replace'))
    except Exception as exc:
        warnings.append(f'WARNING: cannot parse correction JSON: {exc!r}')
        return {}


def correction_section(corr: dict[str, Any]) -> str:
    if not corr:
        return '## LLM consistency correction\n\nWARNING: correction unavailable. Full report uses extended body only.\n'
    contradictions = corr.get('contradictions_found') or []
    if contradictions:
        contradiction_text = '\n'.join(f'- {item}' for item in contradictions)
    else:
        contradiction_text = '- Nessuna contraddizione registrata.'
    film_alive = str(bool(corr.get('film_hook_alive'))).lower()
    return f'''## LLM consistency correction

Nota: i numeri corretti LLM prevalgono sui numeri grezzi del corpo esteso.

- curve_current: **{corr.get('curve_current', 'n/d')}**
- curve_prudential: **{corr.get('curve_prudential', 'n/d')}**
- third_pressure: **{corr.get('third_pressure', 'n/d')}**
- pressione terzo: **{corr.get('third_pressure', 'n/d')}%**
- film_hook_alive: **{film_alive}**
- source: `{corr.get('source', 'unknown')}`
- dominant_patch: `{corr.get('dominant_patch', 'unknown')}`

### Reading short
{corr.get('reading_short', 'n/d')}

### Operational rule
{corr.get('operational_rule', 'n/d')}

### Contradictions found
{contradiction_text}
'''


def apply_correction_to_body(body: str, corr: dict[str, Any]) -> str:
    if not corr:
        return body
    curve = corr.get('curve_current')
    third = corr.get('third_pressure')
    reading = corr.get('reading_short')
    if curve is not None:
        body = re.sub(r'Curva current:\s*\*\*\d+/100\*\*', f'Curva current: **{curve}/100**', body, count=1)
        body = re.sub(r'Posizione sulla curva:\*\*\s*\*\*\d+/100\*\*', f'Posizione sulla curva:** **{curve}/100**', body, count=1)
        body = re.sub(r'1\. Curva current calcolata:\s*\d+/100\.', f'1. Curva current calcolata: {curve}/100.', body, count=1)
    if third is not None:
        body = re.sub(r'\| pressione terzo \| \*\*\d+%\*\* \|', f'| pressione terzo | **{third}%** |', body, count=1, flags=re.I)
    if reading:
        body = re.sub(r'\*\*delta recente: pressione terzo aumentata nel breve\*\*', f'**{reading}**', body, count=1, flags=re.I)
        body = re.sub(
            r'- Delta recente dominante: la pressione del terzo aumenta nel breve.*',
            '- Delta recente dominante: applicare i vincoli espliciti della patch dominante; non produrre letture opposte al delta corrente senza prova contraria esplicita.',
            body,
            flags=re.I,
        )
    return body


def main() -> None:
    warnings: list[str] = []
    missing: list[str] = []
    corr = load_correction(warnings)
    ext = read_limited(OUT_EXT, MAX_EXT_CHARS, missing)
    patch = latest_patch()
    patch_text = read_limited(patch, MAX_PATCH_CHARS, missing) if patch else ''
    if patch is None:
        warnings.append('WARNING: no RL_ABC_PATCH*.txt found')
    for item in missing:
        warnings.append(f'WARNING: missing input: {item}')
    ext = apply_correction_to_body(ext, corr)
    generated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    fresh = abc_memory.freshness(MEM, RUN, patch) if patch else {'stale_input': True}
    patch_meta = f'{patch.name} mtime_utc={datetime.fromtimestamp(patch.stat().st_mtime, tz=timezone.utc).isoformat()} STALE_INPUT={str(bool(fresh.get("stale_input"))).lower()} source={fresh.get("source", "unknown")} message_id={fresh.get("message_id", "")} payload_hash={fresh.get("payload_hash", "")}' if patch else 'none'
    warning_text = '\n'.join(f'- {w}' for w in warnings) if warnings else '- Nessun warning.'
    full = f'''RSC ABC — FULL CURRENT FAST
Generato: {generated}
Builder: build_rsc_full_current.py fast/read-only
Patch dominante: {patch_meta}

## WARNING / input status
{warning_text}

{correction_section(corr)}

## Corpo esteso corretto

{ext.strip() if ext else '_RSC_ABC_RAPPORTO_ESTESO_CURRENT.txt mancante o vuoto._'}

============================================================
APPENDICE — PATCH DOMINANTE
============================================================

{patch_text.strip() if patch_text else '_Patch dominante non disponibile._'}
'''
    OUT_FULL.write_text(full, encoding='utf-8')
    print(OUT_FULL)


if __name__ == '__main__':
    main()
