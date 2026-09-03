from __future__ import annotations

from enum import IntEnum, StrEnum

from pydantic import AnyHttpUrl, Field

from .contracts import StrictModel


class SourceMode(StrEnum):
    OFFICIAL_API = "OFFICIAL_API"
    FRONTEND_API = "FRONTEND_API"
    OPEN_DATA = "OPEN_DATA"
    RSS = "RSS"
    STRUCTURED_HTML = "STRUCTURED_HTML"
    WEB_SEARCH = "WEB_SEARCH"
    BROWSER = "BROWSER"


class SourcePriority(IntEnum):
    OFFICIAL_API = 1
    FRONTEND_API = 2
    OPEN_DATA = 3
    RSS = 4
    STRUCTURED_HTML = 5
    WEB_SEARCH = 6
    BROWSER = 7


class BandiSourceDefinition(StrictModel):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    issuer: str = Field(min_length=1, max_length=240)
    homepage: AnyHttpUrl
    mode: SourceMode
    priority: SourcePriority
    enabled: bool = False
    contract_status: str = Field(default="UNVERIFIED", pattern=r"^(VERIFIED|UNVERIFIED|PARTIAL)$")


SOURCE_CATALOG = (
    BandiSourceDefinition(source_id="comune_milano", issuer="Comune di Milano", homepage="https://servizi.comune.milano.it/", mode=SourceMode.STRUCTURED_HTML, priority=SourcePriority.STRUCTURED_HTML),
    BandiSourceDefinition(source_id="regione_lombardia", issuer="Regione Lombardia", homepage="https://bandi.regione.lombardia.it/servizi/servizio/bandi/ricerca", mode=SourceMode.FRONTEND_API, priority=SourcePriority.FRONTEND_API),
    BandiSourceDefinition(source_id="fondazione_cariplo", issuer="Fondazione Cariplo", homepage="https://www.fondazionecariplo.it/it/bandi/bandi-ricerca-scientifica.html", mode=SourceMode.STRUCTURED_HTML, priority=SourcePriority.STRUCTURED_HTML),
    BandiSourceDefinition(source_id="csv_lombardia", issuer="CSV Lombardia", homepage="https://www.csvlombardia.it/milano/progettazione-sociale/", mode=SourceMode.STRUCTURED_HTML, priority=SourcePriority.STRUCTURED_HTML),
    BandiSourceDefinition(source_id="ministero_lavoro", issuer="Ministero del Lavoro e delle Politiche Sociali", homepage="https://www.lavoro.gov.it/", mode=SourceMode.STRUCTURED_HTML, priority=SourcePriority.STRUCTURED_HTML),
    BandiSourceDefinition(source_id="runts", issuer="Registro Unico Nazionale del Terzo Settore", homepage="https://servizi.lavoro.gov.it/runts/it-it/", mode=SourceMode.STRUCTURED_HTML, priority=SourcePriority.STRUCTURED_HTML),
    BandiSourceDefinition(source_id="politiche_giovanili", issuer="Dipartimento per le Politiche Giovanili", homepage="https://www.politichegiovanili.gov.it/", mode=SourceMode.STRUCTURED_HTML, priority=SourcePriority.STRUCTURED_HTML),
    BandiSourceDefinition(source_id="fondazioni_bancarie", issuer="ACRI e fondazioni bancarie", homepage="https://www.acri.it/", mode=SourceMode.STRUCTURED_HTML, priority=SourcePriority.STRUCTURED_HTML),
    BandiSourceDefinition(source_id="intesa_fondo_beneficenza", issuer="Intesa Sanpaolo Fondo di Beneficenza", homepage="https://group.intesasanpaolo.com/", mode=SourceMode.STRUCTURED_HTML, priority=SourcePriority.STRUCTURED_HTML),
)


def enabled_sources() -> tuple[BandiSourceDefinition, ...]:
    return tuple(sorted((row for row in SOURCE_CATALOG if row.enabled and row.contract_status == "VERIFIED"), key=lambda row: row.priority))


__all__ = ["BandiSourceDefinition", "SOURCE_CATALOG", "SourceMode", "SourcePriority", "enabled_sources"]
