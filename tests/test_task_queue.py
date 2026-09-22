from pathlib import Path

from ralfloop_agent.unified_assistant.task_queue import (
    BotTazziTaskQueue,
    TaskCategory,
    TaskState,
)


def queue(tmp_path: Path) -> BotTazziTaskQueue:
    return BotTazziTaskQueue(tmp_path / "queue.sqlite3", clock=lambda: 1_000_000)


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
    first = BotTazziTaskQueue(path, clock=lambda: 1_000_000)
    task = first.create_task("Sistema il conto bancario")
    first.pin(task.task_id, 1)

    second = BotTazziTaskQueue(path, clock=lambda: 1_000_100)
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
