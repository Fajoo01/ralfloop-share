from openshell_backend.contracts.coder_autofix_runner import maybe_attach_coder_text


class _Resp:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"response": "PATCH_CODE_HERE"}


def test_maybe_attach_coder_text_adds_coder_text(monkeypatch) -> None:
    def _fake_post(*args, **kwargs):
        return _Resp()

    monkeypatch.setattr("openshell_backend.contracts.coder_autofix_runner.requests.post", _fake_post)

    payload = {
        "autofix_candidate": {
            "coder_handoff_prompt": "Produce a minimal patch for the target symbol."
        }
    }
    out = maybe_attach_coder_text(payload=payload, model_name="qwen2.5:7b")
    assert out["autofix_candidate"]["coder_text"] == "PATCH_CODE_HERE"


def test_maybe_attach_coder_text_skips_when_no_prompt() -> None:
    payload = {"autofix_candidate": {}}
    out = maybe_attach_coder_text(payload=payload, model_name="qwen2.5:7b")
    assert "coder_text" not in out["autofix_candidate"]
