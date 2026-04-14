from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub


def test_http_fetch_allowlist_and_block(tmp_path) -> None:
    adapter = OpenShellAdapterStub(base_dir=str(tmp_path / ".sandbox"))
    sandbox = adapter.create_sandbox()

    ok = adapter.http_fetch(sandbox, "http://127.0.0.1:11434/api/tags")
    assert ok.ok is True
    assert '"models"' in ok.stdout

    blocked = adapter.http_fetch(sandbox, "https://example.com")
    assert blocked.ok is False
    assert blocked.error_type == "policy_denied"
