from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from typing import Any


DEFAULT_OUTPUT_ROOT = Path("/home/sibilla-cumana/ralfloop-production/releases")
DEFAULT_CURRENT_LINK = Path("/home/sibilla-cumana/ralfloop-production/current")


def build_release(
    repo: str | Path,
    commit: str = "HEAD",
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    current_link: str | Path | None = None,
    previous_release: str | Path | None = None,
) -> dict[str, Any]:
    repo_path = Path(repo).resolve()
    commit_sha = _git(repo_path, "rev-parse", commit).strip()
    branch = _git(repo_path, "branch", "--show-current").strip()
    output = Path(output_root)
    release = output / commit_sha
    if release.exists():
        raise RuntimeError(f"immutable_release_exists:{release}")
    temporary = output / f".{commit_sha}.tmp-{os.getpid()}"
    if temporary.exists():
        raise RuntimeError(f"temporary_release_exists:{temporary}")
    output.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(mode=0o700)
    try:
        archive = subprocess.check_output(_git_command(repo_path, "archive", "--format=tar", commit_sha), cwd=repo_path)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            bundle.extractall(temporary, filter="data")
        _build_shell_parser(temporary)
        _build_atm_router(temporary)
        metadata = {
            "schema_version": 1,
            "commit": commit_sha,
            "branch": branch,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "previous_release": str(Path(previous_release).resolve()) if previous_release else (str(Path(current_link).resolve()) if current_link and Path(current_link).exists() else None),
        }
        (temporary / "VERSION").write_text(commit_sha + "\n", encoding="utf-8")
        (temporary / "RELEASE.json").write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        rows = _manifest(temporary)
        (temporary / "MANIFEST.sha256").write_text(
            "\n".join(f"{row['sha256']}  {row['path']}" for row in rows) + "\n",
            encoding="utf-8",
        )
        _make_read_only(temporary)
        os.replace(temporary, release)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    if current_link:
        publish_current(release, current_link)
    return {**metadata, "release_dir": str(release), "files": len(rows)}


def publish_current(release: str | Path, current_link: str | Path = DEFAULT_CURRENT_LINK) -> None:
    target = Path(release).resolve()
    link = Path(current_link)
    temporary = link.with_name(f".{link.name}.tmp-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target)
    os.replace(temporary, link)


def _manifest(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        data = path.read_bytes()
        rows.append({"path": str(path.relative_to(root)), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    return rows


def _build_shell_parser(root: Path) -> None:
    source = root / "ralfloop_agent" / "shell_judge" / "mvdan"
    if not source.is_dir():
        return
    destination = root / "bin" / "ralf-shell-ast"
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["go", "build", "-trimpath", "-o", str(destination), "./judge.go"],
        cwd=source, check=True, shell=False,
        env={**os.environ, "CGO_ENABLED": "0"},
    )
    destination.chmod(0o555)


def _build_atm_router(root: Path) -> None:
    source = root / "tools" / "atm_router"
    c_source = source / "atm_router.c"
    header = source / "atm_router.h"

    # Release storiche o repository che non contengono il router ATM
    # devono continuare a essere costruibili.
    if not c_source.is_file() or not header.is_file():
        return

    makefile = source / "Makefile"
    if not makefile.is_file():
        raise RuntimeError("atm_router_makefile_missing")

    subprocess.run(
        ["make", "-C", str(source), "atm-router"],
        check=True,
        shell=False,
    )

    destination = source / "atm-router"
    if not destination.is_file():
        raise RuntimeError("atm_router_binary_missing_after_build")

    destination.chmod(0o555)


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        if path.is_file():
            path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
        else:
            path.chmod(0o555)
    root.chmod(0o555)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(_git_command(repo, *args), cwd=repo, text=True)


def _git_command(repo: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={repo}", *args]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--current-link", default=str(DEFAULT_CURRENT_LINK))
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--record-previous")
    args = parser.parse_args(argv)
    if args.publish:
        raise SystemExit(
            "direct_publish_forbidden_use_deploy_local_arch_release"
        )
    result = build_release(
        args.repo,
        args.commit,
        args.output_root,
        None,
        args.record_previous,
    )
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
