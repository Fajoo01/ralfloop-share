from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex

import requests


@dataclass
class PlannerDecision:
    tool_name: str
    tool_input: dict
    why: str


class DeterministicPlanner:
    def _extract_path(self, goal: str) -> str | None:
        m = re.search(r'((?:out|tmp)/[^\s]+|[A-Za-z0-9_.\-/]+\.[A-Za-z0-9]+)', goal)
        if not m:
            return None
        return m.group(1).rstrip('.,;:')

    def _extract_dir(self, goal: str) -> str | None:
        goal_l = goal.lower()

        m = re.search(r'\b(?:cartella|directory|dir|folder)\s+([A-Za-z0-9_.\-/]+)', goal_l)
        if m:
            return m.group(1).rstrip('.,;:')

        m = re.search(r'\b(?:nella|nel)\s+cartella\s+([A-Za-z0-9_.\-/]+)', goal_l)
        if m:
            return m.group(1).rstrip('.,;:')

        m = re.search(r'\b(?:in|nella|nel)\s+((?:out|tmp)(?:/[A-Za-z0-9_.\-/]+)?)\b', goal_l)
        if m:
            return m.group(1).rstrip('.,;:')

        return None

    def _extract_quoted_text(self, goal: str) -> str | None:
        m = re.search(r'"([^"]+)"', goal)
        if m:
            return m.group(1)
        m = re.search(r"'([^']+)'", goal)
        if m:
            return m.group(1)
        return None

    def _extract_write_pairs(self, goal: str) -> list[tuple[str, str]]:
        pairs = []
        patterns = [
            r'Scrivi\s+"([^"]+)"\s+in\s+([A-Za-z0-9_.\-/]+)',
            r"Scrivi\s+'([^']+)'\s+in\s+([A-Za-z0-9_.\-/]+)",
            r'Write\s+"([^"]+)"\s+to\s+([A-Za-z0-9_.\-/]+)',
            r"Write\s+'([^']+)'\s+to\s+([A-Za-z0-9_.\-/]+)",
        ]
        for pat in patterns:
            for m in re.finditer(pat, goal, flags=re.IGNORECASE):
                text = m.group(1)
                path = m.group(2).rstrip('.,;:')
                pairs.append((text, path))
        return pairs

    def choose_next_action(self, user_goal: str, iteration: int) -> PlannerDecision:
        goal = user_goal.lower()
        path = self._extract_path(user_goal)
        dir_path = self._extract_dir(user_goal)
        quoted_text = self._extract_quoted_text(user_goal)
        write_pairs = self._extract_write_pairs(user_goal)

        if len(write_pairs) >= 2 and ("leggili" in goal or "read them" in goal):
            if iteration < len(write_pairs):
                text, target = write_pairs[iteration]
                escaped = shlex.quote(text)
                return PlannerDecision(
                    tool_name="sandbox_exec",
                    tool_input={
                        "command": f"mkdir -p $(dirname {shlex.quote(target)}) && printf '%s\\n' {escaped} > {shlex.quote(target)} && cat {shlex.quote(target)}",
                        "timeout_sec": 20,
                    },
                    why=f"Scrivo il contenuto richiesto in {target}.",
                )
            read_index = iteration - len(write_pairs)
            if read_index < len(write_pairs):
                _, target = write_pairs[read_index]
                return PlannerDecision(
                    tool_name="sandbox_read_file",
                    tool_input={"path": target},
                    why=f"Leggo il file richiesto: {target}.",
                )

        if ("scrivi" in goal or "write" in goal) and ("mostrami i file" in goal or "show me the files" in goal) and ("poi leggi" in goal or "then read" in goal):
            target = path or "out/hello_exec.txt"
            target_dir = dir_path or "out"
            content = quoted_text or "hello from sandbox_exec"
            escaped = shlex.quote(content)
            if iteration == 0:
                return PlannerDecision(
                    tool_name="sandbox_exec",
                    tool_input={
                        "command": f"mkdir -p $(dirname {shlex.quote(target)}) && printf '%s\\n' {escaped} > {shlex.quote(target)} && cat {shlex.quote(target)}",
                        "timeout_sec": 20,
                    },
                    why=f"Creo il file richiesto e verifico subito il contenuto: {target}.",
                )
            if iteration == 1:
                return PlannerDecision(
                    tool_name="sandbox_list_dir",
                    tool_input={"path": target_dir},
                    why=f"Elenco i file nella directory richiesta: {target_dir}.",
                )
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": target},
                why=f"Rileggo il file appena creato: {target}.",
            )

        if ("scrivi" in goal or "write" in goal) and ("poi leggi" in goal or "then read" in goal):
            target = path or "out/hello_exec.txt"
            content = quoted_text or "hello from sandbox_exec"
            escaped = shlex.quote(content)
            if iteration == 0:
                return PlannerDecision(
                    tool_name="sandbox_exec",
                    tool_input={
                        "command": f"mkdir -p $(dirname {shlex.quote(target)}) && printf '%s\\n' {escaped} > {shlex.quote(target)} && cat {shlex.quote(target)}",
                        "timeout_sec": 20,
                    },
                    why=f"Creo il file richiesto e verifico subito il contenuto: {target}.",
                )
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": target},
                why=f"Rileggo il file appena creato: {target}.",
            )

        if ("leggi" in goal or "mostra il file" in goal or "read" in goal) and path:
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": path},
                why=f"Leggo il file richiesto: {path}.",
            )

        if "hello" in goal or "ciao" in goal:
            if iteration == 0:
                return PlannerDecision(
                    tool_name="sandbox_exec",
                    tool_input={
                        "command": "mkdir -p out && echo 'hello from sandbox_exec' > out/hello_exec.txt && cat out/hello_exec.txt",
                        "timeout_sec": 20,
                    },
                    why="Creo il file tramite exec e verifico subito che il comando abbia funzionato.",
                )
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": "out/hello_exec.txt"},
                why="Rileggo il file creato per confermare il contenuto finale.",
            )

        if "stream probe" in goal or "verifica stream" in goal or "m3u8" in goal or "mpd" in goal or "mediaset" in goal:
            url = "https://mediasetinfinity.mediaset.it/diretta/canale5_cC5"
            m = re.search(r"source page:\s*(https?://\S+)", user_goal, re.I)
            if m:
                url = m.group(1).rstrip('.,;)')
            return PlannerDecision(
                tool_name="sandbox_http_fetch",
                tool_input={"url": url, "method": "GET"},
                why=f"Per stream probe parto da una fetch HTTP della source page: {url}.",
            )

        if "ollama" in goal or "modelli" in goal or "models" in goal:
            return PlannerDecision(
                tool_name="sandbox_http_fetch",
                tool_input={"url": "http://127.0.0.1:11434/api/tags", "method": "GET"},
                why="Interrogo Ollama locale per osservare i modelli disponibili.",
            )

        if "file" in goal or "cartella" in goal or "directory" in goal or "workspace" in goal:
            target_dir = dir_path or "."
            return PlannerDecision(
                tool_name="sandbox_list_dir",
                tool_input={"path": target_dir},
                why=f"Elenco i file della directory richiesta: {target_dir}.",
            )

        return PlannerDecision(
            tool_name="sandbox_exec",
            tool_input={"command": "pwd && ls -la && find . -maxdepth 2 -type f | sort", "timeout_sec": 10},
            why="Fallback minimale per osservare l'ambiente di lavoro.",
        )


class OllamaPlanner:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen2.5:7b",
        fallback: DeterministicPlanner | None = None,
        timeout_sec: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.fallback = fallback or DeterministicPlanner()
        self.timeout_sec = timeout_sec

    def _clean_raw(self, text: str) -> str:
        text = text.strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^```\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return text.strip()

    def _extract_json_object(self, text: str) -> dict:
        text = self._clean_raw(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise json.JSONDecodeError("No JSON object found", text, 0)
        return json.loads(match.group(0))

    def _normalize_decision(self, parsed: dict, user_goal: str, iteration: int) -> PlannerDecision:
        tool_name = parsed.get("tool_name", "")
        tool_input = parsed.get("tool_input", {}) or {}
        why = parsed.get("why", "Decisione generata da Ollama.")

        if tool_name == "sandbox_read_file":
            if "file_path" in tool_input and "path" not in tool_input:
                tool_input["path"] = tool_input.pop("file_path")

        if tool_name == "sandbox_list_dir":
            if "file_path" in tool_input and "path" not in tool_input:
                tool_input["path"] = tool_input.pop("file_path")
            tool_input.setdefault("path", ".")

        if tool_name == "sandbox_http_fetch":
            if "url" not in tool_input and "endpoint" in tool_input:
                tool_input["url"] = tool_input.pop("endpoint")
            tool_input.setdefault("method", "GET")

        if tool_name not in {"sandbox_exec", "sandbox_read_file", "sandbox_list_dir", "sandbox_http_fetch"}:
            raise ValueError(f"Unsupported tool_name from model: {tool_name}")

        return PlannerDecision(tool_name=tool_name, tool_input=tool_input, why=why)

    def choose_next_action(self, user_goal: str, iteration: int) -> PlannerDecision:
        goal = user_goal.lower()
        path = self.fallback._extract_path(user_goal)
        dir_path = self.fallback._extract_dir(user_goal)
        quoted_text = self.fallback._extract_quoted_text(user_goal)
        write_pairs = self.fallback._extract_write_pairs(user_goal)

        if len(write_pairs) >= 2 and ("leggili" in goal or "read them" in goal):
            if iteration < len(write_pairs):
                text, target = write_pairs[iteration]
                escaped = shlex.quote(text)
                return PlannerDecision(
                    tool_name="sandbox_exec",
                    tool_input={
                        "command": f"mkdir -p $(dirname {shlex.quote(target)}) && printf '%s\\n' {escaped} > {shlex.quote(target)} && cat {shlex.quote(target)}",
                        "timeout_sec": 20,
                    },
                    why=f"Scrivo il contenuto richiesto in {target}.",
                )
            read_index = iteration - len(write_pairs)
            if read_index < len(write_pairs):
                _, target = write_pairs[read_index]
                return PlannerDecision(
                    tool_name="sandbox_read_file",
                    tool_input={"path": target},
                    why=f"Leggo il file richiesto: {target}.",
                )

        if ("scrivi" in goal or "write" in goal) and ("mostrami i file" in goal or "show me the files" in goal) and ("poi leggi" in goal or "then read" in goal):
            target = path or "out/hello_exec.txt"
            target_dir = dir_path or "out"
            content = quoted_text or "hello from sandbox_exec"
            escaped = shlex.quote(content)
            if iteration == 0:
                return PlannerDecision(
                    tool_name="sandbox_exec",
                    tool_input={
                        "command": f"mkdir -p $(dirname {shlex.quote(target)}) && printf '%s\\n' {escaped} > {shlex.quote(target)} && cat {shlex.quote(target)}",
                        "timeout_sec": 20,
                    },
                    why=f"Creo il file richiesto e verifico subito il contenuto: {target}.",
                )
            if iteration == 1:
                return PlannerDecision(
                    tool_name="sandbox_list_dir",
                    tool_input={"path": target_dir},
                    why=f"Elenco i file nella directory richiesta: {target_dir}.",
                )
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": target},
                why=f"Rileggo il file appena creato: {target}.",
            )

        if ("scrivi" in goal or "write" in goal) and ("poi leggi" in goal or "then read" in goal):
            target = path or "out/hello_exec.txt"
            content = quoted_text or "hello from sandbox_exec"
            escaped = shlex.quote(content)
            if iteration == 0:
                return PlannerDecision(
                    tool_name="sandbox_exec",
                    tool_input={
                        "command": f"mkdir -p $(dirname {shlex.quote(target)}) && printf '%s\\n' {escaped} > {shlex.quote(target)} && cat {shlex.quote(target)}",
                        "timeout_sec": 20,
                    },
                    why=f"Creo il file richiesto e verifico subito il contenuto: {target}.",
                )
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": target},
                why=f"Rileggo il file appena creato: {target}.",
            )

        if ("leggi" in goal or "mostra il file" in goal or "read" in goal) and path:
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": path},
                why=f"Leggo il file richiesto: {path}.",
            )

        if "hello" in goal or "ciao" in goal:
            if iteration == 0:
                return PlannerDecision(
                    tool_name="sandbox_exec",
                    tool_input={
                        "command": "mkdir -p out && echo 'hello from sandbox_exec' > out/hello_exec.txt && cat out/hello_exec.txt",
                        "timeout_sec": 20,
                    },
                    why="Creo il file tramite exec e verifico subito che il comando abbia funzionato.",
                )
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": "out/hello_exec.txt"},
                why="Rileggo il file creato per confermare il contenuto finale.",
            )

        if "ollama" in goal or "modelli" in goal or "models" in goal:
            return PlannerDecision(
                tool_name="sandbox_http_fetch",
                tool_input={"url": "http://127.0.0.1:11434/api/tags", "method": "GET"},
                why="Interrogo Ollama locale per osservare i modelli disponibili.",
            )

        if "file" in goal or "cartella" in goal or "directory" in goal or "workspace" in goal:
            target_dir = dir_path or "."
            return PlannerDecision(
                tool_name="sandbox_list_dir",
                tool_input={"path": target_dir},
                why=f"Elenco i file della directory richiesta: {target_dir}.",
            )

        schema = {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "enum": ["sandbox_exec", "sandbox_read_file", "sandbox_list_dir", "sandbox_http_fetch"],
                },
                "tool_input": {"type": "object"},
                "why": {"type": "string"},
            },
            "required": ["tool_name", "tool_input", "why"],
            "additionalProperties": False,
        }

        prompt = f"""Sei un planner per un agente sandbox.
Devi scegliere SOLO il prossimo passo minimo.
user_goal: {user_goal}
iteration: {iteration}
"""
        try:
            r = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": schema,
                    "options": {"temperature": 0},
                },
                timeout=self.timeout_sec,
            )
            r.raise_for_status()
            data = r.json()
            raw = data.get("response", "")
            parsed = self._extract_json_object(raw)
            return self._normalize_decision(parsed, user_goal, iteration)
        except Exception:
            return self.fallback.choose_next_action(user_goal, iteration)
