from __future__ import annotations

from ralfloop_agent.unified_assistant.browser_target_selector import (
    BrowserTargetSelector,
    parse_browser_candidates,
    shortlist_browser_targets,
)

SNAPSHOT = '''### Page
- Page Title: Example
### Snapshot
```yaml
- generic [ref=e1]:
  - textbox "Email" [ref=e10] [box=10,10,200,30]
  - textbox "Email del canale YouTube" [ref=e11] [box=10,50,200,30]
  - button "Invia" [ref=e12] [cursor=pointer] [box=10,90,80,30]
  - button "Stampa" [disabled] [ref=e13] [box=10,130,80,30]
  - button "Fuori viewport" [ref=e14] [box=10,1200,80,30]
```
'''


def test_parser_marks_disabled_and_offscreen_candidates():
    by_ref = {item.ref: item for item in parse_browser_candidates(SNAPSHOT)}
    assert by_ref['e10'].visible is True
    assert by_ref['e13'].disabled is True
    assert by_ref['e14'].visible is False


def test_shortlist_never_contains_disabled_or_offscreen_targets():
    refs = {item.ref for item in shortlist_browser_targets('stampa', SNAPSHOT, limit=3)}
    assert 'e13' not in refs
    assert 'e14' not in refs


def test_configured_high_margin_target_is_resolved_without_rizzo():
    selector = BrowserTargetSelector(
        rizzo_enabled=False,
        deterministic_min_margin=4.0,
    )
    result = selector.select('Invia questo modulo', SNAPSHOT)
    assert result.target == 'e12'
    assert result.source == 'deterministic'


def test_ambiguous_target_fails_closed_when_rizzo_is_disabled():
    selector = BrowserTargetSelector(rizzo_enabled=False)
    result = selector.select('Scrivi la mail', SNAPSHOT)
    assert result.target is None
    assert result.source == 'fallback'
    assert result.reason == 'browser_rizzo_targeting_disabled'


class FakeRizzo:
    def __init__(self, choice: str):
        self.choice = choice
        self.calls = []

    def choose(self, goal, candidates):
        self.calls.append((goal, tuple(item.ref for item in candidates)))
        return self.choice, 0.91


def test_rizzo_can_only_choose_from_shortlist():
    client = FakeRizzo('e10')
    selector = BrowserTargetSelector(
        rizzo_enabled=True,
        rizzo_client=client,
        deterministic_min_margin=100,
    )
    result = selector.select('Scrivi la mail', SNAPSHOT)
    assert result.target == 'e10'
    assert result.source == 'rizzo'
    assert client.calls


def test_rizzo_out_of_shortlist_choice_fails_closed():
    selector = BrowserTargetSelector(
        rizzo_enabled=True,
        rizzo_client=FakeRizzo('e999'),
        deterministic_min_margin=100,
    )
    result = selector.select('Scrivi la mail', SNAPSHOT)
    assert result.target is None
    assert result.source == 'fallback'
    assert result.reason == 'browser_rizzo_invalid_choice'


def test_rizzo_failure_fails_closed():
    class Broken:
        def choose(self, _goal, _candidates):
            raise TimeoutError('nope')

    selector = BrowserTargetSelector(
        rizzo_enabled=True,
        rizzo_client=Broken(),
        deterministic_min_margin=100,
    )
    result = selector.select('Scrivi la mail', SNAPSHOT)
    assert result.target is None
    assert result.reason == 'browser_rizzo_unavailable:TimeoutError'
