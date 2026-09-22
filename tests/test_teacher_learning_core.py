"""Fast contracts for the independent deterministic learning layer."""
from ralfloop_agent.teacher.web.learning import Curriculum, choose_activity, fraction_evidence, level, native_content
from ralfloop_agent.teacher.web.state import State


def test_original_content_and_profile_scope():
    curriculum = Curriculum()
    profile = {"school_level":"middle", "grade":2, "school_track":""}
    assert curriculum.require(profile,"fractions")["prerequisites"] == ["equal_parts"]
    assert native_content("fractions","matching").answer == ["2/4","2/6","6/8"]
    assert choose_activity(curriculum.topics["fractions"],{},[],0)["activity_type"] == "simulation"


def test_credentials_survive_reopen(tmp_path):
    path = tmp_path / "student.sqlite3"
    state = State(path)
    student = state.register("DEMO-CORE","CoreDemo!123","Core Demo","primary",4)
    reopened = State(path)
    assert reopened.authenticate(reopened.login("DEMO-CORE","CoreDemo!123"))["id"] == student
    assert reopened.progress(student)["xp"] == 0
    assert level(200) == 3


def test_fraction_guard_does_not_trust_llm_arithmetic():
    assert fraction_evidence("Quanto fa 1/2 + 1/2?", "1, perché due metà formano un intero.")["correct"]
    assert not fraction_evidence("Quanto fa 1/2 + 1/2?", "2/4")["correct"]
    assert fraction_evidence("Completa: 2/4 = ?/8", "4")["correct"]
    assert fraction_evidence("Calcola 1/2 + 1/2 e 1/3 + 1/3", "1") is None
    assert fraction_evidence("Quanto fa 1/2 + 1/2?", "1.5") is None
    assert fraction_evidence("Quanto fa 1/0 + 1/2?", "1") is None
