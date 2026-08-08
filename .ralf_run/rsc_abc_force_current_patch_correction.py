from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/home/sibilla-cumana/ralfloop_agent_scaffold')
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openshell_backend.skills import abc_memory

RUN = ROOT / '.ralf_run'
MEM = Path(os.environ.get('ABC_MEMORY_DIR', str(RUN / 'abc_memory')))
CORR = RUN / 'RSC_ABC_LLM_CORRECTION_CURRENT.json'
FULL = RUN / 'RSC_ABC_RAPPORTO_FULL_CURRENT.txt'
ESTESO = RUN / 'RSC_ABC_RAPPORTO_ESTESO_CURRENT.txt'
RAW = RUN / 'RSC_ABC_RAW_CURRENT.md'


def read(path: Path, limit: int = 120_000) -> str:
    return path.read_text(encoding='utf-8', errors='replace')[:limit] if path.exists() else ''


def selected_patch() -> Path | None:
    env_patch = os.environ.get('RSC_ABC_PATCH_FILE') or os.environ.get('RL_ABC_PATCH_FILE')
    if env_patch and Path(env_patch).exists():
        return Path(env_patch)
    latest = abc_memory.latest_patch_record(MEM, RUN)
    return Path(str(latest['patch_file'])) if latest else None


def int_or_default(value, default=0):
    try:
        return int(round(float(value)))
    except Exception:
        return default


def current_state() -> dict:
    abc_memory.ensure_init(MEM)
    state = abc_memory.read_json(MEM / 'state.json', {}) or abc_memory.read_json(MEM / 'model_state.json', {})
    return state or {}


def constraint_safe_reading(patch_text: str, raw_text: str) -> dict:
    constraints = abc_memory.extract_interpretive_constraints(patch_text)
    consistency_probe = abc_memory.report_consistency(raw_text, constraints)
    state = current_state()
    curve = int_or_default(state.get('curve'), 60)
    third = int_or_default(state.get('third_pressure'), 54)
    if any(c.get('key') == 'third_pressure_non_increase' for c in constraints):
        third = min(third, 60)
        reading = 'Delta corrente con vincolo esplicito: non leggere la pressione terzo come aumentata; mantenere lettura prudente e segnalare evidence insufficiente se il corpo grezzo dice il contrario.'
    else:
        reading = 'Delta corrente integrato da memoria ABC: usare patch dominante e distinguere fatti osservati, interpretazioni e ipotesi contrarie.'
    contradictions = list(consistency_probe.get('contradictions') or [])
    return {
        'curve_current': curve,
        'curve_prudential': max(curve - 2, 0),
        'third_pressure': third,
        'film_hook_alive': None,
        'reading_short': reading,
        'operational_rule': state.get('recommended_action', 'do_nothing_active'),
        'constraints': constraints,
        'contradictions_found': contradictions,
        'inconsistent': bool(contradictions),
    }


def main() -> int:
    patch = selected_patch()
    if patch is None:
        freshness = abc_memory.freshness(MEM, RUN, None)
        patch_text = ''
    else:
        patch_text = read(patch)
        freshness = abc_memory.freshness(MEM, RUN, patch)
    raw = read(RAW)
    corr = constraint_safe_reading(patch_text, raw)
    corr.update({
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': 'abc_memory_structured_current_patch',
        'dominant_patch': str(patch) if patch else '',
        'freshness': freshness,
        'STALE_INPUT': bool(freshness.get('stale_input')),
        'payload_hash': freshness.get('payload_hash', ''),
        'message_id': freshness.get('message_id', ''),
    })
    CORR.write_text(json.dumps(corr, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    constraints = corr.get('constraints') or []
    constraint_lines = '\n'.join(f"- {c.get('key')}: {c.get('text')}" for c in constraints) or '- Nessun vincolo esplicito estratto.'
    contradictions = corr.get('contradictions_found') or []
    contradiction_lines = '\n'.join(f'- {x}' for x in contradictions) or '- Nessuna contraddizione forte rilevata.'
    stale = 'true' if corr['STALE_INPUT'] else 'false'
    report = f'''# RSC ABC RAPPORTO CURRENT

## Freshness

- STALE_INPUT={stale}
- patch_dominante: `{corr.get('dominant_patch')}`
- mtime: `{freshness.get('mtime', 'n/d')}`
- age_hours: `{freshness.get('age_hours', 'n/d')}`
- source: `{freshness.get('source', 'n/d')}`
- message_id: `{freshness.get('message_id', '')}`
- payload_hash: `{freshness.get('payload_hash', '')}`

## Memoria ABC / lettura strutturata

- curva current: **{corr['curve_current']}/100**
- curva prudenziale: **{corr['curve_prudential']}/100**
- pressione terzo: **{corr['third_pressure']}%**
- inconsistent: **{str(corr['inconsistent']).lower()}**

### Vincoli interpretativi estratti
{constraint_lines}

### Lettura corrente
{corr['reading_short']}

### Regola operativa
{corr['operational_rule']}

### Contraddizioni interne
{contradiction_lines}

## Patch dominante

{patch_text[:80000].strip() if patch_text else '_Nessuna patch disponibile._'}
'''
    FULL.write_text(report, encoding='utf-8')
    ESTESO.write_text(report, encoding='utf-8')
    print(CORR)
    print(FULL)
    print(ESTESO)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
