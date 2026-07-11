from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any


DEFAULT_OUTPUT_ROOT = Path("/home/sibilla-cumana/ralfloop-domain-runtime/releases")
DEFAULT_CURRENT_LINK = Path("/home/sibilla-cumana/ralfloop-domain-runtime/current")
SCHEMA_VERSION = "1.0"

RUNTIME_PATHS = [
    "ralfloop_agent/__init__.py",
    "ralfloop_agent/domains/__init__.py",
    "ralfloop_agent/domains/builder.py",
    "ralfloop_agent/domains/bandi_registry.py",
    "ralfloop_agent/domains/calculation_orchestrator.py",
    "ralfloop_agent/domains/capability_registry.py",
    "ralfloop_agent/domains/cli.py",
    "ralfloop_agent/domains/deterministic_engine.py",
    "ralfloop_agent/domains/existing_capability_importer.py",
    "ralfloop_agent/domains/existing_skill_adapter.py",
    "ralfloop_agent/domains/jury_router.py",
    "ralfloop_agent/domains/models.py",
    "ralfloop_agent/domains/promotion.py",
    "ralfloop_agent/domains/provenance.py",
    "ralfloop_agent/domains/registry.py",
    "ralfloop_agent/domains/resolver.py",
    "ralfloop_agent/domains/storage.py",
    "ralfloop_agent/domains/validator.py",
    "ralfloop_agent/integration/__init__.py",
    "ralfloop_agent/integration/cheshire_domain_bridge.py",
    "ralfloop_agent/integration/cheshire_domain_bridge_cli.py",
    "ralfloop_agent/integration/cheshire_plugin_hook.py",
    "ralfloop_agent/integration/recursive_mas_host_client.py",
    "config/domain_capability_mapping.yaml",
    "domains/registry.json",
    "domains/active/.gitkeep",
    "domains/drafts/.gitkeep",
    "domains/deprecated/.gitkeep",
    "domains/quarantine/.gitkeep",
]


def build_release(
    repo: str | Path = ".",
    commit: str = "HEAD",
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    current_link: str | Path | None = DEFAULT_CURRENT_LINK,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    repo_path = Path(repo).resolve()
    commit_sha = _git(repo_path, ["rev-parse", commit]).strip()
    branch = _git(repo_path, ["branch", "--show-current"]).strip()
    release_dir = Path(output_root) / commit_sha
    tmp_dir = release_dir.with_name(release_dir.name + f".tmp-{os.getpid()}")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    selected = paths or RUNTIME_PATHS
    copied: list[dict[str, Any]] = []
    missing: list[str] = []
    for rel in selected:
        if not _path_exists_in_commit(repo_path, commit_sha, rel):
            missing.append(rel)
            continue
        target = tmp_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = _git_bytes(repo_path, ["show", f"{commit_sha}:{rel}"])
        target.write_bytes(data)
        copied.append({"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    if missing:
        shutil.rmtree(tmp_dir)
        raise RuntimeError("paths_missing_from_commit:" + ",".join(missing))
    (tmp_dir / "requirements-runtime.txt").write_text("PyYAML\n", encoding="utf-8")
    (tmp_dir / "VERSION").write_text(commit_sha + "\n", encoding="utf-8")
    manifest = _manifest(tmp_dir)
    release = {
        "schema_version": SCHEMA_VERSION,
        "commit": commit_sha,
        "branch": branch,
        "created_at": _now_iso(),
        "files": manifest["files"],
        "content_hash": manifest["content_hash"],
    }
    (tmp_dir / "RELEASE.json").write_text(json.dumps(release, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    manifest = _manifest(tmp_dir)
    (tmp_dir / "MANIFEST.sha256").write_text("\n".join(f"{row['sha256']}  {row['path']}" for row in manifest["files"]) + "\n", encoding="utf-8")
    _make_read_only(tmp_dir)
    if release_dir.exists():
        shutil.rmtree(release_dir)
    os.replace(tmp_dir, release_dir)
    if current_link:
        link = Path(current_link)
        link.parent.mkdir(parents=True, exist_ok=True)
        tmp_link = link.with_name(link.name + f".tmp-{os.getpid()}")
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(release_dir)
        os.replace(tmp_link, link)
    return {**release, "release_dir": str(release_dir), "copied": copied}


def _manifest(root: Path) -> dict[str, Any]:
    rows = []
    h = hashlib.sha256()
    for file in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = str(file.relative_to(root))
        data = file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        rows.append({"path": rel, "bytes": len(data), "sha256": digest})
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\0")
    return {"files": rows, "content_hash": h.hexdigest()}


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


def _path_exists_in_commit(repo: Path, commit: str, rel: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{commit}:{rel}"], cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def _git(repo: Path, args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True)


def _git_bytes(repo: Path, args: list[str]) -> bytes:
    return subprocess.check_output(["git", *args], cwd=repo)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--current-link", default=str(DEFAULT_CURRENT_LINK))
    parser.add_argument("--publish-current", action="store_true")
    parser.add_argument("--no-current-link", action="store_true")
    args = parser.parse_args(argv)
    publish_current = args.publish_current and not args.no_current_link
    result = build_release(args.repo, args.commit, args.output_root, args.current_link if publish_current else None)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
