from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess


def load_builder():
    path = Path(__file__).parents[1] / "tools/build_ralfloop_production_release.py"
    spec = importlib.util.spec_from_file_location("production_release", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True)


def test_full_release_from_commit_is_immutable_and_atomic(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    git(repo, "init"); git(repo, "config", "user.email", "test@example.invalid"); git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "."); git(repo, "commit", "-m", "initial")
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    builder = load_builder()
    result = builder.build_release(repo, output_root=tmp_path / "releases", current_link=tmp_path / "current")
    release = Path(result["release_dir"])
    assert (release / "app.py").read_text() == "VALUE = 1\n"
    assert json.loads((release / "RELEASE.json").read_text())["commit"] == git(repo, "rev-parse", "HEAD").strip()
    assert (tmp_path / "current").resolve() == release
    assert (release.stat().st_mode & 0o222) == 0
