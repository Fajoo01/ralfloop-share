from __future__ import annotations

from pathlib import Path
from typing import Any

from .registry import PROJECT_ROOT, UnifiedRegistryFacade


_ELEMENTS = (
    ("local_tool_registry", "tool_registry", "config/local_arch_tools_v1.json", "Tool/capability metadata", "catalog query", "ToolRecord list", "read", "none", "all", "none", "source_of_truth", "ready"),
    ("skills_registry", "skill_registry", "src/skills.py", "Built-in and external skill manifest adapter", "natural objective", "matched skill metadata", "read", "adapter-dependent", "all", "skill-dependent", "source_of_truth", "ready"),
    ("local_router", "router", "ralfloop_agent/local_arch/router.py", "Non-executing capability route proposal + deterministic validation", "user objective + compact catalog", "CompactRoute", "read/compute", "none", "all", "none", "derived", "ready"),
    ("unified_planner", "planner", "ralfloop_agent/unified_assistant/planner.py", "Deterministic multi-domain artifact plan", "user objective as data", "AssistantPlan DAG", "read/compute", "none", "all", "none", "derived", "ready"),
    ("unified_core", "workflow", "ralfloop_agent/unified_assistant/core.py", "Deterministic domain, policy, tool and verification facade", "structured plan + bounded conversation", "response or approval-bound pending", "read/write", "adapter-dependent", "all", "policy-dependent", "derived", "ready"),
    ("memory_router", "memory_router", "ralfloop_agent/unified_assistant/memory.py", "Authorized namespace selection and inspectable minimum retrieval", "domain + requested namespaces", "working memory + exclusion trace", "read/compute", "none", "all", "none", "source_of_truth filter", "ready"),
    ("gmail_read_only", "workflow", "ralfloop_agent/unified_assistant/email_search.py", "Read-only Gmail semantic query, hydration and scoped synthesis fallback", "organization + concept", "EmailSearchResult + message provenance", "read", "Gmail API read", "email,tiremm", "none", "source_of_truth", "ready"),
    ("email_recipient_resolver", "workflow", "ralfloop_agent/unified_assistant/recipient.py", "Explicit/thread/Gmail participant resolution without invented addresses", "recipient hint", "resolved or ambiguous recipient", "read", "Gmail API read", "email,tiremm", "none", "source_of_truth", "ready"),
    ("telegram_unified_entry", "workflow", "ralfloop_agent/unified_assistant/runtime.py", "Meowgram-compatible side-effect-free route probe and Unified execution", "Telegram message + identity", "tool-backed response or pending", "read/write", "adapter-dependent", "all", "policy-dependent", "derived", "ready"),
    ("local_director", "planner", "ralfloop_agent/local_arch/director.py", "Bounded strategic plan over compact deltas", "verified facts/artifacts", "CompactDirectorPlan", "read/compute", "none", "general", "none", "derived", "ready"),
    ("interaction_router", "router", "ralfloop_agent/integration/interaction_router.py", "Existing interaction routing facade", "interaction", "route", "read/compute", "none", "general", "none", "derived", "ready"),
    ("canonical_capabilities", "capability_registry", "config/domain_capability_mapping.yaml", "Canonical deterministic executors", "structured request", "evidence result", "read/execute", "adapter-dependent", "bandi,personal_relational", "declared", "source_of_truth", "ready"),
    ("model_tools", "tool_registry", "config/model_tools.json", "Local specialist model tools", "strict schema", "strict schema", "read/compute", "local compute", "research,documents,code", "protected for mutation", "source_of_truth", "ready"),
    ("domain_registry", "domain_registry", "domains/registry.json", "Versioned domain manifests", "domain id", "domain package", "read/write", "promotion", "bandi,reasoning", "promotion approval", "source_of_truth", "ready"),
    ("email_reply_domain_v1", "domain", "ralfloop_agent/domains/email_reply.py", "Evidence-backed email truth domain", "mail/thread/knowledge/intent", "domain + validation", "read/compute", "none", "email", "none", "derived", "ready"),
    ("magnolia_email_workflow", "workflow", "src/google_workspace.py", "Gmail read, draft validation, approval scope", "Gmail evidence + objective", "pending approval", "read/write", "Gmail/approval outbox", "email,tiremm", "required", "derived", "ready"),
    ("domain_approval", "approval", "ralfloop_agent/domains/domain_approval_store.py", "Hash-bound Telegram approval", "scope digest + human decision", "bound decision/execution", "read/write", "SQLite/outbox", "all protected", "required", "source_of_truth", "ready"),
    ("session_store", "state_store", "ralfloop_agent/cli/session_store.py", "Atomic bounded conversation sessions", "session record", "session record", "read/write", "local state", "conversation", "none", "source_of_truth", "ready"),
    ("abc_memory", "memory_store", "openshell_backend/skills/abc_memory.py", "Isolated relational events/hypotheses/scores", "ABC events", "timeline/state", "read/write", "local files", "personal_relational", "none", "source_of_truth+derived", "ready"),
    ("cheshire_bridge", "knowledge_source", "integrations/cheshire_cat/ralfloop_bridge/main_plugin.py", "Declarative Qdrant retrieval", "query", "memory points", "read", "loopback HTTP", "knowledge", "none", "source_of_truth", "constrained"),
    ("google_workspace", "provider", "src/mcp_transport.py", "Brokered Google Workspace MCP", "allowlisted operations", "observed provider result", "read/write", "Google API", "email", "write required", "source_of_truth", "ready"),
    ("semantic_judge", "specialist_model", "ralfloop_agent/semantic_judge/core.py", "DS4 HIGH_ONLY email semantic critic", "domain + draft", "structured critique", "compute", "exclusive GPU", "email", "advisory only", "derived", "experimental"),
    ("gpu_scheduler", "lifecycle_manager", "ralfloop_agent/providers/gpu_engine_scheduler.py", "Transactional exclusive GPU handoff", "engine request", "lease/restore", "read/write", "service lifecycle", "models", "policy gated", "source_of_truth", "experimental"),
    ("agentcpm_lifecycle", "lifecycle_manager", "ralfloop_agent/providers/agentcpm_lifecycle.py", "AgentCPM suspend/restore broker", "lifecycle request", "verified state", "read/write", "service lifecycle", "research", "policy gated", "source_of_truth", "experimental"),
    ("bandi_framework", "domain", "ralfloop_agent/domains/capability_registry.py", "Grant rulebook adapters", "grant evidence", "structured decision evidence", "read/compute", "none", "bandi,tiremm", "none", "source_of_truth", "ready"),
    ("system_inspection", "workflow", "ralfloop_agent/integration/system_inspection.py", "Allowlisted infrastructure inspection", "inspection target", "observed health/state", "read", "local inspection", "infrastructure", "writes separate", "source_of_truth", "constrained"),
    ("remote_code", "workflow", "ralfloop_agent/model_tools/remote_code.py", "Sandboxed code proposal/execution", "typed task", "artifact + audit", "compute/write sandbox", "sandbox", "code", "mutation required", "derived", "ready"),
    ("media", "workflow", "ralfloop_agent/local_arch/media.py", "Local media artifact workflow", "artifact refs + objective", "media artifact", "compute/write artifact", "local artifact", "media", "publish required", "derived", "ready"),
    ("local_finance", "workflow", "ralfloop_agent/local_arch/finance.py", "Local financial analysis artifacts", "structured portfolio data", "analysis artifact", "read/compute", "none", "general", "none", "derived", "experimental"),
    ("local_evolver", "workflow", "ralfloop_agent/local_arch/evolver.py", "Sandboxed algorithm evolution", "typed objective", "verified artifact", "compute/write sandbox", "sandbox", "code", "mutation protected", "derived", "experimental"),
    ("home_adapter", "workflow", "ralfloop_agent/unified_assistant/home.py", "Entity allowlist, policy, readback", "natural command + real registry", "verified state/action", "read/write", "Home Assistant", "home", "protected target confirmation", "derived", "constrained"),
    ("home_provider", "provider", "ralfloop_agent/unified_assistant/home_provider.py", "Authenticated Home Assistant REST adapter", "allowlisted entity/service", "state/service result", "read/write", "Home Assistant", "home", "policy-dependent", "source_of_truth", "ready"),
)


def architecture_census(registry: UnifiedRegistryFacade | None = None) -> dict[str, Any]:
    facade = registry or UnifiedRegistryFacade()
    elements = []
    for row in _ELEMENTS:
        (
            identity, kind, relative_path, function, input_value, output_value,
            access, side_effects, domain, approval, truth, declared_status,
        ) = row
        path = PROJECT_ROOT / relative_path
        elements.append({
            "id": identity,
            "type": kind,
            "path": relative_path,
            "function": function,
            "input": input_value,
            "output": output_value,
            "read_write": access,
            "side_effects": side_effects,
            "domain": domain,
            "approval_requirement": approval,
            "source_of_truth": truth,
            "status": declared_status if path.exists() else "constrained",
            "path_exists": path.exists(),
        })
    return {"elements": elements, "unified_registry": facade.inventory()}


__all__ = ["architecture_census"]
