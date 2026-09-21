from pathlib import Path
from types import SimpleNamespace
import json

import scripts.ralf_md_goodify_whatsapp_worker as worker


class FakeFlow:
    def __init__(self):
        self.calls = []

    def process_qr(self, payload):
        self.calls.append(payload)
        return {
            "ok": True,
            "status": "DONATED_TO_TIREMM",
            "donation_id": "donation-test-1",
            "already_processed": False,
        }


def _message(message_id: str, ts: int = 9999999999999):
    return {
        "message_id": message_id,
        "jid": "self@lid",
        "kind": "image",
        "timestamp_ms": ts,
    }


def _setup(monkeypatch, tmp_path: Path, message_id: str):
    recent = tmp_path / "recent.json"
    media = tmp_path / "media"
    media.mkdir()
    recent.write_text(json.dumps({"messages": [_message(message_id)]}), encoding="utf-8")
    (media / f"{message_id}.bin").write_bytes(b"image-bytes")
    monkeypatch.setattr(worker, "RECENT_FILE", recent)
    monkeypatch.setattr(worker, "MEDIA_DIR", media)
    monkeypatch.setattr(worker, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(worker, "TARGET_JID", "self@lid")
    monkeypatch.setattr(worker, "BACKFILL_SECONDS", 86400)
    return recent, media


def test_whatsapp_worker_ignores_non_qr(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, "wa_msg_1111111111111111")
    monkeypatch.setattr(worker, "_decode_md_qr", lambda _path: None)
    flow = FakeFlow()
    result = worker.run_once(flow)
    assert result["media_seen_now"] == 1
    assert result["qr_found_now"] == 0
    assert flow.calls == []
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["processed"]["wa_msg_1111111111111111"]["status"] == "NO_MD_QR"


def test_whatsapp_worker_processes_qr_once(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, "wa_msg_2222222222222222")
    monkeypatch.setattr(
        worker, "_decode_md_qr", lambda _path: SimpleNamespace(payload="QR-PAYLOAD")
    )
    flow = FakeFlow()
    first = worker.run_once(flow)
    second = worker.run_once(flow)
    assert first["qr_found_now"] == 1
    assert first["donated_now"] == 1
    assert second["media_seen_now"] == 0
    assert flow.calls == ["QR-PAYLOAD"]
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    row = state["processed"]["wa_msg_2222222222222222"]
    assert row["status"] == "DONATED_TO_TIREMM"
    assert row["donation_id"] == "donation-test-1"
