#!/usr/bin/env python3
from __future__ import annotations
import argparse
import difflib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path("/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python")
DEFAULT_CORPUS = ROOT / "tests/data/teacher_redteam_v1.jsonl"
DEFAULT_RESULTS = ROOT / "tests/data/teacher_redteam_baseline_20260918.jsonl"
DEFAULT_REPORT = ROOT / "docs/notes/teacher-redteam-baseline-20260918.md"
GRAMMAR_SOCKET = Path("/run/ralf-teacher-grammar/grammar.sock")
CONCEPTS = ROOT / "ralfloop_agent/teacher/data/concept_evidence.tsv"
CORE_BIN = ROOT / "tools/teacher_core_mcp/ralf-teacher-core-mcp"
SCHEMA = {"type":"object","additionalProperties":False,"required":["response"],
          "properties":{"response":{"type":"string"},"correct":{"type":"boolean"}}}
STOPWORDS = {"questo","quello","perché","come","cosa","sono","della","delle","degli",
             "anche","allora","quindi","solo","senza","voglio","capito","ancora","sempre"}

def load_corpus(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def wait_socket(path: Path, timeout: float = 8.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return
        time.sleep(0.05)
    raise RuntimeError(f"socket_not_ready:{path}")


def extract_student_text(tool: str, args: dict) -> str:
    for key in ("question","concept","student_attempt","student_answer","objective","topic"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(args.get("material") or "")[:1200]


def parse_model_content(content: str) -> dict:
    text = content.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = {"response": text}
    if not isinstance(value, dict):
        value = {"response": text}
    response = value.get("response")
    if not isinstance(response, str) or not response.strip():
        value["response"] = text or "Non riesco a formulare una risposta."
    return {k:v for k,v in value.items() if k in {"response","correct"}}

class CpuInferenceProxy:
    def __init__(self, sock: Path, model: str, base_url: str, timeout: float,
                 slot_id: int | None = None):
        self.sock = sock
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.slot_id = slot_id
        self.stop = threading.Event()
        self.thread = None
        self.calls = []

    def start(self) -> None:
        self.sock.unlink(missing_ok=True)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        wait_socket(self.sock)

    def _infer(self, system_prompt: str, user_prompt: str) -> dict:
        try:
            ctx = json.loads(user_prompt)
        except json.JSONDecodeError:
            ctx = {}
        pedagogy = ctx.get("pedagogy") if isinstance(ctx, dict) else {}
        model_path = pedagogy.get("model_path") if isinstance(pedagogy, dict) else None
        first_cap = 320 if model_path == "deep" else 220
        full_cap = 480 if model_path == "deep" else 320
        messages = [{"role":"system","content":system_prompt},
                    {"role":"user","content":user_prompt}]

        def one_request(max_tokens: int):
            if not self.base_url.endswith(":11434"):
                payload = {"model":self.model,"messages":messages,"stream":False,
                           "temperature":0,"max_tokens":max_tokens,
                           "cache_prompt":True,
                           "response_format":{"type":"json_schema","json_schema":{
                               "name":"teacher_response","strict":True,"schema":SCHEMA}}}
                if self.slot_id is not None:
                    payload["slot_id"] = self.slot_id
                endpoint = self.base_url + "/v1/chat/completions"
            else:
                payload = {"model":self.model,"messages":messages,"stream":False,
                           "format":SCHEMA,"keep_alive":"10m",
                           "options":{"num_gpu":0,"num_ctx":2048,
                                      "num_predict":max_tokens,"temperature":0}}
                endpoint = self.base_url + "/api/chat"
            req = Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode(),
                          headers={"Content-Type":"application/json"}, method="POST")
            with urlopen(req, timeout=self.timeout) as response:
                envelope = json.loads(response.read(512 * 1024))
            if not self.base_url.endswith(":11434"):
                choice = envelope.get("choices", [{}])[0]
                return choice.get("message", {}).get("content", ""), choice.get("finish_reason")
            return envelope.get("message", {}).get("content", ""), envelope.get("done_reason")

        started = time.monotonic()
        content, finish_reason = one_request(first_cap)
        retried = finish_reason in {"length", "max_tokens"}
        if retried:
            content, finish_reason = one_request(full_cap)
        result = parse_model_content(content)
        self.calls.append({"model_path":model_path,
                           "slot_id":self.slot_id,
                           "retried":retried,
                           "finish_reason":finish_reason,
                           "seconds":round(time.monotonic()-started,3)})
        return result

    def _serve(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.sock))
            server.listen(8)
            server.settimeout(0.25)
            while not self.stop.is_set():
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                with client:
                    line = client.makefile("rb").readline(2 * 1024 * 1024)
                    try:
                        request = json.loads(line)
                        result = self._infer(request["system_prompt"], request["user_prompt"])
                        out = {"ok":True,"result":result}
                    except Exception as exc:
                        out = {"ok":False,"error":f"{type(exc).__name__}:{exc}"}
                    try:
                        client.sendall((json.dumps(out, ensure_ascii=False)+"\n").encode())
                    except (BrokenPipeError, ConnectionResetError):
                        pass

    def close(self) -> None:
        self.stop.set()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as poke:
                poke.connect(str(self.sock))
                poke.sendall(b"{}\n")
        except OSError:
            pass
        if self.thread:
            self.thread.join(timeout=2)
        self.sock.unlink(missing_ok=True)


class MCPProcess:
    def __init__(self, env: dict):
        self.proc = subprocess.Popen(
            [str(PYTHON), str(ROOT/"scripts/ralf_teacher_mcp_server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env)
        self.seq = 0

    def rpc(self, method: str, params: dict | None = None) -> dict:
        self.seq += 1
        req = {"jsonrpc":"2.0","id":self.seq,"method":method}
        if params is not None:
            req["params"] = params
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(req, ensure_ascii=False)+"\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()

        if not line:
            err = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError("teacher_mcp_closed:" + err[-2000:])
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(str(response["error"]))
        return response["result"]

    def call(self, name: str, args: dict) -> dict:
        raw = self.rpc("tools/call", {"name":name,"arguments":args})
        return raw.get("structuredContent") or {}

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def latest_event(db: Path, session_id: str) -> dict:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT kind,payload_json FROM events WHERE session_id=? ORDER BY event_id DESC LIMIT 1",
            (session_id,)).fetchone()
    if not row:
        return {}
    payload = json.loads(row[1])
    payload["kind"] = row[0]
    return payload


def contains_any(text: str, group: list[str]) -> bool:
    low = text.casefold()
    expanded = low.replace("÷", " divid ").replace("×", " moltiplic ")
    words = re.findall(r"[a-zà-ÿ0-9]+", expanded)
    for term in group:
        needle = term.casefold().strip()
        if needle in expanded:
            return True
        if re.fullmatch(r"[a-zà-ÿ]+", needle) and len(needle) >= 5:
            stem = needle[:4]
            if any(word.startswith(stem) for word in words):
                return True
    return False

def score_turn(case: dict, turn: dict, output: dict, event: dict, previous: str) -> dict:
    expect = turn.get("expect") or {}
    text = str(output.get("response") or "")
    required = expect.get("required_any") or []
    forbidden = expect.get("forbidden_any") or []
    required_hits = [contains_any(text, group) for group in required]
    forbidden_hits = [term for term in forbidden if term.casefold() in text.casefold()]
    move_expected = expect.get("expected_move")
    strategy_expected = expect.get("expected_strategy")
    mode_expected = expect.get("expected_mode")
    path_expected = expect.get("expected_model_path")
    pedagogy = output.get("pedagogy") if isinstance(output.get("pedagogy"), dict) else {}
    actual_move = event.get("student_move")
    actual_strategy = pedagogy.get("strategy") or event.get("strategy")
    actual_mode = pedagogy.get("mode") or event.get("mode")
    actual_path = pedagogy.get("model_path") or event.get("model_path")
    max_chars = int(expect.get("max_chars") or 12000)
    similarity = difflib.SequenceMatcher(None, previous.casefold(), text.casefold()).ratio() if previous else 0.0
    student_text = extract_student_text(turn["tool"], turn["args"])
    anchors = [w for w in re.findall(r"[A-Za-zÀ-ÿ0-9']{5,}", student_text.casefold())
               if w not in STOPWORDS]
    point_match = (not anchors) or any(a in text.casefold() for a in anchors[:8]) or all(required_hits[:1])
    question_ok = ("?" in text) if expect.get("question_expected") else True
    no_solution = not forbidden_hits

    move_ok = (actual_move == move_expected) if move_expected else True
    strategy_ok = (actual_strategy == strategy_expected) if strategy_expected else True
    mode_ok = (actual_mode == mode_expected) if mode_expected else True
    path_ok = (actual_path == path_expected) if path_expected else True
    correction_markers = ("non basta","non è sufficiente","semplif","in realtà",
                          "distingu","corregg","però")
    correction_ok = True
    if strategy_expected == "error_analysis":
        correction_ok = any(x in text.casefold() for x in correction_markers) or move_ok
    actionable = question_ok or any(x in text.casefold() for x in
                                    ("prova","controll","calcola","confronta","scrivi","verifica"))
    dimensions = {
        "conceptual_correctness": all(required_hits) and no_solution and bool(text.strip()),
        "misconception_recognition": move_ok,
        "responds_to_point": point_match,
        "scaffolding": no_solution and (turn["tool"] != "teacher.hint" or len(text) <= max_chars),
        "no_early_solution": no_solution,
        "corrects_prior_simplification": correction_ok,
        "actionable_feedback": actionable,
        "level_adaptation": mode_ok and len(text) <= max_chars,
        "clarity": bool(text.strip()) and (chr(96)*3) not in text and len(text) <= max_chars * 2,
        "brevity": len(text) <= max_chars,
        "no_unnecessary_repetition": similarity < 0.78,
        "counterexample_handling": move_ok if move_expected == "counterexample" else True,
        "diagnostic_capacity": question_ok,
        "multi_turn_continuity": point_match and similarity < 0.88,
        "model_routing": path_ok,
        "pedagogical_policy": strategy_ok,
    }

    return {"dimensions":dimensions,"required_hits":required_hits,
            "forbidden_hits":forbidden_hits,
            "move":{"expected":move_expected,"actual":actual_move},
            "strategy":{"expected":strategy_expected,"actual":actual_strategy},
            "mode":{"expected":mode_expected,"actual":actual_mode},
            "model_path":{"expected":path_expected,"actual":actual_path},
            "response_chars":len(text),"similarity_previous":round(similarity,3)}


def classify_failures(score: dict) -> list[str]:
    d = score["dimensions"]
    failures = []
    if not d["misconception_recognition"]:
        failures.append("classification_turn")
    if not d["pedagogical_policy"] or not d["scaffolding"] or not d["no_early_solution"]:
        failures.append("pedagogical_policy")
    if not d["level_adaptation"]:
        failures.append("learner_model")
    if not d["model_routing"]:
        failures.append("routing_model")
    if not d["multi_turn_continuity"] or not d["no_unnecessary_repetition"]:
        failures.append("memory_multi_turn")
    if not d["conceptual_correctness"] or not d["responds_to_point"] or not d["corrects_prior_simplification"]:
        failures.append("prompt_or_grounding")
    if not d["clarity"] or not d["brevity"]:
        failures.append("response_quality")
    return sorted(set(failures))

def probe_support(core_sock: Path, case: dict, text: str) -> dict:
    sys.path.insert(0, str(ROOT))
    from ralfloop_agent.teacher.core_client import TeacherCoreClient
    from ralfloop_agent.teacher.grammar_client import GrammarEvidenceClient
    core = TeacherCoreClient(str(core_sock), timeout=3.0)
    probe = {}
    try:
        probe["turn"] = core.classify_turn(text)
    except Exception as exc:
        probe["turn_error"] = str(exc)
    try:
        probe["concept"] = core.concept_evidence(case["topic"])
    except Exception as exc:
        probe["concept_error"] = str(exc)
    scope = f'{case["subject"]} {case["topic"]}'.casefold()
    if any(x in scope for x in ("ital","grammat","lingu","morfolog","sintass","ortograf")):
        try:
            probe["grammar"] = GrammarEvidenceClient(str(GRAMMAR_SOCKET)).evidence_for(text[:1200])
        except Exception as exc:
            probe["grammar_error"] = str(exc)
    return probe


def render_report(results: list[dict], metadata: dict) -> str:
    dim = defaultdict(lambda:[0,0])
    categories = Counter()
    for case in results:
        for turn in case["turns"]:
            if turn.get("error"):
                categories["runtime"] += 1
                continue

            for key, ok in turn["score"]["dimensions"].items():
                dim[key][1] += 1
                dim[key][0] += int(bool(ok))
            categories.update(turn["failure_classes"])
    lines = ["# Teacher Bot-tazzi — baseline red-team pedagogica 2026-09-18","",
             "## Esecuzione",
             f"- casi: {len(results)}; turni: {sum(len(x['turns']) for x in results)}",
             f"- modello baseline: {metadata['model']} su inferenza locale CPU-only controllata",
             "- superficie: Teacher MCP reale; DB temporaneo; Core MCP C temporaneo; Grammar MCP reale",
             f"- tool esposti: {metadata.get('tool_count')} (attesi 13)",
             f"- backend produzione osservato e non modificato: {metadata.get('production_current')}","",
             "## Rubrica automatica","",
             "| Dimensione | Pass | Totale | % |","|---|---:|---:|---:|"]
    for key in sorted(dim):
        passed,total = dim[key]
        pct = 100.0*passed/total if total else 0
        lines.append(f"| {key} | {passed} | {total} | {pct:.1f}% |")
    lines += ["","## Failure per classe","","| Classe | Occorrenze |","|---|---:|"]
    for key,count in categories.most_common():
        lines.append(f"| {key} | {count} |")
    lines += ["","## Casi con failure reali","","| Caso | Persona | Turni falliti | Esempio |",
              "|---|---|---:|---|"]
    for case in results:
        failed = [t for t in case["turns"] if t.get("failure_classes") or t.get("error")]
        if not failed:
            continue

        example = failed[0]
        excerpt = (example.get("response") or example.get("error") or "").replace("|","/").replace("\n"," ")[:180]
        lines.append(f"| {case['id']} | {case['persona']} | {len(failed)}/{len(case['turns'])} | {excerpt} |")
    lines += ["","## Dettaglio dei failure più informativi",""]
    shown = 0
    for case in results:
        for turn in case["turns"]:
            if not turn.get("failure_classes"):
                continue
            lines.append(f"### {case['id']} · turno {turn['index']} · {turn['tool']}")
            lines.append("Studente: " + turn["student_text"])
            lines.append("Tutor: " + str(turn.get("response") or turn.get("error") or "[nessuna risposta]"))
            lines.append("Failure: " + ", ".join(turn["failure_classes"]))
            lines.append("")
            shown += 1
            if shown >= 15:
                break
        if shown >= 15:
            break
    lines += ["## Limiti della baseline","",
              "- Le metriche lessicali sono conservative: segnalano candidati failure, non sostituiscono una revisione pedagogica umana.",
              f"- Il modello di prova è {metadata.get('model','unknown')} CPU; il routing fast/deep viene verificato dalla decisione pedagogica, ma entrambi i percorsi usano la stessa classe di replica CPU in questo run isolato.",
              "- Il corpus è avversariale e piccolo: serve come regression suite, non come stima della qualità media.",
              "- La UI non è esercitata in questo blocco: il test passa dalla superficie MCP studente.",""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()

    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--model", default="gemma3:4b")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--slot-id", type=int, default=None)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    corpus = load_corpus(args.corpus)
    if args.offset:
        corpus = corpus[args.offset:]
    if args.limit:
        corpus = corpus[:args.limit]
    if not PYTHON.exists() or not CORE_BIN.exists() or not GRAMMAR_SOCKET.exists():
        raise SystemExit("teacher_redteam_preflight_missing_dependency")
    results = []
    tmp = tempfile.TemporaryDirectory(prefix="teacher-redteam-")
    tmp_path = Path(tmp.name)
    db = tmp_path/"students.sqlite3"
    core_sock = tmp_path/"core.sock"
    inf_sock = tmp_path/"inference.sock"
    core = subprocess.Popen(
        [str(CORE_BIN),"--socket",str(core_sock),"--allow-uid",str(os.getuid()),
         "--concepts",str(CONCEPTS)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    proxy = CpuInferenceProxy(inf_sock,args.model,args.ollama,args.timeout,
                              slot_id=args.slot_id)
    teacher = None
    try:
        wait_socket(core_sock)
        proxy.start()

        env = os.environ.copy()
        env.update({"PYTHONPATH":str(ROOT),"RALF_TEACHER_DB":str(db),
                    "RALF_TEACHER_INFERENCE_SOCKET":str(inf_sock),
                    "RALF_TEACHER_GRAMMAR_SOCKET":str(GRAMMAR_SOCKET),
                    "RALF_TEACHER_CORE_SOCKET":str(core_sock)})
        teacher = MCPProcess(env)
        teacher.rpc("initialize",{"protocolVersion":"2025-03-26","capabilities":{},
                                  "clientInfo":{"name":"teacher-redteam","version":"1"}})
        listed = teacher.rpc("tools/list",{}).get("tools",[])
        tool_names = {x.get("name") for x in listed}
        if len(tool_names) != 13:
            raise RuntimeError(f"student_surface_changed:{sorted(tool_names)}")
        for ci,case in enumerate(corpus,1):
            login = teacher.call("teacher.login",{
                "card_id":f"REDTEAM-{case['id']}",
                "school_level":case["school_level"],
                "class_year":case.get("class_year"),
                "learner_profile":case.get("profile")})
            student_id = login["student"]["student_id"]
            session = teacher.call("teacher.start_session",{
                "student_id":student_id,"subject":case["subject"],"topic":case["topic"]})
            sid = session["session"]["session_id"]
            previous = ""
            out_case = {k:case[k] for k in ("id","persona","school_level","subject","topic")}
            out_case["turns"] = []
            for ti,turn in enumerate(case["turns"],1):
                call_args = dict(turn["args"])
                call_args["session_id"] = sid
                student_text = extract_student_text(turn["tool"],turn["args"])

                probe = probe_support(core_sock,case,student_text)
                started = time.monotonic()
                try:
                    output = teacher.call(turn["tool"],call_args)
                    if output.get("ok") is not True:
                        raise RuntimeError("teacher_tool_error:" + str(output.get("error") or "unknown"))
                    elapsed = round(time.monotonic()-started,3)
                    event = latest_event(db,sid)
                    score = score_turn(case,turn,output,event,previous)
                    failures = classify_failures(score)
                    response = str(output.get("response") or "")
                    row = {"index":ti,"tool":turn["tool"],"student_text":student_text,
                           "response":response,"seconds":elapsed,"output":output,
                           "event":event,"support_probe":probe,"score":score,
                           "failure_classes":failures}
                    previous = response
                except Exception as exc:
                    row = {"index":ti,"tool":turn["tool"],"student_text":student_text,
                           "error":f"{type(exc).__name__}:{exc}",
                           "failure_classes":["runtime"]}
                out_case["turns"].append(row)
            try:
                teacher.call("teacher.end_session",{"session_id":sid})
            except Exception:
                pass
            results.append(out_case)
            failed_count = sum(bool(t.get("failure_classes")) for t in out_case["turns"])
            print(f"[{ci:02d}/{len(corpus):02d}] {case['id']} failures={failed_count}",flush=True)
        production_current = str(Path("/home/sibilla-cumana/ralfloop-production/current").resolve())

        metadata = {"model":args.model,"tool_count":len(tool_names),
                    "production_current":production_current,"cpu_calls":proxy.calls,
                    "created_at":time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        args.results.parent.mkdir(parents=True,exist_ok=True)
        with args.results.open("w") as fh:
            fh.write(json.dumps({"_metadata":metadata},ensure_ascii=False)+"\n")
            for case in results:
                fh.write(json.dumps(case,ensure_ascii=False)+"\n")
        args.report.write_text(render_report(results,metadata))
        summary = Counter()
        for case in results:
            for turn in case["turns"]:
                summary.update(turn.get("failure_classes") or [])
        print("SUMMARY " + json.dumps({"cases":len(results),"failures":dict(summary),
              "results":str(args.results),"report":str(args.report)}),flush=True)
    finally:
        if teacher:
            teacher.close()
        proxy.close()
        if core.poll() is None:
            core.terminate()
            try:
                core.wait(timeout=2)
            except subprocess.TimeoutExpired:
                core.kill()
        tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
