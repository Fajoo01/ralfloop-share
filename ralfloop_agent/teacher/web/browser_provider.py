"""Loopback-only teaching bridge to the dedicated GPT Browser.

The student surface can choose a language model, but never receives browser targets,
URLs or generic browser capabilities. Each request is isolated server-side.
"""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ALLOWED_PROVIDERS = frozenset({"chatgpt", "kimi", "deepseek"})


class BrowserTeachingProvider:
    def __init__(self, endpoint: str | None = None, timeout: float = 95.0):
        self.endpoint = (endpoint or os.environ.get("TEACHER_GPT_BROWSER_ENDPOINT") or "http://127.0.0.1:19201/api/providers/teaching-query").strip()
        if not self.endpoint.startswith("http://127.0.0.1:") and not self.endpoint.startswith("http://localhost:"):
            raise ValueError("teacher_browser_endpoint_must_be_loopback")
        self.timeout = float(timeout)

    def query(self, provider: str, prompt: str, *, timeout_seconds: int = 90) -> dict:
        name = str(provider or "").strip().lower()
        if name not in ALLOWED_PROVIDERS:
            raise ValueError("teacher_provider_not_allowed")
        text = str(prompt or "").strip()
        if not text or len(text) > 12000:
            raise ValueError("teacher_prompt_invalid")
        body = json.dumps({"provider": name, "text": text, "timeout_seconds": max(5, min(int(timeout_seconds), 120))}, ensure_ascii=False).encode("utf-8")
        request = Request(self.endpoint, data=body, method="POST", headers={"Content-Type":"application/json", "X-Bottazzi-Frontend":"1"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read(256000).decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ConnectionError("teacher_browser_provider_unavailable") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("response"), str):
            raise ConnectionError("teacher_browser_provider_invalid_response")
        return {"provider": name, "response": payload["response"][:8000]}
