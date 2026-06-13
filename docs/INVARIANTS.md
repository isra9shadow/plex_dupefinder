# INVARIANTS.md — rules that must NEVER be broken

These are hard constraints. Any code, PR, or agent output that violates one is a
release blocker. They exist because this platform runs **unattended on
irreplaceable media data**. If you believe an invariant must change, do not work
around it — open an ADR in `docs/adr/`.

---

## I1 · No direct destruction
No module may delete or move data directly. Forbidden anywhere outside `core/fs`:
`os.remove`, `os.unlink`, `Path.unlink`, `shutil.rmtree`, `shutil.move`, `rm`,
`rmdir`, `find ... -delete`, `rsync --remove-source-files`, `mv` of user data.
**All such operations go through `core/fs`, which moves to `quarantine/` and
writes a restore sidecar.** The only real delete in the whole codebase lives in
`core/fs`'s retention purge, and it is audited (count + bytes reported).
*Enforced by:* `tests/security/test_no_direct_delete.py`.

## I2 · Safe by default
A fresh checkout with no/empty config must never destroy or move data. New
modules default `DRY_RUN = True`. Turning on actions is an explicit, per-module,
configured decision.

## I3 · No secrets in code
No tokens, API keys, passwords, or chat IDs in source, config-in-git, or commits.
Secrets come from `core/secrets` (ENV / git-ignored `.env`), fail-closed.
*Enforced by:* secret scanner in CI + `tests/security/test_no_secrets.py`.

## I4 · One shared implementation per concern
Exactly one: config (`core/config`), logging (`core/logging`), locking
(`core/locks`), safety (`core/safety`), filesystem ops (`core/fs`), notifications
(`core/notify`), secrets (`core/secrets`). A second implementation is never
acceptable — that is the very disease we are curing.

## I5 · Python-first; bash is wrappers only
Business logic is Python with full type hints (`mypy --strict`). Bash exists only
in `adapters/bash/` as thin Unraid wrappers (`newperms`, `filebot`, docker) called
from Python. No logic, no state, no secrets in bash.

## I6 · Modules are isolated
A module never imports another module. Shared needs go through `core/` or
`integrations/`. This keeps modules independently developed, tested, and merged.

## I7 · Single-instance & observable
Every actionable run is wrapped by `core/locks` (no concurrent duplicate),
carries a `run_id`, logs structured JSON, and emits a schema-valid report.

## I8 · Config is declarative
No hardcoded paths, disks, thresholds, URLs, or credentials. They live in
`config/config.json` and `config/disk_map.json`, validated against a schema.

## I9 · Never `chmod 777`
Permission handling uses Unraid `newperms` (or explicit `nobody:users` + sane
modes) via `modules/perms`. Recursive `777` on media is forbidden — it was a
legacy footgun and is gone for good.

## I10 · Legacy is retired only after parity
Legacy scripts and the new platform run in shadow (report-only) and are diffed
before cutover. A legacy script is deleted only after its replacement has run
green for ≥ 2 weeks. Never commit/push or delete legacy unless the human asks.
