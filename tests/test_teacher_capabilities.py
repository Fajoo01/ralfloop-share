from __future__ import annotations

import pytest

from ralfloop_agent.teacher.capabilities import (
    StudentTeacherCapabilityRegistry,
    capability_descriptors,
)
from ralfloop_agent.teacher.catalog import TOOL_MODELS
from ralfloop_agent.unified_assistant.platform import (
    CapabilityPermission,
)
from src.teacher import ALL_TOOLS


def test_teacher_descriptors_exactly_match_public_mcp_tools():
    rows = capability_descriptors()

    assert tuple(row.capability_id for row in rows) == ALL_TOOLS
    assert len(rows) == 18

    for row in rows:
        assert row.server_id == "teacher.mcp"
        assert row.domain == "teacher"
        assert row.source_system == "teacher"
        assert row.input_schema == (
            TOOL_MODELS[row.capability_id].model_json_schema()
        )
        assert row.permission in {
            CapabilityPermission.READ,
            CapabilityPermission.DRAFT,
        }
        assert row.approval_required is False


def test_student_registry_rejects_non_teacher_scope():
    rows = list(capability_descriptors())

    rows[0] = rows[0].model_copy(
        update={
            "server_id": "pec_runts.mcp",
            "domain": "pec_runts",
            "source_system": "pec",
        }
    )

    with pytest.raises(
        ValueError,
        match="student_teacher_registry_scope_violation",
    ):
        StudentTeacherCapabilityRegistry(rows)


@pytest.mark.parametrize(
    "query",
    [
        "controlla la PEC",
        "manda una mail",
        "entra nel RUNTS",
        "apri il cancello",
        "riavvia il server",
        "esegui un comando shell",
        "crea una campagna Mailchimp",
    ],
)
def test_student_registry_does_not_route_non_teaching_requests(query):
    registry = StudentTeacherCapabilityRegistry()

    assert registry.retrieve(query) == ()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("accedi con la mia tessera", "teacher.login"),
        (
            "inizia una sessione di matematica",
            "teacher.start_session",
        ),
        (
            "spiegami le equazioni di secondo grado",
            "teacher.explain",
        ),
        (
            "non ho capito spiegamelo in un altro modo",
            "teacher.explain_differently",
        ),
        ("dammi solo un indizio", "teacher.hint"),
        (
            "dammi un esercizio sulle frazioni",
            "teacher.generate_exercise",
        ),
        (
            "correggi la mia risposta",
            "teacher.check_answer",
        ),
        ("fammi un quiz di storia", "teacher.quiz"),
        (
            "fammi un piano di studio",
            "teacher.study_plan",
        ),
        (
            "riassumi questi appunti",
            "teacher.summarize_material",
        ),
        (
            "come sto andando nello studio",
            "teacher.student_progress",
        ),
        (
            "chiudi la sessione",
            "teacher.end_session",
        ),
        (
            "prepara questo testo per ascoltarlo",
            "teacher.prepare_reading",
        ),
    ],
)
def test_student_registry_retrieves_expected_teacher_tool(
    query,
    expected,
):
    registry = StudentTeacherCapabilityRegistry()

    selected = registry.retrieve(query)

    assert selected
    assert selected[0].capability_id == expected
    assert len(selected) <= 3


def test_student_registry_contains_no_privileged_capability():
    registry = StudentTeacherCapabilityRegistry()

    assert {
        row.permission
        for row in registry.list()
    } <= {
        CapabilityPermission.READ,
        CapabilityPermission.DRAFT,
    }

    assert all(
        row.server_id == "teacher.mcp"
        and row.domain == "teacher"
        for row in registry.list()
    )


@pytest.mark.parametrize(
    "query",
    [
        "la",
        "di",
        "per favore",
        "fammi una cosa",
        "controlla la PEC",
        "controlla la posta certificata",
        "entra nel RUNTS",
        "manda una newsletter",
        "riavvia il computer",
        "esegui shell",
        "accendi la luce",
    ],
)
def test_student_registry_ignores_generic_or_external_terms(query):
    registry = StudentTeacherCapabilityRegistry()
    assert registry.retrieve(query) == ()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("spiegami le frazioni", "teacher.explain"),
        ("dammi solo un indizio", "teacher.hint"),
        ("fammi un quiz di geografia", "teacher.quiz"),
        ("correggi la mia risposta", "teacher.check_answer"),
        ("chiudi la sessione", "teacher.end_session"),
    ],
)
def test_student_registry_select_returns_one_teacher_tool(query, expected):
    selected = StudentTeacherCapabilityRegistry().select(query)

    assert selected is not None
    assert selected.capability_id == expected


@pytest.mark.parametrize(
    "query",
    [
        "controlla la PEC",
        "manda una mail",
        "entra nel RUNTS",
        "esegui shell",
        "apri il cancello",
    ],
)
def test_student_registry_select_denies_external_request(query):
    assert StudentTeacherCapabilityRegistry().select(query) is None


def test_privileged_operational_queries_never_route_to_teacher():
    from ralfloop_agent.teacher.capabilities import (
        StudentTeacherCapabilityRegistry,
    )

    registry = StudentTeacherCapabilityRegistry()

    blocked = (
        "leggi la PEC",
        "entra nel RUNTS",
        "invia una newsletter Mailchimp",
        "mandami una email",
        "apri Gmail",
        "usa Bottazzi",
        "apri il browser",
        "esegui un comando shell",
        "fammi ls -la",
        "riavvia il server",
        "systemctl restart",
        "accendi la domotica",
        "controlla Home Assistant",
        "manda un messaggio WhatsApp",
        "gestisci il sito",
    )

    for query in blocked:
        assert registry.select(query) is None, query


def test_privileged_words_remain_teachable_as_subjects():
    from ralfloop_agent.teacher.capabilities import (
        StudentTeacherCapabilityRegistry,
    )

    registry = StudentTeacherCapabilityRegistry()

    for query in (
        "spiegami cos'è la PEC",
        "spiegami cos'è un server",
        "spiegami come funziona una email",
        "spiegami come funziona un browser",
    ):
        selected = registry.select(query)
        assert selected is not None, query
        assert selected.capability_id == "teacher.explain", query
