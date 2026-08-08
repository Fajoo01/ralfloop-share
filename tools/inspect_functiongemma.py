from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def inspect(model: Path, llama_cli: Path) -> dict[str, object]:
    header = model.read_bytes()[:8]
    if header[:4] != b"GGUF":
        raise ValueError("not_gguf")
    digest = hashlib.sha256()
    with model.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    result = subprocess.run(
        [str(llama_cli), "--model", str(model), "--no-warmup", "--verbose", "--n-predict", "0", "--prompt", ""],
        capture_output=True,
        text=True,
        timeout=120,
    )
    metadata = result.stdout + result.stderr
    required = {
        "tokenizer": "tokenizer" in metadata.casefold(),
        "chat_template": "chat_template" in metadata.casefold() or "chat template" in metadata.casefold(),
        "gemma": "gemma" in metadata.casefold(),
    }
    return {
        "v": 1,
        "path": str(model),
        "bytes": model.stat().st_size,
        "sha256": digest.hexdigest(),
        "gguf_version": int.from_bytes(header[4:8], "little"),
        "llama_returncode": result.returncode,
        "checks": required,
        "compatible": result.returncode == 0 and all(required.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    parser.add_argument("--llama-cli", type=Path, default=Path("/home/sibilla-cumana/src/llama.cpp/build/bin/llama-cli"))
    args = parser.parse_args()
    print(json.dumps(inspect(args.model, args.llama_cli), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
