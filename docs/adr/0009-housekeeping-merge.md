# ADR-0009 — Merge housekeeping modules; defer perms

- Status: Accepted
- Date: 2026-06

## Context
`decompress`, `cleanup` and `series_blacklist` are three small, related
"housekeeping" responsibilities. A separate always-on `perms` module assumes
permission drift that does not currently happen (containers run PUID 99/PGID 100).

## Decision
- Merge the three into one `modules/housekeeping.py` (verify+extract rars,
  junk/symlink/empty-dir cleanup, blacklist removal) — all via `core/fs`
  (quarantine), one lock, one report.
- **Defer `perms`** — build it only if drift is observed.

## Consequences
Fewer modules to schedule and reason about; one fewer 🔴 path until needed.
