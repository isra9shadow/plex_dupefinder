# ADR-0011 — plex_dupefinder integrated as a wrapped module (port later)

- Status: Accepted
- Date: 2026-06
- Deciders: project owner (solo operator)

## Context

We must integrate the deployed ~2,900-line `plex_dupefinder.py` into the platform
**without changing behavior**. A native rewrite onto `core/*` now would (a) risk
behavior change on a script that already runs in production, and (b) couple
`plexapi` into the platform's import graph (every `run.py` command would import it).

## Decision

`modules/media/plex_dupefinder.py` registers a module that **invokes the proven
root `plex_dupefinder.py` via `adapters/command`** (subprocess, stdin closed,
timeout). The legacy script keeps its own `config.json` safety (DRY_RUN /
QUARANTINE). No logic is reimplemented.

A native port onto `core/*` (config, logging, fs/quarantine, safety) comes later
as a separate, behavior-preserving refactor **gated by parity tests**
(MIGRATION_PLAN §9), after which this wrapper is replaced.

## Consequences

- Zero behavior change; the production script is untouched and stays the source of
  truth for dedupe logic.
- The platform import graph stays light — `plexapi` is only loaded when the
  subprocess actually runs, not on `registry.discover()`.
- The single pipeline can now run dedupe (`run.py plex_dupefinder`).
- Cost: one subprocess hop until the native port lands. Acceptable and reversible.
