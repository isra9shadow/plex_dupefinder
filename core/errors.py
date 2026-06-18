"""Exception hierarchy for the izumi platform.

Every module raises from this hierarchy (never bare ``Exception``). Each error
carries a ``category`` so failures can be grouped consistently in reports and
metrics. See ``AGENTS.md`` §5 and ``docs/INVARIANTS.md``.
"""

from __future__ import annotations


class IzumiError(Exception):
    """Base class for all platform errors."""

    category: str = "general"

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if category is not None:
            self.category = category


class ConfigError(IzumiError):
    """Invalid, missing, or unparseable configuration."""

    category = "config"


class SecretError(IzumiError):
    """A required secret is missing or empty (fail-closed)."""

    category = "secret"


class SafetyError(IzumiError):
    """A safety invariant would be violated (e.g. a direct delete)."""

    category = "safety"


class QuarantineError(IzumiError):
    """A quarantine move, sidecar write, or restore failed."""

    category = "quarantine"


class LockError(IzumiError):
    """Another instance already holds the lock for this job."""

    category = "lock"


class IntegrationError(IzumiError):
    """An external service (Plex, *arr, qBittorrent, TMDb) call failed."""

    category = "integration"


class ValidationError(IzumiError):
    """Data failed schema or precondition validation."""

    category = "validation"
