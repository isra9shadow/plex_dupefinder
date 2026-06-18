# ADR-0007 — Defer the repository rename

- Status: Accepted
- Date: 2026-06

## Context
The platform outgrows the name `plex_dupefinder` (candidate: `izumi`, the existing
brand — izumiportal.com). But the repo is already deployed, tested, in CI,
documented and in use.

## Decision
**Do not rename now.** Never rename + refactor + migrate at the same time.
Sequence: `plex_dupefinder → modular platform → homelab ops → rename LAST`. The
dedupe logic lives at `modules/media/plex_dupefinder.py` regardless of repo name.

## Consequences
- Production automation (`run.sh`, Unraid User Script, `origin/master`) is
  untouched during the migration.
- The rename becomes a final, isolated, cosmetic step (GitHub auto-redirects).
