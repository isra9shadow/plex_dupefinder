# ADR-0003 — One config, one logging, one pipeline, one safety framework

- Status: Accepted
- Date: 2026-06
- Deciders: project owner (solo operator)

## Context

The legacy ecosystem had configuration scattered across env vars, hardcoded
constants, an `orchestrator.env`, a `disks_map_manual.json`, and `.py` secret
files; two different dupefinder configs; three Telegram clients; three lock
implementations; and per-script ad-hoc logging (much of it stdout-only, lost when
run by cron). There were three rival orchestrators (a "guardian" bash script, the
izumi framework, and the bot daemon) with no single source of truth for "what
runs, when".

## Decision

The platform has **exactly one of each shared concern**, provided by `core/`:

- **One configuration**, layered: built-in defaults → `config/config.json` →
  environment. Disks/shares/margins in `config/disk_map.json`. Secrets in `.env`
  via `core/secrets`. Schema-validated.
- **One logging system**: structured JSON, rotated, per-module logger, `run_id`.
- **One reporting system**: schema-valid `reports/<run_id>.json` per run.
- **One pipeline/orchestrator**: `run.py` + declarative `pipelines.yaml`, invoked
  by a single Unraid User Script via `deploy/run.sh`.
- **One safety framework**: `core/safety` + `core/fs` (see ADR-0002).
- **One notifier**: `core/notify`.

Building a second implementation of any of these is forbidden (see
`docs/INVARIANTS.md` I4).

## Consequences

- A single place to look for behaviour, credentials, schedule, and logs →
  drastically lower operational and debugging cost for a solo operator.
- Modules stay thin: they consume `core`, they don't re-solve cross-cutting
  concerns → consistent output across many parallel agents.
- The freeze of the `core` public API (`core/CONTRACT.md`, FREEZE-2) becomes the
  key synchronisation point enabling conflict-free parallel module development.
- Cost: `core` is a serial bottleneck early in the project; it is prioritised on
  the critical path accordingly.
