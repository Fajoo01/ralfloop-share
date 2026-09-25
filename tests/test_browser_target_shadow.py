from __future__ import annotations

import json

import pytest

from ralfloop_agent.unified_assistant.browser_target_selector import BrowserTargetSelector, RizzoTargetClient
from ralfloop_agent.unified_assistant.browser_target_shadow import BrowserTargetShadowObserver


SNAPSHOT = '''### Page
- Page Title: Example
### Snapshot
```yaml
- generic [ref=e1]:
  - textbox "Email" [ref=e10] [box=10,10,200,30]
  - textbox "Email del canale YouTube" [ref=e11] [box=10,50,200,30]
  - button "Invia" [ref=e12] [cursor=pointer] [box=10,90,80,30]
```
'''


class FakeRizzo:
    def __init__(self, choice: str):
        self.choice = choice
        self.goals = []

    def choose(self, goal, candidates):
        self.goals.append(goal)
        return self.choice, 0.91


def test_rizzo_endpoint_is_loopback_only():
    with pytest.raises(ValueError, match="loopback"):
        RizzoTargetClient("https://example.com/v1/decisions")
    assert RizzoTargetClient("http://127.0.0.1:18017/v1/decisions").endpoint


def test_shadow_observer_logs_only_hashes_and_refs(tmp_path):
    fake = FakeRizzo("e10")
    selector = BrowserTargetSelector(
        rizzo_enabled=True,
        rizzo_client=fake,
        deterministic_min_margin=100,
    )
    audit = tmp_path / "shadow.jsonl"
    observer = BrowserTargetShadowObserver(selector=selector, audit_path=audit)
    assert observer.observe(
        goal='browser scrivi Email e10 testo="segreto@example.com"',
        snapshot=SNAPSHOT,
        authoritative_target="e10",
        logical_action="type",
    ) is True
    assert observer.flush(1.0)
    observer.close()

    raw = audit.read_text(encoding="utf-8")
    assert "segreto@example.com" not in raw
    assert "### Snapshot" not in raw
    event = json.loads(raw)
    assert event["authoritative_target"] == "e10"
    assert event["proposal_target"] == "e10"
    assert event["match"] is True
    assert event["authoritative_in_shortlist"] is True
    assert event["goal_sha256"]
    assert event["snapshot_sha256"]
    assert fake.goals == ["Email"]


def test_shadow_skips_uninformative_exact_ref(tmp_path):
    observer = BrowserTargetShadowObserver(
        selector=BrowserTargetSelector(rizzo_enabled=False),
        audit_path=tmp_path / "shadow.jsonl",
    )
    assert observer.observe(
        goal="browser clicca e12",
        snapshot=SNAPSHOT,
        authoritative_target="e12",
        logical_action="click",
    ) is False
    observer.close()
    assert not (tmp_path / "shadow.jsonl").exists()
