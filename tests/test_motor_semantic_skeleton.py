from __future__ import annotations

from types import SimpleNamespace

from ralfloop_agent.integration.motor_semantic_skeleton import (
    ContextSegment,
    compact_context,
    compact_judge_case,
    compact_text,
    lookup_grammar,
)


ANALYSES = {
    "i": [{"category": "articolo_determinativo", "features": {"lemma": "il"}}],
    "documenti": [{"category": "nome_comune", "features": {"lemma": "documento", "numero": "plurale"}}],
    "devono": [{"category": "verbo", "features": {"lemma": "dovere", "tempo": "presente"}}],
    "essere": [{"category": "verbo", "features": {"lemma": "essere"}}],
    "controllati": [{"category": "verbo", "features": {"lemma": "controllare", "tempo": "passato"}}],
    "inviarli": [{"category": "verbo", "features": {"lemma": "inviare"}}],
}


class FakeTool:
    def __init__(self, name: str):
        self.name = name


class FakeGrammarSession:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def list_tools(self):
        return [FakeTool("grammar.lookup_token")]

    def call_tool(self, name, arguments):
        token = str(arguments["token"]).casefold()
        return {"structuredContent": {"analyses": ANALYSES.get(token, [])}}


def test_grammar_caveman_keeps_critical_operators():
    text = "I documenti devono essere controllati prima di inviarli, non dopo."
    compact = compact_text(text, ANALYSES)
    assert " I " not in f" {compact} "
    assert "documento" in compact
    assert "devono" in compact
    assert "prima" in compact
    assert "non" in compact
    assert "dopo" in compact


def test_context_dedupes_repetition_but_not_contradiction():
    segments = [
        ContextSegment("La mail può essere inviata solo dopo approvazione.", "m1", timestamp="2026-09-18T09:00:00+02:00"),
        ContextSegment("La mail può essere inviata solo dopo approvazione.", "m2", timestamp="2026-09-18T09:05:00+02:00"),
        ContextSegment("La mail non può essere inviata dopo approvazione.", "m3", timestamp="2026-09-18T09:06:00+02:00"),
    ]
    result = compact_context(segments)
    assert len(result.entries) == 2
    positive = next(entry for entry in result.entries if " non " not in f" {entry.text} ")
    negative = next(entry for entry in result.entries if " non " in f" {entry.text} ")
    assert positive.refs == ["m1", "m2"]
    assert positive.timestamp == "2026-09-18T09:05:00+02:00"
    assert negative.refs == ["m3"]


def test_lookup_grammar_is_bounded_and_fail_soft():
    result = lookup_grammar(
        ["I documenti devono essere controllati"],
        session_factory=FakeGrammarSession,
        max_unique_tokens=3,
    )
    assert len(result) == 3
    assert result["documenti"][0]["features"]["lemma"] == "documento"


class BrokenGrammarSession:
    def __enter__(self):
        raise OSError("down")

    def __exit__(self, *args):
        return None


def test_grammar_failure_preserves_context_instead_of_dropping_it():
    segment = ContextSegment("Non inviare il pagamento prima della verifica.", "r1")
    result = compact_context(
        [segment], use_grammar=True, grammar_session_factory=BrokenGrammarSession
    )
    assert len(result.entries) == 1
    assert "non" in result.entries[0].text
    assert "pagamento" in result.entries[0].text
    assert "prima" in result.entries[0].text


def test_judge_case_compactor_includes_goal_rules_facts_and_answer():
    case = SimpleNamespace(
        goal="Controlla la modifica prima del deploy",
        facts=["tests=passed", "diff_present=true"],
        rules=["Non eseguire senza conferma"],
        candidate_answer="Patch pronta",
    )
    result = compact_judge_case(case)
    kinds = {entry.kind for entry in result.entries}
    assert {"goal", "fact", "rule", "answer"} <= kinds
    assert any("non" in entry.text.casefold() for entry in result.entries if entry.kind == "rule")


def test_conversation_context_keeps_recent_turns_exact_and_compresses_old_turns():
    from ralfloop_agent.integration.motor_semantic_skeleton import conversation_segments

    turns = [
        {"id": "t1", "role": "user", "content": "I documenti devono essere controllati"},
        {"id": "t2", "role": "assistant", "content": "Li controllo prima di inviarli"},
        {"id": "t3", "role": "user", "content": "Non inviarli ancora"},
    ]
    segments = conversation_segments(turns, recent_exact=2)
    assert [segment.exact for segment in segments] == [False, True, True]
    result = compact_context(
        segments,
        use_grammar=True,
        grammar_session_factory=FakeGrammarSession,
    )
    old = next(entry for entry in result.entries if "t1" in entry.refs)
    recent = next(entry for entry in result.entries if "t3" in entry.refs)
    assert "documento" in old.text
    assert recent.text == "U>Non inviarli ancora"


def test_dense_profile_uses_determiner_to_prefer_noun_reading():
    from ralfloop_agent.integration.motor_semantic_skeleton import dense_compact_text

    grammar = {
        "la": [{"category": "articolo_determinativo", "features": {"lemma": "il"}}],
        "verifica": [
            {"category": "nome_comune", "features": {"lemma": "verifica"}},
            {"category": "verbo", "features": {"lemma": "verificare", "modo": "indicativo"}},
        ],
        "è": [{"category": "verbo", "features": {"lemma": "essere", "modo": "indicativo"}}],
        "completata": [{"category": "verbo", "features": {"lemma": "completare", "modo": "participio", "tempo": "passato"}}],
    }
    result = dense_compact_text("A>La verifica è completata", grammar, {})
    assert "verificare(" not in result
    assert "verifica=completata" in result.casefold()


def test_dense_profile_does_not_read_determined_texto_as_verb():
    from ralfloop_agent.integration.motor_semantic_skeleton import dense_compact_text

    grammar = {
        "controlla": [{"category": "verbo", "features": {"lemma": "controllare", "modo": "imperativo"}}],
        "il": [{"category": "articolo_determinativo", "features": {"lemma": "il"}}],
        "testo": [
            {"category": "nome_comune", "features": {"lemma": "testo"}},
            {"category": "verbo", "features": {"lemma": "testare", "modo": "indicativo"}},
        ],
    }
    result = dense_compact_text("U>Controlla il testo", grammar, {"controllare": ("[Human] controllare [Artifact]",)})
    assert "testare" not in result
    assert "testo" in result


def test_timeline_facts_use_one_base_and_relative_offsets():
    result = compact_context([
        ContextSegment("primo fatto", "a", timestamp="2026-09-18T09:00:00+02:00"),
        ContextSegment("secondo fatto", "b", timestamp="2026-09-18T09:12:00+02:00"),
    ])
    facts = result.timeline_facts()
    assert facts[0] == "T0=2026-09-18T09:00+02:00"
    assert any(line.startswith("+0m ") for line in facts[1:])
    assert any(line.startswith("+12m ") for line in facts[1:])


def test_invalid_profile_fails_before_silent_semantic_change():
    import pytest

    with pytest.raises(ValueError, match="semantic_skeleton_profile_invalid"):
        compact_context([ContextSegment("dato", "r")], profile="unknown")


def test_adaptive_policy_skips_short_context_and_shadows_long_context_without_text_leak():
    from ralfloop_agent.integration.motor_semantic_skeleton import (
        SkeletonPolicy,
        compression_eligible,
        judge_case_segments,
        skeleton_shadow_summary,
    )

    short = SimpleNamespace(goal="check", facts=["ok"], rules=["do not invent"], candidate_answer="pass")
    assert not compression_eligible(judge_case_segments(short), SkeletonPolicy(min_chars=1000, min_segments=4))

    repeated = "recipient unchanged content unchanged approval required " * 8
    long = SimpleNamespace(
        goal="verify exact approved action",
        facts=[repeated, repeated, repeated, repeated],
        rules=["Never bypass confirmation.", "Missing evidence means review."],
        candidate_answer="advance only if exact",
    )
    summary = skeleton_shadow_summary(
        long,
        policy=SkeletonPolicy(min_chars=1000, min_segments=4),
        use_grammar=False,
    )
    assert summary["eligible"] is True
    assert summary["compact_segments"] < summary["raw_segments"]
    assert len(summary["skeleton_sha256"]) == 64
    assert "text" not in summary
    assert "facts" not in summary


def test_protected_atom_guard_preserves_negation_ids_numbers_and_email():
    from ralfloop_agent.integration.motor_semantic_skeleton import preserves_protected_atoms

    raw = "Non inviare a Alice@example.org prima del 18/09 se hash abc123 cambia."
    good = "non inviare Alice@example.org prima 18/09 se hash abc123 cambia"
    bad = "inviare Alice@example.org 18/09 hash abc123 cambia"
    assert preserves_protected_atoms(raw, good) is True
    assert preserves_protected_atoms(raw, bad) is False


def test_guard_falls_back_to_raw_segment_when_compressor_drops_critical_atom(monkeypatch):
    from ralfloop_agent.integration import motor_semantic_skeleton as module

    monkeypatch.setattr(module, "compact_text", lambda text, analyses=None: "inviare destinatario")
    segment = ContextSegment("Non inviare destinatario prima del 18/09.", "g1")
    result = module.compact_context([segment], profile="safe")
    assert result.guard_fallbacks == 1
    assert result.entries[0].text == "Non inviare destinatario prima del 18/09."


def test_compress_judge_case_facts_changes_only_facts():
    from ralfloop_agent.integration.bottazzi_motor_judge import JudgeCase
    from ralfloop_agent.integration.motor_semantic_skeleton import (
        SkeletonPolicy,
        compress_judge_case_facts,
    )

    repeated = "recipient unchanged content unchanged approval required " * 8
    case = JudgeCase(
        case_id="compress-1",
        goal="keep this goal exact",
        facts=[repeated, repeated, repeated, repeated],
        rules=["keep this rule exact"],
        candidate_actions=["PASS", "REQUEST_REVIEW"],
        candidate_answer="keep answer exact",
    )
    compact, skeleton = compress_judge_case_facts(
        case,
        policy=SkeletonPolicy(min_chars=1000, min_segments=4),
        use_grammar=False,
    )
    assert skeleton is not None
    assert len(compact.facts) < len(case.facts)
    assert compact.goal == case.goal
    assert compact.rules == case.rules
    assert compact.candidate_answer == case.candidate_answer
