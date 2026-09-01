from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, timedelta
import json
import os
import re
import time
from typing import Any, Callable, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.google_workspace import GoogleWorkspaceError, GoogleWorkspaceGateway
from src.mcp_transport import MCPClientSession, MCPError, UnixMCPTransport


READ_ONLY_GMAIL_OPERATIONS = frozenset({"search", "read", "threads", "getThread"})
_SEARCH_SIGNAL = re.compile(
    r"\b(?:controlla|cerca|trova|verifica|guarda|leggi|abbiamo\s+ricevuto|ha\s+mai|ci\s+ha|ci\s+aveva)\b",
    re.I,
)
_COMMUNICATION_SIGNAL = re.compile(
    r"\b(?:mail|email|posta|bozz[ae]|scritto|comunicat[oaie]|comunicazioni|avvisat[oaie]|messaggi?|thread)\b",
    re.I,
)
_ECONOMIC_CHANGE = (
    "aumento", "aumenti", "canone", "rimodulazione", "rimodulazioni",
    "modifica", "modifiche",
    "modifica economica", "modifica commerciale", "modifica condizioni",
    "variazione contrattuale", "condizioni contrattuali", "offerta",
)
_SECRET_RE = re.compile(
    r"(?i)(authorization|bearer|cookie|credential|password|secret|token)\s*[:=]\s*\S+"
)


class EmailSearchIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    organization: str = Field(min_length=1, max_length=160)
    concept: str = Field(min_length=1, max_length=160)
    concept_terms: tuple[str, ...] = Field(min_length=1, max_length=16)
    queries: tuple[str, ...] = Field(min_length=1, max_length=8)
    query_sources: tuple[str, ...] = Field(min_length=1, max_length=8)
    temporal_scope: str = Field(default="unspecified", max_length=80)
    after_date: str = Field(default="", max_length=10)
    before_date: str = Field(default="", max_length=10)
    exhaustive_required: bool = False


class SearchQueryProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=1000)
    query_source: str = Field(min_length=1, max_length=80)
    pages_read: int = Field(ge=0)
    results_seen: int = Field(ge=0)
    deduplicated_message_ids: tuple[str, ...] = ()
    continuation_state: str = Field(default="", max_length=500)
    exhausted: bool = False
    failure: str = Field(default="", max_length=160)


class EmailEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(default="", max_length=128)
    sender: str = Field(default="", max_length=320)
    subject: str = Field(default="", max_length=500)
    date: str = Field(default="", max_length=128)
    matched_terms: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    query_sources: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    excerpt: str = Field(default="", max_length=800)
    provenance_ref: str = Field(min_length=1, max_length=240)
    content_role: Literal["data"] = "data"


class EmailSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "FOUND", "NOT_FOUND_IN_SEARCHED_SCOPE", "SEARCH_INCOMPLETE", "CONNECTOR_UNAVAILABLE"
    ]
    evidence_status: Literal["FOUND", "NOT_FOUND_IN_SEARCHED_SCOPE"]
    search_complete: bool
    intent: EmailSearchIntent
    evidence: tuple[EmailEvidence, ...] = Field(default_factory=tuple, max_length=200)
    searched_queries: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    failed_queries: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    query_progress: tuple[SearchQueryProgress, ...] = Field(default_factory=tuple, max_length=8)
    incomplete_reasons: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    searched_scope: str = Field(min_length=1, max_length=1000)
    connector_operations: tuple[str, ...] = Field(default_factory=tuple, max_length=512)
    pages_read: int = Field(default=0, ge=0)
    messages_seen: int = Field(default=0, ge=0)
    messages_hydrated: int = Field(default=0, ge=0)
    dedup_count: int = Field(default=0, ge=0)
    cap_reached: bool = False
    synthesis_fallback: bool = False
    response: str = Field(min_length=1, max_length=6000)


class GmailReadGateway(Protocol):
    account: str

    def invoke(self, operation: str, **arguments: Any) -> Mapping[str, Any]: ...


class ReadOnlyGoogleWorkspaceGateway:
    """Capability-reducing facade: no Gmail mutation method is exposed."""

    def __init__(self, gateway: GoogleWorkspaceGateway) -> None:
        self._gateway = gateway
        self.account = gateway.account

    @property
    def search_continuation_argument(self) -> str | None:
        return self._gateway.search_continuation_argument

    def invoke(self, operation: str, **arguments: Any) -> Mapping[str, Any]:
        if operation not in READ_ONLY_GMAIL_OPERATIONS:
            raise GoogleWorkspaceError("gmail_read_only_operation_denied")
        return self._gateway.invoke(operation, **arguments)


class GoogleWorkspaceReadContext(AbstractContextManager[ReadOnlyGoogleWorkspaceGateway]):
    def __init__(self, socket_path: str, account: str, timeout: float) -> None:
        self.socket_path = socket_path
        self.account = account
        self.timeout = timeout
        self.session: MCPClientSession | None = None

    def __enter__(self) -> ReadOnlyGoogleWorkspaceGateway:
        self.session = MCPClientSession(UnixMCPTransport(self.socket_path), timeout=self.timeout)
        try:
            self.session.__enter__()
            gateway = GoogleWorkspaceGateway(self.session, account=self.account)
            gateway.discover()
            return ReadOnlyGoogleWorkspaceGateway(gateway)
        except Exception:
            self.session.__exit__(None, None, None)
            self.session = None
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.session is not None:
            self.session.__exit__(exc_type, exc, tb)


@dataclass
class _Page:
    after: date | None
    before: date | None
    token: str = ""


@dataclass
class _Progress:
    query: str
    query_source: str
    pages_read: int = 0
    results_seen: int = 0
    message_ids: list[str] | None = None
    continuation_state: str = ""
    exhausted: bool = False
    failure: str = ""

    def __post_init__(self) -> None:
        self.message_ids = []


class GoogleWorkspaceEmailSearch:
    """Bounded Gmail search with explicit exhaustion and evidence preservation."""

    def __init__(
        self,
        gateway_factory: Callable[[], AbstractContextManager[GmailReadGateway]],
        *,
        max_results: int = 50,
        max_hydrate: int = 160,
        max_pages: int = 32,
        max_total_metadata: int = 2_000_000,
        max_elapsed_seconds: float = 60.0,
        synthesizer: Callable[[EmailSearchResult], str] | None = None,
        today_provider: Callable[[], date] = date.today,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.gateway_factory = gateway_factory
        self.max_results = max(1, min(int(max_results), 50))
        self.max_hydrate = max(1, min(int(max_hydrate), 200))
        self.max_pages = max(1, min(int(max_pages), 256))
        self.max_total_metadata = max(10_000, min(int(max_total_metadata), 20_000_000))
        self.max_elapsed_seconds = max(1.0, min(float(max_elapsed_seconds), 600.0))
        self.synthesizer = synthesizer
        self.today_provider = today_provider
        self.monotonic = monotonic

    @classmethod
    def from_environment(cls) -> "GoogleWorkspaceEmailSearch":
        socket_path = os.getenv(
            "RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock"
        )
        account = os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT", "fabio@tiremminnanz.com")
        timeout = float(os.getenv("RALF_GOOGLE_WORKSPACE_MCP_TIMEOUT", "20"))
        return cls(
            lambda: GoogleWorkspaceReadContext(socket_path, account, timeout),
            max_results=_env_int("RALFLOOP_EMAIL_SEARCH_PAGE_SIZE", 50),
            max_hydrate=_env_int("RALFLOOP_EMAIL_SEARCH_MAX_HYDRATE", 160),
            max_pages=_env_int("RALFLOOP_EMAIL_SEARCH_MAX_PAGES", 32),
            max_total_metadata=_env_int("RALFLOOP_EMAIL_SEARCH_MAX_METADATA_BYTES", 2_000_000),
            max_elapsed_seconds=_env_float("RALFLOOP_EMAIL_SEARCH_MAX_SECONDS", 60.0),
        )

    def search(self, request: str) -> EmailSearchResult:
        intent = plan_email_search(request, today=self.today_provider())
        if intent is None:
            raise ValueError("email_search_intent_unresolved")
        operations: list[str] = []
        failed: list[str] = []
        rows: dict[str, Mapping[str, Any]] = {}
        row_sources: dict[str, set[str]] = {}
        progress_rows: list[_Progress] = []
        incomplete_reasons: list[str] = []
        pages_read = 0
        messages_seen = 0
        metadata_bytes = 0
        started = self.monotonic()
        try:
            with self.gateway_factory() as gateway:
                continuation_arg = getattr(gateway, "search_continuation_argument", None)
                for base_query, query_source in zip(intent.queries, intent.query_sources):
                    progress = _Progress(base_query, query_source)
                    progress_rows.append(progress)
                    queue = self._initial_pages(intent)
                    query_failed = False
                    query_incomplete = False
                    while queue:
                        if pages_read >= self.max_pages:
                            incomplete_reasons.append("max_pages_reached")
                            query_incomplete = True
                            progress.continuation_state = f"{len(queue)} page/range pending"
                            break
                        if self.monotonic() - started >= self.max_elapsed_seconds:
                            incomplete_reasons.append("max_elapsed_time_reached")
                            query_incomplete = True
                            progress.continuation_state = f"{len(queue)} page/range pending"
                            break
                        page = queue.pop(0)
                        arguments: dict[str, Any] = {
                            "query": _page_query(base_query, page),
                            "maxResults": self.max_results,
                        }
                        if page.token and continuation_arg:
                            arguments[str(continuation_arg)] = page.token
                        try:
                            result = gateway.invoke("search", **arguments)
                            operations.append("search")
                            pages_read += 1
                            progress.pages_read += 1
                        except (MCPError, GoogleWorkspaceError, OSError, TimeoutError) as exc:
                            query_failed = True
                            query_incomplete = True
                            progress.failure = type(exc).__name__
                            failed.append(base_query)
                            incomplete_reasons.append("query_failed")
                            break
                        messages = result.get("messages") if isinstance(result, Mapping) else None
                        candidates = messages if isinstance(messages, list) else []
                        progress.results_seen += len(candidates)
                        messages_seen += len(candidates)
                        metadata_bytes += len(json.dumps(candidates, ensure_ascii=False, default=str).encode())
                        if metadata_bytes > self.max_total_metadata:
                            incomplete_reasons.append("max_metadata_reached")
                            query_incomplete = True
                            progress.continuation_state = f"{len(queue)} page/range pending"
                            break
                        for row in candidates:
                            if not isinstance(row, Mapping):
                                continue
                            message_id = _first(row, "messageId", "message_id", "id")
                            if message_id and not row.get("error"):
                                if message_id not in progress.message_ids:
                                    progress.message_ids.append(message_id)
                                rows.setdefault(message_id, row)
                                row_sources.setdefault(message_id, set()).add(query_source)
                        next_token = _next_token(result)
                        if next_token and continuation_arg:
                            queue.insert(0, _Page(page.after, page.before, next_token))
                            progress.continuation_state = "connector continuation pending"
                            continue
                        if next_token and not continuation_arg:
                            if _splittable(page):
                                queue[0:0] = _split_page(page)
                                progress.continuation_state = "temporal partition pending"
                            else:
                                incomplete_reasons.append("unsupported_connector_continuation")
                                query_incomplete = True
                            continue
                        if len(candidates) >= self.max_results or _result_size_estimate(result) > len(candidates):
                            if _splittable(page):
                                queue[0:0] = _split_page(page)
                                progress.continuation_state = "temporal partition pending"
                            else:
                                incomplete_reasons.append("result_cap_without_continuation")
                                query_incomplete = True
                            continue
                    if not query_failed and not query_incomplete and not queue:
                        progress.exhausted = True
                        progress.continuation_state = "exhausted"
                if not any(item.pages_read for item in progress_rows):
                    return self._connector_unavailable(intent, operations)
                selected = list(rows.items())
                if len(selected) > self.max_hydrate:
                    incomplete_reasons.append("max_messages_hydrated_reached")
                    selected = selected[: self.max_hydrate]
                evidence: list[EmailEvidence] = []
                hydrated = 0
                for message_id, summary in selected:
                    if self.monotonic() - started >= self.max_elapsed_seconds:
                        incomplete_reasons.append("max_elapsed_time_reached")
                        break
                    try:
                        detail = gateway.invoke("read", messageId=message_id)
                        operations.append("read")
                        hydrated += 1
                    except (MCPError, GoogleWorkspaceError, OSError, TimeoutError):
                        incomplete_reasons.append("message_hydration_failed")
                        continue
                    message = detail.get("message") if isinstance(detail, Mapping) else None
                    if not isinstance(message, Mapping):
                        incomplete_reasons.append("message_hydration_invalid")
                        continue
                    item = _evidence(
                        intent, message_id, summary, message,
                        tuple(sorted(row_sources.get(message_id) or ())),
                    )
                    if item is not None:
                        evidence.append(item)
        except (MCPError, GoogleWorkspaceError, OSError, TimeoutError):
            return self._connector_unavailable(intent, operations)

        complete = not incomplete_reasons and all(item.exhausted for item in progress_rows)
        evidence_status: Literal["FOUND", "NOT_FOUND_IN_SEARCHED_SCOPE"] = (
            "FOUND" if evidence else "NOT_FOUND_IN_SEARCHED_SCOPE"
        )
        status: Literal["FOUND", "NOT_FOUND_IN_SEARCHED_SCOPE", "SEARCH_INCOMPLETE"] = (
            evidence_status if complete else "SEARCH_INCOMPLETE"
        )
        progress = tuple(SearchQueryProgress(
            query=item.query,
            query_source=item.query_source,
            pages_read=item.pages_read,
            results_seen=item.results_seen,
            deduplicated_message_ids=tuple(item.message_ids or ()),
            continuation_state=item.continuation_state,
            exhausted=item.exhausted,
            failure=item.failure,
        ) for item in progress_rows)
        unique_reasons = tuple(dict.fromkeys(incomplete_reasons))
        seed = EmailSearchResult(
            status=status,
            evidence_status=evidence_status,
            search_complete=complete,
            intent=intent,
            evidence=tuple(evidence),
            searched_queries=intent.queries,
            failed_queries=tuple(dict.fromkeys(failed)),
            query_progress=progress,
            incomplete_reasons=unique_reasons,
            searched_scope=_scope_description(intent, complete),
            connector_operations=tuple(operations),
            pages_read=pages_read,
            messages_seen=messages_seen,
            messages_hydrated=hydrated,
            dedup_count=max(0, messages_seen - len(rows)),
            cap_reached=any(reason.startswith("max_") or "cap" in reason for reason in unique_reasons),
            response=_deterministic_response(status, intent, evidence, not complete),
        )
        if self.synthesizer is None:
            return seed
        try:
            response = str(self.synthesizer(seed)).strip()
            if not response:
                raise ValueError("empty_synthesis")
            return seed.model_copy(update={"response": response[:6000]})
        except Exception:
            return seed.model_copy(update={"synthesis_fallback": True})

    def _initial_pages(self, intent: EmailSearchIntent) -> list[_Page]:
        after = _parse_date(intent.after_date)
        before = _parse_date(intent.before_date)
        if intent.exhaustive_required and after is None:
            after = date(1970, 1, 1)
        if (intent.exhaustive_required or after is not None) and before is None:
            before = self.today_provider() + timedelta(days=1)
        return [_Page(after, before)]

    @staticmethod
    def _connector_unavailable(
        intent: EmailSearchIntent, operations: list[str]
    ) -> EmailSearchResult:
        return EmailSearchResult(
            status="CONNECTOR_UNAVAILABLE",
            evidence_status="NOT_FOUND_IN_SEARCHED_SCOPE",
            search_complete=False,
            intent=intent,
            searched_scope="Gmail connector unavailable; no claim about communication history",
            incomplete_reasons=("connector_unavailable",),
            connector_operations=tuple(operations),
            response="Connettore Gmail non disponibile. Non posso verificare le comunicazioni.",
        )


def is_email_search_request(text: str) -> bool:
    return bool(_SEARCH_SIGNAL.search(text) and _COMMUNICATION_SIGNAL.search(text))


def plan_email_search(text: str, *, today: date | None = None) -> EmailSearchIntent | None:
    if not is_email_search_request(text):
        return None
    organization = _extract_organization(text)
    if not organization:
        return None
    folded = text.casefold()
    if any(term in folded for term in _ECONOMIC_CHANGE):
        concept = "economic_or_contract_change"
        terms = _ECONOMIC_CHANGE
    else:
        generic = _generic_concept_terms(text, organization) or ("comunicazione", "messaggio")
        concept = " ".join(generic[:4])
        terms = generic
    org = _gmail_phrase(organization)
    query_terms = " OR ".join(f'"{_gmail_phrase(term)}"' for term in terms[:10])
    queries = (
        f'from:("{org}") ({query_terms})',
        f'"{org}" ({query_terms})',
    )
    temporal_scope, after, before, exhaustive = _temporal_scope(text, today or date.today())
    return EmailSearchIntent(
        organization=organization,
        concept=concept,
        concept_terms=tuple(terms[:16]),
        queries=tuple(dict.fromkeys(queries)),
        query_sources=("sender_identity_and_concept", "organization_and_concept"),
        temporal_scope=temporal_scope,
        after_date=after,
        before_date=before,
        exhaustive_required=exhaustive,
    )


def _temporal_scope(text: str, today: date) -> tuple[str, str, str, bool]:
    folded = text.casefold()
    year = re.search(r"\bnel\s+(20\d{2})\b", folded)
    if year:
        value = int(year.group(1))
        return f"year:{value}", f"{value:04d}-01-01", f"{value + 1:04d}-01-01", True
    recent = re.search(r"\bultim[ioe]\s+(\w+)\s+mes[ei]\b", folded)
    if recent:
        months = _italian_number(recent.group(1))
        if months:
            after = today - timedelta(days=months * 31)
            return f"recent_months:{months}", after.isoformat(), (today + timedelta(days=1)).isoformat(), True
    if (
        re.search(r"\bmai\b", folded)
        or any(signal in folded for signal in ("prima volta", "tutte le comunicazioni", "ci aveva già"))
    ):
        return "available_mailbox_history", "1970-01-01", (today + timedelta(days=1)).isoformat(), True
    if re.search(r"\b(?:ultima|pi[uù]\s+recente)\b", folded):
        return "latest", "", "", False
    if re.search(r"\bquando\b", folded):
        return "available_mailbox_history", "1970-01-01", (today + timedelta(days=1)).isoformat(), True
    return "unspecified", "", "", False


def _extract_organization(text: str) -> str:
    patterns = (
        r"\b(?:bozza|mail|email|messaggio)\s+che\s+(?:ci\s+)?ha\s+(?:mandat[oa]|inviat[oa])\s+(.{1,120}?)(?=[?.!,]|$)",
        r"\b(?:bozza|mail|email|messaggio)\s+(?:pi[uù]\s+recente\s+)?(?:mandat[oa]|inviat[oa])\s+da\s+(.{1,120}?)(?=[?.!,]|$)",
        r"\bse\s+(.{1,120}?)\s+(?:ci\s+)?(?:ha|aveva)\s+(?:mai\s+|gi[aà]\s+)?(?:comunicato|scritto|avvisato)",
        r"\bcomunicazion[ei]\s+(?:di|da)\s+(.{1,120}?)(?=\s+(?:su|sull?[aoe]?|relative|riguardo|per|nel|negli?)\b|[?.!,]|$)",
        r"\bmail\s+(?:di\s+|da\s+)?(.{1,120}?)(?=\s+(?:su|sull?[aoe]?|relative|riguardo|per|nel|negli?)\b|[?.!,]|$)",
        r"^(?:(?:controlla|cerca|trova|verifica|quando)\s+)?(.{1,120}?)\s+ci\s+(?:ha|aveva)\s+(?:mai\s+|gi[aà]\s+)?(?:scritto|comunicato|avvisato)",
    )
    for pattern in patterns:
        match = re.search(pattern, text.strip(), re.I)
        if match:
            value = re.sub(r"^(?:la|le|i|gli|un|una)\s+", "", match.group(1).strip(), flags=re.I)
            return " ".join(value.split())[:160]
    return ""


def _generic_concept_terms(text: str, organization: str) -> tuple[str, ...]:
    stop = {
        "abbiamo", "avvisato", "cerca", "comunicato", "comunicazioni", "controlla",
        "email", "fast", "mail", "messaggio", "ricevuto", "scritto", "trova", "verifica",
        "questa", "questo", "relative", "riguardo", "sempre", "prima", "ultima",
    }
    stop.update(_words(organization))
    values = [word for word in _words(text) if len(word) >= 4 and word not in stop]
    return tuple(dict.fromkeys(values))[:8]


def _evidence(
    intent: EmailSearchIntent,
    message_id: str,
    summary: Mapping[str, Any],
    message: Mapping[str, Any],
    query_sources: tuple[str, ...],
) -> EmailEvidence | None:
    sender = _first(message, "from", "sender") or _first(summary, "from", "sender")
    subject = _first(message, "subject") or _first(summary, "subject")
    body = _first(message, "body", "snippet") or _first(summary, "snippet")
    haystack = " ".join((sender, subject, body)).casefold()
    organization_words = _words(intent.organization)
    if not organization_words or not all(word in haystack for word in organization_words):
        return None
    matched = _matched_terms(intent, haystack)
    if not matched:
        return None
    thread_id = _first(message, "threadId", "thread_id") or _first(summary, "threadId", "thread_id")
    excerpt = _redact(" ".join(body.split()))[:800]
    return EmailEvidence(
        message_id=message_id,
        thread_id=thread_id,
        sender=_redact(sender)[:320],
        subject=_redact(subject)[:500],
        date=_first(message, "date", "receivedAt") or _first(summary, "date", "receivedAt"),
        matched_terms=matched,
        query_sources=query_sources,
        excerpt=excerpt,
        provenance_ref=f"gmail:message:{message_id}",
    )


def _matched_terms(intent: EmailSearchIntent, haystack: str) -> tuple[str, ...]:
    direct = tuple(term for term in intent.concept_terms if term.casefold() in haystack)
    if intent.concept != "economic_or_contract_change":
        return direct
    strong = tuple(term for term in direct if term not in {"offerta", "modifica", "modifiche"})
    if strong:
        return strong
    has_change = any(term in haystack for term in ("modifica", "modifiche"))
    has_contract_context = any(
        term in haystack
        for term in ("canone", "condizioni", "contratto", "contrattuale", "economica", "commerciale")
    )
    return tuple(
        term for term in direct if term in {"modifica", "modifiche"}
    ) if has_change and has_contract_context else ()


def _page_query(base_query: str, page: _Page) -> str:
    parts = [base_query]
    if page.after is not None:
        parts.append("after:" + (page.after - timedelta(days=1)).strftime("%Y/%m/%d"))
    if page.before is not None:
        parts.append("before:" + page.before.strftime("%Y/%m/%d"))
    return " ".join(parts)


def _splittable(page: _Page) -> bool:
    return page.after is not None and page.before is not None and (page.before - page.after).days > 1


def _split_page(page: _Page) -> list[_Page]:
    assert page.after is not None and page.before is not None
    midpoint = page.after + timedelta(days=(page.before - page.after).days // 2)
    return [_Page(page.after, midpoint), _Page(midpoint, page.before)]


def _next_token(result: Mapping[str, Any]) -> str:
    for key in ("nextPageToken", "next_page_token", "cursor", "continuation"):
        value = result.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _result_size_estimate(result: Mapping[str, Any]) -> int:
    for key in ("resultSizeEstimate", "result_size_estimate", "total"):
        try:
            return max(0, int(result.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return 0


def _scope_description(intent: EmailSearchIntent, complete: bool) -> str:
    state = "exhausted" if complete else "bounded/incomplete"
    return (
        f"Gmail account; temporal_scope={intent.temporal_scope}; "
        f"queries={len(intent.queries)}; traversal={state}"
    )


def _deterministic_response(
    status: str,
    intent: EmailSearchIntent,
    evidence: list[EmailEvidence],
    incomplete: bool,
) -> str:
    if evidence:
        prefix = "Ricerca incompleta; ho comunque trovato" if incomplete else "Ho trovato"
        count = "una comunicazione" if len(evidence) == 1 else f"{len(evidence)} comunicazioni"
        lines = [f"{prefix} {count} pertinente di {intent.organization}:" if len(evidence) == 1
                 else f"{prefix} {count} pertinenti di {intent.organization}:"]
        for item in evidence[:8]:
            date_value = item.date or "data non disponibile"
            subject = item.subject or "oggetto non disponibile"
            lines.append(f"- {date_value} — {subject} — {item.sender or 'mittente non disponibile'}")
        lines.append("Risultati basati sullo scope Gmail cercato e sulle evidenze indicate.")
        return "\n".join(lines)
    if status == "SEARCH_INCOMPLETE":
        return "Ricerca Gmail incompleta; nessuna conclusione affidabile sulle comunicazioni storiche."
    return (
        f"Non ho trovato comunicazioni pertinenti di {intent.organization} nello scope Gmail cercato. "
        "Questo non dimostra che non siano mai esistite fuori dallo scope disponibile."
    )


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _italian_number(value: str) -> int:
    words = {"uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5,
             "sei": 6, "sette": 7, "otto": 8, "nove": 9, "dieci": 10, "dodici": 12}
    return int(value) if value.isdigit() else words.get(value, 0)


def _first(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        candidate = value.get(key)
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return ""


def _words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9à-ÿ]+", value.casefold())


def _gmail_phrase(value: str) -> str:
    cleaned = value.replace("\\", " ").replace('"', " ").replace("\r", " ").replace("\n", " ")
    return " ".join(cleaned.split())[:160]


def _redact(value: str) -> str:
    return _SECRET_RE.sub(lambda match: match.group(1) + "=[REDACTED]", value)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


__all__ = [
    "EmailEvidence",
    "EmailSearchIntent",
    "EmailSearchResult",
    "GoogleWorkspaceEmailSearch",
    "GoogleWorkspaceReadContext",
    "READ_ONLY_GMAIL_OPERATIONS",
    "ReadOnlyGoogleWorkspaceGateway",
    "SearchQueryProgress",
    "is_email_search_request",
    "plan_email_search",
]
