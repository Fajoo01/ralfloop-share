from __future__ import annotations

from src.router import route_task


def test_pheromone_routing_is_off_by_default(monkeypatch):
    monkeypatch.delenv("RALF_PHEROMONE_ROUTING", raising=False)

    route = route_task("invia email e scrivi su drive")

    assert route.adaptive_routing is None


def test_shadow_mode_observes_without_changing_deterministic_route(monkeypatch, tmp_path):
    goal = "invia email e scrivi su drive"
    monkeypatch.delenv("RALF_PHEROMONE_ROUTING", raising=False)
    baseline = route_task(goal)

    db = tmp_path / "pheromone.sqlite3"
    monkeypatch.setenv("RALF_PHEROMONE_ROUTING", "shadow")
    monkeypatch.setenv("RALF_PHEROMONE_DB", str(db))
    shadow = route_task(goal)

    assert shadow.mode == baseline.mode
    assert shadow.mcp_used == baseline.mcp_used
    assert shadow.skills_used == baseline.skills_used
    assert shadow.requires_confirmation == baseline.requires_confirmation
    assert shadow.adaptive_routing is not None
    assert shadow.adaptive_routing["selection_applied"] is False
    assert shadow.adaptive_routing["activation_gate"] == "explicit_equivalence_group_required"
    assert {item["candidate"] for item in shadow.adaptive_routing["groups"]["mcp"]["scores"]} == set(shadow.mcp_used)
    assert sum(item["probability"] for item in shadow.adaptive_routing["groups"]["mcp"]["scores"]) == 1.0
    assert not db.exists()
