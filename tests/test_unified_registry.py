from __future__ import annotations

from ralfloop_agent.unified_assistant.inventory import architecture_census
from ralfloop_agent.unified_assistant.contracts import AssistantFeatureFlags
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade


def test_facade_reuses_existing_registries_and_exposes_required_domains():
    registry = UnifiedRegistryFacade()
    required = {
        "email", "home", "tiremm", "bandi", "projects", "research", "documents",
        "knowledge", "calendar", "contacts", "infrastructure", "code", "media",
        "personal_relational", "general_assistant", "runts", "arci", "jellyfin", "education",
    }

    assert required <= set(registry.domains)
    assert registry.source_registries == (
        "config/local_arch_tools_v1.json",
        "config/domain_capability_mapping.yaml",
        "config/model_tools.json",
        "domains/registry.json",
        "src/skills.py",
        "/home/sibilla-cumana/ralfloop_data/skills/*/skill.json",
    )
    assert registry.domain("posta").id == "email"
    assert registry.domain("rsc").id == "personal_relational"
    assert registry.skill("bottazzi_citofono").domains == ("home",)
    assert registry.skill("relational_strategy_calculator").domains == ("personal_relational",)
    assert registry.skill("tiremm_comms").classification == "PROTECTED"
    assert {row["id"] for row in registry.existing_domain_records} == {
        "abc_reasoning",
        "bandi/fondazione_cariplo_nuovi_ponti_culturali_2026",
        "bandi/fondazione_unipolis_act_2026",
        "bandi_framework",
        "calculation_reasoning",
    }
    assert not any(row["routing_eligible"] for row in registry.existing_domain_records)


def test_facade_derives_tools_without_parallel_executor_registry():
    registry = UnifiedRegistryFacade()
    tools = {item.id: item for item in registry.list_tools()}

    assert tools["google_workspace.gmail"].source_registry.endswith("config/local_arch_tools_v1.json")
    gmail_read = tools["google_workspace.gmail.read_only"]
    assert gmail_read.classification == "READ"
    assert gmail_read.side_effect_class == "none"
    assert "google_workspace.gmail.search" in gmail_read.capabilities
    assert tools["abc_formula_loop"].source_registry.endswith("config/domain_capability_mapping.yaml")
    assert tools["deep_web_research_agentcpm_v1"].source_registry.endswith("config/model_tools.json")
    assert tools["home_assistant.adapter"].availability == "available"
    memory = tools["memory.operational.mcp"]
    assert memory.classification == "READ"
    assert "memory.documents.search" in memory.capabilities
    assert "memory.entities.read" in memory.capabilities
    assert tools["arci.read_only.mcp"].classification == "READ"
    assert tools["jellyfin.identity.mcp.write"].classification == "PROTECTED"
    visual = tools["visual.memory.local"]
    assert visual.availability == "constrained:text_regions_only"
    ds4 = tools["deepseek_v4_flash.semantic_critic"]
    assert ds4.capabilities == ("semantic_critic",)
    assert "HIGH_ONLY" in ds4.verification_method
    assert "max_output=128" in ds4.verification_method


def test_capability_map_and_census_are_inspectable():
    registry = UnifiedRegistryFacade()
    rows = registry.capability_map()
    email = next(row for row in rows if row["domain"] == "email" and row["skill"] == "email.compose")
    assert email["policy"] == "CONFIRM_WRITE"
    assert "google_workspace.gmail.draft" in email["capabilities"]

    census = architecture_census(registry)
    required_fields = {
        "id", "type", "path", "function", "input", "output", "read_write",
        "side_effects", "domain", "approval_requirement", "source_of_truth", "status",
    }
    assert census["elements"]
    assert all(required_fields <= set(item) for item in census["elements"])
    assert all(item["path_exists"] for item in census["elements"])


def test_domain_memory_scopes_are_explicit():
    registry = UnifiedRegistryFacade()
    assert {item.value for item in registry.domain("email").allowed_memory_namespaces} == {
        "tiremm", "general_preferences"
    }
    assert {item.value for item in registry.domain("home").allowed_memory_namespaces} == {
        "home", "general_preferences"
    }
    assert {item.value for item in registry.domain("personal_relational").allowed_memory_namespaces} == {
        "personal_relational", "general_preferences"
    }


def test_feature_flags_are_independent_and_default_off(monkeypatch):
    names = (
        "RALFLOOP_EMAIL_ASSISTANT_LIVE",
        "RALFLOOP_HOME_ASSISTANT_LIVE",
        "RALFLOOP_UNIFIED_ASSISTANT",
        "RALFLOOP_SEMANTIC_JUDGE_ENABLED",
        "RALFLOOP_SEMANTIC_JUDGE_ALLOW_NORMAL",
        "RALFLOOP_SEMANTIC_JUDGE_SHADOW",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    assert not any(AssistantFeatureFlags.from_env().model_dump().values())

    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_HOME_ASSISTANT_LIVE", "1")
    flags = AssistantFeatureFlags.from_env()
    assert flags.unified_assistant
    assert flags.home_assistant_live
    assert not flags.email_assistant_live
    assert not flags.semantic_judge_allow_normal
