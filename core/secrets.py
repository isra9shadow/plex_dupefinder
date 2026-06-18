"""Fail-closed secret loading. The ONLY place the platform reads secrets.

Resolution order per name: ``os.environ`` first, then an optional ``.env`` file
(git-ignored). A required secret that is missing or empty raises ``SecretError``
— we never silently fall back, and never log secret values. See INVARIANT I3 and
``.env.example`` for the expected variable names.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.errors import SecretError

_ENV_FILE = Path(os.environ.get("IZUMI_ENV_FILE", ".env"))
_dotenv_cache: dict[str, str] | None = None


def _parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            data[key] = value
    return data


def _load_dotenv() -> dict[str, str]:
    global _dotenv_cache
    if _dotenv_cache is None:
        _dotenv_cache = _parse_env_file(_ENV_FILE)
    return _dotenv_cache


def reset_cache() -> None:
    """Clear the cached ``.env`` contents (used by tests)."""
    global _dotenv_cache
    _dotenv_cache = None


def get_secret(name: str, *, default: str | None = None, required: bool = True) -> str | None:
    """Return the secret ``name`` or ``default``; raise if required and missing."""
    value = os.environ.get(name)
    if value is None:
        value = _load_dotenv().get(name)
    if not value:
        if required and default is None:
            raise SecretError(f"Missing required secret: {name}")
        return default
    return value


def require(name: str) -> str:
    """Return the secret ``name`` or raise ``SecretError`` (never returns empty)."""
    value = get_secret(name, required=True)
    if value is None:  # pragma: no cover - get_secret already raised
        raise SecretError(f"Missing required secret: {name}")
    return value
