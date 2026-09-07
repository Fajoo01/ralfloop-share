"""Verify the companion patch without changing/restarting production Meowgram."""
import ast
from pathlib import Path
import shutil
import subprocess

import pytest


def test_runts_review_formatter_patch_preserves_approval_and_other_domains(tmp_path):
    source = Path("/srv/projects/Meowgram/src/meowgram/bot.py")
    if not source.is_file():
        pytest.skip("Installed Meowgram source required for companion patch check")
    target = tmp_path / "src/meowgram/bot.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(source, target)
    patch = Path(__file__).resolve().parents[1] / "integrations/meowgram/runts-review-display.patch"
    subprocess.run(["patch", "--batch", "-p1", "-i", str(patch)], cwd=tmp_path, check=True, capture_output=True)
    tree = ast.parse(target.read_text())
    formatter = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                     and node.name == "_format_ralfloop_natural_response")
    namespace = {}
    exec(compile(ast.Module(body=[formatter], type_ignores=[]), str(target), "exec"), namespace)
    render = namespace[formatter.name]
    payload = {"ok": True, "approval_required": True, "human_review_required": True,
               "capability": "runts_prepare_practice_response", "response": "Oggetto/body/PDF/SHA256/B00\nApprovo 2603942",
               "metadata": {"pending_domain": "runts", "pending_action": "runts_practice_reply", "writes": 0}}
    assert render(None, payload) == payload["response"]
    assert payload["approval_required"] is True
    assert render(None, {**payload, "capability": "other"}).startswith("Serve conferma esplicita")
