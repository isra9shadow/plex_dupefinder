"""Google Gemini client for media title identification (free-tier friendly).

Stdlib HTTP, injectable fetcher (so tests never touch the network), API key from
``core.secrets`` (never hardcoded). Read-only: it only *asks* the model to parse
messy filenames into structured title metadata with a self-reported confidence.

The model is instructed to return STRICT JSON (``responseMimeType`` =
``application/json``) so parsing is deterministic. Any malformed response raises
``IntegrationError`` — we never guess on a bad payload.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence

from core.errors import IntegrationError

from integrations._net import require_http_url

# (url, body, headers, timeout) -> response text
JsonPoster = Callable[[str, bytes, Mapping[str, str], float], str]

_PROMPT_HEADER = (
    "You are a media librarian. For each input filename, identify the title as "
    "reliably as possible (more reliably than a regex-based renamer like FileBot). "
    "Decide whether it is a movie or a TV series episode. Return STRICT JSON only: "
    "a JSON array with one object per input, in the SAME ORDER as the input, each "
    "with exactly these keys:\n"
    '  "filename"   : the original input filename (echo it back verbatim)\n'
    '  "type"       : "movie" | "series" | "unknown"\n'
    '  "title"      : canonical title in its original language, no year/tags\n'
    '  "year"       : integer release year, or null if unknown\n'
    '  "season"     : integer season number for series, else null\n'
    '  "episode"    : integer episode number for series, else null\n'
    '  "confidence" : integer 0-100, your honest certainty in this identification\n'
    'Do not invent titles. If you cannot tell, use type "unknown" and a low '
    "confidence. Output ONLY the JSON array, no prose, no code fences.\n\n"
    "Filenames:\n"
)


def _urllib_post(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float
) -> str:  # pragma: no cover - real IO
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return str(response.read().decode("utf-8"))


def _as_dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def build_prompt(filenames: Sequence[str]) -> str:
    """Compose the identification prompt for a batch of filenames."""
    lines = [f"{i + 1}. {name}" for i, name in enumerate(filenames)]
    return _PROMPT_HEADER + "\n".join(lines)


class GeminiClient:
    """Minimal Gemini ``generateContent`` client returning parsed suggestions."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gemini-2.0-flash",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: float = 30.0,
        batch_size: int = 50,
        poster: JsonPoster = _urllib_post,
    ) -> None:
        if not api_key:
            raise IntegrationError("Gemini API key is empty")
        self._key = api_key
        self._model = model
        self._base = require_http_url(base_url).rstrip("/")
        self._timeout = timeout
        self._batch_size = max(1, batch_size)
        self._post = poster

    def _generate(self, prompt: str) -> str:
        url = f"{self._base}/models/{self._model}:generateContent?key={self._key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {"content-type": "application/json"}
        try:
            return self._post(url, body, headers, self._timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise IntegrationError(f"Gemini request failed: {exc}") from exc

    @staticmethod
    def _extract_text(raw: str) -> str:
        """Pull the model's text payload out of a generateContent response."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise IntegrationError("Gemini returned invalid JSON envelope") from exc
        candidates = _as_dict(data).get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise IntegrationError("Gemini response had no candidates")
        content = _as_dict(_as_dict(candidates[0]).get("content"))
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            raise IntegrationError("Gemini response had no content parts")
        text = _as_dict(parts[0]).get("text")
        if not isinstance(text, str) or not text.strip():
            raise IntegrationError("Gemini response text was empty")
        return text

    @staticmethod
    def _parse_suggestions(text: str) -> list[dict[str, object]]:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IntegrationError("Gemini suggestion payload was not JSON") from exc
        if not isinstance(parsed, list):
            raise IntegrationError("Gemini suggestions must be a JSON array")
        return [_as_dict(item) for item in parsed]

    def identify(self, filenames: Sequence[str]) -> list[dict[str, object]]:
        """Return one raw suggestion dict per filename (batched).

        The returned dicts are the model's payload, lightly normalised to dicts;
        the caller is responsible for validating/typing them.
        """
        out: list[dict[str, object]] = []
        names = list(filenames)
        for start in range(0, len(names), self._batch_size):
            batch = names[start : start + self._batch_size]
            text = self._extract_text(self._generate(build_prompt(batch)))
            out.extend(self._parse_suggestions(text))
        return out
