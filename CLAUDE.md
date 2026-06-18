# CLAUDE.md — session bootstrap for Claude Code

This repo is `izumi`, a modular Python homelab-automation platform built on the
`plex_dupefinder` codebase. **Before doing any work, read `AI_CONTEXT.md` then
`AGENTS.md`.** They are the contract; this file is just the quick reference.

## Non-negotiables (full list in `docs/INVARIANTS.md`)
1. **Never delete directly** — all destructive/move ops go through `core/fs` → quarantine. No `rm`/`os.remove`/`shutil.rmtree`/`--remove-source-files`/`find -delete` in modules.
2. New modules default `DRY_RUN = True`.
3. Secrets only via `core/secrets` (ENV/`.env`); never hardcode or commit.
4. One shared impl each: `core/config`, `core/logging`, `core/locks`, `core/safety`, `core/notify`.
5. Python-first; bash only in `adapters/bash/`.

## Layout
`run.py` (CLI) · `pipelines.yaml` · `core/` (spine) · `modules/` (one job each, never import each other) · `integrations/` (typed clients) · `adapters/bash/` · `config/` · `tests/` · `docs/adr/`.

## Commands
```bash
ruff check . && mypy --strict .          # lint + types
pytest --cov --cov-report=term-missing   # tests (module coverage gate > 90%)
python run.py <module> --dry-run         # run one module safely
python run.py health                     # healthcheck
```

## Workflow
- Branching model (`AGENTS.md` §7): `main` (prod) ← `release/*`/`hotfix/*`; `develop` (integration) ← `feature/IZ-NNN-slug` (one story, small PR).
- One story = one `feature/IZ-NNN-slug` branch off `develop`. Stories are ≤4 h.
- Follow the DoD checklist in `AGENTS.md` §8. CI must be green.
- Pick work from `BACKLOG.md`. Code against `core/CONTRACT.md` (FREEZE-2).
- **Do not** commit `config.json`/`.env`/`logs/`/`reports/`/`quarantine/`.
- **Do not** commit or push unless asked. If on `master`, branch first.
- **Do not** delete legacy files until their replacement is ≥ 2 weeks green.

## Language
User prefers Spanish — reply in Spanish. Docs/code/identifiers stay in English.
