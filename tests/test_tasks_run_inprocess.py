from __future__ import annotations

import asyncio
import json

import openshell_backend.app as backend_app
import ralfloop_agent.adapters.openshell_real_adapter as real_adapter_module
import ralfloop_agent.providers.ollama as ollama_module
from ralfloop_agent.adapters.openshell_adapter import OpenShellAdapterStub
from ralfloop_agent.providers.ollama import DeterministicPlanner


class _DeterministicOllamaPlanner:
    def __init__(self, *args, fallback=None, **kwargs) -> None:
        self.fallback = fallback or DeterministicPlanner()

    def choose_next_action(self, user_goal: str, iteration: int):
        return self.fallback.choose_next_action(user_goal, iteration)

    def _extract_write_pairs(self, goal: str):
        return self.fallback._extract_write_pairs(goal)


async def _post_json_inprocess(path: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode('utf-8')
    sent = False
    messages: list[dict] = []

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode('ascii'),
        "query_string": b'',
        "root_path": '',
        "headers": [(b'content-type', b'application/json')],
        "client": ('testclient', 123),
        "server": ('testserver', 80),
    }

    await backend_app.app(scope, receive, send)

    start = next(message for message in messages if message['type'] == 'http.response.start')
    chunks = [message.get('body', b'') for message in messages if message['type'] == 'http.response.body']
    response_body = b''.join(chunks)
    return start['status'], json.loads(response_body.decode('utf-8'))


def _post_tasks_run(user_goal: str) -> dict:
    status_code, payload = asyncio.run(
        _post_json_inprocess(
            '/tasks/run',
            {
                'user_goal': user_goal,
                'skill_context': '',
                'extra_context': {},
            },
        )
    )
    assert status_code == 200
    return payload


def _patch_runtime_dependencies(tmp_path, monkeypatch) -> None:
    class _InProcessOpenShellAdapter(OpenShellAdapterStub):
        def __init__(self, base_url: str = 'http://127.0.0.1:19090', policy=None) -> None:
            super().__init__(base_dir=str(tmp_path / '.sandbox'), policy=policy)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(real_adapter_module, 'OpenShellAdapterReal', _InProcessOpenShellAdapter)
    monkeypatch.setattr(ollama_module, 'OllamaPlanner', _DeterministicOllamaPlanner)


def test_tasks_run_inprocess_multi_file_case_returns_real_contents(tmp_path, monkeypatch) -> None:
    _patch_runtime_dependencies(tmp_path, monkeypatch)

    payload = _post_tasks_run(
        'Scrivi due file: out/spesa.txt con "latte, pane, pomodori" e out/note.txt con "ricordati di chiamare Sonia". Poi leggili entrambi e dimmi il contenuto finale.',
    )

    assert payload['ok'] is True
    assert payload['stop_reason'] == 'goal_completed'
    assert 'latte, pane, pomodori' in payload['final_answer']
    assert 'ricordati di chiamare Sonia' in payload['final_answer']
    assert 'with open(' not in payload['final_answer']
    assert 'cat >' not in payload['final_answer']
    assert 'print(' not in payload['final_answer']


def test_tasks_run_inprocess_multi_file_variant_is_not_hardcoded(tmp_path, monkeypatch) -> None:
    _patch_runtime_dependencies(tmp_path, monkeypatch)

    payload = _post_tasks_run(
        'Crea due file out/a.txt e out/b.txt con contenuti diversi, poi leggili entrambi e dimmi il contenuto finale.',
    )

    assert payload['ok'] is True
    assert payload['stop_reason'] == 'goal_completed'
    assert 'out/a.txt' in payload['final_answer']
    assert 'out/b.txt' in payload['final_answer']
    assert 'contenuto a' in payload['final_answer']
    assert 'contenuto b' in payload['final_answer']
    assert 'with open(' not in payload['final_answer']
    assert 'cat >' not in payload['final_answer']
    assert 'print(' not in payload['final_answer']
