from __future__ import annotations

"""Versioned reference registry for the ABC relationship domain.

These sources provide interpretive and communication frameworks, never evidence
about another person's hidden intentions.  Rules consuming a framework must keep
observable facts, inference and advice separate.
"""

from typing import Any


REFERENCE_LIBRARY: tuple[dict[str, Any], ...] = (
    {
        "id": "nardone_salvini_dialogo_strategico_2004",
        "framework": "dialogo_strategico",
        "citation": "Giorgio Nardone, Alessandro Salvini, Il dialogo strategico. Comunicare persuadendo: tecniche evolute per il cambiamento, Ponte alle Grazie, 2004.",
        "concepts": [
            "domande strategiche",
            "parafrasi ristrutturanti",
            "riassumere per ridefinire",
            "scoperta congiunta",
        ],
        "allowed_use": [
            "formulare domande non accusatorie",
            "riassumere e verificare la comprensione",
            "costruire alternative conversazionali esplicite e rispettose",
            "ridurre escalation e rigidita nel dialogo",
        ],
        "not_evidence_for": [
            "attrazione",
            "gelosia",
            "intenzioni nascoste",
            "diagnosi psicologiche",
        ],
        "guardrails": [
            "non usare false alternative per intrappolare l'interlocutore",
            "non usare persuasione occulta, pressione o inganno",
            "la risposta reale dell'interlocutore prevale sempre sul modello",
        ],
    },
    {
        "id": "miller_rollnick_mi_4e_2023",
        "framework": "motivational_interviewing",
        "citation": "William R. Miller, Stephen Rollnick, Motivational Interviewing: Helping People Change and Grow, 4th ed., Guilford Press, 2023.",
        "concepts": ["engaging", "focusing", "evoking", "planning", "deep listening"],
        "allowed_use": [
            "ascolto riflessivo",
            "domande aperte",
            "rispetto dell'autonomia",
            "evitare il riflesso di correggere o convincere",
        ],
        "not_evidence_for": ["impegno romantico", "preferenza relazionale", "intenzione futura"],
        "guardrails": ["autonomia prima dell'esito", "nessuna tecnica deve sostituire un consenso esplicito"],
    },
    {
        "id": "rusbult_agnew_arriaga_investment_2011",
        "framework": "investment_model_interdependence",
        "citation": "Caryl E. Rusbult, Christopher R. Agnew, Ximena B. Arriaga, The Investment Model of Commitment Processes, 2011.",
        "concepts": ["satisfaction", "quality_of_alternatives", "investment", "commitment"],
        "allowed_use": [
            "organizzare osservazioni longitudinali",
            "distinguere investimento pratico da commitment dichiarato",
            "evitare di dedurre commitment da un singolo segnale",
        ],
        "not_evidence_for": ["scelta gia compiuta", "esclusivita", "sentimento romantico certo"],
        "guardrails": ["usare serie temporali e controevidenze", "non trasformare il modello in previsione certa"],
    },
    {
        "id": "rusbult_vanlange_interdependence_2003",
        "framework": "interdependence_theory",
        "citation": "Caryl E. Rusbult, Paul A. M. Van Lange, Interdependence, Interaction, and Relationships, Annual Review of Psychology 54, 2003.",
        "concepts": ["interaction_patterns", "dependence", "transformation", "long_term_goals"],
        "allowed_use": ["leggere pattern ripetuti invece di episodi isolati", "separare struttura della situazione e attribuzioni mentali"],
        "not_evidence_for": ["motivazioni interne non dichiarate"],
        "guardrails": ["attribuzioni restano inferenze", "preferire comportamenti osservabili"],
    },
    {
        "id": "attachment_secure_base_heuristic",
        "framework": "attachment_secure_base",
        "citation": "Attachment / secure-base literature; use as a broad heuristic, not a diagnostic label.",
        "concepts": ["secure_base", "safe_haven", "proximity_regulation"],
        "allowed_use": [
            "descrivere pattern di avvicinamento-allontanamento senza patologizzarli",
            "distinguere ricerca di prossimita da prova di relazione romantica",
        ],
        "not_evidence_for": ["stile di attaccamento diagnosticato", "disturbo", "attrazione certa"],
        "guardrails": ["non diagnosticare Arianna", "non inferire stile di attaccamento da pochi episodi"],
    },
)


def reference_library() -> list[dict[str, Any]]:
    return [dict(item) for item in REFERENCE_LIBRARY]


__all__ = ["REFERENCE_LIBRARY", "reference_library"]
