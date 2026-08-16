from __future__ import annotations

import pytest

from ralfloop_agent.unified_assistant.contracts import PolicyClass
from ralfloop_agent.unified_assistant.home import (
    HomeCommand,
    HomeEntity,
    HomeEntityRegistry,
    HomeIntentParser,
    HomeWorkflow,
)


def entity(
    entity_id: str,
    name: str,
    aliases: tuple[str, ...],
    area: str,
    device_class: str,
    services: tuple[str, ...],
    *,
    auto: bool = False,
    protected: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
) -> HomeEntity:
    return HomeEntity(
        entity_id=entity_id,
        friendly_name=name,
        aliases=aliases,
        area=area,
        device_class=device_class,
        capabilities=services,
        allowed_services=services,
        auto_write=auto,
        protected=protected,
        minimum=minimum,
        maximum=maximum,
    )


class FakeHomeBackend:
    def __init__(self, states, *, ignore_writes=False, fail_reads=False):
        self.states = dict(states)
        self.ignore_writes = ignore_writes
        self.fail_reads = fail_reads
        self.calls = []

    def read_state(self, entity_id):
        if self.fail_reads:
            raise RuntimeError("offline")
        return self.states[entity_id]

    def call_service(self, service, entity_id, data):
        self.calls.append((service, entity_id, dict(data)))
        if self.ignore_writes:
            return {"ok": True}
        if service == "turn_on":
            self.states[entity_id] = "on"
        elif service == "turn_off":
            self.states[entity_id] = "off"
        elif service == "open_cover":
            self.states[entity_id] = "open"
        elif service == "close_cover":
            self.states[entity_id] = "closed"
        elif service == "set_temperature":
            self.states[entity_id] = {"state": "heat", "temperature": data["temperature"]}
        return {"ok": True}


@pytest.fixture
def entities():
    return (
        entity("light.kitchen", "Luce cucina", ("luce cucina",), "cucina", "light", ("turn_on", "turn_off"), auto=True),
        entity("light.living_main", "Luce soggiorno", ("luce soggiorno",), "soggiorno", "light", ("turn_on", "turn_off"), auto=True),
        entity("light.living_floor", "Piantana soggiorno", ("piantana soggiorno",), "soggiorno", "light", ("turn_on", "turn_off"), auto=True),
        entity("climate.bedroom", "Clima camera", ("clima camera",), "camera", "climate", ("set_temperature", "turn_on", "turn_off"), auto=True, minimum=16, maximum=30),
        entity("sensor.bedroom_temperature", "Temperatura camera", ("camera", "temperatura camera"), "camera", "temperature", ()),
        entity("cover.gate", "Cancello", ("cancello",), "esterno", "gate", ("open_cover", "close_cover"), protected=True),
        entity("lock.front", "Porta ingresso", ("porta ingresso",), "ingresso", "lock", ("open_cover",), protected=True),
    )


def test_auto_write_reads_before_writes_and_verifies(entities):
    backend = FakeHomeBackend({"light.kitchen": "off"})
    workflow = HomeWorkflow(HomeEntityRegistry((entities[0],)), backend)
    command = HomeIntentParser().parse("Accendi la luce in cucina")
    prepared = workflow.prepare(command)

    result = workflow.execute(prepared)

    assert prepared.policy is PolicyClass.AUTO_WRITE
    assert result.status == "verified"
    assert result.before == {"light.kitchen": "off"}
    assert result.after == {"light.kitchen": "on"}
    assert backend.calls == [("turn_on", "light.kitchen", {})]


def test_all_in_area_resolves_multiple_allowlisted_entities(entities):
    backend = FakeHomeBackend({"light.living_main": "on", "light.living_floor": "on"})
    workflow = HomeWorkflow(HomeEntityRegistry((entities[1], entities[2])), backend)
    prepared = workflow.prepare(HomeIntentParser().parse("Spegni tutto in soggiorno"))
    result = workflow.execute(prepared)

    assert result.status == "verified"
    assert set(result.targets) == {"light.living_main", "light.living_floor"}
    assert len(backend.calls) == 2


def test_implicit_heat_statement_is_read_only(entities):
    backend = FakeHomeBackend({"sensor.bedroom_temperature": {"state": "27.1"}})
    workflow = HomeWorkflow(HomeEntityRegistry((entities[4],)), backend)
    prepared = workflow.prepare(HomeIntentParser().parse("Fa caldo in camera"))
    result = workflow.execute(prepared)

    assert prepared.policy is PolicyClass.READ
    assert result.status == "read"
    assert backend.calls == []


def test_climate_range_is_deterministic(entities):
    backend = FakeHomeBackend({"climate.bedroom": {"state": "heat", "temperature": 22}})
    workflow = HomeWorkflow(HomeEntityRegistry((entities[3],)), backend)
    prepared = workflow.prepare(HomeIntentParser().parse("Metti il clima in camera a 34 gradi"))
    result = workflow.execute(prepared)

    assert prepared.policy is PolicyClass.DENY
    assert result.status == "denied"
    assert backend.calls == []


def test_gate_requires_confirmation_before_write(entities):
    backend = FakeHomeBackend({"cover.gate": "closed"})
    workflow = HomeWorkflow(HomeEntityRegistry((entities[5],)), backend)
    prepared = workflow.prepare(HomeIntentParser().parse("Apri il cancello"))

    waiting = workflow.execute(prepared)
    done = workflow.execute(prepared, confirmed=True)

    assert prepared.policy is PolicyClass.CONFIRM_WRITE
    assert waiting.status == "confirmation_required"
    assert waiting.write_calls == 0
    assert done.status == "verified"
    assert len(backend.calls) == 1


def test_access_lock_needs_bound_protected_approval(entities):
    backend = FakeHomeBackend({"lock.front": "closed"})
    workflow = HomeWorkflow(HomeEntityRegistry((entities[6],)), backend)
    command = HomeCommand(operation="open_cover", target_text="porta ingresso")
    prepared = workflow.prepare(command)

    assert prepared.policy is PolicyClass.PROTECTED
    assert workflow.execute(prepared, confirmed=True).status == "protected_approval_required"
    assert workflow.execute(prepared, protected_approval=True).status == "verified"


def test_unknown_or_ambiguous_target_causes_zero_action(entities):
    ambiguous = entity("light.kitchen_aux", "Luce cucina aux", ("luce cucina",), "cucina", "light", ("turn_on",), auto=True)
    backend = FakeHomeBackend({})

    with pytest.raises(ValueError, match="home_target_unresolved"):
        HomeWorkflow(HomeEntityRegistry((entities[0],)), backend).prepare(
            HomeIntentParser().parse("Accendi la luce in garage")
        )
    with pytest.raises(ValueError, match="home_target_ambiguous"):
        HomeWorkflow(HomeEntityRegistry((entities[0], ambiguous)), backend).prepare(
            HomeIntentParser().parse("Accendi la luce in cucina")
        )
    assert backend.calls == []


def test_http_success_without_readback_does_not_claim_success(entities):
    backend = FakeHomeBackend({"light.kitchen": "off"}, ignore_writes=True)
    workflow = HomeWorkflow(HomeEntityRegistry((entities[0],)), backend)
    result = workflow.execute(workflow.prepare(HomeIntentParser().parse("Accendi luce cucina")))

    assert result.status == "command_sent_unverified"
    assert not result.verified
    assert len(backend.calls) == 1


def test_backend_unavailable_fails_closed_before_write(entities):
    backend = FakeHomeBackend({"light.kitchen": "off"}, fail_reads=True)
    workflow = HomeWorkflow(HomeEntityRegistry((entities[0],)), backend)
    result = workflow.execute(workflow.prepare(HomeIntentParser().parse("Accendi luce cucina")))

    assert result.status == "unavailable"
    assert result.write_calls == 0
    assert backend.calls == []
