from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get('RALFLOOP_ROOT', Path(__file__).resolve().parents[1])).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from openshell_backend.skills import abc_memory
from openshell_backend.skills import abc_formula_loop
RUN = Path(os.environ.get('RALFLOOP_RUN_DIR', ROOT / '.ralf_run')).resolve()
MEM = Path(os.environ.get('ABC_MEMORY_DIR', str(ROOT / 'abc_memory')))
OUT_FULL = RUN / 'RSC_ABC_RAPPORTO_FULL_CURRENT.txt'
OUT_EXT = RUN / 'RSC_ABC_RAPPORTO_ESTESO_CURRENT.txt'
CORR_JSON = RUN / 'RSC_ABC_LLM_CORRECTION_CURRENT.json'
MAX_EXT_CHARS = int(os.environ.get('RSC_MAX_EXT_CHARS', '200000'))
MAX_PATCH_CHARS = int(os.environ.get('RSC_MAX_PATCH_CHARS', '80000'))


def configure_extractor() -> None:
    use_llm = os.environ.get('ABC_USE_LLM_EXTRACTOR', '').lower() in {'1', 'true', 'yes', 'on'}
    if not use_llm:
        os.environ.setdefault('ABC_DISABLE_LLM_EXTRACTOR', '1')


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


def current_patch_record() -> dict[str, Any] | None:
    return abc_memory.current_context_record(MEM, RUN)


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
        return '## Structured memory legacy correction\n\nWARNING: correction unavailable. Full report uses extended body only.\n'
    contradictions = corr.get('contradictions_found') or []
    if contradictions:
        contradiction_text = '\n'.join(f'- {item}' for item in contradictions)
    else:
        contradiction_text = '- Nessuna contraddizione registrata.'
    film_alive = str(bool(corr.get('film_hook_alive'))).lower()
    return f'''## Structured memory legacy correction

Nota: questi numeri sono legacy/diagnostici da memoria strutturata. Se `abc_formula_loop_v1` e' presente, la fonte operativa di score/range/action e' ABC Formula Loop.

- legacy_curve_current: **{corr.get('curve_current', 'n/d')}**
- legacy_curve_prudential: **{corr.get('curve_prudential', 'n/d')}**
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


def formula_primary_section(report_text: str, warnings: list[str]) -> str:
    try:
        configure_extractor()
        result = abc_formula_loop.score_text(
            report_text,
            report_path=None,
            memory_dir=MEM,
            evidence_cache_path=RUN / 'last_evidence.json',
            force_extract=False,
        )
    except Exception as exc:
        warnings.append(f'WARNING: ABC Formula Loop primary score unavailable: {exc!r}')
        return '''## ABC Formula Loop primary score

WARNING: ABC Formula Loop unavailable. Legacy structured memory numbers remain diagnostic only.
'''
    bounded = result.get('bounded_delta_adjustment') or {}
    third = result.get('third_delta_assessment') or {}
    bounded_lines = ''
    if bounded:
        bounded_lines = f'''
- bounded_delta_applied: **{str(bool(bounded.get('applied'))).lower()}**
- bounded_prudential_delta: **{bounded.get('prudential_delta', 'n/d')}**
- bounded_delta_reason: {bounded.get('reason', 'n/d')}
'''
    third_lines = ''
    if third:
        third_flags = [
            f'{key}={str(bool(third.get(key))).lower()}'
            for key in (
                'third_presence_observed',
                'third_presence_late_evening',
                'third_omitted_from_prior_narrative',
                'no_affection_observed',
                'no_overnight',
                'subject_self_driving_or_autonomous',
                'third_passive_or_low_support',
                'next_morning_repair',
                'functional_explanation',
                'accepted_hug_repair',
                'affection_or_overnight_positive',
            )
            if key in third
        ]
        third_lines = f'''
- third_delta_net_effect: **{third.get('net_effect', 'n/d')}**
- third_delta_classification: **{third.get('classification', 'n/d')}**
- third_delta_flags: {', '.join(third_flags) or 'none'}
'''
    return f'''## ABC Formula Loop primary score

Fonte operativa per score/range/action: `abc_formula_loop_v1`.

- rlfull_current: **{result.get('rlfull_current', 'n/d')}**
- prudential_score: **{result.get('prudential_score', 'n/d')}**
- relcalc_score: **{result.get('relcalc_score', 'n/d')}**
- confidence: **{result.get('confidence', 'n/d')}**
- operative_range: **{result.get('operative_range', 'n/d')}**
- action: **{result.get('action', 'n/d')}**
- evidence_count: **{result.get('evidence_count', 'n/d')}**
- bias_flags: {', '.join(map(str, result.get('bias_flags') or [])) or 'none'}
{bounded_lines}{third_lines}
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
    current = current_patch_record()
    patch = Path(str(current['patch_file'])) if current else None
    patch_text = read_limited(patch, MAX_PATCH_CHARS, missing) if patch else ''
    if patch is None:
        warnings.append('WARNING: no RL_ABC_PATCH*.txt found')
    for item in missing:
        warnings.append(f'WARNING: missing input: {item}')
    ext = apply_correction_to_body(ext, corr)
    generated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    fresh = (current.get('freshness') if current else None) or (abc_memory.freshness(MEM, RUN, patch) if patch else {'stale_input': True})
    patch_meta = f'{patch.name} mtime_utc={datetime.fromtimestamp(patch.stat().st_mtime, tz=timezone.utc).isoformat()} STALE_INPUT={str(bool(fresh.get("stale_input"))).lower()} source={fresh.get("source", "unknown")} message_id={fresh.get("message_id", "")} payload_hash={fresh.get("payload_hash", "")}' if patch else 'none'
    warning_text = '\n'.join(f'- {w}' for w in warnings) if warnings else '- Nessun warning.'
    full_body = f'''## WARNING / input status
{warning_text}

{correction_section(corr)}

## Corpo esteso corretto

{ext.strip() if ext else '_RSC_ABC_RAPPORTO_ESTESO_CURRENT.txt mancante o vuoto._'}

============================================================
APPENDICE — PATCH DOMINANTE
============================================================

{patch_text.strip() if patch_text else '_Patch dominante non disponibile._'}
'''
    current_context = abc_memory.build_current_context(MEM, RUN)
    formula_input = current_context.get('formula_scoring_input') or patch_text or full_body
    formula_section = formula_primary_section(formula_input, warnings)
    warning_text = '\n'.join(f'- {w}' for w in warnings) if warnings else '- Nessun warning.'
    full = f'''RSC ABC — FULL CURRENT FAST
Generato: {generated}
Builder: build_rsc_full_current.py fast/read-only
Patch dominante: {patch_meta}

{formula_section}

## formula_scoring_input

{formula_input.strip() if formula_input else '_Formula scoring input non disponibile._'}

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
