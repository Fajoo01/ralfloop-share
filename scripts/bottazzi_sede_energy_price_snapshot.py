#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / 'config' / 'sede_climate_policy.json'


def positive(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    value = float(value)
    if value <= 0:
        raise SystemExit(f'{name}_must_be_positive')
    return value


def snapshot_path(policy_path: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    policy = json.loads(policy_path.read_text(encoding='utf-8'))
    configured = (((policy.get('energy_economics') or {}).get('price_snapshot') or {}).get('path'))
    if not configured:
        raise SystemExit('price_snapshot_path_missing_in_policy')
    return Path(str(configured)).expanduser()


def main() -> int:
    ap = argparse.ArgumentParser(description='Atomically update Sede energy price snapshot')
    ap.add_argument('--policy', default=str(DEFAULT_POLICY))
    ap.add_argument('--path')
    ap.add_argument('--source', required=True)
    ap.add_argument('--observed-at')
    ap.add_argument('--valid-until')
    ap.add_argument('--electricity-below', type=float)
    ap.add_argument('--electricity-above', type=float)
    ap.add_argument('--gas-per-smc', type=float)
    args = ap.parse_args()
    values = {
        'electricity_marginal_eur_per_kwh_below_threshold': positive(args.electricity_below, 'electricity_below'),
        'electricity_marginal_eur_per_kwh_above_threshold': positive(args.electricity_above, 'electricity_above'),
        'gas_marginal_eur_per_smc': positive(args.gas_per_smc, 'gas_per_smc'),
    }
    if not any(value is not None for value in values.values()):
        raise SystemExit('at_least_one_price_required')
    now = datetime.now(ZoneInfo('Europe/Rome'))
    payload = {
        'schema_version': 1,
        'observed_at': args.observed_at or now.isoformat(),
        'source': args.source,
        **{key: value for key, value in values.items() if value is not None},
    }
    if args.valid_until:
        payload['valid_until'] = args.valid_until
    path = snapshot_path(Path(args.policy), args.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)
    print(json.dumps({'ok': True, 'path': str(path), 'snapshot': payload}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
