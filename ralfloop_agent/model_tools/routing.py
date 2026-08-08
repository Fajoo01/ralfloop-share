from __future__ import annotations

from dataclasses import dataclass

from src.routing_config import any_trigger_matches

from .registry import ModelToolRegistry


CAPABILITY_TRIGGERS = {
    "deep_web_research": (
        "ricerca approfondita",
        "ricerca web approfondita",
        "deep web research",
        "ricerca online con fonti",
        "confronta fonti online",
        "cerca sul web e verifica",
        "controlla i log",
        "cerca nei log",
        "analizza i log",
        "inspect logs",
    ),
    "semantic_search": ("ricerca semantica", "trova documenti pertinenti", "memoria semantica"),
    "rerank_documents": ("riordina documenti", "rerank", "documenti più pertinenti"),
    "verify_claim_support": ("verifica supporto", "entailment", "contraddizione nli"),
    "extract_entities": ("estrai entità", "persone organizzazioni date importi", "entity extraction"),
    "retrieve_code_context": ("trova codice rilevante", "ricerca nel codice", "code retrieval"),
    "sandboxed_remote_code": (
        "esegui codice remoto in sandbox",
        "analizza codice remoto",
        "sandboxed remote code",
    ),
}


@dataclass(frozen=True)
class ModelToolRoute:
    capability: str | None
    tool_id: str | None
    status: str
    reason: str


def route_model_tool(user_goal: str, registry: ModelToolRegistry) -> ModelToolRoute:
    capability = next(
        (
            name
            for name, triggers in CAPABILITY_TRIGGERS.items()
            if any_trigger_matches(triggers, user_goal)
        ),
        None,
    )
    if capability is None:
        return ModelToolRoute(None, None, "not_applicable", "no_model_tool_capability_match")
    spec = registry.for_capability(capability)
    if spec is None:
        return ModelToolRoute(capability, None, "tool_unavailable", "capability_not_registered")
    status = registry.availability(spec)
    if not status.ok:
        return ModelToolRoute(capability, spec.tool_id, "tool_unavailable", status.status)
    reason = "enabled_builtin_sandbox" if spec.artifact_kind == "builtin" else "enabled_local_snapshot"
    return ModelToolRoute(capability, spec.tool_id, "ready", reason)


__all__ = ["CAPABILITY_TRIGGERS", "ModelToolRoute", "route_model_tool"]
