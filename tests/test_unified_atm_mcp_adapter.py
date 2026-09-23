from __future__ import annotations

import pytest

from ralfloop_agent.unified_assistant.atm_mcp_adapter import ATMMCPReadOnly, _destination, _named_route


def test_imperative_single_destination_is_not_origin():
    assert _destination("Portami da Sonia") == "Sonia"
    assert _destination("Portami dal dentista") == "dentista"
    assert _destination("Portami da Zelig") == "Zelig"


def test_natural_mobility_destination_is_extracted():
    assert _destination("Devo andare da Sonia") == "Sonia"
    assert _destination("Voglio andare al Duomo") == "Duomo"
    assert _destination("Vorrei arrivare in Centrale") == "Centrale"


def test_explicit_origin_destination_remains_named_route():
    assert _named_route("Portami da casa a Sonia") == ("casa", "Sonia")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "come vado da Duomo a Cadorna con ATM?",
            ("Duomo", "Cadorna"),
        ),
        (
            "come vado da Duomo a Cadorna con i mezzi ATM?",
            ("Duomo", "Cadorna"),
        ),
        (
            "come vado da Duomo a Cadorna con i mezzi?",
            ("Duomo", "Cadorna"),
        ),
        (
            "da Duomo a Cadorna",
            ("Duomo", "Cadorna"),
        ),
        (
            "come vado da Porta Venezia a Piazza Abbiategrasso con ATM?",
            ("Porta Venezia", "Piazza Abbiategrasso"),
        ),
    ],
)
def test_named_route_strips_transport_qualifier(
    text: str,
    expected: tuple[str, str],
) -> None:
    assert _named_route(text) == expected



def test_atm_adapter_uses_bounded_realtime_timeout() -> None:
    adapter = ATMMCPReadOnly({})
    assert adapter.socket_timeout_s == 4.0


def test_destination_accepts_ad_preposition() -> None:
    assert _destination("Portami ad Aumai") == "Aumai"
    assert _destination("come vado ad Aumai?") == "Aumai"
