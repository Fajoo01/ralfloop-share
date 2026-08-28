from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from ralfloop_agent.shell_judge import RalfShellJudge, ShellDecision, ShellPolicy
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


def test_subshell_tracks_inner_cwd(judge):
    root, value = judge
    result = value.review("(cd foo && rm item)", str(root), {})
    assert result.capabilities.resolved_paths_delete == (str(root / "foo/item"),)


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
