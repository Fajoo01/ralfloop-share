from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest


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


def test_cli_direct_publish_is_forbidden_before_build(tmp_path):
    builder = load_builder()

    with pytest.raises(
        SystemExit,
        match="direct_publish_forbidden_use_deploy_local_arch_release",
    ):
        builder.main([
            "--repo",
            str(tmp_path / "missing"),
            "--publish",
        ])

    assert not (tmp_path / "releases").exists()


def test_release_builds_atm_router_from_committed_sources(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    git(repo, "init")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")

    source = repo / "tools" / "atm_router"
    source.mkdir(parents=True)

    (source / "atm_router.h").write_text(
        "#ifndef ATM_ROUTER_H\n"
        "#define ATM_ROUTER_H\n"
        "#endif\n",
        encoding="utf-8",
    )

    (source / "atm_router.c").write_text(
        '#include <stdio.h>\n'
        'int main(void) { puts("atm-router-test"); return 0; }\n',
        encoding="utf-8",
    )

    (source / "Makefile").write_text(
        "CC ?= cc\n"
        "CFLAGS ?= -O2 -std=c11 -Wall -Wextra -Werror -pedantic\n"
        "\n"
        "atm-router: atm_router.c atm_router.h\n"
        "\t$(CC) $(CFLAGS) -o $@ atm_router.c\n",
        encoding="utf-8",
    )

    git(repo, "add", ".")
    git(repo, "commit", "-m", "atm router sources")

    builder = load_builder()
    result = builder.build_release(
        repo,
        output_root=tmp_path / "releases",
    )

    release = Path(result["release_dir"])
    binary = release / "tools" / "atm_router" / "atm-router"

    assert binary.is_file()
    assert binary.stat().st_mode & 0o111

    assert subprocess.check_output(
        [str(binary)],
        text=True,
    ).strip() == "atm-router-test"

    manifest = (
        release / "MANIFEST.sha256"
    ).read_text(encoding="utf-8")

    assert "tools/atm_router/atm-router" in manifest


def test_release_builder_compiles_teacher_core_mcp(tmp_path):
    project = Path(__file__).resolve().parents[1]
    root = tmp_path / "release-root"
    (root / "tools").mkdir(parents=True)
    (root / "third_party").mkdir(parents=True)
    shutil.copytree(
        project / "tools" / "teacher_core_mcp",
        root / "tools" / "teacher_core_mcp",
        ignore=shutil.ignore_patterns("ralf-teacher-core-mcp", "__pycache__"),
    )
    shutil.copytree(project / "third_party" / "jsmn", root / "third_party" / "jsmn")
    builder = load_builder()
    builder._build_teacher_core_mcp(root)
    binary = root / "bin" / "ralf-teacher-core-mcp"
    assert binary.is_file()
    assert binary.stat().st_mode & 0o111
    out = subprocess.check_output([str(binary), "--stdio"], input='', text=True)
    assert out == ''


def test_builder_drops_stale_archived_quality_gate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init"); git(repo, "config", "user.email", "test@example.invalid"); git(repo, "config", "user.name", "Test")
    gate = repo / ".ralf_run/local_arch_v1/gates.json"
    gate.parent.mkdir(parents=True)
    gate.write_text(json.dumps({"tested_commit": "old"}), encoding="utf-8")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "."); git(repo, "commit", "-m", "initial")
    builder = load_builder()
    result = builder.build_release(repo, output_root=tmp_path / "releases")
    release = Path(result["release_dir"])
    assert not (release / ".ralf_run/local_arch_v1/gates.json").exists()


def test_builder_accepts_only_commit_bound_quality_gate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init"); git(repo, "config", "user.email", "test@example.invalid"); git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "."); git(repo, "commit", "-m", "initial")
    commit = git(repo, "rev-parse", "HEAD").strip()
    builder = load_builder()
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"tested_commit": "0" * 40}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="quality_gate_commit_mismatch"):
        builder.build_release(repo, output_root=tmp_path / "wrong-releases", quality_gate_file=wrong)
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"tested_commit": commit, "scoped_tests": True}), encoding="utf-8")
    result = builder.build_release(repo, output_root=tmp_path / "good-releases", quality_gate_file=good)
    release = Path(result["release_dir"])
    assert json.loads((release / ".ralf_run/local_arch_v1/gates.json").read_text())["tested_commit"] == commit
