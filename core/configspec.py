"""Single declarative registry of EVERY configurable setting on the platform.

This module is **stdlib-only** on purpose: it is imported by the host launcher
``menu.py`` which runs on a Python *without* ``jsonschema`` (or any third-party
package). It therefore uses nothing but the standard library and ``core.errors``
for its exception type, and pulls in no heavy modules (no ``core.config``, no
``integrations.*``).

It is the single source of truth that drives:

  * ``modules/ops/configcheck.py`` — the read-only config doctor.
  * a future menu editor — the same specs describe how to set each value.

Design (data-driven):

  * :data:`SPECS` is a flat list of :class:`SettingSpec` describing each setting:
    its ``key``, where it lives (``location`` = ``"env"`` for a ``.env`` variable
    or a dotted ``config.json`` path such as ``"integrations.radarr.url"``),
    whether it is ``required``, its ``default``, a ``validator`` kind, and a
    human ``how`` hint (menu path / bot command / ``.env`` line).
  * Pure functions :func:`evaluate_setting` / :func:`evaluate` take a plain
    env-dict + a config-dict and return per-setting :class:`SettingStatus`
    (``OK`` / ``MISSING`` / ``INVALID``) plus the remediation hint. No I/O.

The env/config dicts are injected by the caller, so evaluation is fully
deterministic and offline (the doctor module wires the real env via
``core/secrets`` and the real config via ``core/config``).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

# menu.py imports this module on the Unraid HOST, whose Python is 3.9; StrEnum
# only arrived in 3.11. Fall back to a str+Enum with StrEnum's __str__ semantics
# so the host launchers import cleanly (the containerised modules use real StrEnum).
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - exercised only on the 3.9 host launcher
    from enum import Enum

    class StrEnum(str, Enum):
        __str__ = str.__str__


# A path checker maps a path string to whether it exists. Injected so the pure
# evaluation functions never touch the real filesystem (tests stay offline).
PathChecker = Callable[[str], bool]

# --- validator kinds -----------------------------------------------------------


class Validator(StrEnum):
    """How a setting's value is validated once it is present."""

    NONEMPTY = "nonempty"  # any non-empty string
    INT = "int"  # parses as an integer
    URL = "url"  # http(s):// URL
    PATH_EXISTS = "path_exists"  # a file OR dir that exists on disk
    DIR_EXISTS = "dir_exists"  # a directory that exists on disk
    ENUM = "enum"  # one of ``choices``


class Status(StrEnum):
    """Outcome of evaluating one setting against the supplied env + config."""

    OK = "ok"
    MISSING = "missing"  # required but absent / empty
    INVALID = "invalid"  # present but fails its validator


@dataclass(frozen=True)
class SettingSpec:
    """Declarative description of a single configurable setting."""

    key: str  # short identifier, unique within SPECS
    location: str  # "env" or a dotted config.json path
    validator: Validator
    required: bool = False
    default: object = None
    how: str = ""  # human "how to set it" (menu path / bot cmd / .env line)
    group: str = "general"  # for grouping in the doctor report
    choices: tuple[str, ...] = ()  # only meaningful for Validator.ENUM
    description: str = ""

    @property
    def is_env(self) -> bool:
        return self.location == "env"


@dataclass(frozen=True)
class SettingStatus:
    """Result of evaluating one :class:`SettingSpec`."""

    spec: SettingSpec
    status: Status
    value: str | None  # the resolved raw value (None if absent), never a secret echo
    detail: str  # short human reason for MISSING/INVALID (empty when OK)

    @property
    def key(self) -> str:
        return self.spec.key

    @property
    def remediation(self) -> str:
        """The 'how to set it' hint — what an operator should do next."""
        return self.spec.how


# --- URL matcher (stdlib only; no urllib import needed) ------------------------

_URL_RE: Final = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def _looks_like_url(value: str) -> bool:
    return bool(_URL_RE.match(value.strip()))


def is_valid_url(value: str) -> bool:
    """Public http(s):// URL check (used by the menu config editor / callers)."""
    return _looks_like_url(value)


# --- resolution: pull a value out of env or a dotted config path ---------------


def _dotted_get(config: Mapping[str, object], dotted: str) -> object | None:
    """Walk ``config`` following a dotted path; return None if any hop is missing.

    Lists are addressable by integer index (e.g. ``a.b.0.c``), so specs can point
    at array members if ever needed.
    """
    node: object = config
    for part in dotted.split("."):
        if isinstance(node, Mapping):
            if part not in node:
                return None
            node = node[part]
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            try:
                idx = int(part)
            except ValueError:
                return None
            if not 0 <= idx < len(node):
                return None
            node = node[idx]
        else:
            return None
    return node


def resolve(
    spec: SettingSpec, env: Mapping[str, str], config: Mapping[str, object]
) -> object | None:
    """Return the raw configured value for ``spec`` (None if unset/empty).

    ``env`` lookups treat an empty string as unset. ``config`` lookups walk the
    dotted path. The spec ``default`` is **not** applied here — callers decide
    whether a missing required value with no default is MISSING.
    """
    if spec.is_env:
        raw = env.get(spec.key)
        if raw is None or raw == "":
            return None
        return raw
    return _dotted_get(config, spec.location)


# --- validation ----------------------------------------------------------------


def _to_text(value: object) -> str:
    """Best-effort string form for display/validation (never raises)."""
    return value if isinstance(value, str) else str(value)


def _validate(
    spec: SettingSpec,
    value: object,
    *,
    path_exists: PathChecker,
) -> tuple[bool, str]:
    """Return ``(ok, detail)`` for a present ``value`` against ``spec.validator``."""
    kind = spec.validator
    if kind is Validator.NONEMPTY:
        ok = _to_text(value).strip() != ""
        return ok, "" if ok else "value is empty"
    if kind is Validator.INT:
        if isinstance(value, bool):
            return False, "expected an integer, got a boolean"
        if isinstance(value, int):
            return True, ""
        try:
            int(_to_text(value).strip())
        except ValueError:
            return False, f"not an integer: {_to_text(value)!r}"
        return True, ""
    if kind is Validator.URL:
        ok = _looks_like_url(_to_text(value))
        return ok, "" if ok else "not an http(s):// URL"
    if kind is Validator.ENUM:
        text = _to_text(value)
        ok = text in spec.choices
        return ok, "" if ok else f"must be one of {', '.join(spec.choices)}"
    if kind in (Validator.PATH_EXISTS, Validator.DIR_EXISTS):
        # I/O is injected so tests stay offline/deterministic. ``path_exists`` is a
        # callable (text) -> bool that the doctor supplies (file-or-dir vs dir-only
        # is encoded by the callable the module hands in per kind).
        text = _to_text(value).strip()
        if not text:
            return False, "value is empty"
        try:
            exists = bool(path_exists(text))
        except OSError:
            exists = False
        if exists:
            return True, ""
        what = "directory" if kind is Validator.DIR_EXISTS else "path"
        return False, f"{what} does not exist: {text}"
    return True, ""  # pragma: no cover - exhaustive above


def _always_false(_: str) -> bool:
    """Default path checker: treats every path as absent (offline-safe default)."""
    return False


def evaluate_setting(
    spec: SettingSpec,
    env: Mapping[str, str],
    config: Mapping[str, object],
    *,
    path_exists: PathChecker = _always_false,
    dir_exists: PathChecker | None = None,
) -> SettingStatus:
    """Evaluate one setting purely against the supplied env + config.

    ``path_exists`` validates :data:`Validator.PATH_EXISTS` (file or dir);
    ``dir_exists`` validates :data:`Validator.DIR_EXISTS` and defaults to
    ``path_exists`` when not given. Both are injected so the function never
    touches the real filesystem in tests.
    """
    value = resolve(spec, env, config)
    if value is None:
        if spec.required and spec.default is None:
            return SettingStatus(spec, Status.MISSING, None, "required but not set")
        # Not required (or has a usable default) -> OK, reported as default.
        shown = None if spec.default is None else _to_text(spec.default)
        return SettingStatus(spec, Status.OK, shown, "")

    checker = path_exists
    if spec.validator is Validator.DIR_EXISTS:
        checker = dir_exists if dir_exists is not None else path_exists
    ok, detail = _validate(spec, value, path_exists=checker)
    shown = _to_text(value)
    if ok:
        return SettingStatus(spec, Status.OK, shown, "")
    return SettingStatus(spec, Status.INVALID, shown, detail)


def evaluate(
    env: Mapping[str, str],
    config: Mapping[str, object],
    *,
    specs: Sequence[SettingSpec] | None = None,
    path_exists: PathChecker = _always_false,
    dir_exists: PathChecker | None = None,
) -> list[SettingStatus]:
    """Evaluate every spec (or a supplied subset) and return their statuses."""
    return [
        evaluate_setting(spec, env, config, path_exists=path_exists, dir_exists=dir_exists)
        for spec in (specs if specs is not None else SPECS)
    ]


def by_status(statuses: Sequence[SettingStatus]) -> dict[Status, list[SettingStatus]]:
    """Group evaluated statuses by their :class:`Status` (stable spec order)."""
    grouped: dict[Status, list[SettingStatus]] = {s: [] for s in Status}
    for st in statuses:
        grouped[st.status].append(st)
    return grouped


# --- THE REGISTRY --------------------------------------------------------------
#
# Enumerated from .env.example, config/config.sample.json and the modules. Keep
# this list as the single place a new configurable setting is declared.

SPECS: Final[list[SettingSpec]] = [
    # --- secrets / integrations credentials (.env) ----------------------------
    SettingSpec(
        key="PLEX_TOKEN",
        location="env",
        validator=Validator.NONEMPTY,
        required=True,
        how="Set PLEX_TOKEN in .env (X-Plex-Token from a Plex web request).",
        group="plex",
        description="Plex auth token used by the dupefinder/integrity modules.",
    ),
    SettingSpec(
        key="RADARR_API_KEY",
        location="env",
        validator=Validator.NONEMPTY,
        required=True,
        how="Set RADARR_API_KEY in .env (Radarr > Settings > General > API Key).",
        group="radarr",
    ),
    SettingSpec(
        key="SONARR_API_KEY",
        location="env",
        validator=Validator.NONEMPTY,
        required=True,
        how="Set SONARR_API_KEY in .env (Sonarr > Settings > General > API Key).",
        group="sonarr",
    ),
    SettingSpec(
        key="QBIT_USERNAME",
        location="env",
        validator=Validator.NONEMPTY,
        required=False,
        how="Set QBIT_USERNAME in .env (qBittorrent WebUI user).",
        group="qbittorrent",
    ),
    SettingSpec(
        key="QBIT_PASSWORD",
        location="env",
        validator=Validator.NONEMPTY,
        required=False,
        how="Set QBIT_PASSWORD in .env (qBittorrent WebUI password).",
        group="qbittorrent",
    ),
    SettingSpec(
        key="TMDB_BEARER_TOKEN",
        location="env",
        validator=Validator.NONEMPTY,
        required=False,
        how="Set TMDB_BEARER_TOKEN in .env (TMDb API Read Access Token).",
        group="tmdb",
    ),
    SettingSpec(
        key="GEMINI_API_KEY",
        location="env",
        validator=Validator.NONEMPTY,
        required=False,
        how="Set GEMINI_API_KEY in .env (Google AI Studio key; organizer AI).",
        group="gemini",
    ),
    SettingSpec(
        key="IZUMI_TELEGRAM_BOT_TOKEN",
        location="env",
        validator=Validator.NONEMPTY,
        required=False,
        how="Set IZUMI_TELEGRAM_BOT_TOKEN in .env (from @BotFather).",
        group="telegram",
    ),
    SettingSpec(
        key="IZUMI_TELEGRAM_CHAT_ID",
        location="env",
        validator=Validator.INT,
        required=False,
        how="Set IZUMI_TELEGRAM_CHAT_ID in .env (numeric id from @userinfobot).",
        group="telegram",
    ),
    SettingSpec(
        key="IZUMI_SENTINEL_SERVER",
        location="env",
        validator=Validator.NONEMPTY,
        required=False,
        how="Set IZUMI_SENTINEL_SERVER=host:port in .env (external watchdog).",
        group="sentinel",
    ),
    # --- config.json: safety --------------------------------------------------
    SettingSpec(
        key="safety.mode",
        location="safety.mode",
        validator=Validator.ENUM,
        required=False,
        default="dry_run",
        choices=("dry_run", "audit", "live"),
        how="Edit config.json safety.mode (dry_run|audit|live); keep dry_run unless acting.",
        group="safety",
    ),
    SettingSpec(
        key="safety.quarantine_dir",
        location="safety.quarantine_dir",
        validator=Validator.DIR_EXISTS,
        required=False,
        default="/mnt/user/Temp/izumi_quarantine",
        how="Edit config.json safety.quarantine_dir to an existing directory on the array.",
        group="safety",
    ),
    SettingSpec(
        key="safety.retention_days",
        location="safety.retention_days",
        validator=Validator.INT,
        required=False,
        default=7,
        how="Edit config.json safety.retention_days (days quarantine is kept).",
        group="safety",
    ),
    # --- config.json: logging / reporting -------------------------------------
    SettingSpec(
        key="logging.dir",
        location="logging.dir",
        validator=Validator.DIR_EXISTS,
        required=False,
        default="/mnt/cache/appdata/izumi/logs",
        how="Edit config.json logging.dir to an existing log directory.",
        group="logging",
    ),
    SettingSpec(
        key="reporting.dir",
        location="reporting.dir",
        validator=Validator.DIR_EXISTS,
        required=False,
        default="/mnt/cache/appdata/izumi/reports",
        how="Edit config.json reporting.dir to an existing reports directory.",
        group="reporting",
    ),
    # --- config.json: notify --------------------------------------------------
    SettingSpec(
        key="notify.level",
        location="notify.level",
        validator=Validator.ENUM,
        required=False,
        default="fail",
        choices=("none", "fail", "all"),
        how="Edit config.json notify.level (none|fail|all).",
        group="notify",
    ),
    SettingSpec(
        key="notify.provider",
        location="notify.provider",
        validator=Validator.NONEMPTY,
        required=False,
        default="telegram",
        how="Edit config.json notify.provider (currently 'telegram').",
        group="notify",
    ),
    # --- config.json: paths ---------------------------------------------------
    SettingSpec(
        key="paths.media_root",
        location="paths.media_root",
        validator=Validator.DIR_EXISTS,
        required=False,
        how="Edit config.json paths.media_root to your Plex media root.",
        group="paths",
    ),
    SettingSpec(
        key="paths.movies_root",
        location="paths.movies_root",
        validator=Validator.DIR_EXISTS,
        required=False,
        how="Edit config.json paths.movies_root to the movies library directory.",
        group="paths",
    ),
    SettingSpec(
        key="paths.series_root",
        location="paths.series_root",
        validator=Validator.DIR_EXISTS,
        required=False,
        how="Edit config.json paths.series_root to the series library directory.",
        group="paths",
    ),
    SettingSpec(
        key="paths.organizer_source",
        location="paths.organizer_source",
        validator=Validator.DIR_EXISTS,
        required=False,
        how="Edit config.json paths.organizer_source to the organizer intake directory.",
        group="paths",
    ),
    # --- config.json: integrations.radarr / sonarr ----------------------------
    SettingSpec(
        key="radarr.url",
        location="integrations.radarr.url",
        validator=Validator.URL,
        required=False,
        how="Edit config.json integrations.radarr.url (http://host:7878).",
        group="radarr",
    ),
    SettingSpec(
        key="sonarr.url",
        location="integrations.sonarr.url",
        validator=Validator.URL,
        required=False,
        how="Edit config.json integrations.sonarr.url (http://host:8989).",
        group="sonarr",
    ),
    SettingSpec(
        key="qbittorrent.url",
        location="integrations.qbittorrent.url",
        validator=Validator.URL,
        required=False,
        how="Edit config.json integrations.qbittorrent.url (http://host:8080).",
        group="qbittorrent",
    ),
    # --- config.json: integrations.ollama / gemini (AI backends) --------------
    SettingSpec(
        key="ollama.base_url",
        location="integrations.ollama.base_url",
        validator=Validator.URL,
        required=False,
        default="http://localhost:11434",
        how="Edit config.json integrations.ollama.base_url (http://host:11434).",
        group="ollama",
    ),
    SettingSpec(
        key="ollama.model",
        location="integrations.ollama.model",
        validator=Validator.NONEMPTY,
        required=False,
        default="qwen3:8b",
        how="Edit config.json integrations.ollama.model (e.g. qwen3:8b).",
        group="ollama",
    ),
    SettingSpec(
        key="gemini.model",
        location="integrations.gemini.model",
        validator=Validator.NONEMPTY,
        required=False,
        default="gemini-2.0-flash",
        how="Edit config.json integrations.gemini.model.",
        group="gemini",
    ),
    # --- config.json: module knobs --------------------------------------------
    SettingSpec(
        key="logwatch.days",
        location="integrations.logwatch.days",
        validator=Validator.INT,
        required=False,
        default=7,
        how="Edit config.json integrations.logwatch.days (log window in days).",
        group="logwatch",
    ),
    SettingSpec(
        key="analyst.dupefinder_reports",
        location="integrations.analyst.dupefinder_reports",
        validator=Validator.DIR_EXISTS,
        required=False,
        how="Edit config.json integrations.analyst.dupefinder_reports to the DF REPORTS dir.",
        group="analyst",
    ),
    SettingSpec(
        key="uptime.timeout",
        location="integrations.uptime.timeout",
        validator=Validator.INT,
        required=False,
        default=3,
        how="Edit config.json integrations.uptime.timeout (TCP connect seconds).",
        group="uptime",
    ),
]

# A defensive guard so a future edit can't silently introduce a duplicate key.
_seen: set[str] = set()
for _spec in SPECS:
    if _spec.key in _seen:  # pragma: no cover - guards authoring mistakes
        raise ValueError(f"duplicate SettingSpec key: {_spec.key}")
    _seen.add(_spec.key)
del _seen


def specs_for_group(group: str) -> list[SettingSpec]:
    """All specs belonging to ``group`` (for a future per-section menu editor)."""
    return [s for s in SPECS if s.group == group]


def groups() -> list[str]:
    """Distinct setting groups in first-seen order (for menu sectioning)."""
    out: list[str] = []
    for s in SPECS:
        if s.group not in out:
            out.append(s.group)
    return out


__all__ = [
    "SPECS",
    "PathChecker",
    "SettingSpec",
    "SettingStatus",
    "Status",
    "Validator",
    "by_status",
    "evaluate",
    "evaluate_setting",
    "groups",
    "is_valid_url",
    "resolve",
    "specs_for_group",
]
