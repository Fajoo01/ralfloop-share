from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)


def _allowed(root: Path) -> bool:
    raw = os.environ.get("RALF_CODE_WORKTREE_ROOTS", "/home/bandi:/home/sibilla-cumana")
    roots = [Path(p).resolve() for p in raw.split(":") if p.strip()]
    return any(root == allowed or allowed in root.parents for allowed in roots)


def _git_root(workdir: Path) -> Path | None:
    result = _run(["git", "rev-parse", "--show-toplevel"], workdir)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("ok") else 1


def apply_patch(workdir: Path, patch_file: Path, validator: str) -> dict:
    root = _git_root(workdir.resolve())
    if root is None or root != workdir.resolve():
        return {"ok": False, "status": "worktree_root_required"}
    if not _allowed(root):
        return {"ok": False, "status": "worktree_not_allowlisted"}
    status = _run(["git", "status", "--porcelain"], root)
    if status.returncode != 0 or status.stdout.strip():
        return {"ok": False, "status": "worktree_not_clean"}
    patch = patch_file.resolve()
    if not patch.is_file():
        return {"ok": False, "status": "patch_missing"}
    check = _run(["git", "apply", "--check", str(patch)], root)
    if check.returncode != 0:
        return {"ok": False, "status": "patch_check_failed", "stderr": check.stderr[-2000:]}
    applied = _run(["git", "apply", str(patch)], root)
    if applied.returncode != 0:
        return {"ok": False, "status": "patch_apply_failed", "stderr": applied.stderr[-2000:]}
    validation = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", validator],
        cwd=root, text=True, capture_output=True, check=False,
    )
    if validation.returncode != 0:
        rollback = _run(["git", "apply", "-R", str(patch)], root)
        return {
            "ok": False, "status": "validator_failed", "validator_rc": validation.returncode,
            "validator_output": (validation.stdout + validation.stderr)[-3000:],
            "rollback_ok": rollback.returncode == 0,
        }
    diff = _run(["git", "diff", "--check"], root)
    if diff.returncode != 0:
        rollback = _run(["git", "apply", "-R", str(patch)], root)
        return {"ok": False, "status": "diff_check_failed", "rollback_ok": rollback.returncode == 0}
    changed = _run(["git", "diff", "--name-only"], root)
    return {
        "ok": True,
        "status": "applied_and_validated",
        "changed_files": [p for p in changed.stdout.splitlines() if p],
        "validator_rc": validation.returncode,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--patch", required=True, type=Path)
    parser.add_argument("--validator", default="git diff --check")
    args = parser.parse_args()
    return _emit(apply_patch(args.workdir, args.patch, args.validator))


if __name__ == "__main__":
    raise SystemExit(main())
