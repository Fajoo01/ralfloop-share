from __future__ import annotations

from collections.abc import Iterable
import re
import unicodedata

from ralfloop_agent.unified_assistant.platform import (
    CapabilityDescriptor,
    CapabilityPermission,
    CapabilityRegistry,
    PromotionState,
)
from src.teacher import (
    ALL_TOOLS,
    CHECK_ANSWER,
    END_SESSION,
    EXPLAIN,
    EXPLAIN_DIFFERENTLY,
    GENERATE_EXERCISE,
    HINT,
    LOGIN,
    PREPARE_READING,
    MINDMAP_GENERATE,
    MINDMAP_UPDATE,
    MINDMAP_EXPLAIN,
    STUDY_AUDIO_GENERATE,
    DOCUMENTARY_GENERATE,
    QUIZ,
    START_SESSION,
    STUDENT_PROGRESS,
    STUDY_PLAN,
    SUMMARIZE_MATERIAL,
)

from .catalog import DESCRIPTIONS, TOOL_MODELS


_KEYWORDS = {
    LOGIN: (
        "tessera",
        "login",
        "accedi",
        "accesso studente",
        "riconosci studente",
        "identifica studente",
    ),
    START_SESSION: (
        "inizia sessione",
        "avvia sessione",
        "cominciamo",
        "materia",
        "argomento",
        "studiare",
    ),
    EXPLAIN: (
        "spiega",
        "spiegami",
        "capire",
        "concetto",
        "lezione",
        "come funziona",
    ),
    EXPLAIN_DIFFERENTLY: (
        "spiega diversamente",
        "altro modo",
        "non ho capito",
        "piu semplice",
        "esempio diverso",
        "riprova spiegazione",
    ),
    HINT: (
        "indizio",
        "suggerimento",
        "aiuto senza soluzione",
        "non dirmi la risposta",
        "passo successivo",
    ),
    GENERATE_EXERCISE: (
        "esercizio",
        "esercizi",
        "esercitazione",
        "fammi provare",
        "allenamento",
        "difficolta",
    ),
    CHECK_ANSWER: (
        "correggi risposta",
        "risposta studente",
        "ho risposto",
        "correzione",
        "errore",
        "soluzione dopo tentativo",
    ),
    QUIZ: (
        "quiz",
        "interrogazione",
        "domande",
        "mettimi alla prova",
        "ripasso orale",
    ),
    STUDY_PLAN: (
        "piano di studio",
        "programma studio",
        "organizza studio",
        "tempo disponibile",
        "obiettivo studio",
    ),
    SUMMARIZE_MATERIAL: (
        "riassumi",
        "riassunto",
        "sintesi",
        "materiale fornito",
        "appunti",
        "testo da studiare",
    ),
    STUDENT_PROGRESS: (
        "progressi",
        "come sto andando",
        "andamento",
        "apprendimento",
        "risultati studio",
        "storico studio",
    ),
    END_SESSION: (
        "fine sessione",
        "termina sessione",
        "chiudi sessione",
        "ho finito",
        "stop studio",
    ),
    PREPARE_READING: (
        "lettura",
        "audiolibro",
        "tts",
        "ascoltare",
        "ascoltarlo",
        "leggi testo",
        "prepara lettura",
    ),
    MINDMAP_GENERATE: (
        "mappa mentale",
        "mappa concettuale",
        "crea mappa",
        "genera mappa",
        "schema concetti",
        "organizza concetti",
    ),
    MINDMAP_UPDATE: (
        "modifica mappa",
        "aggiorna mappa",
        "riorganizza mappa",
        "correggi mappa",
        "rinomina nodo",
        "sposta nodo",
    ),
    MINDMAP_EXPLAIN: (
        "spiega nodo",
        "approfondisci nodo",
        "spiega collegamento",
        "collegamento mappa",
        "nodo mappa",
    ),
    STUDY_AUDIO_GENERATE: (
        "audio studio",
        "ripasso audio",
        "genera audio studio",
        "lezione audio",
        "ascolto didattico",
        "script audio studio",
    ),
    DOCUMENTARY_GENERATE: (
        "documentario",
        "mini documentario",
        "documentario didattico",
        "genera documentario",
        "script documentario",
        "audio documentario",
    ),
}


_STOP_TERMS = frozenset({
    "a", "ad", "al", "alla", "allo", "ai", "agli", "alle",
    "che", "chi", "ci", "con", "da", "dal", "dalla", "dallo",
    "dei", "del", "della", "dello", "di", "e", "ed", "gli",
    "ho", "i", "il", "in", "io", "la", "le", "lo", "ma",
    "mi", "nel", "nella", "nello", "non", "o", "per",
    "pero", "piu", "puoi", "potresti", "questo", "questa",
    "questi", "queste", "se", "si", "solo", "su", "sul",
    "sulla", "ti", "tra", "un", "una", "uno", "vi",
    "voglio", "vorrei", "favore", "fammi", "dammi",
})


# Administrative/infrastructure targets are never actionable from the
# student Teacher boundary.  They may still be discussed as school subjects:
# e.g. "spiegami cos'è un server" is pedagogical, while "riavvia il server"
# must not route to any Teacher capability.
_PRIVILEGED_OPERATIONAL_TARGETS = frozenset({
    "pec",
    "runts",
    "mailchimp",
    "gmail",
    "email",
    "browser",
    "shell",
    "systemctl",
    "server",
    "domotica",
    "whatsapp",
    "bottazzi",
    "sito",
})

_PRIVILEGED_OPERATIONAL_PHRASES = (
    "home assistant",
)

_PRIVILEGED_ACTION_TERMS = frozenset({
    "accedi",
    "accendi",
    "apri",
    "avvia",
    "cancella",
    "controlla",
    "crea",
    "elimina",
    "entra",
    "esegui",
    "gestisci",
    "invia",
    "leggi",
    "manda",
    "mandami",
    "modifica",
    "pubblica",
    "restart",
    "riavvia",
    "scarica",
    "spegni",
    "stop",
})


def _normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(
        char
        for char in decomposed
        if not unicodedata.combining(char)
    )


def _meaningful_terms(text: str) -> frozenset[str]:
    return frozenset(
        token
        for token in re.findall(
            r"[a-z0-9_]+",
            _normalize_text(text),
        )
        if len(token) >= 2
        and token not in _STOP_TERMS
    )


def _is_privileged_operational_query(text: str) -> bool:
    normalized = _normalize_text(text)
    terms = _meaningful_terms(normalized)

    has_privileged_target = bool(
        terms & _PRIVILEGED_OPERATIONAL_TARGETS
    ) or any(
        phrase in normalized
        for phrase in _PRIVILEGED_OPERATIONAL_PHRASES
    )

    if not has_privileged_target:
        return False

    return bool(terms & _PRIVILEGED_ACTION_TERMS)


def _descriptor_routing_terms(
    row: CapabilityDescriptor,
) -> frozenset[str]:
    # Routing intent comes only from the explicit semantic keywords.
    # Generic prose in descriptions must not create accidental matches.
    return _meaningful_terms(" ".join(row.keywords))


def capability_descriptors() -> tuple[CapabilityDescriptor, ...]:
    rows: list[CapabilityDescriptor] = []

    for tool_id in ALL_TOOLS:
        model = TOOL_MODELS[tool_id]

        # Tutti i tool che possono aggiornare lo stato pedagogico locale
        # sono DRAFT. Nessun tool studente è EXECUTE o ADMIN.
        permission = (
            CapabilityPermission.READ
            if tool_id == STUDENT_PROGRESS
            else CapabilityPermission.DRAFT
        )

        rows.append(
            CapabilityDescriptor(
                capability_id=tool_id,
                server_id="teacher.mcp",
                domain="teacher",
                name=tool_id,
                description=DESCRIPTIONS[tool_id],
                keywords=_KEYWORDS[tool_id],
                input_schema=model.model_json_schema(),
                permission=permission,
                side_effect=permission is CapabilityPermission.DRAFT,
                approval_required=False,
                source_system="teacher",
                version="1",
                promotion=PromotionState.SHADOW,
                enabled=True,
                health="local_student_teacher",
            )
        )

    return tuple(rows)


class StudentTeacherCapabilityRegistry(CapabilityRegistry):
    """
    Capability boundary for student-facing Teacher sessions.

    It is intentionally impossible to construct this registry with PEC,
    RUNTS, Mailchimp, browser, shell, administration or any other Ralf
    capability.
    """

    def __init__(
        self,
        capabilities: Iterable[CapabilityDescriptor] | None = None,
    ) -> None:
        rows = tuple(
            capability_descriptors()
            if capabilities is None
            else capabilities
        )

        ids = {row.capability_id for row in rows}

        if ids != set(ALL_TOOLS):
            raise ValueError(
                "student_teacher_registry_capability_set_violation"
            )

        for row in rows:
            if (
                row.capability_id not in ALL_TOOLS
                or row.server_id != "teacher.mcp"
                or row.domain != "teacher"
                or row.source_system != "teacher"
                or row.permission
                not in {
                    CapabilityPermission.READ,
                    CapabilityPermission.DRAFT,
                }
            ):
                raise ValueError(
                    "student_teacher_registry_scope_violation"
                )

        super().__init__(rows)

    def retrieve(
        self,
        query: str,
        *,
        limit: int = 3,
    ) -> tuple[CapabilityDescriptor, ...]:
        query_terms = _meaningful_terms(query)

        if not query_terms:
            return ()

        # Fail closed for operational requests aimed at administrative or
        # infrastructure systems.  This check runs before semantic ranking,
        # preventing collisions such as "leggi la PEC" -> prepare_reading.
        if _is_privileged_operational_query(query):
            return ()

        compatible = tuple(
            row
            for row in self.list()
            if query_terms & _descriptor_routing_terms(row)
        )

        if not compatible:
            return ()

        # Keep the existing Ralf capability ranking. The local pre-filter
        # only removes matches caused exclusively by generic stopwords.
        registry = CapabilityRegistry(compatible)

        return registry.retrieve(
            _normalize_text(query),
            domains=("teacher",),
            allowed_permissions=(
                CapabilityPermission.READ,
                CapabilityPermission.DRAFT,
            ),
            limit=limit,
        )


    def select(
        self,
        query: str,
    ) -> CapabilityDescriptor | None:
        selected = self.retrieve(query, limit=1)
        return selected[0] if selected else None


__all__ = [
    "StudentTeacherCapabilityRegistry",
    "capability_descriptors",
]
