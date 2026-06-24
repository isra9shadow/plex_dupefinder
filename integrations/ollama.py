"""Ollama local-LLM client for media identification (free, unlimited, private).

Same ``identify()`` interface as ``GeminiClient`` so the organizer can use it
interchangeably as an AI backend. Talks to Ollama's ``/api/generate`` with
``format=json`` so the model returns parseable JSON. No API key (local server),
no quota — ideal as the primary AI backend with Gemini only as a last resort.

Reuses ``integrations.gemini.build_prompt`` so both backends ask for the exact
same structured output.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence

from core.errors import IntegrationError

from integrations._net import require_http_url
from integrations.gemini import build_prompt

JsonPoster = Callable[[str, bytes, Mapping[str, str], float], str]


def _urllib_post(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float
) -> str:  # pragma: no cover - real IO
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return str(response.read().decode("utf-8"))


def _as_dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


class OllamaClient:
    """Minimal Ollama ``/api/generate`` client returning parsed suggestions."""

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3:8b",
        timeout: float = 120.0,
        batch_size: int = 50,
        request_delay: float = 0.0,
        poster: JsonPoster = _urllib_post,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base = require_http_url(base_url).rstrip("/")
        self._model = model
        self._timeout = timeout
        self._batch_size = max(1, batch_size)
        self._request_delay = max(0.0, request_delay)
        self._post = poster
        self._sleep = sleeper

    def _generate(self, prompt: str) -> str:
        url = f"{self._base}/api/generate"
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        body = json.dumps(payload).encode("utf-8")
        try:
            return self._post(url, body, {"content-type": "application/json"}, self._timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise IntegrationError(f"Ollama request failed: {exc}") from exc

    @staticmethod
    def _extract_text(raw: str) -> str:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise IntegrationError("Ollama returned invalid JSON envelope") from exc
        text = _as_dict(data).get("response")
        if not isinstance(text, str) or not text.strip():
            raise IntegrationError("Ollama response was empty")
        return text

    @staticmethod
    def _parse_suggestions(text: str) -> list[dict[str, object]]:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IntegrationError("Ollama suggestion payload was not JSON") from exc
        if isinstance(parsed, list):
            items: list[object] = parsed
        elif isinstance(parsed, dict):
            # format=json sometimes wraps the array in an object; take the first list.
            lists = [v for v in parsed.values() if isinstance(v, list)]
            items = lists[0] if lists else []
        else:
            raise IntegrationError("Ollama suggestions must be a JSON array")
        return [_as_dict(item) for item in items]

    def identify(
        self,
        paths: Sequence[str],
        *,
        errors: list[IntegrationError] | None = None,
    ) -> list[dict[str, object]]:
        """Return one raw suggestion dict per input path (batched, resilient).

        Mirrors ``GeminiClient.identify``: a failing batch is appended to
        ``errors`` and skipped (when provided) instead of aborting the rest.
        """
        out: list[dict[str, object]] = []
        names = list(paths)
        for start in range(0, len(names), self._batch_size):
            if start and self._request_delay:
                self._sleep(self._request_delay)
            batch = names[start : start + self._batch_size]
            try:
                out.extend(
                    self._parse_suggestions(self._extract_text(self._generate(build_prompt(batch))))
                )
            except IntegrationError as exc:
                if errors is None:
                    raise
                errors.append(exc)
        return out
