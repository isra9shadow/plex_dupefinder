# SECURITY.md — secrets, safety & recovery model

> Implements ADR-0002 and INVARIANTS I1–I3, I9. Sprint 0 establishes the secret
> model and the security gate; the quarantine/rollback machinery lands with
> `core/safety` + `core/fs` in Sprint 1.

## Secrets
- **Single source:** `core/secrets.py`. Resolution: `os.environ` → optional
  git-ignored `.env`. Required secrets are **fail-closed** (missing → `SecretError`).
- **Never** hardcode or commit a secret. `.env` is git-ignored; only `.env.example`
  (names, no values) is versioned. Secret values are never logged.
- **Enforcement:** `tests/security/test_no_secrets.py` (pattern scan of platform
  source) + `detect-private-key` pre-commit hook + gitleaks in CI.
- **Rotation (do this in Sprint 0):** every credential that ever lived in the
  legacy `scripts/` repo is considered compromised — rotate TMDB tokens,
  Radarr/Sonarr API keys, the qBittorrent password, and all Telegram bot tokens,
  then put the new values only in `.env`.

## Safety model (from Sprint 1, designed now)
Every actionable run passes the shared gauntlet: `DRY_RUN`/`AUDIT` (safe default)
→ `MIN_FILE_AGE` → `STABILITY_CHECK` → `MAX_SIZE_RATIO` → **move to `quarantine/`
with a restore sidecar** (never a direct delete) → audited retention purge.
`core/fs` is the only code permitted to move or delete data.

## Recovery / rollback (summary; full table in `MIGRATION_PLAN.md` §6)
- **Code:** `git revert`; deploy wrapper falls back to previous good HEAD.
- **Data:** restore from the quarantine sidecar `restore_command`.
- **DB:** swap back the timestamped pre-change backup (arr_db integrity gate).
- **Config/secrets:** layered + revertible; defaults stay safe (DRY_RUN on).
- **Cutover:** flip a module back to `DRY_RUN=true`; legacy stays runnable until
  2 weeks post-cutover.

## Reporting a problem
This is a private homelab project. If you find a committed secret, treat it as
compromised: rotate it immediately and scrub it from history before pushing.
