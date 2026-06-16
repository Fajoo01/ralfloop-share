"""Append-only local memory for the ABC relational/forensic model."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVENT_TYPES = {
    "observed_fact",
    "message",
    "movement",
    "logistic",
    "emotional_signal",
    "model_update",
}
STATUS_VALUES = {"active", "weakened", "paused", "discarded"}
REBOUND_VALUES = {"low", "medium", "high", "unknown"}
FIELD_VALUES = {"cold", "neutral", "warm_non_pressing", "pressing", "unknown"}


DEFAULT_HYPOTHESES = {
    "fabio_papabile_reale_ma_congelato": {
        "confidence": 0.65,
        "status": "active",
        "notes": "Default iniziale prudente: compatibile con campo reale ma non convertito.",
    },
    "solo_logistica_appoggio": {
        "confidence": 0.25,
        "status": "active",
        "notes": "Ipotesi contraria da tenere viva: ruolo pratico senza scelta simbolica.",
    },
    "antonluca_nodo_sporco_non_chiuso": {
        "confidence": 0.65,
        "status": "active",
        "notes": "Terzo ancora non risolto; non trasformare ipotesi in certezza.",
    },
}


DEFAULT_STATE = {
    "curve": 60,
    "third_pressure": 54,
    "rebound_risk": "medium",
    "fabio_field": "warm_non_pressing",
    "recommended_action": "do_nothing_active",
    "forbidden_moves": [
        "rilanciare film",
        "chiedere di AntonLuca",
        "fare domande su lunedi sera",
        "forzare cena/cinema",
    ],
}


RULES_MD = """# ABC memory rules

- Separare sempre fatti osservati, interpretazioni, ipotesi contrarie, confidenza e azione.
- Non trasformare segnali ambigui in certezze.
- Usare formule caute: compatibile con, indebolisce, rafforza leggermente, non basta per concludere.
- Mantenere vive le ipotesi contrarie finche' non sono contraddette da evidence forte.
- Non leggere logistica come scelta simbolica automatica.
- Non usare la memoria come predizione rigida.
- Prossima azione minima prima di mossa emotiva.
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def memory_dir(path: str | None = None) -> Path:
    return Path(path or os.environ.get("ABC_MEMORY_DIR", "abc_memory"))


def ensure_init(base: Path) -> None:
    base.mkdir(parents=True, exist_ok=True)
    for name in ("events.jsonl", "timeline.jsonl", "contradictions.jsonl", "sources.jsonl", "readings.jsonl"):
        (base / name).touch(exist_ok=True)
    _write_json_if_missing(base / "hypotheses.json", _with_timestamps(DEFAULT_HYPOTHESES))
    _write_json_if_missing(base / "state.json", {**DEFAULT_STATE, "last_updated": utc_now()})
    _write_json_if_missing(base / "model_state.json", {**DEFAULT_STATE, "last_updated": utc_now()})
    if not (base / "rules.md").exists():
        (base / "rules.md").write_text(RULES_MD, encoding="utf-8")
    if not (base / "last_reading.md").exists():
        (base / "last_reading.md").write_text("", encoding="utf-8")


def _with_timestamps(data: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    now = utc_now()
    return {k: {**v, "last_updated": now} for k, v in data.items()}


def _write_json_if_missing(path: Path, data: Any) -> None:
    if not path.exists():
        write_json(path, data)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def add_event(
    base: Path,
    *,
    date: str,
    text: str,
    event_type: str,
    confidence: float,
    interpretation: str = "",
    counter_hypothesis: str = "",
    tags: list[str] | None = None,
    effect_on_model: dict[str, str] | None = None,
) -> dict[str, Any]:
    ensure_init(base)
    if event_type not in EVENT_TYPES:
        raise ValueError(f"invalid event type: {event_type}")
    row = {
        "id": str(uuid.uuid4()),
        "date": date,
        "created_at": utc_now(),
        "type": event_type,
        "text": text,
        "evidence_strength": max(0.0, min(1.0, float(confidence))),
        "interpretation": interpretation,
        "counter_hypothesis": counter_hypothesis,
        "effect_on_model": effect_on_model
        or {
            "fabio_field": "unknown",
            "third_pressure": "unknown",
            "rebound_risk": "unknown",
            "trust": "unknown",
        },
        "tags": tags or [],
    }
    append_jsonl(base / "events.jsonl", row)
    append_jsonl(base / "timeline.jsonl", row)
    return row


def parse_abc_command(text: str) -> dict[str, Any]:
    raw = text or ""
    match = re.match(r"(?is)^\s*(rl|rsc)(full)?(?:\s*:?\s*(abc|full))?\s*:?\s*(.*)\s*$", raw)
    if not match:
        return {"is_abc": False, "payload": ""}
    prefix, full_suffix, target, payload = match.groups()
    command = (prefix or "").lower()
    target = (target or "").lower()
    payload = payload or ""
    if full_suffix:
        target = "full"
    is_abc = command in {"rl", "rsc"} and target in {"", "abc", "full"}
    if not is_abc:
        return {"is_abc": False, "payload": ""}
    report_kind = "full" if target == "full" else "extended"
    return {
        "is_abc": True,
        "command": command,
        "target": target or "abc",
        "report_kind": report_kind,
        "payload": payload.strip(),
        "has_payload": bool(payload.strip()),
    }


def payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def save_telegram_patch(
    base: Path,
    run_dir: Path,
    *,
    raw_text: str,
    payload: str,
    message_id: str | int | None,
    sender_id: str | int | None,
    created_at: str | None = None,
) -> dict[str, Any]:
    ensure_init(base)
    run_dir.mkdir(parents=True, exist_ok=True)
    now = created_at or utc_now()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    digest = payload_hash(payload)
    patch_file = run_dir / f"RL_ABC_PATCH_TELEGRAM_{stamp}_{digest[:8]}.txt"
    metadata = {
        "source": "telegram",
        "message_id": str(message_id or ""),
        "sender_id": str(sender_id or ""),
        "created_at": now,
        "mtime": None,
        "payload_hash": digest,
        "payload_length": len(payload),
        "first_line": payload.splitlines()[0] if payload.splitlines() else "",
        "last_line": payload.splitlines()[-1] if payload.splitlines() else "",
    }
    header = "\n".join(f"{k.upper()}={v}" for k, v in metadata.items() if k != "mtime")
    patch_file.write_text(f"{header}\n\n{raw_text.rstrip()}\n", encoding="utf-8")
    current = run_dir / "RL_ABC_PATCH_TELEGRAM_CURRENT.txt"
    current.write_text(patch_file.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    metadata["mtime"] = patch_file.stat().st_mtime
    record = {
        "id": str(uuid.uuid4()),
        "kind": "telegram_patch",
        "patch_file": str(patch_file),
        "current_alias": str(current),
        **metadata,
    }
    append_jsonl(base / "sources.jsonl", record)
    add_event(
        base,
        date=now[:10],
        text=payload,
        event_type="model_update",
        confidence=1.0,
        interpretation="Delta Telegram raw indicizzato; lettura da derivare senza hardcode semantico.",
        counter_hypothesis="Il payload puo' contenere vincoli o ipotesi non ancora verificati.",
        tags=["telegram", "rl:abc", "delta"],
    )
    reading = build_reading(base)
    append_jsonl(
        base / "readings.jsonl",
        {
            "id": str(uuid.uuid4()),
            "created_at": utc_now(),
            "source_patch": str(patch_file),
            "payload_hash": digest,
            "text": reading,
        },
    )
    return record


def latest_patch_record(base: Path, run_dir: Path) -> dict[str, Any] | None:
    ensure_init(base)
    records = [row for row in load_jsonl(base / "sources.jsonl") if row.get("kind") == "telegram_patch"]
    records = [row for row in records if Path(str(row.get("patch_file", ""))).exists()]
    if records:
        return max(records, key=lambda r: float(r.get("mtime") or 0.0))
    patches = list(run_dir.glob("RL_ABC_PATCH*.txt"))
    if not patches:
        return None
    patch = max(patches, key=lambda p: p.stat().st_mtime)
    text = patch.read_text(encoding="utf-8", errors="replace")
    return {
        "kind": "legacy_patch",
        "source": "filesystem",
        "patch_file": str(patch),
        "current_alias": str(run_dir / "RL_ABC_PATCH_TELEGRAM_CURRENT.txt"),
        "created_at": _metadata_value(text, "CREATED_AT") or datetime.fromtimestamp(patch.stat().st_mtime, timezone.utc).isoformat(),
        "mtime": patch.stat().st_mtime,
        "payload_hash": _metadata_value(text, "PAYLOAD_HASH") or payload_hash(text),
        "message_id": _metadata_value(text, "MESSAGE_ID") or "",
        "sender_id": _metadata_value(text, "SENDER_ID") or "",
    }


def _metadata_value(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}\s*=\s*(.+)$", text, re.I | re.M)
    return match.group(1).strip() if match else ""


def freshness(base: Path, run_dir: Path, used_patch: str | Path | None) -> dict[str, Any]:
    latest = latest_patch_record(base, run_dir)
    if latest is None:
        return {"stale_input": True, "reason": "no_patch_available"}
    latest_path = Path(str(latest["patch_file"])).resolve()
    used = Path(str(used_patch or latest_path)).resolve()
    now = datetime.now(timezone.utc).timestamp()
    age_hours = round((now - float(latest.get("mtime") or latest_path.stat().st_mtime)) / 3600, 3)
    stale = used != latest_path
    return {
        "stale_input": stale,
        "reason": "used_patch_is_not_latest" if stale else "latest_patch_used",
        "patch_file": str(latest_path),
        "mtime": float(latest.get("mtime") or latest_path.stat().st_mtime),
        "age_hours": age_hours,
        "source": latest.get("source", "unknown"),
        "message_id": latest.get("message_id", ""),
        "sender_id": latest.get("sender_id", ""),
        "payload_hash": latest.get("payload_hash", ""),
    }


def extract_interpretive_constraints(payload: str) -> list[dict[str, str]]:
    low = payload.lower()
    constraints: list[dict[str, str]] = []
    third_terms = ("pressione terzo", "pressione del terzo", "terzo")
    if any(term in low for term in third_terms) and re.search(r"non\s+(aumenta|aumentare|alzare|sale|cresce)|riduc", low):
        constraints.append(
            {
                "key": "third_pressure_non_increase",
                "target": "third_pressure",
                "polarity": "non_increase",
                "text": "Il payload vincola a non leggere la pressione terzo come aumentata salvo evidence successiva forte.",
            }
        )
    return constraints


def report_consistency(report: str, constraints: list[dict[str, str]]) -> dict[str, Any]:
    low = report.lower()
    contradictions: list[str] = []
    for constraint in constraints:
        if constraint.get("key") == "third_pressure_non_increase" and re.search(
            r"pressione (del )?terzo (aument|sale|cresce)|terzo aumentat", low
        ):
            contradictions.append("Report contraddice vincolo payload: pressione terzo non deve risultare aumentata.")
    return {"consistent": not contradictions, "contradictions": contradictions}


def add_contradiction(base: Path, *, date: str, text: str, confidence: float = 0.5) -> dict[str, Any]:
    ensure_init(base)
    row = {
        "id": str(uuid.uuid4()),
        "date": date,
        "created_at": utc_now(),
        "text": text,
        "evidence_strength": max(0.0, min(1.0, float(confidence))),
    }
    append_jsonl(base / "contradictions.jsonl", row)
    return row


def update_hypothesis(base: Path, key: str, confidence: float, note: str, status: str | None = None) -> dict[str, Any]:
    ensure_init(base)
    hypotheses = read_json(base / "hypotheses.json", {})
    current = dict(hypotheses.get(key, {}))
    current["confidence"] = max(0.0, min(1.0, float(confidence)))
    current["notes"] = note
    current["status"] = status or current.get("status", "active")
    if current["status"] not in STATUS_VALUES:
        raise ValueError(f"invalid hypothesis status: {current['status']}")
    current["last_updated"] = utc_now()
    hypotheses[key] = current
    write_json(base / "hypotheses.json", hypotheses)
    return current


def set_state(
    base: Path,
    *,
    curve: int | None = None,
    third_pressure: int | None = None,
    rebound_risk: str | None = None,
    fabio_field: str | None = None,
    recommended_action: str | None = None,
) -> dict[str, Any]:
    ensure_init(base)
    state = read_json(base / "model_state.json", {})
    if curve is not None:
        state["curve"] = int(curve)
    if third_pressure is not None:
        state["third_pressure"] = int(third_pressure)
    if rebound_risk is not None:
        if rebound_risk not in REBOUND_VALUES:
            raise ValueError(f"invalid rebound_risk: {rebound_risk}")
        state["rebound_risk"] = rebound_risk
    if fabio_field is not None:
        if fabio_field not in FIELD_VALUES:
            raise ValueError(f"invalid fabio_field: {fabio_field}")
        state["fabio_field"] = fabio_field
    if recommended_action is not None:
        state["recommended_action"] = recommended_action
    state["last_updated"] = utc_now()
    write_json(base / "model_state.json", state)
    return state


def state_payload(base: Path) -> dict[str, Any]:
    ensure_init(base)
    return {
        "model_state": read_json(base / "model_state.json", {}),
        "hypotheses": read_json(base / "hypotheses.json", {}),
        "timeline_count": len(load_jsonl(base / "timeline.jsonl")),
        "contradictions_count": len(load_jsonl(base / "contradictions.jsonl")),
    }


def search_memory(base: Path, query: str) -> list[dict[str, Any]]:
    ensure_init(base)
    q = query.lower()
    rows = load_jsonl(base / "timeline.jsonl") + load_jsonl(base / "contradictions.jsonl")
    return [row for row in rows if q in json.dumps(row, ensure_ascii=False).lower()]


def build_reading(base: Path, limit: int = 8) -> str:
    ensure_init(base)
    events = sorted(load_jsonl(base / "timeline.jsonl"), key=lambda x: (x.get("date", ""), x.get("created_at", "")))[-limit:]
    contradictions = sorted(load_jsonl(base / "contradictions.jsonl"), key=lambda x: (x.get("date", ""), x.get("created_at", "")))[-limit:]
    hypotheses = read_json(base / "hypotheses.json", {})
    state = read_json(base / "model_state.json", {})

    active = [
        (key, value)
        for key, value in hypotheses.items()
        if value.get("status", "active") in {"active", "weakened", "paused"}
    ]
    increased, decreased = _model_deltas(events)
    cannot = [
        "Non si può concludere che Arianna voglia Fabio come certezza.",
        "Non si può concludere che la logistica equivalga a scelta simbolica.",
        "Non si può chiudere l'ipotesi terzo senza evidence contraria forte.",
    ]
    if contradictions:
        cannot.append("Le contraddizioni recenti obbligano a mantenere lettura prudente.")

    lines = [
        "# ABC reading",
        "",
        "## Stato corrente",
        f"- curva: {state.get('curve', 'unknown')}",
        f"- pressione terzo: {state.get('third_pressure', 'unknown')}",
        f"- rebound_risk: {state.get('rebound_risk', 'unknown')}",
        f"- campo Fabio: {state.get('fabio_field', 'unknown')}",
        "",
        "## Ultimi eventi rilevanti",
    ]
    lines += [f"- {e.get('date')}: {e.get('text')} (confidenza {e.get('evidence_strength')})" for e in events] or ["- Nessun evento registrato."]
    lines += ["", "## Ipotesi attive"]
    lines += [
        f"- {key}: {value.get('status', 'active')} / {value.get('confidence', 'unknown')} - {value.get('notes', '')}"
        for key, value in active
    ] or ["- Nessuna ipotesi attiva."]
    lines += ["", "## Contraddizioni"]
    lines += [f"- {c.get('date')}: {c.get('text')}" for c in contradictions] or ["- Nessuna contraddizione registrata."]
    lines += ["", "## Cosa e' aumentato/diminuito"]
    lines += [f"- aumentato: {', '.join(increased) if increased else 'nessun incremento chiaro'}"]
    lines += [f"- diminuito: {', '.join(decreased) if decreased else 'nessuna diminuzione chiara'}"]
    lines += ["", "## Cosa NON si può concludere"]
    lines += [f"- {item}" for item in cannot]
    lines += ["", "## Prossima azione minima", f"- {state.get('recommended_action', 'do_nothing_active')}"]
    text = "\n".join(lines) + "\n"
    (base / "last_reading.md").write_text(text, encoding="utf-8")
    return text


def _model_deltas(events: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    labels = {
        "fabio_field": "campo Fabio",
        "third_pressure": "pressione terzo",
        "rebound_risk": "rischio rebound",
        "trust": "fiducia",
    }
    increased: list[str] = []
    decreased: list[str] = []
    for event in events:
        effect = event.get("effect_on_model") or {}
        for key, value in effect.items():
            if value == "+":
                increased.append(labels.get(key, key))
            elif value == "-":
                decreased.append(labels.get(key, key))
    return sorted(set(increased)), sorted(set(decreased))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ABC local relational memory")
    parser.add_argument("--dir", default=None, help="memory directory; default ABC_MEMORY_DIR or ./abc_memory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    add = sub.add_parser("add-event")
    add.add_argument("--date", required=True)
    add.add_argument("--text", required=True)
    add.add_argument("--type", default="observed_fact", choices=sorted(EVENT_TYPES))
    add.add_argument("--confidence", type=float, default=0.5)
    add.add_argument("--interpretation", default="")
    add.add_argument("--counter-hypothesis", default="")
    add.add_argument("--tags", default="")

    c = sub.add_parser("add-contradiction")
    c.add_argument("--date", required=True)
    c.add_argument("--text", required=True)
    c.add_argument("--confidence", type=float, default=0.5)

    h = sub.add_parser("update-hypothesis")
    h.add_argument("key")
    h.add_argument("--confidence", type=float, required=True)
    h.add_argument("--note", required=True)
    h.add_argument("--status", choices=sorted(STATUS_VALUES))

    s = sub.add_parser("set-state")
    s.add_argument("--curve", type=int)
    s.add_argument("--third-pressure", type=int)
    s.add_argument("--rebound-risk", choices=sorted(REBOUND_VALUES))
    s.add_argument("--fabio-field", choices=sorted(FIELD_VALUES))
    s.add_argument("--recommended-action")

    sub.add_parser("state")
    sub.add_parser("reading")
    search = sub.add_parser("search")
    search.add_argument("query")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = memory_dir(args.dir)
    try:
        if args.cmd == "init":
            ensure_init(base)
            print(json.dumps({"ok": True, "dir": str(base)}, ensure_ascii=False))
        elif args.cmd == "add-event":
            row = add_event(
                base,
                date=args.date,
                text=args.text,
                event_type=args.type,
                confidence=args.confidence,
                interpretation=args.interpretation,
                counter_hypothesis=args.counter_hypothesis,
                tags=[x.strip() for x in args.tags.split(",") if x.strip()],
            )
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        elif args.cmd == "add-contradiction":
            print(json.dumps(add_contradiction(base, date=args.date, text=args.text, confidence=args.confidence), ensure_ascii=False, sort_keys=True))
        elif args.cmd == "update-hypothesis":
            print(json.dumps(update_hypothesis(base, args.key, args.confidence, args.note, args.status), ensure_ascii=False, sort_keys=True))
        elif args.cmd == "set-state":
            print(json.dumps(set_state(base, curve=args.curve, third_pressure=args.third_pressure, rebound_risk=args.rebound_risk, fabio_field=args.fabio_field, recommended_action=args.recommended_action), ensure_ascii=False, sort_keys=True))
        elif args.cmd == "state":
            print(json.dumps(state_payload(base), ensure_ascii=False, indent=2, sort_keys=True))
        elif args.cmd == "reading":
            print(build_reading(base), end="")
        elif args.cmd == "search":
            print(json.dumps(search_memory(base, args.query), ensure_ascii=False, indent=2, sort_keys=True))
    except Exception as exc:
        print(f"abc_memory error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
