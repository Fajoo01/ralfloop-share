from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from ralfloop_agent.shell_judge import RalfShellJudge, ShellDecision, ShellPolicy, safe_execution_environment
from ralfloop_agent.shell_judge.reviewer import MixtureShellReviewer, ShellReviewer


@pytest.fixture(scope="session")
def parser_binary(tmp_path_factory) -> Path:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path_factory.mktemp("shell-judge") / "ralf-shell-ast"
    subprocess.run(
        ["go", "build", "-trimpath", "-o", str(output), "./judge.go"],
        cwd=root / "ralfloop_agent/shell_judge/mvdan", check=True, shell=False,
    )
    return output


@pytest.fixture
def judge(tmp_path, parser_binary):
    (tmp_path / "allowed.txt").write_text("ok\n")
    (tmp_path / "sandbox/tmp").mkdir(parents=True)
    return tmp_path, RalfShellJudge(ShellPolicy.for_sandbox(tmp_path), parser=parser_binary)


@pytest.mark.parametrize(("command", "decision"), [
    ("git status", ShellDecision.ALLOW_READONLY),
    ("git diff --check", ShellDecision.ALLOW_READONLY),
    ("cat ./allowed.txt", ShellDecision.ALLOW_READONLY),
    ("cat /etc/passwd", ShellDecision.REVIEW),
    ("echo done > sandbox/out.txt", ShellDecision.ALLOW),
    ("echo done > /etc/passwd", ShellDecision.DENY),
    ("rm -rf sandbox/tmp/item", ShellDecision.REVIEW),
    ("rm -rf /home", ShellDecision.DENY),
    ("command rm -rf /home", ShellDecision.DENY),
    ("busybox rm -rf /home", ShellDecision.DENY),
    ("env PATH=/tmp git diff", ShellDecision.REVIEW),
    ("PATH=/tmp command", ShellDecision.REVIEW),
    ("GIT_EXTERNAL_DIFF=/tmp/x git diff", ShellDecision.REVIEW),
    ("cat ../escape/file", ShellDecision.REVIEW),
    ('command > "$UNRESOLVED"', ShellDecision.REVIEW),
    ("cat input | grep foo > output", ShellDecision.ALLOW),
    ("test -f x && rm x", ShellDecision.REVIEW),
    ("(cd foo && rm item)", ShellDecision.REVIEW),
    ("command &", ShellDecision.REVIEW),
    ("eval echo safe", ShellDecision.DENY),
    ("find . -delete", ShellDecision.DENY),
    ("bash -c 'echo safe'", ShellDecision.REVIEW),
    ("/tmp/git status", ShellDecision.REVIEW),
    ("./cat allowed.txt", ShellDecision.REVIEW),
    ("rg --pre /tmp/x foo", ShellDecision.REVIEW),
    ("git diff --ext-diff", ShellDecision.REVIEW),
    ("'", ShellDecision.DENY),
])
def test_required_decisions(judge, command, decision):
    root, value = judge
    assert value.review(command, str(root), {}).decision is decision


@pytest.mark.parametrize("command", ["echo $(rm file)", 'echo "$(hidden-command)"', "VAR=$(command) true"])
def test_substitutions_are_detected_and_propagated(judge, command):
    root, value = judge
    result = value.review(command, str(root), {})
    assert result.decision is not ShellDecision.ALLOW_READONLY
    assert result.capabilities.substitutions
    assert len(result.capabilities.commands) >= 2


def test_pipeline_paths(judge):
    root, value = judge
    result = value.review("cat input | grep foo > output", str(root), {})
    assert str(root / "input") in result.capabilities.resolved_paths_read
    assert str(root / "output") in result.capabilities.resolved_paths_write


def test_quoted_literal_path_is_normalized_without_quote_bytes(judge):
    root, value = judge
    result = value.review('cat "allowed file.txt"', str(root), {})
    assert result.capabilities.resolved_paths_read == (str(root / "allowed file.txt"),)


def test_subshell_tracks_inner_cwd(judge):
    root, value = judge
    result = value.review("(cd foo && rm item)", str(root), {})
    assert result.capabilities.resolved_paths_delete == (str(root / "foo/item"),)


def test_redirect_after_cwd_change_is_not_auto_allowed(judge):
    root, value = judge
    result = value.review("cd foo && echo x > out", str(root), {})
    assert result.decision is ShellDecision.REVIEW
    assert "redirect_after_cwd_change" in result.capabilities.unresolved_elements


def test_symlink_escape_denied(judge, tmp_path):
    root, value = judge
    (root / "escape").symlink_to("/etc")
    result = value.review("echo x > escape/passwd", str(root), {})
    assert result.decision is ShellDecision.DENY


def test_reviewer_mixture_only_accepts_review(judge):
    root, value = judge
    review = value.review("unknown-command", str(root), {})
    classifiers = tuple(ShellReviewer(lambda _: {
        "risk": "low", "authorization": "explicitly_yes",
        "correctness": "valid", "rationale": "bounded",
    }) for _ in range(2))
    result = MixtureShellReviewer(classifiers).classify(review)
    assert result.risk == "low"
    denied = value.review("rm -rf /home", str(root), {})
    with pytest.raises(ValueError, match="reviewer_only_accepts_review"):
        classifiers[0].classify(denied)
    assert denied.capabilities.destructive_level.value == "TOO_DESTRUCTIVE"


@pytest.fixture
def git_judge(tmp_path, parser_binary):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path, RalfShellJudge(ShellPolicy.for_sandbox(tmp_path), parser=parser_binary)


@pytest.mark.parametrize(("key", "value"), [
    ("diff.external", "/tmp/x"),
    ("diff.foo.command", "/tmp/x"),
    ("diff.foo.textconv", "/tmp/x"),
    ("core.pager", "/tmp/x"),
    ("pager.log", "/tmp/x"),
    ("alias.x", "!/tmp/x"),
    ("include.path", "/tmp/config"),
    ("includeIf.gitdir:/tmp/.path", "/tmp/config"),
])
def test_git_indirect_config_requires_review(git_judge, key, value):
    root, value_judge = git_judge
    subprocess.run(["git", "config", "--local", key, value], cwd=root, check=True)
    result = value_judge.review("git diff", str(root), {})
    assert result.decision is ShellDecision.REVIEW
    assert result.deterministic_reason == "git_indirect_effect_configured"


def test_git_cli_config_injection_requires_review(git_judge):
    root, value = git_judge
    assert value.review("git -c diff.external=/tmp/x diff", str(root), {}).decision is ShellDecision.REVIEW


def test_git_external_diff_sentinel_is_never_executed(git_judge):
    root, value = git_judge
    sentinel = root / "external-ran"
    external = root / "external"
    external.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
    external.chmod(0o700)
    subprocess.run(["git", "config", "--local", "diff.external", str(external)], cwd=root, check=True)
    result = value.review("git diff", str(root), {})
    assert result.decision is ShellDecision.REVIEW
    assert not sentinel.exists()


def test_git_attributes_driver_requires_review(git_judge):
    root, value = git_judge
    (root / ".gitattributes").write_text("*.txt diff=foo\n", encoding="utf-8")
    subprocess.run(["git", "config", "--local", "diff.foo.textconv", "/tmp/x"], cwd=root, check=True)
    assert value.review("git diff", str(root), {}).decision is ShellDecision.REVIEW


def test_nested_git_attributes_driver_requires_review(git_judge):
    root, value = git_judge
    nested = root / "nested"
    nested.mkdir()
    (nested / ".gitattributes").write_text("*.txt diff=foo\n", encoding="utf-8")
    assert value.review("git diff", str(root), {}).decision is ShellDecision.REVIEW


@pytest.mark.parametrize("command", [
    "GIT_EXTERNAL_DIFF=/tmp/x git diff",
    "GIT_PAGER=/tmp/x git log",
    "PAGER=/tmp/x git log",
    "GIT_CONFIG_COUNT=1 git status",
])
def test_git_environment_injection_requires_review(git_judge, command):
    root, value = git_judge
    assert value.review(command, str(root), {}).decision is ShellDecision.REVIEW


def test_reviewer_payload_redacts_arguments_and_paths(judge):
    root, value = judge
    marker = "RALF_SECRET_MARKER_93A7"
    review = value.review(f"unknown-command {marker} > {marker}", str(root), {})
    captured = []
    mixture = MixtureShellReviewer(tuple(ShellReviewer(lambda payload: (
        captured.append(payload) or {"risk": "high", "authorization": "neutral", "correctness": "unknown"}
    )) for _ in range(2)))
    mixture.classify(review)
    assert marker not in repr(captured)


def test_execution_environment_removes_runtime_and_git_injection(tmp_path):
    source = {
        "PATH": "/tmp", "LD_PRELOAD": "marker", "PYTHONPATH": "marker",
        "NODE_OPTIONS": "marker", "GIT_EXTERNAL_DIFF": "marker",
        "GIT_TRACE2_EVENT": "marker", "RIPGREP_CONFIG_PATH": "marker",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "diff.external",
        "GIT_CONFIG_VALUE_0": "/tmp/x", "PAGER": "/tmp/x",
    }
    result = safe_execution_environment(source)
    assert result["PATH"] == "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    assert result["GIT_PAGER"] == result["PAGER"] == "cat"
    assert result["GIT_OPTIONAL_LOCKS"] == "0"
    assert not any("marker" in value for value in result.values())
    assert not any(key.startswith("GIT_") and key not in {
        "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_PAGER", "GIT_OPTIONAL_LOCKS"
    } for key in result)
