import json
from pathlib import Path

from openshell_backend.skills import abc_memory


def test_init_creates_all_files(tmp_path: Path):
    abc_memory.ensure_init(tmp_path)

    for name in (
        "timeline.jsonl",
        "contradictions.jsonl",
        "hypotheses.json",
        "model_state.json",
        "rules.md",
        "last_reading.md",
    ):
        assert (tmp_path / name).exists()


def test_add_event_appends_valid_json(tmp_path: Path):
    row = abc_memory.add_event(
        tmp_path,
        date="2026-06-16",
        text="evento osservato",
        event_type="observed_fact",
        confidence=0.6,
    )

    lines = (tmp_path / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    saved = json.loads(lines[-1])
    assert saved["id"] == row["id"]
    assert saved["type"] == "observed_fact"
    assert saved["evidence_strength"] == 0.6


def test_add_contradiction_appends_valid_json(tmp_path: Path):
    row = abc_memory.add_contradiction(tmp_path, date="2026-06-16", text="dato contrario")

    lines = (tmp_path / "contradictions.jsonl").read_text(encoding="utf-8").splitlines()
    saved = json.loads(lines[-1])
    assert saved["id"] == row["id"]
    assert saved["text"] == "dato contrario"


def test_update_hypothesis_changes_only_requested_key(tmp_path: Path):
    abc_memory.ensure_init(tmp_path)
    before = abc_memory.read_json(tmp_path / "hypotheses.json", {})

    abc_memory.update_hypothesis(
        tmp_path,
        "fabio_papabile_reale_ma_congelato",
        0.7,
        "nota nuova",
    )
    after = abc_memory.read_json(tmp_path / "hypotheses.json", {})

    assert after["fabio_papabile_reale_ma_congelato"]["confidence"] == 0.7
    assert after["fabio_papabile_reale_ma_congelato"]["notes"] == "nota nuova"
    assert after["solo_logistica_appoggio"] == before["solo_logistica_appoggio"]


def test_state_payload_is_json_serializable(tmp_path: Path):
    payload = abc_memory.state_payload(tmp_path)

    encoded = json.dumps(payload, ensure_ascii=False)
    assert "model_state" in encoded
    assert payload["model_state"]["curve"] == 60


def test_reading_contains_cannot_conclude_section(tmp_path: Path):
    abc_memory.add_event(
        tmp_path,
        date="2026-06-16",
        text="messaggio ambiguo",
        event_type="message",
        confidence=0.5,
        effect_on_model={"fabio_field": "+", "third_pressure": "0", "rebound_risk": "unknown", "trust": "unknown"},
    )

    reading = abc_memory.build_reading(tmp_path)

    assert "Cosa NON si può concludere" in reading
    assert "Non si può concludere che Arianna voglia Fabio come certezza." in reading


def test_cli_init_and_search(tmp_path: Path, capsys):
    assert abc_memory.main(["--dir", str(tmp_path), "init"]) == 0
    assert abc_memory.main(["--dir", str(tmp_path), "add-event", "--date", "2026-06-16", "--text", "film Bottazzi", "--type", "observed_fact", "--confidence", "0.6"]) == 0
    assert abc_memory.main(["--dir", str(tmp_path), "search", "Bottazzi"]) == 0

    out = capsys.readouterr().out
    assert "film Bottazzi" in out


def test_parse_abc_commands_with_payload_variants():
    cases = [
        "rl:abc: payload",
        "rl:abc:\npayload",
        "rl:abc\npayload",
        "rsc:abc: payload",
        "rsc:abc:\npayload",
        "rsc:abc\npayload",
    ]
    for text in cases:
        parsed = abc_memory.parse_abc_command(text)
        assert parsed["is_abc"] is True
        assert parsed["has_payload"] is True
        assert parsed["payload"] == "payload"

    parsed = abc_memory.parse_abc_command("rl")
    assert parsed["is_abc"] is True
    assert parsed["has_payload"] is False
    assert parsed["report_kind"] == "extended"


def test_save_telegram_patch_with_metadata_and_latest(tmp_path: Path):
    mem = tmp_path / "memory"
    run = tmp_path / "run"
    record = abc_memory.save_telegram_patch(
        mem,
        run,
        raw_text="rl:abc:\nDelta nuovo",
        payload="Delta nuovo",
        message_id=123,
        sender_id=456,
        created_at="2026-06-16T10:00:00+00:00",
    )

    patch = Path(record["patch_file"])
    current = run / "RL_ABC_PATCH_TELEGRAM_CURRENT.txt"
    assert patch.exists()
    assert current.exists()
    text = patch.read_text(encoding="utf-8")
    assert "SOURCE=telegram" in text
    assert "MESSAGE_ID=123" in text
    assert "SENDER_ID=456" in text
    assert "PAYLOAD_HASH=" in text
    latest = abc_memory.latest_patch_record(mem, run)
    assert latest is not None
    assert latest["patch_file"] == str(patch)


def test_freshness_false_when_current_patch_used(tmp_path: Path):
    mem = tmp_path / "memory"
    run = tmp_path / "run"
    record = abc_memory.save_telegram_patch(
        mem,
        run,
        raw_text="rl:abc:\nDelta fresco",
        payload="Delta fresco",
        message_id=1,
        sender_id=2,
    )

    fresh = abc_memory.freshness(mem, run, record["patch_file"])
    assert fresh["stale_input"] is False
    assert fresh["source"] == "telegram"
    assert fresh["message_id"] == "1"


def test_freshness_true_when_old_patch_used(tmp_path: Path):
    mem = tmp_path / "memory"
    run = tmp_path / "run"
    old = abc_memory.save_telegram_patch(mem, run, raw_text="rl:abc:\nOld", payload="Old", message_id=1, sender_id=2)
    new = abc_memory.save_telegram_patch(mem, run, raw_text="rl:abc:\nNew", payload="New", message_id=3, sender_id=4)
    assert old["patch_file"] != new["patch_file"]

    fresh = abc_memory.freshness(mem, run, old["patch_file"])
    assert fresh["stale_input"] is True
    assert fresh["reason"] == "used_patch_is_not_latest"


def test_report_consistency_respects_third_pressure_non_increase_constraint():
    payload = "Vincolo: non aumenta la pressione del terzo; riduce scenario terzo."
    constraints = abc_memory.extract_interpretive_constraints(payload)
    bad = abc_memory.report_consistency("Sintesi: pressione terzo aumentata nel breve.", constraints)
    good = abc_memory.report_consistency("Sintesi: pressione terzo stabile/non aumentata.", constraints)

    assert bad["consistent"] is False
    assert good["consistent"] is True


def test_memory_append_only_and_report_idempotence(tmp_path: Path):
    mem = tmp_path / "memory"
    run = tmp_path / "run"
    abc_memory.save_telegram_patch(mem, run, raw_text="rl:abc:\nUno", payload="Uno", message_id=1, sender_id=2)
    first_count = len(abc_memory.load_jsonl(mem / "events.jsonl"))
    abc_memory.save_telegram_patch(mem, run, raw_text="rl:abc:\nDue", payload="Due", message_id=2, sender_id=2)
    second_count = len(abc_memory.load_jsonl(mem / "events.jsonl"))
    abc_memory.build_reading(mem)
    third_count = len(abc_memory.load_jsonl(mem / "events.jsonl"))

    assert second_count == first_count + 1
    assert third_count == second_count
