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
