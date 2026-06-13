# AGENTS.md — Contract for every agent working on this repository

> **Read this file first, every session, before writing a single line.**
> It is the behavioural contract (FREEZE-1). If code you write violates it, it
> will be rejected in review. When in doubt, prefer the rule here over your own
> judgement, and open an ADR (`docs/adr/`) if you think a rule should change.

---

## Contents
1. What this project is
2. The five GOLDEN RULES (non-negotiable invariants)
3. Repository layout (where things go)
4. The module contract
5. Coding conventions
6. Testing (DoD gate)
7. Git branching model & PR workflow
8. Definition of Ready / Definition of Done
9. How to start a story (agent checklist)
10. Freeze points (synchronisation)

---

## 1. What this project is

This repo is becoming **`izumi`**, a single modular **Python** platform that
runs an Unraid homelab 24/7. It started life as `plex_dupefinder` (a Plex
duplicate finder) and that maturity — config-merge, DRY_RUN + quarantine safety
model, JSON reporting, rotated logging, auto-update deploy, tests — is the
**spine** we build everything else on.

We are consolidating an old, organically-grown ecosystem (flat bash scripts, a
bash orchestrator, and a Python Telegram bot) that **triplicated** the same
dangerous logic (move/balance disks, cleanup, decompress, kill stalled
downloads, notify). The goal is exactly one of each:

> **1 repo · 1 config · 1 logging · 1 reporting · 1 pipeline · 1 safety framework · N modules.**

The dedupe logic is no longer "the project" — it is just `modules/plex_dupefinder.py`.

---

## 2. The five GOLDEN RULES (non-negotiable invariants)

These are also in `docs/INVARIANTS.md`. Breaking any of them is a release blocker.

1. **Nothing is deleted directly. Ever.** No `os.remove`, `shutil.rmtree`,
   `Path.unlink`, `rm`, `rsync --remove-source-files`, or `find -delete` inside
   a module. **All destructive/moving operations go through `core/fs`**, which
   moves to **quarantine** with a restore sidecar. Direct deletion is only
   allowed inside `core/fs` itself, and only for retention purge with audit.
2. **Safe by default.** New modules default to `DRY_RUN = True`. A fresh
   checkout, run with no config, must never destroy or move data.
3. **No secrets in code.** Tokens, API keys, passwords, chat IDs come from
   `core/secrets` (ENV / `.env`, git-ignored). Never hardcode, never commit.
   If you find a secret in code, treat it as compromised and flag it.
4. **One of each shared concern.** Use `core/config`, `core/logging`,
   `core/locks`, `core/safety`, `core/notify`. Do **not** roll your own logger,
   config parser, lock, or Telegram client. There must never be a second one.
5. **Python-first.** Everything is Python with full type hints. **Bash is only
   allowed in `adapters/bash/`** for Unraid-specific calls that cannot be done
   from Python (`newperms`, `filebot`, docker control). A bash adapter is a thin
   wrapper invoked from a Python module — never business logic.

---

## 3. Repository layout (where things go)

```
run.py                  ÚNICO entrypoint/CLI: `run.py <module|pipeline> [--dry-run] ...`
pipelines.yaml          declarative job ordering (daily / monitor)
pyproject.toml          ruff + mypy(strict) + pytest + coverage

core/                   shared platform — the contract every module consumes
  config.py logging.py locks.py safety.py fs.py notify.py secrets.py
  errors.py types.py metrics.py health.py docker.py
  CONTRACT.md           the frozen public API (FREEZE-2). Do not change without an ADR.

modules/                one responsibility per file; each is independently runnable
  plex_dupefinder.py media_integrity.py arr_orphans.py arr_db.py perms.py
  downloads_watchdog.py disk_balance.py share_rebalance.py decompress.py
  cleanup.py series_blacklist.py

integrations/           typed API clients (no business logic)
  plex.py radarr.py sonarr.py qbittorrent.py tmdb.py tvmaze.py

adapters/bash/          the ONLY bash allowed (Unraid wrappers): newperms.sh filebot.sh

config/                 config.json (git-ignored) · disk_map.json
.env / .env.example     secrets (real .env git-ignored)
logs/ reports/ quarantine/   observability + recovery (git-ignored)

tests/                  unit/ integration/ security/ smoke/ regression/ conftest.py
tools/                  shadow_diff.py analyze_report.py compare_plans.py
deploy/                 run.sh (auto-update) · unraid_user_script.sh
docs/                   adr/ INVARIANTS.md OPERATIONS RUNBOOK TROUBLESHOOTING ...
```

If your story needs a file that doesn't fit here, stop and ask in the PR — do
not invent a parallel structure.

---

## 4. The module contract (what every `modules/*.py` must look like)

> The exact signatures are frozen in `core/CONTRACT.md` by STORY-016. Until then,
> code against this shape; it will not change in spirit.

- A module exposes a single callable entrypoint, e.g. `run(ctx: Context) -> ModuleResult`.
- It receives a `Context` carrying: resolved `config`, a module `logger`,
  `fs` (the only mover/deleter), `notify`, `dry_run`/`audit` flags, and a `run_id`.
- It is **idempotent**, **single-instance** (wrapped by `core/locks`),
  **report-only-capable**, and emits a JSON report fragment + metrics.
- It never imports another module. Cross-module needs go through `core/` or
  `integrations/`. This keeps `EPIC-003/004/005/006` conflict-free in parallel.

---

## 5. Coding conventions

- **Python 3.11+**, full type hints, `from __future__ import annotations`.
- `mypy --strict` and `ruff` must pass. No `# type: ignore` without a reason.
- **Naming:** `snake_case` functions/modules, `PascalCase` classes,
  `UPPER_SNAKE` config keys. Module names are verbs/nouns of their job.
- **Logging:** `logger = core.logging.get_logger(__name__)`. Structured, never
  `print()` in library code. Levels: DEBUG (tracing), INFO (actions), WARNING
  (recoverable), ERROR (failed op). One `run_id` per execution.
- **Errors:** raise from `core/errors` hierarchy; never bare `except:`. Failures
  are recorded (category, message, src/dest) and surfaced in the report.
- **Config:** read via `core/config`. No literals for paths, disks, thresholds,
  URLs — they live in `config.json` / `disk_map.json`.
- **No network/FS side effects at import time.** Modules must be importable by
  the test suite without touching Plex, disks, or the network.

---

## 6. Testing (DoD gate — see §8)

- `pytest`; module coverage **must be > 90%**.
- Required per module: **unit**, **integration** (mocked Plex/ARR/qBit via
  `tests/conftest.py` fakes), **dry-run** (assert nothing changed),
  and the shared **security** tests (no direct delete, no secrets in code).
- Destructive modules additionally need a **quarantine + restore** test and a
  **regression/parity** test against the legacy behaviour where one exists.
- Tests use a temporary filesystem, never real `/mnt/user`.

---

## 7. Git branching model & PR workflow

Branches:
- **`main`** — production. Only `release/*` and `hotfix/*` merge here; every commit
  is deployable and tagged. The Unraid User Script tracks `main`.
- **`develop`** — integration. All feature work merges here; must always be green.
- **`feature/IZ-NNN-slug`** — exactly one story, branched off `develop`, merged back
  via a small PR.
- **`release/x.y`** — stabilisation off `develop` (only fixes/docs); merges to
  `main` and back to `develop`; tag on `main`.
- **`hotfix/x.y.z`** — urgent production fix off `main`; merges to `main` and `develop`.

Rules:
- One story = one `feature/IZ-NNN-slug` branch = one small PR into `develop`
  (**a ≤4 h story** — split anything larger). Rebase on `develop`; never merge
  `develop` into your feature branch.
- PR links the IZ id, lists changes, confirms the DoD checklist; CI
  (lint + type + test + coverage + secret-scan) must be green.
- **Never commit** `config.json`, `.env`, anything under `logs/ reports/
  quarantine/`, or any credential. **Never push or commit unless the human asks.**
  If you're on `main`/`develop`, branch first.
- Do not delete or rewrite legacy files until their replacement has run in
  shadow/parity ≥ 2 weeks green (see `MIGRATION_PLAN.md`).

---

## 8. Definition of Ready / Definition of Done

**DoR:** deps merged · inputs/outputs + config paths defined · uses only frozen
contracts · acceptance criteria has an identified fixture.

**DoD (every story):**
- [ ] `mypy --strict` + `ruff` clean
- [ ] unit + integration tests, module coverage > 90 %
- [ ] structured logging via `core/logging`; errors via `core/errors`
- [ ] **no destructive op outside `core/fs`** (security test proves it)
- [ ] DRY_RUN respected and tested; safe default
- [ ] technical doc + AI doc + one real example
- [ ] declarative config (no hardcoded paths/disks/secrets)
- [ ] CI green · PR reviewed · ADR added if a decision changed

---

## 9. How to start a story (checklist for an agent)

1. Read `AI_CONTEXT.md`, then `docs/INVARIANTS.md`, then your story in `BACKLOG.md`.
2. Confirm DoR. Create `story/STORY-NNN-slug`.
3. Code against `core/CONTRACT.md` (if FREEZE-2 is published) — do not assume
   internals, only the published API.
4. Write tests first where practical. Keep within the module's files only.
5. Update docs (module README + relevant section). Open a PR with the DoD checklist.
6. If you must break an invariant or a freeze, **stop** and write an ADR instead.

---

## 10. Freeze points (synchronisation)

- **FREEZE-1 — Conventions:** this file. Done.
- **FREEZE-2 — Core API:** `core/CONTRACT.md` (STORY-016). No module starts
  before it; once published it does not change without an ADR.
- **FREEZE-3 — Integrations API:** the `integrations/*` interfaces. Modules mock
  against these; the real client can finish in parallel.

The whole parallelisation strategy depends on these freezes holding. Respect them.
