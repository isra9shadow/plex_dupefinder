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
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence

from core.errors import IntegrationError

from integrations._net import require_http_url

# (url, body, headers, timeout) -> response text
JsonPoster = Callable[[str, bytes, Mapping[str, str], float], str]

# HTTP status codes that are worth retrying with backoff: rate-limit/quota (429)
# and the transient server-side 5xx family. Everything else is a hard failure.
_RATE_LIMIT = 429
_RETRYABLE_STATUS = frozenset({_RATE_LIMIT, 500, 502, 503, 504})

_PROMPT_HEADER = (
    "You are a media librarian. Each input is a RELATIVE FILE PATH (folders + "
    "filename, '/'-separated). Identify the title as reliably as possible (more "
    "reliably than a regex-based renamer like FileBot). IMPORTANT: use the WHOLE "
    "path for clues — the parent/grandparent folder name is often the series or "
    "movie title (e.g. 'Breaking Bad/S01/ep1.mkv') even when the filename alone is "
    "uninformative; but folder names are not always reliable, so weigh the "
    "filename too and lower your confidence when they conflict or are vague. "
    "Some release names are deliberately OBFUSCATED with leetspeak or symbol "
    "substitution to mask the title (e.g. 'gr31l@nd' = 'Greenland', 'Gr4Vill4no', "
    "'M4tr1x', 'Th3.M4nd4l0r14n'); de-obfuscate digits/symbols back to letters "
    "(3->e, 1->i/l, 4->a, 0->o, @->a, $->s, 5->s) to recover the real title. "
    "Decide whether it is a movie or a TV series episode. Return STRICT JSON only: "
    "a JSON array with one object per input, in the SAME ORDER as the input, each "
    "with exactly these keys:\n"
    '  "filename"   : the input path, echoed back VERBATIM (exactly as given)\n'
    '  "type"       : "movie" | "series" | "unknown"\n'
    '  "title"      : canonical title in its original language, no year/tags\n'
    '  "year"       : integer release year, or null if unknown\n'
    '  "season"     : integer season number for series, else null\n'
    '  "episode"    : integer episode number for series, else null\n'
    '  "episode_title": for a series, the episode name if present in the input '
    "(or known), else null\n"
    '  "tmdb_id"    : for a MOVIE, the TMDB id (integer) if confident, else null\n'
    '  "tvdb_id"    : for a SERIES, the TVDB id (integer) if confident, else '
    "null. Do NOT guess random numbers; null is better than a wrong id\n"
    '  "video_format": source+resolution parsed from the name tags, e.g. '
    '"Bluray-1080p" / "WEBDL-1080p" / "HDTV-720p" / "DVD", else null\n'
    '  "video_codec": video codec parsed from the name tags, e.g. "x265" / '
    '"x264" / "h265" / "h264" / "AV1", else null\n'
    '  "confidence" : integer 0-100, your honest certainty in this identification\n'
    'Do not invent titles. If you cannot tell, use type "unknown" and a low '
    "confidence. Output ONLY the JSON array, no prose, no code fences.\n"
    "A line may have a '   |HINTS| ' suffix with extra clues read from the file "
    "(duration / embedded title/show/season/episode tags / audio languages) — "
    'USE them, but set "filename" to the path BEFORE that suffix, echoed verbatim.'
    "\n\nPaths:\n"
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
        max_retries: int = 3,
        backoff_base: float = 1.0,
        request_delay: float = 0.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise IntegrationError("Gemini API key is empty")
        self._key = api_key
        self._model = model
        self._base = require_http_url(base_url).rstrip("/")
        self._timeout = timeout
        self._batch_size = max(1, batch_size)
        self._post = poster
        self._max_retries = max(0, max_retries)
        self._backoff_base = backoff_base
        self._request_delay = max(0.0, request_delay)
        self._sleep = sleeper

    def _generate(self, prompt: str) -> str:
        url = f"{self._base}/models/{self._model}:generateContent?key={self._key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {"content-type": "application/json"}
        # attempt 0 is the initial call; attempts 1..max_retries are retries.
        for attempt in range(self._max_retries + 1):
            try:
                return self._post(url, body, headers, self._timeout)
            except urllib.error.HTTPError as exc:
                if exc.code not in _RETRYABLE_STATUS or attempt >= self._max_retries:
                    raise self._http_error(exc) from exc
                self._sleep(self._backoff_base * 2**attempt)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                if attempt >= self._max_retries:
                    raise IntegrationError(f"Gemini request failed: {exc}") from exc
                self._sleep(self._backoff_base * 2**attempt)
        # Unreachable: the loop either returns or raises on the final attempt.
        raise IntegrationError("Gemini request failed: retries exhausted")

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> IntegrationError:
        """Map an HTTPError to a clear IntegrationError (quota-aware on 429)."""
        if exc.code == _RATE_LIMIT:
            return IntegrationError(
                "Gemini quota/rate-limit hit (HTTP 429): wait and retry later or "
                f"check your API quota ({exc})"
            )
        return IntegrationError(f"Gemini request failed (HTTP {exc.code}): {exc}")

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

    def identify(
        self,
        paths: Sequence[str],
        *,
        errors: list[IntegrationError] | None = None,
    ) -> list[dict[str, object]]:
        """Return one raw suggestion dict per input path (batched).

        Each input is a relative path so the model can use folder-name hints (see
        the prompt). The returned dicts are the model's payload, lightly
        normalised; the caller validates/maps them by the echoed ``filename``.

        Resilience: when ``errors`` is provided, a failing batch (e.g. a 429 after
        retries) is appended to it and SKIPPED so the other batches still produce
        results (partial success). When ``errors`` is None the first batch failure
        propagates (back-compat). ``request_delay`` throttles between batches to
        stay under free-tier rate limits.
        """
        out: list[dict[str, object]] = []
        names = list(paths)
        for start in range(0, len(names), self._batch_size):
            if start and self._request_delay:
                self._sleep(self._request_delay)
            batch = names[start : start + self._batch_size]
            try:
                text = self._extract_text(self._generate(build_prompt(batch)))
                out.extend(self._parse_suggestions(text))
            except IntegrationError as exc:
                if errors is None:
                    raise
                errors.append(exc)
        return out
