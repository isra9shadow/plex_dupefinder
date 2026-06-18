# ADR-0004 — Core public API is frozen (FREEZE-2)

- Status: Accepted
- Date: 2026-06

## Context
Parallel module development needs a stable foundation. If the `core` API drifts,
every module breaks and merges conflict.

## Decision
The public API of `core` (`config, logging, locks, safety, fs, notify, secrets,
types, errors, registry, report`) is **frozen** and documented in
`core/CONTRACT.md` (FREEZE-2). Changing any frozen signature requires a new ADR.

## Consequences
- Modules build against a stable contract; conflict-free parallel work.
- The implementation may evolve internally as long as the contract holds.
- `core/CONTRACT.md` is the single source of truth for the module-facing API.
