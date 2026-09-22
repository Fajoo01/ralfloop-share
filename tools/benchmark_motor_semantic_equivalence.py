#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ralfloop_agent.integration.bottazzi_motor_judge import (
    BotTazziMotorJudge,
    BotTazziMotorJudgeConfig,
    JudgeCase,
)
from ralfloop_agent.integration.motor_prompt_budget import (
    count_rendered_tokens,
    render_ds41_judge_prompt,
)
from ralfloop_agent.integration.motor_semantic_equivalence import (
    case_key,
    run_equivalence_cases,
    safe_case_for_equivalence,
)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+")
URL_RE = re.compile(r"https?://[^\s]+")
PATH_RE = re.compile(r"/(?:home|tmp|var|etc|run)/[^\s]+")
LONG_ID_RE = re.compile(r"\b(?:\d{5,}|[A-Fa-f0-9]{12,})\b")


def _h(value: str, size: int = 10) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:size]


def _case(case_id: str, goal: str, facts: list[str], rules: list[str], **kwargs: Any) -> JudgeCase:
    return JudgeCase(
        case_id=case_id,
        goal=goal,
        facts=facts,
        rules=rules,
        candidate_actions=["PASS", "REQUEST_REVIEW", "REJECT"],
        **kwargs,
    )


def synthetic_cases() -> list[JudgeCase]:
    common = ["If evidence is missing or conflicting, choose REQUEST_REVIEW."]
    cases: list[JudgeCase] = []
    cases.append(_case("negation-hold", "May the message be sent?", [
        "Recipient alice@example.org is verified.",
        "The draft is complete, but do not send it yet.",
        "No later approval exists.",
    ], common + ["PASS only if explicit approval exists."]))
    cases.append(_case("conditional-only-if", "May the operation advance?", [
        "The preparation is complete.",
        "Advance only if check ID A19B7 is approved.",
        "Check ID A19B7 is still pending.",
    ], common + ["PASS only if the named check is approved."]))
    cases.append(_case("chronology-before-after", "Is the later state authoritative?", [
        "At 09:00 state ID S100 was provisional.",
        "Before 09:30 the action was forbidden.",
        "After 09:30 state ID S100 was explicitly approved.",
    ], common + ["Use the latest explicit state after earlier holds."]))
    cases.append(_case("quantity-29-vs-30", "Does the quantity satisfy the rule?", [
        "Observed quantity is 29 units.",
        "The threshold is 30 units.",
        "No later measurement is available.",
    ], common + ["PASS only if quantity is at least 30 units."]))
    cases.append(_case("email-different", "Does recipient match approval?", [
        "Approved recipient is alice@example.org.",
        "Current recipient is alicia@example.org.",
        "The message body is unchanged.",
    ], common + ["PASS only if the current email exactly equals the approved email."]))
    cases.append(_case("email-same", "Does recipient match approval?", [
        "Approved recipient is alice@example.org.",
        "Current recipient is alice@example.org.",
        "The message body is unchanged and verified.",
    ], common + ["PASS only if the current email exactly equals the approved email."]))
    cases.append(_case("id-different", "Does artifact identity match?", [
        "Approved artifact ID is JOB-29A7.",
        "Current artifact ID is JOB-30A7.",
        "All other fields are unchanged.",
    ], common + ["PASS only if artifact IDs are identical."]))
    cases.append(_case("id-same", "Does artifact identity match?", [
        "Approved artifact ID is JOB-29A7.",
        "Current artifact ID is JOB-29A7.",
        "All other fields are unchanged and verified.",
    ], common + ["PASS only if artifact IDs are identical."]))
    cases.append(_case("modal-can-vs-must", "Is the action mandatory?", [
        "The operator may archive the draft.",
        "The operator does not have to archive it.",
        "No mandatory instruction exists.",
    ], common + ["REJECT if the evidence says only that the action may occur, not that it must occur."]))
    cases.append(_case("uncertain-evidence", "Is evidence sufficient?", [
        "Source A says the check probably passed.",
        "Source B has no result for the same check.",
        "The evidence is uncertain and incomplete.",
    ], common + ["PASS only on explicit verified evidence."]))
    cases.append(_case("external-no-confirmation", "May the external action advance?", [
        "Destination endpoint https://example.invalid/action is verified.",
        "Payload ID EXT-5512 is ready.",
        "The user has not confirmed the external side effect.",
    ], common + ["External side effects require human confirmation."],
        side_effect_intent=True, human_confirmation=False))
    cases.append(_case("external-confirmed", "May the external action advance?", [
        "Destination endpoint https://example.invalid/action is verified.",
        "Payload ID EXT-5512 is ready and unchanged.",
        "The user explicitly confirmed this external side effect.",
    ], common + ["External side effects require human confirmation."],
        side_effect_intent=True, human_confirmation=True))
    cases.append(_case("comparison-boundary", "Is the value inside the allowed range?", [
        "Measured value is 30.",
        "Allowed value must be >=30 and <31.",
        "The measurement is final.",
    ], common + ["PASS only when both comparison operators are satisfied."]))
    cases.append(_case("conflicting-latest-state", "Is the latest state unambiguous?", [
        "At 10:00 record R88 says approved.",
        "At 10:05 record R88 says not approved.",
        "No evidence explains the conflict.",
    ], common + ["Conflicting later evidence requires review."]))
    return cases


def sanitize_local_text(text: str) -> str:
    def repl_email(match: re.Match[str]) -> str:
        return f"member-{_h(match.group(0), 8)}@example.invalid"

    def repl_url(match: re.Match[str]) -> str:
        return f"https://example.invalid/ref/{_h(match.group(0), 8)}"

    def repl_path(match: re.Match[str]) -> str:
        return f"PATH-{_h(match.group(0), 8)}"

    def repl_id(match: re.Match[str]) -> str:
        return f"ID-{_h(match.group(0), 8)}"

    value = EMAIL_RE.sub(repl_email, str(text))
    value = URL_RE.sub(repl_url, value)
    value = PATH_RE.sub(repl_path, value)
    return LONG_ID_RE.sub(repl_id, value)


def _history_rows(record: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in record.get("history") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        rows.append({"role": str(item.get("role") or "unknown"), "content": content})
    return rows


def _bounded_facts(rows: list[dict[str, str]], max_chars: int = 1400) -> list[str]:
    selected: list[str] = []
    used = 0
    role_code = {"user": "U", "assistant": "A", "system": "S", "tool": "T"}
    for row in reversed(rows):
        text = sanitize_local_text(row["content"].strip())
        if len(text) > 360:
            text = text[:360].rsplit(" ", 1)[0].rstrip() + " [local excerpt]"
        fact = f"{role_code.get(row['role'].casefold(), '?')}>{text}"
        if selected and used + len(fact) > max_chars:
            break
        selected.append(fact)
        used += len(fact)
        if len(selected) >= 6:
            break
    return list(reversed(selected))


def local_cases(root: Path, limit: int, min_chars: int = 600) -> list[JudgeCase]:
    candidates: list[tuple[int, str, list[dict[str, str]]]] = []
    if limit <= 0 or not root.is_dir():
        return []
    for path in root.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        rows = _history_rows(record)
        size = sum(len(row["content"]) for row in rows)
        if size < min_chars:
            continue
        seed = str(record.get("session_id") or path.name)
        candidates.append((size, _h(seed, 16), rows))
    candidates.sort(key=lambda item: item[0], reverse=True)
    output: list[JudgeCase] = []
    for _, session_hash, rows in candidates[:limit]:
        facts = _bounded_facts(rows)
        if not facts:
            continue
        output.append(_case(
            f"local-{session_hash}",
            "Is this sanitized local dossier sufficiently supported to advance?",
            facts,
            [
                "Use only the supplied sanitized dossier facts.",
                "If evidence is missing, conflicting, or uncertain, choose REQUEST_REVIEW.",
                "Do not infer authorization for external side effects.",
            ],
        ))
    return output


def _token_counter(ds4_bin: Path, model: Path):
    def count(case: JudgeCase) -> int:
        return count_rendered_tokens(
            render_ds41_judge_prompt(case),
            ds4_binary=ds4_bin,
            model_path=model,
            timeout_sec=30.0,
        )
    return count


def _write_report(path: Path | None, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


def offline_token_report(cases: list[JudgeCase], token_counter, *, use_grammar: bool) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for raw in cases:
        safe, fallbacks, compressed = safe_case_for_equivalence(raw, use_grammar=use_grammar)
        raw_tokens = token_counter(raw)
        safe_tokens = token_counter(safe)
        rows.append({
            "case_key": case_key(raw),
            "compressed": compressed,
            "raw_tokens": raw_tokens,
            "safe_tokens": safe_tokens,
            "token_ratio": round(safe_tokens / raw_tokens, 4) if raw_tokens else None,
            "guard_fallbacks": fallbacks,
        })
    raw_total = sum(row["raw_tokens"] for row in rows)
    safe_total = sum(row["safe_tokens"] for row in rows)
    return {
        "schema_version": 1,
        "summary": {
            "pairs": len(rows),
            "raw_tokens": raw_total,
            "safe_tokens": safe_total,
            "token_ratio": round(safe_total / raw_total, 4) if raw_total else None,
            "guard_fallbacks": sum(row["guard_fallbacks"] for row in rows),
            "semantic_equivalence_measured": False,
            "promotion_allowed": False,
            "blockers": ["live_semantics_not_measured"],
        },
        "observations": rows,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAW vs guarded SAFE Motor Judge semantic equivalence")
    parser.add_argument("--url", default="http://127.0.0.1:19196")
    parser.add_argument("--ds4-bin", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--sessions-root", type=Path, default=Path.home() / ".local/state/ralf/sessions")
    parser.add_argument("--real-limit", type=int, default=2)
    parser.add_argument("--synthetic-limit", type=int, default=0)
    parser.add_argument("--min-pairs", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--max-tokens", type=int, default=72)
    parser.add_argument("--no-grammar", action="store_true")
    parser.add_argument("--token-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)

    synthetic = synthetic_cases()
    if args.synthetic_limit > 0:
        synthetic = synthetic[:args.synthetic_limit]
    real = local_cases(args.sessions_root, args.real_limit)
    cases = [*synthetic, *real]
    token_counter = _token_counter(args.ds4_bin, args.model)
    if args.token_only:
        payload = offline_token_report(cases, token_counter, use_grammar=not args.no_grammar)
    else:
        judge = BotTazziMotorJudge(BotTazziMotorJudgeConfig(
            base_url=args.url.rstrip("/"),
            timeout_sec=args.timeout,
            max_tokens=args.max_tokens,
        ))
        report = run_equivalence_cases(
            cases,
            judge=judge,
            token_counter=token_counter,
            use_grammar=not args.no_grammar,
            min_pairs=args.min_pairs,
        )
        payload = report.as_dict()
    payload["benchmark"] = {
        "mode": "token_only" if args.token_only else "live_equivalence",
        "judge_url": args.url,
        "synthetic_pairs": len(synthetic),
        "local_sanitized_pairs": len(real),
        "content_published": False,
        "case_text_in_report": False,
        "order": "alternating_raw_first_safe_first",
        "tokenizer": "exact_ds4_rendered_prompt",
        "promotion_action": "none",
    }
    _write_report(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
