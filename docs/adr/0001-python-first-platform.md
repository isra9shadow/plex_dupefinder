# ADR-0001 — Python-first platform, bash only for Unraid wrappers

- Status: Accepted
- Date: 2026-06
- Deciders: project owner (solo operator)

## Context

The homelab automation grew organically into three overlapping codebases: flat
bash scripts, a bash orchestrator (`izumi-orchestrator`), and a Python Telegram
bot. The same dangerous logic (disk balancing, cleanup, decompress, stalled
downloads, notifications) was implemented up to three times with diverging rules.
One subsystem — `plex_dupefinder` — already had a mature Python foundation
(typed-ish code, tests, CI, a DRY_RUN + quarantine safety model, JSON reporting,
rotated logging, auto-update deploy).

The owner works solo and uses AI agents (Claude Code) as the development force.
Consistency across many parallel agents matters more than language preference.

## Decision

The consolidated platform is **Python-first**. All business logic is Python with
full type hints (`mypy --strict`). **Bash is allowed only in `adapters/bash/`** as
thin wrappers around Unraid-specific commands that are impractical from Python
(`newperms`, `filebot`, docker control). Bash carries no business logic, state,
or secrets.

## Consequences

- One language, one toolchain (ruff, mypy, pytest), one mental model → agents
  produce consistent code.
- The 1422-line bash `media_triage_daily.sh` and similar become typed, testable
  Python modules.
- Unraid-coupled operations remain shell, but isolated and minimal.
- Cost: a one-time rewrite of legacy bash into modules (planned, incremental,
  shadow-tested — see `MIGRATION_PLAN.md`).
