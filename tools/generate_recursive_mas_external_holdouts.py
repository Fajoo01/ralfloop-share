from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ralfloop_agent.domains.recursive_mas_domain_dataset import generate_dataset
from ralfloop_agent.domains.recursive_mas_external_holdouts import (
    contamination_report,
    dataset_manifest,
    generate_external_holdouts,
    jsonl_bytes,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runner-manifest", type=Path, required=True)
    args = parser.parse_args()
    final, reserve = generate_external_holdouts()
    previous, _ = generate_dataset()
    contamination = contamination_report(final, reserve, previous)
    if contamination["FINAL_A"]["contaminated"] or contamination["RESERVE_B"]["contaminated"]:
        raise RuntimeError("external_holdout_contamination")
    runner_hash = hashlib.sha256(args.runner_manifest.read_bytes()).hexdigest()
    manifest = dataset_manifest(
        final,
        reserve,
        runner_frozen_manifest_hash=runner_hash,
        contamination=contamination,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "final_a.jsonl").write_bytes(jsonl_bytes(final))
    reserve_path = args.output_dir / "reserve_b.jsonl"
    reserve_path.write_bytes(jsonl_bytes(reserve))
    reserve_path.chmod(0o600)
    (args.output_dir / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
