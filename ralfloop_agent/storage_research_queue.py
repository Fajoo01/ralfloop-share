from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib import request

DEFAULT_QUEUE_URL = "http://127.0.0.1:19201/api/jobs"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _critical_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in report.get("filesystems", []) if row.get("severity") == "critical"]


def build_job_payload(report: dict[str, Any]) -> dict[str, Any]:
    trigger = report.get("research_trigger")
    if not trigger:
        raise ValueError("research_trigger_missing")
    rows = _critical_rows(report)
    prompt = (
        "Lo storage checker ha rilevato una soglia critica. Verifica prima lo stato reale del sistema, "
        "hardware, interfacce disponibili, capacità e vincoli di ridondanza; poi ricerca sul mercato attuale "
        "dischi compatibili e alternative di espansione. Non acquistare e non migrare/cancellare dati senza "
        "una successiva approvazione esplicita. Usa repository/issue GitHub e runtime come fonti di verità.\n\n"
        f"Host: {report.get('host', '')}\n"
        f"Trigger: {_canonical(trigger)}\n"
        f"Filesystem critici: {_canonical(rows)}\n"
        f"Soglie: {_canonical(report.get('thresholds', {}))}"
    )
    return {
        "title": "Storage critical — ricerca nuovi dischi",
        "prompt": prompt,
        "project_name": "",
        "project_url": None,
        "auto_start": True,
    }


def enqueue_report(
    report: dict[str, Any],
    *,
    state_dir: Path,
    queue_url: str = DEFAULT_QUEUE_URL,
    opener: Callable[..., Any] = request.urlopen,
) -> dict[str, Any]:
    trigger = report.get("research_trigger")
    if not trigger:
        return {"action": "no_trigger"}

    state_dir.mkdir(parents=True, exist_ok=True)
    marker_path = state_dir / "last-enqueued-trigger.json"
    if marker_path.exists():
        try:
            previous = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
        if _canonical(previous.get("trigger")) == _canonical(trigger):
            return {"action": "duplicate_skipped", "job_id": previous.get("job_id")}

    payload = build_job_payload(report)
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        queue_url,
        data=body,
        headers={"Content-Type": "application/json", "X-Bottazzi-Frontend": "1"},
        method="POST",
    )
    with opener(req, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))
    job = result.get("job") or {}
    job_id = job.get("job_id")
    if not job_id:
        raise RuntimeError("queue_job_id_missing")
    marker_path.write_text(
        json.dumps({"trigger": trigger, "job_id": job_id}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"action": "enqueued", "job_id": job_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enqueue disk research when storage checker emits a critical trigger.")
    parser.add_argument("--report", default=os.getenv("STORAGE_CHECK_REPORT", ""))
    parser.add_argument("--state-dir", default=os.getenv("STORAGE_CHECK_STATE_DIR", str(Path.home() / ".local/state/ralfloop-storage-checker")))
    parser.add_argument("--queue-url", default=os.getenv("STORAGE_RESEARCH_QUEUE_URL", DEFAULT_QUEUE_URL))
    args = parser.parse_args(argv)
    if not args.report:
        raise SystemExit("report path required")
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    result = enqueue_report(report, state_dir=Path(args.state_dir), queue_url=args.queue_url)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
