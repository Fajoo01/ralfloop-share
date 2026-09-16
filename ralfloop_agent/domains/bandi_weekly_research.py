from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pypdf import PdfReader

from .source_discovery import SearchProviderUnavailable, SearxngSearchProvider, canonicalize_url
from .source_fetcher import SourceFetcher
from .bandi_semantic_retrieval import (
    AvailableDocument,
    ExpectedDocument,
    assess_document_completeness,
    canonical_url,
    claim_citations_from_evidence,
    discover_official_documents,
    make_evidence,
    retrieve_for_bandi,
)
from ralfloop_agent.model_tools import ModelToolManager, ModelToolRegistry


SCHEMA_VERSION = 3
DEFAULT_STATE_DIR = Path.home() / ".local/state/ralfloop/bandi_weekly_deep_research"
DEFAULT_SEARXNG_URL = "http://127.0.0.1:8889"
DEFAULT_ENGINES = "bing,startpage,duckduckgo"
CALL_TERMS = ("bando", "avviso", "call", "grant", "contribut", "finanziament", "subgrant", "regrant")
ELIGIBILITY_TERMS = ("aps", "ets", "terzo settore", "non profit", "no profit", "associazion", "runts")
APPLICATION_TERMS = (
    "presentare domanda",
    "presentazione delle domande",
    "presentazione della domanda",
    "inviare la domanda",
    "candidature",
    "domanda di partecipazione",
    "termine per la presentazione",
)
NON_CALL_TITLE_TERMS = (
    "elenco dei beneficiari",
    "elenco delle associazioni beneficiarie",
    "graduatoria",
    "ammessi al finanziamento",
    "associazioni beneficiarie",
    "deliberazione",
    "delibera di giunta",
    "decreto n.",
    "commissione",
    "faq ",
    "home page",
    "argomenti",
    "cessazione",
    "piano operativo",
    "servizi online",
)
THEME_GROUPS = {
    "education_youth": ("educazione", "doposcuola", "minori", "giovani", "scuola", "povertà educativa"),
    "culture_interculture": ("cultura", "intercultura", "laboratori", "cittadinanza"),
    "social_inclusion": ("inclusione", "coesione", "fragilità", "welfare", "sociale"),
    "territorial_regeneration": ("rigenerazione", "quartiere", "spazi", "territoriale", "municipio"),
    "environment_energy": ("ambiente", "energia", "clima", "sostenibilità"),
}
PRIMARY_DOMAINS = {
    "comune.milano.it",
    "servizi.comune.milano.it",
    "cittametropolitana.mi.it",
    "regione.lombardia.it",
    "bandi.regione.lombardia.it",
    "fondazionecariplo.it",
    "fondazionecomunitamilano.org",
    "fondazionevodafone.it",
    "fondazionesnam.it",
    "fondazioneconilsud.it",
    "csvlombardia.it",
    "acri.it",
    "funding-tenders.ec.europa.eu",
    "ec.europa.eu",
}
DIRECT_DISCOVERY_URLS = (
    ("regione_terzo_settore", "https://www.bandi.regione.lombardia.it/servizi/servizio/bandi/terzo-settore-volontariato"),
    ("regione_clima", "https://www.bandi.regione.lombardia.it/servizi/servizio/bandi/ambiente-territorio/clima"),
    ("regione_politiche_sociali", "https://www.bandi.regione.lombardia.it/servizi/servizio/bandi/politiche-sociali"),
    ("regione_istruzione", "https://www.bandi.regione.lombardia.it/servizi/servizio/bandi/istruzione-formazione-lavoro"),
    ("regione_cultura", "https://www.bandi.regione.lombardia.it/servizi/servizio/bandi/cultura-turismo-sport"),
)
MAX_DISCOVERED_DETAILS_PER_LISTING = 5


QUERY_FAMILIES = (
    ("municipio", "site:comune.milano.it Municipio 2 bando associazioni contributi 2026"),
    ("comune", "site:comune.milano.it bando contributi APS ETS cultura giovani inclusione 2026"),
    ("metropolitana", "site:cittametropolitana.mi.it bando enti terzo settore associazioni 2026"),
    ("regione", "site:regione.lombardia.it bandi enti terzo settore giovani cultura inclusione 2026"),
    ("regione", "site:bandi.regione.lombardia.it APS ETS educazione ambiente 2026"),
    ("nazionale", "site:lavoro.gov.it avviso enti terzo settore RUNTS 2026"),
    ("nazionale", "site:politichegiovanili.gov.it avviso giovani enti terzo settore 2026"),
    ("nazionale", "site:cultura.gov.it bando associazioni cultura inclusione 2026"),
    ("fondazioni", "site:fondazionecariplo.it bandi 2026 welfare cultura ambiente giovani"),
    ("fondazioni", "site:fondazionecomunitamilano.org bando 2026 associazioni"),
    ("corporate", "site:fondazionevodafone.it bando 2026 terzo settore"),
    ("corporate", "site:fondazionesnam.it bando 2026 associazioni inclusione"),
    ("europeo", "site:funding-tenders.ec.europa.eu call youth inclusion culture 2026 nonprofit"),
    ("europeo", "site:ec.europa.eu funding call social innovation youth 2026"),
    ("reti", "site:csvlombardia.it bandi opportunità associazioni 2026"),
    ("subgrant", "subgrant regranting associazioni Italia 2026 bando fonte ufficiale"),
)


@dataclass
class WeeklyConfig:
    state_dir: Path = DEFAULT_STATE_DIR
    searxng_url: str = DEFAULT_SEARXNG_URL
    engines: str = DEFAULT_ENGINES
    timeout_sec: float = 18.0
    max_results_per_query: int = 5
    max_primary_fetches: int = 45
    max_bytes: int = 15_000_000
    semantic_retrieval_enabled: bool = True
    semantic_minimum_documents: int = 2
    semantic_minimum_tokens: int = 4_000
    semantic_top_k: int = 5
    max_attachments_per_call: int = 6
    glm_review_auto_enqueue: bool = False
    glm_review_max_jobs: int = 3

    @classmethod
    def from_env(cls) -> "WeeklyConfig":
        return cls(
            state_dir=Path(os.getenv("BANDI_WEEKLY_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser(),
            searxng_url=os.getenv("RALFLOOP_SEARXNG_URL", DEFAULT_SEARXNG_URL),
            engines=os.getenv("RALFLOOP_SEARXNG_ENGINES", DEFAULT_ENGINES),
            timeout_sec=float(os.getenv("BANDI_WEEKLY_TIMEOUT_SEC", "18")),
            max_results_per_query=int(os.getenv("BANDI_WEEKLY_RESULTS_PER_QUERY", "5")),
            max_primary_fetches=int(os.getenv("BANDI_WEEKLY_MAX_PRIMARY_FETCHES", "45")),
            max_bytes=int(os.getenv("BANDI_WEEKLY_MAX_BYTES", "15000000")),
            semantic_retrieval_enabled=os.getenv("BANDI_SEMANTIC_RETRIEVAL_ENABLED", "1") == "1",
            semantic_minimum_documents=int(os.getenv("BANDI_SEMANTIC_MIN_DOCUMENTS", "2")),
            semantic_minimum_tokens=int(os.getenv("BANDI_SEMANTIC_MIN_TOKENS", "4000")),
            semantic_top_k=int(os.getenv("BANDI_SEMANTIC_TOP_K", "5")),
            max_attachments_per_call=int(os.getenv("BANDI_MAX_ATTACHMENTS_PER_CALL", "6")),
            glm_review_auto_enqueue=os.getenv("BANDI_GLM_REVIEW_AUTO_ENQUEUE", "0") == "1",
            glm_review_max_jobs=int(os.getenv("BANDI_GLM_REVIEW_MAX_JOBS", "3")),
        )


@dataclass
class RunMetrics:
    sources_discovered: int = 0
    primary_sources_opened: int = 0
    calls_examined: int = 0
    new_calls: int = 0
    updated_calls: int = 0
    high_priority_calls: int = 0
    duplicates_removed: int = 0
    failed_sources: int = 0
    duration_sec: float = 0.0


@dataclass
class RunResult:
    schema_version: int
    run_id: str
    started_at: str
    finished_at: str
    ok: bool
    partial: bool
    status: str
    organization_profile: dict[str, Any]
    opportunities: list[dict[str, Any]] = field(default_factory=list)
    discarded: list[dict[str, Any]] = field(default_factory=list)
    sources_checked: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    report_json: str = ""
    report_markdown: str = ""


def organization_profile() -> dict[str, Any]:
    return {
        "name": "Tiremm Innanz APS",
        "legal_form": "APS",
        "runts_registered": True,
        "territory": ["Milano", "Municipio 2", "Lombardia"],
        "themes": [item for values in THEME_GROUPS.values() for item in values],
        "eligible_funders": ["public", "foundation", "corporate_philanthropy", "EU", "subgrant"],
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80] or "unknown-call"


def _canonical_key(funding_body: str, title: str, url: str) -> str:
    raw = "\n".join((funding_body.strip().lower(), _slug(title), canonicalize_url(url)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _domain(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def _is_primary_domain(url: str) -> bool:
    domain = _domain(url)
    if not domain:
        return False
    if any(domain == item or domain.endswith("." + item) for item in PRIMARY_DOMAINS):
        return True
    if domain.endswith((".gov.it", ".europa.eu")):
        return True
    return any(token in domain for token in ("comune", "regione", "minister", "fondazione", "cittametropolitana"))


def _funding_body(url: str, text: str) -> str:
    domain = _domain(url)
    known = {
        "comune.milano.it": "Comune di Milano",
        "cittametropolitana.mi.it": "Città Metropolitana di Milano",
        "regione.lombardia.it": "Regione Lombardia",
        "fondazionecariplo.it": "Fondazione Cariplo",
        "fondazionecomunitamilano.org": "Fondazione di Comunità Milano",
        "fondazionevodafone.it": "Fondazione Vodafone",
        "fondazionesnam.it": "Fondazione Snam",
        "csvlombardia.it": "CSV Lombardia",
        "funding-tenders.ec.europa.eu": "Commissione europea",
        "ec.europa.eu": "Commissione europea",
    }
    for suffix, label in known.items():
        if domain == suffix or domain.endswith("." + suffix):
            return label
    match = re.search(r"(?:ente|promosso da|a cura di)\s*[:\-]?\s*([^\n.]{3,90})", text, re.I)
    return match.group(1).strip() if match else domain


def _read_source(cache_path: str, content_type: str) -> tuple[str, str]:
    path = Path(cache_path)
    raw = path.read_bytes()
    if content_type == "application/pdf":
        try:
            reader = PdfReader(path)
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:120])
            return text[:1_500_000], "pdf"
        except Exception as exc:
            return "", f"pdf_parse_error:{type(exc).__name__}"
    decoded = raw.decode("utf-8", errors="replace")
    if content_type == "text/html":
        soup = BeautifulSoup(decoded, "html.parser")
        for node in soup(("script", "style", "noscript")):
            node.decompose()
        return soup.get_text("\n", strip=True)[:1_500_000], "html"
    return decoded[:1_500_000], "text"


def _stable_id(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16].upper()


def _text_chunks(text: str, *, limit: int = 1_800) -> list[str]:
    paragraphs = [re.sub(r"\s+", " ", item).strip() for item in re.split(r"\n{2,}", text)]
    paragraphs = [item for item in paragraphs if item]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        while len(paragraph) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        candidate = f"{current}\n{paragraph}".strip() if current else paragraph
        if len(candidate) > limit and current:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or ([text[:limit]] if text else [])


def _source_evidence(fetched: Any, text: str, document_type: str) -> tuple[list[Any], AvailableDocument]:
    url = canonical_url(fetched.final_url or fetched.url)
    source_id = _stable_id("S", url)
    document_id = _stable_id("D", f"{url}\n{fetched.checksum}")
    source_type = "pdf" if fetched.content_type == "application/pdf" else "web"
    rows = []
    content_path = Path(fetched.cache_path) / "content"
    if source_type == "pdf":
        try:
            reader = PdfReader(content_path)
            ordinal = 0
            for page_number, page in enumerate(reader.pages[:120], 1):
                for page_chunk in _text_chunks(page.extract_text() or ""):
                    rows.append(
                        make_evidence(
                            source_id=source_id,
                            document_id=document_id,
                            canonical_url_value=url,
                            source_type="pdf",
                            page=page_number,
                            anchor=f"page:{page_number}",
                            text=page_chunk,
                            ordinal=ordinal,
                        )
                    )
                    ordinal += 1
        except Exception:
            rows = []
    if not rows:
        rows = [
            make_evidence(
                source_id=source_id,
                document_id=document_id,
                canonical_url_value=url,
                source_type=source_type,
                section=f"chunk-{ordinal + 1}",
                anchor=f"chunk:{ordinal + 1}",
                text=chunk,
                ordinal=ordinal,
            )
            for ordinal, chunk in enumerate(_text_chunks(text))
        ]
    return rows, AvailableDocument(document_id, document_type, url, fetched.checksum)


def _raw_html(fetched: Any) -> str | None:
    if fetched.content_type != "text/html" or not fetched.cache_path:
        return None
    try:
        return (Path(fetched.cache_path) / "content").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _discover_detail_links(raw_html: str | None, base_url: str, *, limit: int = MAX_DISCOVERED_DETAILS_PER_LISTING) -> list[dict[str, str]]:
    if not raw_html:
        return []
    soup = BeautifulSoup(raw_html, "html.parser")
    base_host = _domain(base_url)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in soup.find_all("a", href=True):
        href = canonical_url(urllib.parse.urljoin(base_url, str(node.get("href") or "")))
        if _domain(href) != base_host:
            continue
        path = urllib.parse.urlparse(href).path
        if "/servizi/servizio/bandi/dettaglio/" not in path and "/servizi/servizio/catalogo/dettaglio/" not in path:
            continue
        if href in seen:
            continue
        title = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if not title or title.casefold() in {"scopri di più", "fai domanda"}:
            continue
        seen.add(href)
        out.append({"url": href, "title": title[:500]})
        if len(out) >= limit:
            break
    return out


def _default_semantic_invoke() -> Any:
    manager = ModelToolManager(ModelToolRegistry.load())

    def invoke(query: str, documents: list[dict[str, str]]) -> dict[str, Any]:
        return manager.invoke(
            "semantic_retriever_v1", {"query": query, "documents": documents}
        ).model_dump(mode="json")

    return invoke


SEMANTIC_BANDI_QUERY = (
    "beneficiari APS ETS RUNTS ammissibilità territorio scadenza contributo minimo massimo "
    "cofinanziamento partenariato durata spese escluse documenti richiesti"
)


MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}


def _extract_dates(text: str) -> list[date]:
    found: set[date] = set()
    for day, month, year in re.findall(r"\b([0-3]?\d)[/.-]([01]?\d)[/.-](20\d{2})\b", text):
        try:
            found.add(date(int(year), int(month), int(day)))
        except ValueError:
            pass
    month_pattern = "|".join(MONTHS)
    for day, month, year in re.findall(rf"\b([0-3]?\d)\s+({month_pattern})\s+(20\d{{2}})\b", text, re.I):
        try:
            found.add(date(int(year), MONTHS[month.lower()], int(day)))
        except ValueError:
            pass
    return sorted(found)


def _extract_amounts(text: str) -> list[str]:
    patterns = (
        r"(?:€|euro)\s*[\d.]+(?:,\d{1,2})?",
        r"[\d.]+(?:,\d{1,2})?\s*(?:€|euro)",
        r"\d+(?:[.,]\d+)?\s+milion[ei]\s+di\s+euro",
    )
    values: list[str] = []
    for pattern in patterns:
        values.extend(re.findall(pattern, text, re.I))
    return list(dict.fromkeys(re.sub(r"\s+", " ", item).strip() for item in values))[:12]


def _evidence_snippets(text: str, terms: tuple[str, ...], limit: int = 4) -> list[str]:
    compact = re.sub(r"\s+", " ", text)
    out: list[str] = []
    for term in terms:
        match = re.search(rf".{{0,100}}\b{re.escape(term)}\w*.{{0,180}}", compact, re.I)
        if match:
            out.append(match.group(0).strip())
        if len(out) >= limit:
            break
    return out


def _extract_title(search_title: str, text: str) -> str:
    title = re.sub(r"\s+", " ", search_title).strip()
    if title and len(title) >= 8:
        return title[:240]
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) >= 12 and any(term in line.lower() for term in CALL_TERMS):
            return line[:240]
    return title or "Titolo non estratto"


def _territory(text: str, url: str) -> str:
    low = text.lower()
    domain = _domain(url)
    if "comune.milano.it" in domain or "cittametropolitana.mi.it" in domain:
        return "Milano Municipio 2" if "municipio 2" in low else "Milano"
    if "regione.lombardia.it" in domain:
        return "Lombardia"
    if "municipio 2" in low:
        return "Milano Municipio 2"
    if "milano" in low or "milano" in domain:
        return "Milano"
    if "lombardia" in low or "lombardia" in domain:
        return "Lombardia"
    if domain.endswith(".gov.it") or "territorio nazionale" in low:
        return "Italia"
    if domain.endswith(".europa.eu") or "european union" in low:
        return "Unione europea"
    return "non determinato"


DATE_TOKEN = r"(?:[0-3]?\d[/.-][01]?\d[/.-]20\d{2}|[0-3]?\d\s+(?:" + "|".join(MONTHS) + r")\s+20\d{2})"


def _labeled_deadline(text: str, today: date) -> date | None:
    compact = re.sub(r"\s+", " ", text)
    closing_labels = (
        r"scade(?:\s+il)?",
        r"data\s+scadenza",
        r"data\s+chiusura",
        r"termina(?:no)?(?:\s+il)?",
        r"entro\s+e\s+non\s+oltre",
        r"termine(?:\s+ultimo)?\s+(?:per\s+la\s+)?presentazione\s+(?:delle|della)\s+domand[ae]",
    )
    generic_labels = (r"scadenza", r"termine(?:\s+ultimo)?(?:\s+per)?")
    for labels in (closing_labels, generic_labels):
        dates: set[date] = set()
        for label in labels:
            for match in re.finditer(rf"{label}[^0-9]{{0,120}}({DATE_TOKEN})", compact, re.I):
                dates.update(_extract_dates(match.group(1)))
        future = sorted(item for item in dates if item >= today)
        if future:
            return future[0]
        if dates:
            return max(dates)
    return None


def _status_and_deadline(text: str, today: date) -> tuple[str, str | None, int | None]:
    low = text.lower()
    deadline = _labeled_deadline(text, today)
    days = (deadline - today).days if deadline else None
    strong_closed = any(
        term in low
        for term in (
            "bando chiuso",
            "avviso chiuso",
            "non più possibile presentare",
            "stato: aggiudicato",
            "stato aggiudicato",
        )
    )
    weak_closed = any(term in low for term in ("scaduto", "approvazione della graduatoria"))
    if strong_closed:
        status = "closed"
    elif deadline and deadline >= today:
        status = "open"
    elif deadline and deadline < today:
        status = "expired"
    elif weak_closed:
        status = "closed"
    elif any(term in low for term in ("prossima apertura", "sarà pubblicato", "annunciato")):
        status = "announced"
    elif "aperto" in low and any(term in low for term in APPLICATION_TERMS):
        status = "open"
    else:
        status = "unknown"
    return status, deadline.isoformat() if deadline else None, days


def _eligibility_evidence(text: str, limit: int = 4) -> list[str]:
    compact = re.sub(r"\s+", " ", text)
    labels = (
        r"beneficiari(?:\s+ammessi)?",
        r"soggetti\s+ammessi",
        r"chi\s+può\s+partecipare",
        r"possono\s+(?:partecipare|presentare\s+domanda)",
        r"destinatari",
    )
    eligibility = r"(?:APS|ETS|RUNTS|enti\s+del\s+Terzo\s+Settore|non\s+profit|no\s+profit|associazion\w*)"
    out: list[str] = []
    for label in labels:
        for match in re.finditer(rf"({label}.{{0,520}}?{eligibility}.{{0,180}})", compact, re.I):
            out.append(match.group(1).strip())
            if len(out) >= limit:
                return out
    return out


def _call_rejection_reason(search_row: dict[str, Any], text: str, today: date) -> str | None:
    title = re.sub(r"\s+", " ", str(search_row.get("title", ""))).strip().lower()
    snippet = re.sub(r"\s+", " ", str(search_row.get("snippet", ""))).strip().lower()
    lead = " ".join((title, snippet, text[:80_000].lower()))
    if any(term in title for term in NON_CALL_TITLE_TERMS):
        return "historical_result_or_non_call_document"
    title_or_snippet = " ".join((title, snippet))
    path = urllib.parse.urlparse(str(search_row.get("url") or "")).path
    official_detail = _is_primary_domain(str(search_row.get("url") or "")) and (
        "/servizi/servizio/bandi/dettaglio/" in path
        or "/servizi/servizio/catalogo/dettaglio/" in path
    )
    explicit_call = official_detail or any(term in title_or_snippet for term in CALL_TERMS) or any(
        term in text[:30_000].lower() for term in ("bando pubblico", "avviso pubblico", "invito a presentare", "chi può partecipare", "come partecipare")
    )
    if not explicit_call:
        return "primary_source_not_call_like"
    status, _, _ = _status_and_deadline(text, today)
    if status in {"closed", "expired"}:
        return "call_not_actionable"
    if status not in {"open", "announced"}:
        return "call_status_not_verified"
    if not _eligibility_evidence(lead):
        return "beneficiary_eligibility_not_verified"
    return None


def _themes(text: str) -> list[str]:
    low = text.lower()
    return [name for name, terms in THEME_GROUPS.items() if any(term in low for term in terms)]


def _project_candidate(themes: list[str]) -> str:
    mapping = {
        "education_youth": "Doposcuola e laboratori educativi per minori e giovani",
        "culture_interculture": "Laboratori culturali e interculturali di quartiere",
        "social_inclusion": "Percorsi territoriali di inclusione e cittadinanza",
        "territorial_regeneration": "Rigenerazione sociale di spazi e reti nel Municipio 2",
        "environment_energy": "Educazione ambientale ed energia di comunità",
    }
    return mapping.get(themes[0], "Attività territoriale APS coerente col bando") if themes else "Da definire dopo verifica requisiti"


def _tiremm_eligibility_fit(evidence: list[str]) -> tuple[str, str]:
    text = " ".join(evidence).casefold()
    if not text:
        return "needs_review", "beneficiary_evidence_missing"
    strong = any(token in text for token in ("aps", "ets", "runts", "terzo settore", "non profit", "no profit"))
    restricted = (
        ("pro loco", "pro_loco_only"),
        ("associazioni sportive dilettantistiche", "asd_only"),
        ("associazione sportiva dilettantistica", "asd_only"),
    )
    for token, reason in restricted:
        if token in text:
            return "ineligible", reason
    if strong:
        return "compatible", "aps_ets_explicit"
    if "associazion" in text:
        return "needs_review", "generic_association_only"
    return "needs_review", "beneficiary_class_unclear"


def _score(opportunity: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    rules: list[dict[str, Any]] = []
    evidence_rows = list(opportunity.get("beneficiaries_evidence", []) or [])
    fit, fit_reason = _tiremm_eligibility_fit(evidence_rows)
    points = 25 if fit == "compatible" else 8 if fit == "needs_review" else 0
    rules.append({"rule": "aps_ets_eligibility", "points": points, "reason": fit_reason})
    territory = opportunity.get("territory", "")
    territory_points = {"Milano Municipio 2": 20, "Milano": 18, "Lombardia": 15, "Italia": 10, "Unione europea": 6}.get(territory, 2)
    rules.append({"rule": "territorial_fit", "points": territory_points, "reason": territory})
    theme_points = min(20, 8 * len(opportunity.get("themes", [])))
    rules.append({"rule": "activity_fit", "points": theme_points, "reason": opportunity.get("themes", [])})
    money_points = 10 if opportunity.get("amounts") else 4
    rules.append({"rule": "economic_realism", "points": money_points, "reason": "amount_found" if opportunity.get("amounts") else "unknown"})
    partner_required = opportunity.get("partnership_required") is True
    capacity_points = 5 if partner_required else 10
    rules.append({"rule": "organizational_capacity", "points": capacity_points, "reason": "partner_required" if partner_required else "no_explicit_partner_gate"})
    days = opportunity.get("days_to_deadline")
    if opportunity.get("status") in {"closed", "expired"}:
        time_points = 0
    elif days is None:
        time_points = 4
    elif days > 30:
        time_points = 10
    elif days >= 14:
        time_points = 7
    elif days >= 0:
        time_points = 3
    else:
        time_points = 0
    rules.append({"rule": "time_remaining", "points": time_points, "reason": days})
    completeness = sum(bool(opportunity.get(field)) for field in ("deadline", "beneficiaries_evidence", "amounts"))
    completeness_points = 5 if completeness == 3 else completeness
    rules.append({"rule": "source_completeness", "points": completeness_points, "reason": completeness})
    score = max(0, min(100, sum(int(item["points"]) for item in rules)))
    if opportunity.get("tiremm_compatibility") == "ineligible":
        score = min(score, 20)
    elif opportunity.get("status") in {"closed", "expired"}:
        score = min(score, 39)
    elif opportunity.get("status") == "unknown":
        score = min(score, 59)
    return score, rules


def _priority(score: int) -> str:
    if score >= 80:
        return "HIGH"
    if score >= 60:
        return "REVIEW"
    if score >= 40:
        return "WEAK"
    return "NOT_PRIORITY"


def _build_opportunity(search_row: dict[str, Any], fetched: Any, text: str, today: date) -> dict[str, Any]:
    url = canonicalize_url(fetched.final_url or fetched.url)
    title = _extract_title(search_row.get("title", ""), text)
    funding_body = _funding_body(url, text)
    status, deadline, days = _status_and_deadline(text, today)
    themes = _themes(text)
    beneficiary_evidence = _eligibility_evidence(text)
    eligibility_fit, eligibility_reason = _tiremm_eligibility_fit(beneficiary_evidence)
    partnership_evidence = _evidence_snippets(text, ("partenariato", "partnership", "capofila", "partner"), 2)
    document_evidence = _evidence_snippets(text, ("statuto", "bilancio", "runts", "formulario", "allegato"), 4)
    opportunity: dict[str, Any] = {
        "call_key": _canonical_key(funding_body, title, url),
        "canonical_call_id": _slug(title),
        "title": title,
        "funding_body": funding_body,
        "primary_url": url,
        "status": status,
        "opening_date": None,
        "deadline": deadline,
        "days_to_deadline": days,
        "beneficiaries_evidence": beneficiary_evidence,
        "territory": _territory(text, url),
        "amounts": _extract_amounts(text),
        "total_budget": None,
        "contribution_min": None,
        "contribution_max": None,
        "cofinancing_evidence": _evidence_snippets(text, ("cofinanziamento", "quota a carico", "contributo massimo"), 2),
        "advance_evidence": _evidence_snippets(text, ("anticipo", "acconto", "erogazione"), 2),
        "partnership_required": bool(partnership_evidence),
        "partnership_evidence": partnership_evidence,
        "runts_seniority_financial_evidence": _evidence_snippets(text, ("runts", "anzianità", "bilanci", "costituit"), 4),
        "eligible_costs_evidence": _evidence_snippets(text, ("spese ammissibili", "costi ammissibili", "personale", "attrezzature"), 4),
        "project_duration_evidence": _evidence_snippets(text, ("durata del progetto", "mesi", "avvio delle attività"), 3),
        "themes": themes,
        "tiremm_compatibility": eligibility_fit if themes else "needs_review",
        "eligibility_reason": eligibility_reason,
        "candidate_project": _project_candidate(themes),
        "criticalities": [],
        "required_documents_evidence": document_evidence,
        "next_action": "Aprire il regolamento completo e verificare eleggibilità, budget e scadenza",
        "why_tiremm_should_apply": (
            "APS RUNTS con attività territoriali coerenti" if eligibility_fit == "compatible" and themes
            else "Soggetto beneficiario non compatibile con Tiremm Innanz APS" if eligibility_fit == "ineligible"
            else "Compatibilità da confermare sui requisiti completi"
        ),
        "next_three_actions": [
            "Verificare requisiti soggettivi e territoriali nel regolamento",
            "Stimare progetto, budget e cofinanziamento realistici",
            "Raccogliere documenti e partner richiesti prima della scadenza",
        ],
        "source": {
            "content_type": fetched.content_type,
            "checksum": fetched.checksum,
            "size_bytes": fetched.size_bytes,
            "retrieved_url": fetched.final_url or fetched.url,
        },
        "content_hash": _content_hash(text),
        "search_queries": sorted(set(search_row.get("queries", []))),
    }
    score, rules = _score(opportunity)
    opportunity["score"] = score
    opportunity["score_rules"] = rules
    opportunity["priority"] = _priority(score)
    if status in {"closed", "expired"}:
        opportunity["criticalities"].append("call_not_open")
    if eligibility_fit == "ineligible":
        opportunity["criticalities"].append("beneficiary_class_excludes_tiremm")
    elif not beneficiary_evidence:
        opportunity["criticalities"].append("beneficiaries_not_explicitly_extracted")
    elif eligibility_fit == "needs_review":
        opportunity["criticalities"].append("beneficiary_class_needs_review")
    if not deadline:
        opportunity["criticalities"].append("deadline_not_extracted")
    return opportunity


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _render_markdown(result: RunResult) -> str:
    opportunities = result.opportunities
    lines = [
        "# Bandi Weekly Deep Research",
        "",
        f"Run: `{result.run_id}`  ",
        f"Status: `{result.status}` — partial: `{str(result.partial).lower()}`  ",
        f"Durata: `{result.metrics.get('duration_sec', 0)} s`",
        "",
        "## Executive summary",
        "",
        f"Fonti scoperte: {result.metrics.get('sources_discovered', 0)}; fonti primarie aperte: {result.metrics.get('primary_sources_opened', 0)}; opportunità esaminate: {result.metrics.get('calls_examined', 0)}.",
        f"Nuove: {result.metrics.get('new_calls', 0)}; aggiornate: {result.metrics.get('updated_calls', 0)}; priorità alta: {result.metrics.get('high_priority_calls', 0)}.",
        "",
        "## Nuovi bandi prioritari",
        "",
    ]
    for item in opportunities:
        if item.get("lifecycle") != "NEW" or item.get("priority") not in {"HIGH", "REVIEW"}:
            continue
        lines.extend(
            [
                f"### {item['title']}",
                f"- Ente: {item['funding_body']}",
                f"- Fonte primaria: {item['primary_url']}",
                f"- Stato/scadenza: {item['status']} / {item.get('deadline') or 'non estratta'}",
                f"- Score: {item['score']}/100 — {item['priority']}",
                f"- Perché Tiremm dovrebbe candidarsi: {item['why_tiremm_should_apply']}",
                "- Prossime 3 azioni concrete:",
                *[f"  {index}. {action}" for index, action in enumerate(item["next_three_actions"], 1)],
                "",
            ]
        )
    lines.extend(["## Bandi aggiornati", ""])
    for item in opportunities:
        if item.get("lifecycle") == "UPDATED":
            lines.append(f"- [{item['title']}]({item['primary_url']}): {', '.join(item.get('changes', []))}")
    lines.extend(["", "## Scadenze entro 30 giorni", ""])
    for item in opportunities:
        days = item.get("days_to_deadline")
        if isinstance(days, int) and 0 <= days <= 30:
            lines.append(f"- [{item['title']}]({item['primary_url']}): {days} giorni")
    lines.extend(["", "## Opportunità che richiedono partner", ""])
    for item in opportunities:
        if item.get("partnership_required"):
            lines.append(f"- [{item['title']}]({item['primary_url']})")
    lines.extend(["", "## Opportunità scartate", ""])
    for item in result.discarded[:80]:
        lines.append(f"- {item.get('title') or item.get('url')}: {item.get('reason')}")
    lines.extend(["", "## Fonti controllate", ""])
    for item in result.sources_checked[:120]:
        lines.append(f"- {item.get('status')}: {item.get('url')}")
    lines.extend(["", "## Errori/fonti non raggiungibili", ""])
    for item in result.errors:
        lines.append(f"- {item.get('stage')}: {item.get('reason')} — {item.get('subject')}")
    return "\n".join(lines).rstrip() + "\n"


def run_weekly(
    config: WeeklyConfig | None = None,
    *,
    provider: Any | None = None,
    fetcher: Any | None = None,
    semantic_invoke: Any | None = None,
    today: date | None = None,
    extra_queries: tuple[tuple[str, str], ...] = (),
) -> RunResult:
    config = config or WeeklyConfig.from_env()
    today = today or date.today()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.state_dir / "weekly.lock"
    lock_handle = lock_path.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_handle.close()
        raise RuntimeError("bandi_weekly_already_running") from exc
    started_monotonic = time.monotonic()
    started_at = _utc_now()
    run_entropy = f"{started_at}:{time.time_ns()}"
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + hashlib.sha256(run_entropy.encode()).hexdigest()[:8]
    run_dir = config.state_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    metrics = RunMetrics()
    errors: list[dict[str, Any]] = []
    sources_checked: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    opportunities: list[dict[str, Any]] = []
    try:
        use_verified_direct_discovery = provider is None and fetcher is None
        provider = provider or SearxngSearchProvider(
            config.searxng_url,
            timeout_sec=config.timeout_sec,
            engines=config.engines,
            max_pages=1,
        )
        fetcher = fetcher or SourceFetcher(
            cache_dir=config.state_dir / "cache",
            timeout_sec=config.timeout_sec,
            max_bytes=config.max_bytes,
            user_agent="Ralfloop-BandiWeekly/1.0",
        )
        semantic_runner = semantic_invoke
        if config.semantic_retrieval_enabled and semantic_runner is None:
            try:
                semantic_runner = _default_semantic_invoke()
            except Exception as exc:
                errors.append({
                    "stage": "semantic_init",
                    "subject": "semantic_retriever_v1",
                    "reason": f"{type(exc).__name__}:{exc}",
                })
        discovered: dict[str, dict[str, Any]] = {}
        successful_queries = 0
        for family, query in (*QUERY_FAMILIES, *extra_queries):
            try:
                results = provider.search(query, limit=config.max_results_per_query)
                successful_queries += 1
            except SearchProviderUnavailable as exc:
                errors.append({"stage": "search", "subject": query, "reason": str(exc)})
                continue
            except Exception as exc:
                errors.append({"stage": "search", "subject": query, "reason": f"{type(exc).__name__}:{exc}"})
                continue
            for item in results:
                metrics.sources_discovered += 1
                url = canonicalize_url(item.url)
                if url in discovered:
                    metrics.duplicates_removed += 1
                    discovered[url]["queries"].append(query)
                    continue
                discovered[url] = {
                    "url": url,
                    "title": item.title,
                    "snippet": item.snippet,
                    "publisher": item.publisher,
                    "family": family,
                    "queries": [query],
                }
        if successful_queries == 0:
            raise RuntimeError("bandi_search_backend_unavailable")
        if use_verified_direct_discovery:
            for family, url in DIRECT_DISCOVERY_URLS:
                canonical = canonicalize_url(url)
                if canonical not in discovered:
                    discovered[canonical] = {
                        "url": canonical, "title": family.replace("_", " "), "snippet": "",
                        "publisher": "Regione Lombardia", "family": "verified_listing", "queries": ["direct_verified_listing"],
                    }
                    metrics.sources_discovered += 1
        direct_rank = {canonicalize_url(url): index for index, (_family, url) in enumerate(DIRECT_DISCOVERY_URLS)}
        primary_rows = sorted(
            (row for row in discovered.values() if _is_primary_domain(row["url"])),
            key=lambda row: (
                0 if row.get("family") == "verified_listing" else 1,
                direct_rank.get(row["url"], 999),
                row.get("title") or "",
            ),
        )
        queued_urls = {row["url"] for row in primary_rows}
        for row in discovered.values():
            if not _is_primary_domain(row["url"]):
                discarded.append({"title": row["title"], "url": row["url"], "reason": "discovery_only_no_primary_confirmation"})
        primary_index = 0
        processed_primary = 0
        while primary_index < len(primary_rows) and processed_primary < config.max_primary_fetches:
            row = primary_rows[primary_index]
            primary_index += 1
            processed_primary += 1
            try:
                fetched = fetcher.fetch(row["url"])
            except Exception as exc:
                metrics.failed_sources += 1
                errors.append({"stage": "fetch", "subject": row["url"], "reason": f"{type(exc).__name__}:{exc}"})
                continue
            sources_checked.append({"url": row["url"], "status": fetched.status, "content_type": fetched.content_type, "checksum": fetched.checksum})
            if not fetched.ok or not fetched.cache_path:
                metrics.failed_sources += 1
                errors.append({"stage": "fetch", "subject": row["url"], "reason": fetched.error or fetched.status})
                continue
            metrics.primary_sources_opened += 1
            text, parse_status = _read_source(str(Path(fetched.cache_path) / "content"), fetched.content_type)
            if not text:
                metrics.failed_sources += 1
                errors.append({"stage": "parse", "subject": row["url"], "reason": parse_status})
                continue
            if use_verified_direct_discovery and fetched.content_type == "text/html":
                for detail in _discover_detail_links(_raw_html(fetched), str(fetched.final_url or fetched.url)):
                    if detail["url"] in queued_urls:
                        continue
                    queued_urls.add(detail["url"])
                    primary_rows.append({
                        "url": detail["url"], "title": detail["title"], "snippet": "",
                        "publisher": row.get("publisher") or _domain(detail["url"]),
                        "family": "listing_detail", "queries": [row["url"]],
                    })
                    metrics.sources_discovered += 1
            rejection_reason = _call_rejection_reason(row, text, today)
            if rejection_reason:
                discarded.append({"title": row["title"], "url": row["url"], "reason": rejection_reason})
                continue
            primary_url = canonical_url(fetched.final_url or fetched.url)
            expected_documents = [ExpectedDocument("official_call_text", primary_url, True)]
            raw_html = _raw_html(fetched)
            if raw_html:
                expected_documents.extend(discover_official_documents(raw_html, primary_url))
            expected_by_url = {
                item.canonical_url: item
                for item in expected_documents
                if item.canonical_url is not None
            }
            evidence_rows, primary_document = _source_evidence(
                fetched, text, "official_call_text"
            )
            available_documents = [primary_document]
            document_texts = [text]
            for attachment in list(expected_by_url.values())[: config.max_attachments_per_call + 1]:
                if attachment.canonical_url == primary_url:
                    continue
                try:
                    attachment_fetch = fetcher.fetch(attachment.canonical_url)
                except Exception as exc:
                    metrics.failed_sources += 1
                    errors.append({
                        "stage": "attachment_fetch",
                        "subject": attachment.canonical_url,
                        "reason": f"{type(exc).__name__}:{exc}",
                    })
                    continue
                sources_checked.append({
                    "url": attachment.canonical_url,
                    "status": attachment_fetch.status,
                    "content_type": attachment_fetch.content_type,
                    "checksum": attachment_fetch.checksum,
                    "document_type": attachment.document_type,
                })
                if not attachment_fetch.ok or not attachment_fetch.cache_path:
                    metrics.failed_sources += 1
                    errors.append({
                        "stage": "attachment_fetch",
                        "subject": attachment.canonical_url,
                        "reason": attachment_fetch.error or attachment_fetch.status,
                    })
                    continue
                attachment_text, attachment_status = _read_source(
                    str(Path(attachment_fetch.cache_path) / "content"),
                    attachment_fetch.content_type,
                )
                if not attachment_text:
                    metrics.failed_sources += 1
                    errors.append({
                        "stage": "attachment_parse",
                        "subject": attachment.canonical_url,
                        "reason": attachment_status,
                    })
                    continue
                metrics.primary_sources_opened += 1
                attachment_evidence, available = _source_evidence(
                    attachment_fetch, attachment_text, attachment.document_type
                )
                evidence_rows.extend(attachment_evidence)
                available_documents.append(available)
                document_texts.append(attachment_text)
            completeness = assess_document_completeness(
                expected_documents, available_documents
            )
            retrieval = retrieve_for_bandi(
                SEMANTIC_BANDI_QUERY,
                evidence_rows,
                semantic_invoke=semantic_runner if config.semantic_retrieval_enabled else None,
                top_k=config.semantic_top_k,
                minimum_documents=config.semantic_minimum_documents,
                minimum_tokens=config.semantic_minimum_tokens,
            )
            metrics.calls_examined += 1
            opportunity = _build_opportunity(
                row, fetched, "\n\n".join(document_texts), today
            )
            opportunity["documents"] = [asdict(item) for item in available_documents]
            opportunity["document_completeness"] = completeness.to_dict()
            opportunity["eligibility_status"] = completeness.eligibility_status
            opportunity["retrieval"] = retrieval.to_dict()
            opportunity["retrieval_scope"] = "bandi_only"
            opportunity["semantic_retrieval_used"] = retrieval.mode == "hybrid_rrf"
            citations, rendered_citations = claim_citations_from_evidence(
                retrieval.evidence
            )
            opportunity["critical_claim_citations"] = citations
            opportunity["citation_provenance"] = {
                field_name: list(provenance)
                for field_name, provenance in rendered_citations.items()
            }
            opportunity["citation_contract"] = {
                "allowed_evidence_ids": [item.chunk_id for item in retrieval.evidence],
                "llm_constructed_urls": False,
                "validator": "deterministic_evidence_allowlist_v1",
            }
            if completeness.status in {"partial_critical", "unknown"}:
                opportunity["tiremm_compatibility"] = "conditional_missing_primary_documents"
                opportunity["criticalities"].append("missing_critical_primary_documents")
                opportunity["score"] = min(opportunity["score"], 79)
                opportunity["priority"] = _priority(opportunity["score"])
            opportunities.append(opportunity)
        history_path = config.state_dir / "history.json"
        history = _load_json(history_path, {"schema_version": SCHEMA_VERSION, "calls": {}})
        previous_calls = dict(history.get("calls") or {}) if history.get("schema_version") == SCHEMA_VERSION else {}
        current_calls: dict[str, Any] = dict(previous_calls)
        for opportunity in opportunities:
            key = opportunity["call_key"]
            previous = previous_calls.get(key)
            changes: list[str] = []
            if previous is None:
                lifecycle = "NEW"
                metrics.new_calls += 1
            elif (
                previous.get("content_hash") != opportunity["content_hash"]
                or (
                    "documents" in previous
                    and previous.get("documents") != opportunity.get("documents")
                )
            ):
                lifecycle = "UPDATED"
                metrics.updated_calls += 1
                for field_name in ("status", "deadline", "score", "primary_url"):
                    if previous.get(field_name) != opportunity.get(field_name):
                        changes.append(f"{field_name}:{previous.get(field_name)}->{opportunity.get(field_name)}")
                if not changes:
                    changes.append("primary_document_set_or_content_changed")
            elif opportunity["status"] in {"closed", "expired"}:
                lifecycle = "CLOSED"
            elif isinstance(opportunity.get("days_to_deadline"), int) and 0 <= opportunity["days_to_deadline"] <= 30:
                lifecycle = "CLOSING_SOON"
            else:
                lifecycle = "UNCHANGED"
            opportunity["lifecycle"] = lifecycle
            opportunity["changes"] = changes
            if opportunity["priority"] == "HIGH" and opportunity["status"] not in {"closed", "expired"}:
                metrics.high_priority_calls += 1
            current_calls[key] = {
                "title": opportunity["title"],
                "funding_body": opportunity["funding_body"],
                "primary_url": opportunity["primary_url"],
                "content_hash": opportunity["content_hash"],
                "status": opportunity["status"],
                "deadline": opportunity["deadline"],
                "score": opportunity["score"],
                "documents": opportunity.get("documents", []),
                "first_seen": previous.get("first_seen") if previous else started_at,
                "last_seen": started_at,
                "last_run_id": run_id,
            }
        _write_json_atomic(history_path, {"schema_version": SCHEMA_VERSION, "calls": current_calls})
        metrics.duration_sec = round(time.monotonic() - started_monotonic, 3)
        partial = bool(errors)
        result = RunResult(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            started_at=started_at,
            finished_at=_utc_now(),
            ok=True,
            partial=partial,
            status="completed_partial" if partial else "completed",
            organization_profile=organization_profile(),
            opportunities=sorted(opportunities, key=lambda item: (-item["score"], item["title"])),
            discarded=discarded,
            sources_checked=sources_checked,
            errors=errors,
            metrics=asdict(metrics),
        )
    except Exception as exc:
        metrics.duration_sec = round(time.monotonic() - started_monotonic, 3)
        errors.append({"stage": "workflow", "subject": run_id, "reason": f"{type(exc).__name__}:{exc}"})
        result = RunResult(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            started_at=started_at,
            finished_at=_utc_now(),
            ok=False,
            partial=True,
            status="failed",
            organization_profile=organization_profile(),
            opportunities=opportunities,
            discarded=discarded,
            sources_checked=sources_checked,
            errors=errors,
            metrics=asdict(metrics),
        )
    if result.ok and config.glm_review_auto_enqueue:
        enqueued, enqueue_errors = _enqueue_slow_reviews(result, config)
        result.metrics["glm_jobs_enqueued"] = enqueued
        if enqueue_errors:
            result.errors.extend(enqueue_errors)
            result.partial = True
            result.status = "completed_partial"
    json_path = run_dir / "report.json"
    markdown_path = run_dir / "report.md"
    result.report_json = str(json_path)
    result.report_markdown = str(markdown_path)
    _write_json_atomic(json_path, asdict(result))
    markdown_path.write_text(_render_markdown(result), encoding="utf-8")
    status_payload = {
        "schema_version": SCHEMA_VERSION,
        "last_run_id": result.run_id,
        "last_run": result.finished_at,
        "last_successful_run": result.finished_at if result.ok else _load_json(config.state_dir / "status.json", {}).get("last_successful_run"),
        "last_exit_status": 0 if result.ok else 1,
        "last_report_json": result.report_json,
        "last_report_markdown": result.report_markdown,
    }
    _write_json_atomic(config.state_dir / "status.json", status_payload)
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock_handle.close()
    return result


def _enqueue_slow_reviews(result: RunResult, config: WeeklyConfig) -> tuple[int, list[dict[str, Any]]]:
    from ralfloop_agent.glm_review.service import GlmReviewService

    service = GlmReviewService()
    enqueued = 0
    errors: list[dict[str, Any]] = []
    candidates = [
        item for item in result.opportunities
        if item.get("priority") == "HIGH" and item.get("status") not in {"closed", "expired"}
    ][: max(0, min(config.glm_review_max_jobs, 10))]
    for opportunity in candidates:
        retrieval = opportunity.get("retrieval") or {}
        evidence_rows = retrieval.get("evidence") if isinstance(retrieval, dict) else []
        evidence = []
        for item in evidence_rows or []:
            if not isinstance(item, dict):
                continue
            evidence.append(
                {
                    "source_id": item.get("chunk_id") or item.get("source_id"),
                    "location": item.get("canonical_url") or opportunity.get("primary_url"),
                    "claim": item.get("section") or item.get("anchor") or "retrieved call evidence",
                    "excerpt": item.get("text"),
                    "classification": "fact",
                }
            )
        payload = {
            "goal": f"Review the pre-application candidate for {opportunity.get('title')}",
            "constraints": ["Use only supplied official evidence", "Do not submit or contact external parties"],
            "draft": "\n".join(
                [
                    str(opportunity.get("candidate_project") or ""),
                    str(opportunity.get("why_tiremm_should_apply") or ""),
                    *[str(item) for item in opportunity.get("next_three_actions") or []],
                ]
            ),
            "requirements": [
                f"eligibility: {opportunity.get('beneficiaries_evidence') or 'missing'}",
                f"deadline: {opportunity.get('deadline') or 'missing'}",
                f"partnership_required: {opportunity.get('partnership_required')}",
                "objectives, activities and KPI must be coherent",
                "budget must be internally consistent",
            ],
            "evidence": evidence,
            "budget_summary": {"amounts": opportunity.get("amounts") or []},
            "known_gaps": list(opportunity.get("criticalities") or []),
            "requested_review": ["requirements coverage", "coherence", "budget risk", "missing evidence"],
            "deadline": opportunity.get("deadline"),
            "priority": int(opportunity.get("score") or 0),
        }
        task_id = f"glm-grant-{str(opportunity.get('call_key'))[:12]}-{str(opportunity.get('content_hash'))[:8]}"
        try:
            service.enqueue_payload(
                payload,
                task_type="grant_review",
                task_id=task_id,
                source_path=result.report_json or f"bandi_weekly:{result.run_id}",
            )
            enqueued += 1
        except Exception as exc:
            errors.append({"stage": "glm_enqueue", "subject": task_id, "reason": f"{type(exc).__name__}:{exc}"})
    return enqueued, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bandi_weekly_deep_research")
    parser.add_argument("--state-dir", default=None)
    args = parser.parse_args(argv)
    config = WeeklyConfig.from_env()
    if args.state_dir:
        config.state_dir = Path(args.state_dir).expanduser()
    try:
        result = run_weekly(config)
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "status": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "ok": result.ok,
                "status": result.status,
                "run_id": result.run_id,
                "partial": result.partial,
                "metrics": result.metrics,
                "report_json": result.report_json,
                "report_markdown": result.report_markdown,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
