from __future__ import annotations

from dataclasses import dataclass
import json
import re

import requests


@dataclass
class PlannerDecision:
    tool_name: str
    tool_input: dict
    why: str


class DeterministicPlanner:
    def _extract_path(self, goal: str) -> str | None:
        m = re.search(r'((?:out|tmp)/[^\s]+|[A-Za-z0-9_.\-/]+\.[A-Za-z0-9]+)', goal)
        return m.group(1) if m else None

    def _extract_dir(self, goal: str) -> str | None:
        goal_l = goal.lower()

        m = re.search(r'\b(?:cartella|directory|dir|folder)\s+([A-Za-z0-9_.\-/]+)', goal_l)
        if m:
            return m.group(1)

        m = re.search(r'\b(?:nella|nel)\s+cartella\s+([A-Za-z0-9_.\-/]+)', goal_l)
        if m:
            return m.group(1)

        m = re.search(r'\b(?:in|nella|nel)\s+((?:out|tmp)(?:/[A-Za-z0-9_.\-/]+)?)\b', goal_l)
        if m:
            return m.group(1)

        return None

    def choose_next_action(self, user_goal: str, iteration: int) -> PlannerDecision:
        goal = user_goal.lower()
        path = self._extract_path(user_goal)
        dir_path = self._extract_dir(user_goal)

        if ("scrivi" in goal or "write" in goal) and ("poi leggi" in goal or "then read" in goal):
            target = path or "out/hello_exec.txt"
            if iteration == 0:
                return PlannerDecision(
                    tool_name="sandbox_exec",
                    tool_input={
                        "command": f"mkdir -p out && echo 'hello from sandbox_exec' > {target} && cat {target}",
                        "timeout_sec": 20,
                    },
                    why=f"Creo il file richiesto e verifico subito il contenuto: {target}.",
                )
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": target},
                why=f"Rileggo il file appena creato: {target}.",
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

        if ("leggi" in goal or "mostra il file" in goal or "read" in goal) and path:
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": path},
                why=f"Leggo il file richiesto: {path}.",
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

        if ("scrivi" in goal or "write" in goal) and ("poi leggi" in goal or "then read" in goal):
            target = path or "out/hello_exec.txt"
            if iteration == 0:
                return PlannerDecision(
                    tool_name="sandbox_exec",
                    tool_input={
                        "command": f"mkdir -p out && echo 'hello from sandbox_exec' > {target} && cat {target}",
                        "timeout_sec": 20,
                    },
                    why=f"Creo il file richiesto e verifico subito il contenuto: {target}.",
                )
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": target},
                why=f"Rileggo il file appena creato: {target}.",
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

        if ("leggi" in goal or "mostra il file" in goal or "read" in goal) and path:
            return PlannerDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": path},
                why=f"Leggo il file richiesto: {path}.",
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

Regole:
- Non usare comandi distruttivi.
- Se l'obiettivo parla di file esplicito da leggere, usa sandbox_read_file con tool_input.path.
- Se l'obiettivo parla di scrivere e poi leggere, prima usa sandbox_exec e poi sandbox_read_file.
- Se l'obiettivo parla di hello o ciao:
  - iteration 0 => usa sandbox_exec per creare out/hello_exec.txt con contenuto esatto: hello from sandbox_exec
  - iteration 1 => usa sandbox_read_file con tool_input.path = out/hello_exec.txt
- Se l'obiettivo parla di modelli Ollama:
  - usa sandbox_http_fetch con url = http://127.0.0.1:11434/api/tags e method = GET
- Se l'obiettivo parla di file/cartelle/workspace:
  - usa sandbox_list_dir con path uguale alla directory richiesta, altrimenti .
- Per sandbox_read_file usa SEMPRE la chiave path, non file_path.
- Se non sei sicuro, usa sandbox_exec con un comando innocuo di osservazione.

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
