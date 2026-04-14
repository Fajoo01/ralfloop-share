import os

from ralfloop_agent.main import build_adapter


def test_build_adapter_defaults_to_local() -> None:
    os.environ.pop("RALFLOOP_ADAPTER", None)
    adapter = build_adapter()
    assert adapter.__class__.__name__ == "OpenShellAdapterStub"


def test_build_adapter_can_select_openshell() -> None:
    os.environ["RALFLOOP_ADAPTER"] = "openshell"
    adapter = build_adapter()
    assert adapter.__class__.__name__ == "OpenShellAdapterReal"
    os.environ.pop("RALFLOOP_ADAPTER", None)
