import json
import tempfile
import unittest
from pathlib import Path

from ralfloop_agent import storage_research_queue as srq


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def critical_report():
    return {
        "host": "sibilla-cumana",
        "overall": "critical",
        "thresholds": {"critical_percent": 92.0},
        "filesystems": [
            {"path": "/", "used_percent": 95.0, "free_gib": 40.0, "severity": "critical"}
        ],
        "research_trigger": {
            "type": "disk_purchase_research",
            "reason": "storage_critical",
            "paths": ["/"],
            "requested_action": "research_replacement_or_expansion_disks",
        },
    }


class StorageResearchQueueTests(unittest.TestCase):
    def test_no_trigger_does_not_call_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fail(*args, **kwargs):
                raise AssertionError("opener must not be called")
            result = srq.enqueue_report({"research_trigger": None}, state_dir=Path(tmp), opener=fail)
        self.assertEqual(result, {"action": "no_trigger"})

    def test_critical_trigger_enqueues_durably_and_persists_marker(self):
        calls = []

        def opener(req, timeout):
            calls.append((req, timeout))
            if req.get_method() == "GET":
                return FakeResponse({"queue": {"jobs": []}})
            return FakeResponse({"ok": True, "job": {"job_id": "job-123"}})

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            result = srq.enqueue_report(critical_report(), state_dir=state, opener=opener)
            marker = json.loads((state / "last-enqueued-trigger.json").read_text(encoding="utf-8"))
        self.assertEqual(result, {"action": "enqueued", "job_id": "job-123"})
        self.assertEqual(marker["job_id"], "job-123")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0].get_method(), "GET")
        self.assertTrue(calls[0][0].full_url.endswith("/api/state"))
        self.assertEqual(calls[1][0].get_method(), "POST")
        payload = json.loads(calls[1][0].data.decode("utf-8"))
        self.assertFalse(payload["auto_start"])
        self.assertIn("Filesystem critici", payload["prompt"])
        self.assertEqual(calls[1][0].headers.get("X-bottazzi-frontend"), "1")

    def test_local_marker_skips_queue_lookup_and_post(self):
        report = critical_report()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "last-enqueued-trigger.json").write_text(
                json.dumps({"trigger": report["research_trigger"], "job_id": "job-existing"}),
                encoding="utf-8",
            )

            def fail(*args, **kwargs):
                raise AssertionError("duplicate must not touch queue")

            result = srq.enqueue_report(report, state_dir=state, opener=fail)
        self.assertEqual(result, {"action": "duplicate_skipped", "job_id": "job-existing"})

    def test_queue_duplicate_is_persisted_without_post(self):
        report = critical_report()
        trigger_text = srq._canonical(report["research_trigger"])
        calls = []

        def opener(req, timeout):
            calls.append((req, timeout))
            if req.get_method() != "GET":
                raise AssertionError("existing queue job must prevent POST")
            return FakeResponse({
                "queue": {
                    "jobs": [
                        {
                            "job_id": "job-queued",
                            "state": "failed",
                            "prompt": "prefix\nTrigger: " + trigger_text + "\nsuffix",
                        }
                    ]
                }
            })

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            result = srq.enqueue_report(report, state_dir=state, opener=opener)
            marker = json.loads((state / "last-enqueued-trigger.json").read_text(encoding="utf-8"))
        self.assertEqual(result, {"action": "queue_duplicate_skipped", "job_id": "job-queued"})
        self.assertEqual(marker["job_id"], "job-queued")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0].get_method(), "GET")


if __name__ == "__main__":
    unittest.main()
