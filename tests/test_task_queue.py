from pathlib import Path

from ralfloop_agent.unified_assistant.task_queue import (
    BotTazziTaskQueue,
    DeterministicJedPriorityClassifier,
    JevPriorityClassifier,
    TaskCategory,
    TaskState,
)


def queue(tmp_path: Path) -> BotTazziTaskQueue:
    return BotTazziTaskQueue(
        tmp_path / "queue.sqlite3",
        classifier=DeterministicJedPriorityClassifier(),
        clock=lambda: 1_000_000,
    )


def test_money_love_family_have_priority_over_general(tmp_path: Path) -> None:
    q = queue(tmp_path)
    general = q.create_task("Aggiorna la documentazione tecnica")
    money = q.create_task("Controlla il pagamento TARI")
    love = q.create_task("Organizza un appuntamento romantico")
    family = q.create_task("Prepara i documenti per mia madre")

    entries = q.list_queue()
    top_ids = {entry.task.task_id for entry in entries[:3]}

    assert general.category is TaskCategory.GENERAL
    assert money.category is TaskCategory.MONEY
    assert love.category is TaskCategory.LOVE
    assert family.category is TaskCategory.FAMILY
    assert general.task_id not in top_ids
    assert {money.task_id, love.task_id, family.task_id} == top_ids


def test_human_pin_keeps_absolute_slot_when_new_auto_task_arrives(tmp_path: Path) -> None:
    q = queue(tmp_path)
    first = q.create_task("Controlla il pagamento TARI")
    chosen = q.create_task("Aggiorna documentazione")
    q.create_task("Sistema una pagina interna")

    q.pin(chosen.task_id, 2)
    new_high = q.create_task("Verifica una fattura urgente")

    entries = q.list_queue()

    assert entries[1].task.task_id == chosen.task_id
    assert entries[1].human_pinned is True
    assert new_high.task_id != chosen.task_id
    assert first.task_id in {entry.task.task_id for entry in entries}


def test_blocked_head_is_skipped_without_reordering(tmp_path: Path) -> None:
    q = queue(tmp_path)
    blocked = q.create_task("Pagamento urgente")
    ready = q.create_task("Prepara i documenti per mia madre")
    q.pin(blocked.task_id, 1)
    q.set_state(blocked.task_id, TaskState.BLOCKED, blocked_reason="awaiting_approval")

    before = q.list_queue()
    next_entry = q.next_runnable()
    after = q.list_queue()

    assert before[0].task.task_id == blocked.task_id
    assert after[0].task.task_id == blocked.task_id
    assert next_entry is not None
    assert next_entry.task.task_id == ready.task_id


def test_dependency_blocks_until_parent_completed(tmp_path: Path) -> None:
    q = queue(tmp_path)
    parent = q.create_task("Prepara la fattura")
    child = q.create_task(
        "Invia il pagamento",
        depends_on=(parent.task_id,),
        category_hint="money",
    )
    q.pin(child.task_id, 1)

    first = q.list_queue()[0]
    assert first.task.task_id == child.task_id
    assert first.runnable is False
    assert first.wait_reasons[0].startswith("dependencies:")

    q.set_state(parent.task_id, TaskState.COMPLETED)
    assert q.next_runnable().task.task_id == child.task_id


def test_unpin_returns_task_to_jed_order(tmp_path: Path) -> None:
    q = queue(tmp_path)
    general = q.create_task("Aggiorna documentazione")
    money = q.create_task("Controlla bonifico")
    q.pin(general.task_id, 1)

    assert q.list_queue()[0].task.task_id == general.task_id

    q.unpin(general.task_id)

    assert q.list_queue()[0].task.task_id == money.task_id


def test_persistence_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    first = BotTazziTaskQueue(
        path,
        classifier=DeterministicJedPriorityClassifier(),
        clock=lambda: 1_000_000,
    )
    task = first.create_task("Sistema il conto bancario")
    first.pin(task.task_id, 1)

    second = BotTazziTaskQueue(
        path,
        classifier=DeterministicJedPriorityClassifier(),
        clock=lambda: 1_000_100,
    )
    loaded = second.get_task(task.task_id)

    assert loaded.category is TaskCategory.MONEY
    assert loaded.human_rank == 1
    assert second.list_queue()[0].task.task_id == task.task_id


def test_human_pin_conflict_is_reported_not_reordered(tmp_path: Path) -> None:
    q = queue(tmp_path)
    general = q.create_task("Aggiorna documentazione")
    money = q.create_task("Controlla pagamento TARI")
    q.pin(general.task_id, 1)

    snapshot = q.snapshot()

    assert snapshot["tasks"][0]["task"]["task_id"] == general.task_id
    assert snapshot["priority_conflicts"] == [{
        "pinned_task_id": general.task_id,
        "pinned_position": 1,
        "candidate_task_id": money.task_id,
        "candidate_position": 2,
        "candidate_auto_priority": 85,
        "reason": "higher_auto_priority_below_human_pin",
    }]


class _FakeJevResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class _FakeJevSession:
    def __init__(self, selected: str = "B") -> None:
        self.selected = selected
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict, timeout: float):
        self.calls.append((url, json))
        if url.endswith("/apply-template"):
            return _FakeJevResponse({"prompt": "typed-prompt"})
        probs = {"A": 0.02, "B": 0.94, "C": 0.02, "D": 0.02}
        return _FakeJevResponse({
            "content": self.selected,
            "completion_probabilities": [{
                "top_probs": [
                    {"token": letter, "prob": prob}
                    for letter, prob in probs.items()
                ]
            }],
        })


def test_jev_classifier_uses_typed_local_options() -> None:
    session = _FakeJevSession("B")
    classifier = JevPriorityClassifier(
        base_url="http://127.0.0.1:19110",
        session=session,
    )

    decision = classifier.classify("Organizza un appuntamento romantico")

    assert decision.category is TaskCategory.LOVE
    assert decision.score == 85
    assert decision.source == "jev_typed_local"
    assert decision.confidence == 0.94
    assert [url.rsplit("/", 1)[-1] for url, _ in session.calls] == [
        "apply-template",
        "completion",
    ]
    completion = session.calls[1][1]
    assert completion["n_predict"] == 1
    assert completion["post_sampling_probs"] is True
    assert completion["grammar"] == 'root ::= "A" | "B" | "C" | "D"'


class _BrokenJevSession:
    def post(self, *args, **kwargs):
        raise OSError("offline")


def test_jev_classifier_fallback_is_explicit_when_runtime_unavailable() -> None:
    classifier = JevPriorityClassifier(
        base_url="http://127.0.0.1:19110",
        session=_BrokenJevSession(),
    )

    decision = classifier.classify("Controlla il pagamento TARI")

    assert decision.category is TaskCategory.MONEY
    assert decision.score == 85
    assert decision.source == "jev_unavailable_fallback"
    assert decision.reasons[0] == "jev_unavailable"
