"""Service / container up-check — a read-only "is everything up?" probe.

Two independent, best-effort checks (a failure in one never aborts the other):

  1. TCP TARGETS — for every configured ``{"name","host","port"}`` it opens a
     short-lived TCP connection (via an injected prober so tests stay offline)
     and records the target as UP or DOWN. A connection that refuses / times out
     is DOWN, never an exception that aborts the run.
  2. EXPECTED CONTAINERS — it lists running docker containers via
     ``adapters/docker`` and flags any name in ``expect_running`` that is NOT
     currently running. ``ignore_containers`` (batch / one-shot dockers such as
     Configarr, recyclarr, watchtower that exit normally) are removed from the
     expected set first, so a healthy one-shot is never reported as "down".

This module is strictly READ-ONLY (INVARIANT I1): it never moves, deletes or
modifies any host file or media. The only thing it writes is its own report
under ``reporting.dir / "uptime"`` (summary.md + plan.json). ``subprocess`` is
never touched directly — container state comes from the docker adapter and TCP
checks use the stdlib ``socket`` module.

Config (config.json):
  integrations.uptime :
    targets           : list of {"name","host","port"} probed over TCP
    expect_running    : list of docker container names that SHOULD be running
    ignore_containers : batch/one-shot containers that exit normally and must
                        NOT be flagged down (e.g. Configarr, recyclarr, watchtower)
    timeout           : per-target TCP connect timeout in seconds (default 3.0)
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from dataclasses import dataclass

from adapters import docker
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext

_DEFAULT_TIMEOUT = 3.0  # seconds per TCP connect attempt

# A prober takes (host, port, timeout) and returns True iff a TCP connection
# could be established. Injected so tests need no real network.
TcpProber = Callable[[str, int, float], bool]


def tcp_probe(host: str, port: int, timeout: float) -> bool:
    """Return True iff a TCP connection to ``host:port`` succeeds within ``timeout``.

    Uses the stdlib ``socket`` module (no urllib / subprocess). Any failure —
    refused, timed out, DNS error — is reported as "down" (False), never raised.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass(frozen=True)
class Target:
    """A configured TCP endpoint to probe."""

    name: str
    host: str
    port: int


@dataclass(frozen=True)
class TargetResult:
    """The outcome of probing a single :class:`Target`."""

    name: str
    host: str
    port: int
    up: bool


@dataclass(frozen=True)
class _Settings:
    targets: list[Target]
    expect_running: list[str]
    ignore_containers: set[str]
    timeout: float


def parse_targets(raw: object) -> list[Target]:
    """Parse the ``targets`` config into typed :class:`Target` objects.

    Skips malformed entries (missing/empty name or host, non-integer/out-of-range
    port) rather than raising, so one bad line can't break the whole check.
    """
    if not isinstance(raw, list):
        return []
    out: list[Target] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        host = entry.get("host")
        port = entry.get("port")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(host, str) or not host.strip():
            continue
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            continue
        out.append(Target(name=name.strip(), host=host.strip(), port=port))
    return out


def _str_list(raw: object) -> list[str]:
    """Coerce a config value into a clean list of non-empty strings."""
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _settings(ctx: RunContext) -> _Settings:
    """Read ``integrations.uptime`` into a typed settings object (sane defaults)."""
    cfg = ctx.config.integrations.get("uptime", {})
    timeout_raw = cfg.get("timeout", _DEFAULT_TIMEOUT)
    timeout = (
        float(timeout_raw)
        if isinstance(timeout_raw, int | float)
        and not isinstance(timeout_raw, bool)
        and timeout_raw > 0
        else _DEFAULT_TIMEOUT
    )
    return _Settings(
        targets=parse_targets(cfg.get("targets")),
        expect_running=_str_list(cfg.get("expect_running")),
        ignore_containers={c.lower() for c in _str_list(cfg.get("ignore_containers"))},
        timeout=timeout,
    )


def probe_targets(targets: list[Target], prober: TcpProber, timeout: float) -> list[TargetResult]:
    """Probe every target with ``prober`` (pure: I/O is the injected callable)."""
    return [
        TargetResult(t.name, t.host, t.port, up=bool(prober(t.host, t.port, timeout)))
        for t in targets
    ]


def missing_containers(
    expect_running: list[str], running: list[str], ignore: set[str]
) -> list[str]:
    """Names that SHOULD be running but are not (batch/ignore containers removed).

    Comparison is case-insensitive (docker is case-sensitive but operators are
    not). Order follows ``expect_running``; duplicates collapse. A name on both
    ``expect_running`` and ``ignore`` is treated as ignored and never flagged.
    """
    running_set = {name.lower() for name in running}
    seen: set[str] = set()
    out: list[str] = []
    for name in expect_running:
        key = name.lower()
        if key in ignore or key in seen:
            continue
        seen.add(key)
        if key not in running_set:
            out.append(name)
    return out


def _write_report(
    ctx: RunContext,
    results: list[TargetResult],
    running: list[str],
    missing: list[str],
    note: str,
) -> None:
    out_dir = ctx.config.reporting.dir / "uptime"
    out_dir.mkdir(parents=True, exist_ok=True)
    down_targets = [r for r in results if not r.up]
    up_targets = [r for r in results if r.up]
    down_count = len(down_targets) + len(missing)

    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "down_count": down_count,
                "note": note,
                "targets": [
                    {
                        "name": r.name,
                        "host": r.host,
                        "port": r.port,
                        "status": "up" if r.up else "down",
                    }
                    for r in results
                ],
                "containers_running": sorted(running),
                "expected_not_running": missing,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    lines = [
        "# Uptime summary",
        "",
        f"Targets checked: {len(results)} ({len(up_targets)} up, {len(down_targets)} down)",
        f"Expected containers not running: {len(missing)}",
        f"Down count: {down_count}",
        "",
        f"## TCP targets ({len(results)})",
    ]
    if results:
        lines += [
            f"- {'DOWN' if not r.up else 'UP'}: {r.name} ({r.host}:{r.port})"
            for r in sorted(results, key=lambda r: (r.up, r.name.lower()))
        ]
    else:
        lines.append("(no targets configured)")
    lines += [
        "",
        "## Expected containers not running",
        *([f"- {name}" for name in missing] or ["(all expected containers are running)"]),
    ]
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("uptime")
def run(ctx: RunContext, prober: TcpProber = tcp_probe) -> ModuleResult:
    result = ModuleResult(module="uptime", run_id=ctx.run_id, mode=ctx.mode)
    settings = _settings(ctx)

    # 1) TCP targets — a probe failure is captured as "down" by tcp_probe itself,
    #    so this loop cannot raise on a single unreachable host.
    results = probe_targets(settings.targets, prober, settings.timeout)
    down_targets = [r for r in results if not r.up]

    # 2) Expected containers — list running dockers (best-effort; the adapter
    #    returns [] and never raises if docker is unreachable).
    running: list[str] = []
    missing: list[str] = []
    note = ""
    if settings.expect_running:
        running = docker.container_names(include_stopped=False)
        if not running and not docker.probe():
            note = (
                "Docker NOT reachable from the container (binary missing or "
                "/var/run/docker.sock not mounted); expected-container checks "
                "were skipped."
            )
            result.add_failure(FailureRecord(category="integration", message=note))
        else:
            missing = missing_containers(
                settings.expect_running, running, settings.ignore_containers
            )

    for r in down_targets:
        result.add_failure(
            FailureRecord(
                category="integration",
                message=f"target down: {r.name} ({r.host}:{r.port})",
            )
        )
    for name in missing:
        result.add_failure(
            FailureRecord(category="integration", message=f"container not running: {name}")
        )

    down_count = len(down_targets) + len(missing)
    _write_report(ctx, results, running, missing, note)
    ctx.logger.info(
        "uptime done",
        targets=len(results),
        targets_down=len(down_targets),
        containers_missing=len(missing),
    )
    result.metrics["down_count"] = float(down_count)
    result.actions = 0  # read-only
    return result
