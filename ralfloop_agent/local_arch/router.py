from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .contracts import ACTION_NAMES, CompactRoute, ContractError, ToolRecord
from .store import VersionedCache, canonical_json, sha256_bytes


ROUTER_SCHEMA_VERSION = "router-ir-v1"
ROUTER_POLICY_VERSION = "small-first-v1"
APPROVAL_WORDS = ("pubblica", "invia", "manda", "upload", "telegram", "instagram", "email")
INJECTION_WORDS = ("ignora le regole", "ignore previous", "esegui comunque", "bypass policy")
PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("VR", ("pdf", "scansione", "screenshot", "grafico", "tabella", "locandina", "visual rag")),
    ("AB", ("audiolibro", "audiobook", "epub", "narrazione")),
    ("SV", ("reel", "video social", "microclip", "storyboard")),
    ("SI", ("immagine social", "locandina social", "post social", "social image")),
    ("MC", ("ffmpeg", "componi media", "transcodifica", "sottotitoli")),
    ("AF", ("percorso minimo", "shortest path", "parser ultraveloce", "ottimizza query", "scheduling", "riduci ram", "strategia investimento")),
)


@dataclass(frozen=True)
class RouteDecision:
    route: CompactRoute
    source: str
    candidates: tuple[str, ...]
    latency_ms: float
    cache_hit: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route.as_dict(),
            "source": self.source,
            "candidates": list(self.candidates),
            "latency_ms": round(self.latency_ms, 3),
            "cache_hit": self.cache_hit,
            "error": self.error,
        }


class ToolRegistry:
    def __init__(self, version: int, tools: Iterable[ToolRecord]):
        self.version = version
        self.tools = tuple(tools)
        ids = [tool.name for tool in self.tools]
        if len(ids) != len(set(ids)):
            raise ContractError("duplicate_tool_name")

    @classmethod
    def load(cls, path: str | Path) -> "ToolRegistry":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1 or not isinstance(raw.get("tools"), list):
            raise ContractError("invalid_tool_registry")
        return cls(1, (ToolRecord.from_mapping(item) for item in raw["tools"]))

    def candidates(self, text: str, *, limit: int = 8) -> tuple[ToolRecord, ...]:
        words = set(re.findall(r"[a-z0-9_]+", text.casefold()))
        scored: list[tuple[int, ToolRecord]] = []
        for tool in self.tools:
            haystack = set(re.findall(r"[a-z0-9_]+", " ".join((tool.name, *tool.capabilities)).casefold()))
            score = len(words & haystack)
            if score or tool.compact_id in {"LM", "AU", "RJ"}:
                scored.append((score, tool))
        scored.sort(key=lambda item: (-item[0], item[1].cost_class, item[1].latency_class, item[1].name))
        return tuple(item[1] for item in scored[:limit])

    def available_target(self, action: str, target: str) -> bool:
        if action in {"AP", "AU", "FN", "RJ", "LM", "AF"}:
            return True
        return any(tool.compact_id == action and tool.name == target and tool.availability == "available" for tool in self.tools)

    def compact_catalog(self, candidates: Iterable[ToolRecord]) -> list[dict[str, Any]]:
        return [
            {"a": item.compact_id, "t": item.name, "cap": list(item.capabilities), "av": item.availability}
            for item in candidates
        ]


class FunctionGemmaClient:
    """Loopback-only, non-executing router client."""

    def __init__(self, base_url: str = "http://127.0.0.1:19104", timeout: float = 4.0):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("functiongemma_must_be_loopback")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> bool:
        try:
            with urlopen(f"{self.base_url}/health", timeout=min(self.timeout, 1.0)) as response:
                return response.status == 200
        except (OSError, HTTPError, URLError):
            return False

    def classify(self, text: str, catalog: list[dict[str, Any]]) -> CompactRoute:
        system = (
            "Return one compact route JSON object only. Keys: v,a,t,i,k,c,r. "
            f"a is one of {','.join(ACTION_NAMES)}. Never execute, explain, or write code."
        )
        payload = {
            "model": "functiongemma-router",
            "temperature": 0,
            "max_tokens": 48,
            "tools": [{
                "type": "function",
                "function": {
                    "name": "route",
                    "description": "Choose exactly one Ralf capability route without executing it.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["v", "a", "t", "c"],
                        "properties": {
                            "v": {"type": "integer", "enum": [1]},
                            "a": {"type": "string", "enum": list(ACTION_NAMES)},
                            "t": {"type": "string", "enum": sorted({item["t"] for item in catalog})},
                            "i": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                            "k": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                            "c": {"type": "number", "minimum": 0, "maximum": 1},
                            "r": {"type": "string"}
                        }
                    }
                }
            }],
            "tool_choice": {"type": "function", "function": {"name": "route"}},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"q": text, "catalog": catalog}, ensure_ascii=False, separators=(",", ":"))},
            ],
        }
        request = Request(
            f"{self.base_url}/v1/chat/completions",
            data=canonical_json(payload),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            envelope = json.loads(response.read(65536))
        choice = envelope["choices"][0]["message"]
        tool_calls = choice.get("tool_calls")
        if tool_calls:
            if len(tool_calls) != 1 or tool_calls[0].get("function", {}).get("name") != "route":
                raise ContractError("unexpected_tool_call")
            arguments = tool_calls[0]["function"].get("arguments")
            if isinstance(arguments, dict):
                return CompactRoute.from_mapping(arguments)
            if isinstance(arguments, str):
                return CompactRoute.parse_model_output(arguments)
            raise ContractError("invalid_tool_arguments")
        content = choice.get("content")
        if not isinstance(content, str):
            raise ContractError("missing_route_content")
        return CompactRoute.parse_model_output(content)


class LocalRouter:
    def __init__(
        self,
        registry: ToolRegistry,
        client: FunctionGemmaClient | None = None,
        cache: VersionedCache | None = None,
        *,
        confidence_floor: float = 0.65,
    ):
        self.registry = registry
        self.client = client
        self.cache = cache
        self.confidence_floor = confidence_floor

    def classify(self, text: str, *, dry_run: bool = False) -> RouteDecision:
        started = time.perf_counter_ns()
        normalized = " ".join(text.casefold().split())
        candidates = self.registry.candidates(normalized)
        candidate_names = tuple(item.name for item in candidates)
        deterministic = self._preflight(normalized)
        if deterministic:
            return self._decision(deterministic, "deterministic", candidate_names, started)
        cache_key = self._cache_key(normalized, candidates)
        if self.cache:
            cached = self.cache.get(cache_key)
            if cached:
                route = CompactRoute.from_mapping(cached)
                return self._decision(route, "cache", candidate_names, started, cache_hit=True)
        if self.client is None or not self.client.health():
            route = self._fallback(candidates, "ROUTER_UNAVAILABLE")
            return self._decision(route, "fallback", candidate_names, started, error="router_unavailable")
        try:
            route = self.client.classify(normalized, self.registry.compact_catalog(candidates))
            if route.c < self.confidence_floor:
                route = self._fallback(candidates, "LOW_CONFIDENCE")
            elif not self.registry.available_target(route.a, route.t):
                raise ContractError("hallucinated_or_unavailable_tool")
            if self.cache and route.a not in {"AP"}:
                self.cache.put(cache_key, route.as_dict())
            return self._decision(route, "functiongemma", candidate_names, started)
        except (ContractError, KeyError, OSError, HTTPError, URLError, json.JSONDecodeError) as exc:
            route = self._fallback(candidates, "INVALID_ROUTER_OUTPUT")
            return self._decision(route, "fallback", candidate_names, started, error=str(exc))

    def _preflight(self, text: str) -> CompactRoute | None:
        if not text:
            return CompactRoute(1, "AU", "missing_request", c=1.0, r="MISSING_INPUT")
        if any(term in text for term in INJECTION_WORDS):
            return CompactRoute(1, "RJ", "policy_bypass", c=1.0, r="PROMPT_INJECTION")
        if any(term in text for term in ("manca il file", "richiesta ambigua", "quale documento", "da chiarire")):
            return CompactRoute(1, "AU", "missing_or_ambiguous_input", c=1.0, r="MISSING_INPUT")
        if any(term in text for term in ("finisci", "task completato", "nessuna altra azione", "chiudi il piano")):
            return CompactRoute(1, "FN", "result", c=1.0, r="COMPLETE")
        if any(term in text for term in APPROVAL_WORDS) and any(term in text for term in ("pubblica", "invia", "manda", "upload")):
            return CompactRoute(1, "AP", "external_action", c=1.0, r="PROTECTED_ACTION")
        if any(term in text for term in ("graph solver", "tool tradizionale", "convertitore locale", "cache validata")):
            return CompactRoute(1, "ET", "graph_solver", c=0.98, r="DETERMINISTIC_MATCH")
        if any(term in text for term in ("classifica", "estrai campi", "identifica la lingua")):
            return CompactRoute(1, "SM", "structured_extraction", c=0.98, r="DETERMINISTIC_MATCH")
        if "embedding" in text:
            return CompactRoute(1, "SM", "embeddings", c=0.98, r="DETERMINISTIC_MATCH")
        if any(term in text for term in ("strategico multi step", "conflitto tra worker", "problema nuovo", "architettura complessa")):
            return CompactRoute(1, "LM", "colibri_director", c=0.98, r="STRATEGIC_TASK")
        for action, patterns in PATTERNS:
            if any(pattern in text for pattern in patterns):
                target = {
                    "VR": "visual_rag",
                    "AB": "audiobook_factory",
                    "SV": "social_video",
                    "SI": "social_image",
                    "MC": "media_compose",
                    "AF": self._algorithm_target(text),
                }[action]
                return CompactRoute(1, action, target, c=0.99, r="DETERMINISTIC_MATCH")
        return None

    @staticmethod
    def _algorithm_target(text: str) -> str:
        for phrase, target in (
            ("percorso minimo", "shortest_path"),
            ("shortest path", "shortest_path"),
            ("parser", "parser"),
            ("query", "query_optimizer"),
            ("investimento", "quantitative_strategy"),
            ("scheduling", "scheduler"),
            ("ram", "program_optimization"),
        ):
            if phrase in text:
                return target
        return "program_optimization"

    def _fallback(self, candidates: tuple[ToolRecord, ...], reason: str) -> CompactRoute:
        available = [item for item in candidates if item.availability == "available" and not item.side_effect]
        if available and available[0].compact_id in {"ET", "SM"}:
            return CompactRoute(1, available[0].compact_id, available[0].name, c=0.5, r=reason)
        return CompactRoute(1, "LM", "colibri_director", c=0.0, r=reason)

    def _cache_key(self, text: str, candidates: tuple[ToolRecord, ...]) -> str:
        fields = {
            "input_hash": sha256_bytes(text.encode()),
            "model_hash": "functiongemma-unresolved",
            "config_hash": sha256_bytes(canonical_json(self.registry.compact_catalog(candidates))),
            "schema_version": ROUTER_SCHEMA_VERSION,
            "policy_version": ROUTER_POLICY_VERSION,
            "context_hash": sha256_bytes(canonical_json([item.name for item in candidates])),
        }
        return self.cache.key(fields) if self.cache else sha256_bytes(canonical_json(fields))

    @staticmethod
    def _decision(
        route: CompactRoute,
        source: str,
        candidates: tuple[str, ...],
        started: int,
        *,
        cache_hit: bool = False,
        error: str | None = None,
    ) -> RouteDecision:
        return RouteDecision(route, source, candidates, (time.perf_counter_ns() - started) / 1_000_000, cache_hit, error)
