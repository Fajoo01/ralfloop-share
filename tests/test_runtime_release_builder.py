import importlib.util
import json
import os
import subprocess
from pathlib import Path


def _load_builder():
    path = Path(__file__).resolve().parents[1] / "tools" / "build_cheshire_domain_runtime_release.py"
    spec = importlib.util.spec_from_file_location("release_builder", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("VALUE = 'clean'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_release_built_from_commit_excludes_dirty_worktree(tmp_path):
    builder = _load_builder()
    repo = _make_repo(tmp_path)
    (repo / "pkg" / "__init__.py").write_text("VALUE = 'dirty'\n", encoding="utf-8")
    result = builder.build_release(repo, "HEAD", tmp_path / "out", tmp_path / "current", paths=["pkg/__init__.py"])
    release_file = Path(result["release_dir"]) / "pkg" / "__init__.py"
    assert "clean" in release_file.read_text(encoding="utf-8")
    assert "dirty" not in release_file.read_text(encoding="utf-8")


def test_release_manifest_checksums_and_current_link(tmp_path):
    builder = _load_builder()
    repo = _make_repo(tmp_path)
    result = builder.build_release(repo, "HEAD", tmp_path / "out", tmp_path / "current", paths=["pkg/__init__.py"])
    release = Path(result["release_dir"])
    data = json.loads((release / "RELEASE.json").read_text(encoding="utf-8"))
    assert data["commit"] == _git(repo, "rev-parse", "HEAD").strip()
    assert (release / "MANIFEST.sha256").exists()
    assert (tmp_path / "current").resolve() == release
    assert not os.access(release / "pkg" / "__init__.py", os.W_OK)


def test_release_missing_path_fails(tmp_path):
    builder = _load_builder()
    repo = _make_repo(tmp_path)
    try:
        builder.build_release(repo, "HEAD", tmp_path / "out", None, paths=["missing.py"])
    except RuntimeError as exc:
        assert "paths_missing_from_commit" in str(exc)
    else:
        raise AssertionError("missing commit path must fail")
