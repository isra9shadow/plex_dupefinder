"""Pure logic for the interactive menu config EDITOR (stdlib-only).

The read-only config *doctor* (``modules/ops/configcheck.py``) tells the operator
WHAT is missing/invalid; this module provides the pure pieces the SSH menu needs
to SET a value: coerce+validate a raw string against a :class:`configspec.SettingSpec`,
and upsert a ``KEY=value`` line in a ``.env`` file. The menu owns the actual I/O
(``config.json`` via its ``_cfg_set``; the ``.env`` file read/write) — everything
here is deterministic and unit-tested with no filesystem access.

Stdlib-only on purpose: ``menu.py`` imports this on Unraid's bare Python (no
third-party packages), exactly like :mod:`core.configspec`.
"""

from __future__ import annotations

from collections.abc import Callable

from core.configspec import SettingSpec, Validator, is_valid_url

# Injected existence check for path validators (advisory in the editor: a
# not-yet-existing path is accepted with a warning, since it may be created later).
PathChecker = Callable[[str], bool]

_SECRET_MARKERS = ("token", "key", "password", "secret", "bearer")


def is_secret(spec: SettingSpec) -> bool:
    """True for env credentials whose value must never be echoed back in full."""
    if not spec.is_env:
        return False
    low = spec.key.lower()
    return any(marker in low for marker in _SECRET_MARKERS)


def redact(value: str) -> str:
    """Mask a secret for display: keep only the last 4 chars (empty stays empty)."""
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * (len(value) - 4) + value[-4:]


def coerce_value(
    spec: SettingSpec, raw: str, *, path_exists: PathChecker | None = None
) -> tuple[bool, object, str]:
    """Coerce + validate a raw operator string for ``spec``.

    Returns ``(ok, value, detail)``:
      * ``ok`` False → ``value`` is None and ``detail`` says why (reject the input).
      * ``ok`` True  → ``value`` is the typed value to store (int for INT, else the
        trimmed string); ``detail`` may carry a non-fatal warning (e.g. a path that
        does not exist yet), empty otherwise.
    An empty input is always rejected (use the existing value / clear it elsewhere).
    """
    text = raw.strip()
    if text == "":
        return False, None, "valor vacío"

    kind = spec.validator
    if kind is Validator.INT:
        try:
            return True, int(text), ""
        except ValueError:
            return False, None, f"no es un entero: {text!r}"
    if kind is Validator.ENUM:
        if text not in spec.choices:
            return False, None, f"debe ser uno de: {', '.join(spec.choices)}"
        return True, text, ""
    if kind is Validator.URL:
        if not is_valid_url(text):
            return False, None, "no es una URL http(s)://"
        return True, text, ""
    if kind in (Validator.PATH_EXISTS, Validator.DIR_EXISTS):
        # Existence is advisory in the editor — accept but warn so the operator
        # can create the directory afterwards without being blocked.
        if path_exists is not None and not path_exists(text):
            what = "directorio" if kind is Validator.DIR_EXISTS else "ruta"
            return True, text, f"aviso: el {what} no existe todavía"
        return True, text, ""
    # NONEMPTY and any future kind: a non-empty trimmed string is fine.
    return True, text, ""


def upsert_env_line(text: str, key: str, value: str) -> str:
    """Return ``text`` with ``KEY=value`` set (replaced in place, or appended).

    Comments and unrelated lines are preserved; only the first assignment to
    ``key`` is replaced. Always ends with a trailing newline. Pure — the menu
    reads the ``.env`` file, calls this, and writes the result back.
    """
    lines = text.splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if (
            not replaced
            and stripped
            and not stripped.startswith("#")
            and "=" in stripped
            and stripped.split("=", 1)[0].strip() == key
        ):
            out.append(f"{key}={value}")
            replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


__all__ = [
    "PathChecker",
    "coerce_value",
    "is_secret",
    "redact",
    "upsert_env_line",
]
