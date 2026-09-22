"""Deterministic unified-assistant facade over Ralfloop capabilities.

Heavy integrations are loaded lazily so narrow MCP services (PEC, ARCI, etc.)
do not inherit unrelated runtime dependencies merely by importing a submodule.
"""

from importlib import import_module

from .contracts import (
    AssistantFeatureFlags,
    AssistantPlan,
    DomainSpec,
    MemoryItem,
    MemoryNamespace,
    MemoryType,
    PolicyClass,
    SkillSpec,
)

_LAZY_EXPORTS = {
    "UnifiedRegistryFacade": (".registry", "UnifiedRegistryFacade"),
    "StructuredArtifact": (".executor", "StructuredArtifact"),
    "UnifiedDAGExecutor": (".executor", "UnifiedDAGExecutor"),
    "HomeAssistantRESTBackend": (".home_provider", "HomeAssistantRESTBackend"),
    "GoogleWorkspaceRecipientResolver": (".recipient", "GoogleWorkspaceRecipientResolver"),
    "GoogleWorkspaceEmailSearch": (".email_search", "GoogleWorkspaceEmailSearch"),
    "ReadOnlyGoogleWorkspaceGateway": (".email_search", "ReadOnlyGoogleWorkspaceGateway"),
    "FastwebPortalReadOnly": (".fastweb_portal", "FastwebPortalReadOnly"),
    "WhatsAppScopeRegistry": (".whatsapp_web", "WhatsAppScopeRegistry"),
    "WhatsAppWebReadOnly": (".whatsapp_web", "WhatsAppWebReadOnly"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


__all__ = [
    "AssistantFeatureFlags",
    "AssistantPlan",
    "DomainSpec",
    "MemoryItem",
    "MemoryNamespace",
    "MemoryType",
    "PolicyClass",
    "SkillSpec",
    "UnifiedRegistryFacade",
    "StructuredArtifact",
    "UnifiedDAGExecutor",
    "HomeAssistantRESTBackend",
    "GoogleWorkspaceRecipientResolver",
    "GoogleWorkspaceEmailSearch",
    "ReadOnlyGoogleWorkspaceGateway",
    "FastwebPortalReadOnly",
    "WhatsAppScopeRegistry",
    "WhatsAppWebReadOnly",
]
