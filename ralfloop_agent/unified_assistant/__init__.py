"""Deterministic unified-assistant facade over Ralfloop capabilities."""

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
from .registry import UnifiedRegistryFacade
from .executor import StructuredArtifact, UnifiedDAGExecutor
from .home_provider import HomeAssistantRESTBackend
from .recipient import GoogleWorkspaceRecipientResolver
from .email_search import GoogleWorkspaceEmailSearch, ReadOnlyGoogleWorkspaceGateway
from .fastweb_portal import FastwebPortalReadOnly
from .whatsapp_web import WhatsAppScopeRegistry, WhatsAppWebReadOnly

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
