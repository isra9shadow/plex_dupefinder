# ADR-0008 — Simplicity cuts (what we deliberately do NOT build)

- Status: Accepted
- Date: 2026-06

## Context
Solo operator: reliability and speed-of-evolution over sophistication.

## Decision
- `metrics` → counters inside the JSON report (no Prometheus module yet).
- `health` → a `run.py health` subcommand (not a module).
- `pipelines` → a section of `config.json` (no separate format/loader).
- `contracts`/`events`/`context` → folded into `core/types.py`.
- Config/report validation → typed dataclasses (no `jsonschema` dependency).
- Drop `prowlarr`/`tvmaze` integrations until a module needs them (YAGNI).
- **`disk_balance`/`share_rebalance` movers → a read-only `disk_monitor`**; Unraid's
  mover handles cache↔array.
- `arr_db` → restore from *arr's own backup zip (no custom SQLite surgery).

## Consequences
Fewer files, fewer 🔴 destructive paths, same capability. Re-add a cut piece only
when a concrete need appears, via a new ADR.
