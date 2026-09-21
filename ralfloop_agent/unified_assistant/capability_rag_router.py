from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import unicodedata
from typing import Iterable

from ralfloop_agent.local_arch.contracts import CompactRoute, ToolRecord
from ralfloop_agent.local_arch.router import FunctionGemmaClient, ToolRegistry

from .contracts import PolicyClass
from .mcp_live_catalog import LiveMCPCatalog
from .registry import UnifiedRegistryFacade


# Prima tranche: skill READ "foglia" che il Core sa già eseguire.
# I workflow strutturali/multi-step e tutte le write restano al planner/policy.
LEAF_READ_SKILLS = frozenset(
    {
        "atm.route",
        "meteo.read",
        "email.search",
        "whatsapp.read",
        "mailchimp.read",
        "fastweb.portal.read",
        "home.read",
        "knowledge.retrieve",
        "runts.context",
        "arci.context",
        "jellyfin.identify",
        "education.tutor",
        "bandi.research",
        "browser.inspect",
    }
)


# Questo è vocabolario del RAG DELLE CAPABILITY, non RAG documentale.
# Le frasi servono solo al recupero top-k. Non decidono l'esecuzione.
CAPABILITY_HINTS: dict[str, tuple[str, ...]] = {
    "atm.route": (
        "atm",
        "mezzi",
        "mezzi pubblici",
        "trasporto pubblico",
        "metro",
        "metropolitana",
        "tram",
        "autobus",
        "bus",
        "portami",
        "come vado",
        "come arrivo",
        "percorso",
    ),
    "meteo.read": (
        "meteo",
        "tempo",
        "che tempo",
        "piove",
        "piovera",
        "pioggia",
        "radar",
        "previsioni",
        "previsione",
        "weather",
    ),
    "email.search": (
        "mail",
        "email",
        "gmail",
        "posta",
        "cerca mail",
        "trova mail",
        "leggi mail",
        "cerca email",
        "trova email",
    ),
    "whatsapp.read": (
        "whatsapp",
        "wapp",
        "messaggi whatsapp",
        "chat whatsapp",
        "cerca messaggi",
        "leggi whatsapp",
    ),
    "mailchimp.read": (
        "mailchimp",
        "campagne",
        "audience",
        "segmenti",
        "tag",
        "newsletter",
        "contatti mailchimp",
    ),
    "fastweb.portal.read": (
        "fastweb",
        "portale fastweb",
        "canone fastweb",
        "fattura fastweb",
        "offerta fastweb",
    ),
    "home.read": (
        "home assistant",
        "domotica",
        "stato luce",
        "stato della luce",
        "temperatura casa",
        "stato dispositivo",
    ),
    "knowledge.retrieve": ("memoria", "documenti memoria", "cosa sappiamo", "ricorda documento"),
    "runts.context": ("runts", "pratica runts", "registro terzo settore", "bilancio runts"),
    "arci.context": ("arci", "tessera arci", "circolo arci", "profilo arci"),
    "jellyfin.identify": ("jellyfin", "film non identificati", "film da identificare", "metadata film"),
    "education.tutor": ("insegnante", "tutor", "spiegami", "quiz", "esercizio didattico"),
    "bandi.research": ("bandi aperti", "bandi disponibili", "opportunita finanziamento", "contributi aps", "finanziamenti tiremm", "grant opportunities"),
    "browser.inspect": ("browser", "playwright", "snapshot browser", "snapshot pagina", "ispeziona pagina web", "leggi pagina web", "schede browser", "tab browser"),
}

# One shared lexical/phrase hit is noise; domain-bearing queries in this
# catalog score >= 8. Keep the floor below that for genuine ambiguous pairs.
MIN_RETRIEVAL_SCORE = 4.0


_STOPWORDS = frozenset(
    {
        "a", "ad", "al", "alla", "alle", "allo",
        "da", "dal", "dalla", "dalle",
        "di", "del", "della", "delle",
        "e", "fa", "i", "il", "in", "la", "le",
        "lo", "mi", "per", "su", "un", "una",
        "che", "cosa", "qual", "quale",
    }
)


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("’", "'")
    return " ".join(value.split())


def _terms(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in re.findall(r"[a-z0-9_.]+", _normalize(value))
        if len(token) > 1 and token not in _STOPWORDS
    )


def _availability_rank(value: str) -> int:
    folded = value.casefold()
    if folded == "available":
        return 0
    if folded == "ready":
        return 1
    if folded == "degraded":
        return 2
    if folded.startswith("constrained"):
        return 3
    return 4


_POLICY_RANK = {
    PolicyClass.READ: 0,
    PolicyClass.AUTO_WRITE: 1,
    PolicyClass.CONFIRM_WRITE: 2,
    PolicyClass.PROTECTED: 3,
    PolicyClass.DENY: 4,
}


@dataclass(frozen=True)
class CapabilityCandidate:
    skill_id: str
    domain: str
    score: float
    capabilities: tuple[str, ...]
    providers: tuple[str, ...]
    policy: PolicyClass = PolicyClass.READ
    status: str = "ready"

    def as_dict(self) -> dict:
        return {
            "skill": self.skill_id,
            "domain": self.domain,
            "score": round(self.score, 3),
            "capabilities": list(self.capabilities),
            "providers": list(self.providers),
            "policy": self.policy.value,
            "status": self.status,
        }


class CapabilityRAGIndex:
    """
    Retrieval sui tool/capability di Ralf.

    Non indicizza documenti utente.
    Non esegue MCP.
    Non decide approval.
    """

    def __init__(
        self, registry: UnifiedRegistryFacade | None = None,
        live_catalog: LiveMCPCatalog | None = None,
    ) -> None:
        self.registry = registry or UnifiedRegistryFacade()
        self.live_catalog = live_catalog or LiveMCPCatalog()
        self.tools = self.registry.list_tools()

        providers: dict[str, list] = {}
        for tool in self.tools:
            for capability in tool.capabilities:
                providers.setdefault(capability, []).append(tool)
        self.providers = providers

    def _best_provider(self, capability: str, skill_policy: PolicyClass) -> str:
        rows = list(self.providers.get(capability, ()))
        if not rows:
            return f"{capability}=>NONE"

        # Privilegio minimo prima di tutto; disponibilità a parità di policy.
        rows.sort(
            key=lambda row: (
                0 if row.classification == skill_policy else 1,
                _POLICY_RANK[row.classification],
                _availability_rank(row.availability),
                row.id,
            )
        )
        row = rows[0]
        return (
            f"{capability}=>{row.id}"
            f"[{row.classification.value}/{row.availability}]"
        )

    def _skill_document(self, skill_id: str) -> tuple[str, tuple[str, ...]]:
        skill = self.registry.skill(skill_id)
        capability_words = tuple(
            capability.replace(".", " ").replace("_", " ")
            for capability in skill.required_capabilities
        )
        chunks = [
            skill.id, skill.id.replace(".", " "), skill.workflow,
            skill.verification_method, *skill.domains,
            *skill.required_capabilities, *capability_words,
        ]
        aliases: list[str] = []
        for index, domain_id in enumerate(skill.domains):
            try:
                domain = self.registry.domain(domain_id)
            except KeyError:
                continue
            chunks.extend((domain.id, domain.description, *domain.aliases, *domain.specialist_capabilities))
            if index == 0:
                aliases.extend(domain.aliases)
        for capability in skill.required_capabilities:
            for provider in self.providers.get(capability, ()):
                chunks.extend((
                    provider.id, *provider.capabilities, provider.input_schema,
                    provider.output_schema, provider.verification_method,
                ))
        chunks.extend(CAPABILITY_HINTS.get(skill_id, ()))
        return " ".join(str(value) for value in chunks if value), tuple(aliases)

    def _rank(
        self, query: str, *, skill_ids: Iterable[str], limit: int, min_score: float,
        expand_catalog: bool = True,
    ) -> tuple[CapabilityCandidate, ...]:
        if not 1 <= limit <= 20:
            raise ValueError("capability_rag_limit_invalid")
        normalized = _normalize(query)
        query_terms = _terms(normalized)
        ranked: list[CapabilityCandidate] = []
        for skill_id in sorted(set(skill_ids)):
            try:
                skill = self.registry.skill(skill_id)
            except KeyError:
                continue
            if not skill.required_capabilities:
                continue
            if expand_catalog:
                document, aliases = self._skill_document(skill_id)
            else:
                hints = CAPABILITY_HINTS.get(skill_id, ())
                document = " ".join((
                    skill.id, *skill.domains, *skill.required_capabilities, *hints,
                ))
                aliases = ()
            overlap = query_terms & _terms(document)
            score = float(len(overlap))
            hints = CAPABILITY_HINTS.get(skill_id, ())
            score += 6.0 * sum(1 for phrase in hints if _normalize(phrase) in normalized)
            score += 6.0 * sum(1 for alias in aliases if _normalize(alias) in normalized)
            prefix = _normalize(skill.id.split(".", 1)[0])
            if prefix and prefix in query_terms:
                score += 4.0
            if skill_id == "atm.route" and "portami" in normalized:
                score += 8.0
            # An explicit communication channel outranks a named organization/domain.
            # Example: "cerca la mail di ARCI" is Gmail evidence about ARCI, not ARCI portal data.
            if query_terms & {"mail", "email", "gmail", "posta"} and skill_id != "email.search":
                score -= 12.0
            if query_terms & {"whatsapp", "wapp"} and skill_id != "whatsapp.read":
                score -= 12.0
            if score < min_score:
                continue
            providers = tuple(
                self._best_provider(cap, skill.classification)
                for cap in skill.required_capabilities
            )
            ranked.append(CapabilityCandidate(
                skill_id=skill.id, domain=skill.domains[0], score=score,
                capabilities=tuple(skill.required_capabilities), providers=providers,
                policy=skill.classification, status=skill.status,
            ))
        ranked.sort(key=lambda row: (-row.score, _POLICY_RANK[row.policy], row.skill_id))
        return tuple(ranked[:limit])

    def discover_live_tools(self, query: str, *, limit: int = 20):
        """Discover currently exposed raw MCP tools; never calls tools/call."""
        return self.live_catalog.discover(query, limit=limit)

    def live_inventory(self):
        """Return live MCP tools plus provider health from tools/list only."""
        return self.live_catalog.snapshot()

    def discover(self, query: str, *, limit: int = 10) -> tuple[CapabilityCandidate, ...]:
        """Search the complete logical MCP/capability catalog without authorizing execution."""
        skill_ids = (
            skill.id for skill in self.registry.skills.values()
            if skill.required_capabilities
        )
        return self._rank(
            query, skill_ids=skill_ids, limit=limit, min_score=2.0,
            expand_catalog=True,
        )

    def retrieve(self, query: str, *, limit: int = 6) -> tuple[CapabilityCandidate, ...]:
        """Retrieve only safe leaf READ skills eligible for automatic routing."""
        return self._rank(
            query, skill_ids=LEAF_READ_SKILLS, limit=limit,
            min_score=MIN_RETRIEVAL_SCORE, expand_catalog=False,
        )


def _record(candidate: CapabilityCandidate) -> ToolRecord:
    digest = hashlib.sha256(
        (
            candidate.skill_id
            + "\x00"
            + "\x00".join(candidate.capabilities)
        ).encode()
    ).hexdigest()

    # FunctionGemma vede skill logiche ET, non i provider MCP concorrenti.
    return ToolRecord(
        compact_id="ET",
        name=candidate.skill_id,
        category="unified_capability",
        capabilities=(
            candidate.skill_id,
            *candidate.capabilities,
        ),
        input_schema_hash=digest,
        output_schema_hash=digest,
        side_effect=False,
        network_requirement="none",
        approval_requirement="none",
        runtime="unified_assistant",
        model=None,
        availability="available",
        cost_class="tiny",
        latency_class="low",
        trust_level="high",
        provenance_policy="capability_rag_then_deterministic_provider_binding",
    )


class CapabilityRAGRouter:
    """
    RAG capability -> top-k skill -> FunctionGemma rerank -> provider binding.

    FunctionGemma non esegue il tool.
    Il planner/core/policy restano autoritativi.
    """

    def __init__(
        self,
        registry: UnifiedRegistryFacade | None = None,
        client: FunctionGemmaClient | None = None,
    ) -> None:
        self.registry = registry or UnifiedRegistryFacade()
        self.index = CapabilityRAGIndex(self.registry)
        self.client = client or FunctionGemmaClient()

    @staticmethod
    def _needs_disambiguation(candidates: tuple[CapabilityCandidate, ...]) -> bool:
        """Spend neural routing only when retrieval is weak or close.

        A single strong hit (score >= 8) or a lead >= 4 points is deterministic
        enough for this leaf-read tranche; close/weak results remain rerankable.
        """
        if not candidates:
            return False
        if candidates[0].score < 8.0:
            return True
        return len(candidates) > 1 and candidates[0].score - candidates[1].score < 4.0

    def route(self, query: str, *, limit: int = 6) -> dict | None:
        candidates = self.index.retrieve(query, limit=limit)
        if not candidates:
            return None

        selected = candidates[0]
        source = "capability_rag"
        confidence = 0.0

        records = tuple(_record(row) for row in candidates)
        tool_registry = ToolRegistry(1, records)

        if self._needs_disambiguation(candidates) and self.client.health():
            try:
                route = self.client.classify(
                    query,
                    tool_registry.compact_catalog(records),
                    tool_registry,
                )
                if (
                    route.a == "ET"
                    and any(row.skill_id == route.t for row in candidates)
                ):
                    selected = next(
                        row for row in candidates if row.skill_id == route.t
                    )
                    source = "functiongemma"
                    confidence = route.c
            except Exception:
                # Shadow router: fail closed verso il risultato RAG,
                # nessuna esecuzione e nessuna modifica di policy.
                source = "capability_rag_functiongemma_failed"

        return {
            "skill": selected.skill_id,
            "domain": selected.domain,
            "source": source,
            "confidence": confidence,
            "providers": list(selected.providers),
            "candidates": [row.as_dict() for row in candidates],
        }


__all__ = [
    "CapabilityCandidate",
    "CapabilityRAGIndex",
    "CapabilityRAGRouter",
]
