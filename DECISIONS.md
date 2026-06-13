# DECISIONS.md — Architecture Decision Records (index)

Every architecture-affecting decision is a short ADR in [`docs/adr/`](docs/adr/).
Decisions are immutable once Accepted; to change one, add a new ADR that supersedes
it. This file is the index.

| ADR | Decision | Status |
|---|---|---|
| [0001](docs/adr/0001-python-first-platform.md) | Python-first platform; bash only for Unraid wrappers | Accepted |
| [0002](docs/adr/0002-quarantine-over-direct-deletion.md) | Quarantine over direct deletion; `core/fs` is the only mover/deleter | Accepted |
| [0003](docs/adr/0003-single-config-logging-pipeline.md) | One config, logging, reporting, pipeline, safety, notifier | Accepted |
| [0004](docs/adr/0004-core-contract-frozen.md) | Core public API frozen in `core/CONTRACT.md` (FREEZE-2) | Accepted |
| [0005](docs/adr/0005-single-homelab-repo.md) | One repository for the whole homelab (absorb scripts + homelab-infra) | Accepted |
| [0006](docs/adr/0006-inventory-as-source-of-truth.md) | Inventory is the single source of truth for topology | Accepted |
| [0007](docs/adr/0007-defer-rename.md) | Defer the repository rename (keep `plex_dupefinder` for now) | Accepted |
| [0008](docs/adr/0008-simplicity-cuts.md) | Simplicity cuts — what we deliberately do not build | Accepted |
| [0009](docs/adr/0009-housekeeping-merge.md) | Merge housekeeping modules; defer `perms` | Accepted |
| [0010](docs/adr/0010-inventory-from-day-one.md) | Auto-generated inventory from day one | Accepted |
| [0011](docs/adr/0011-plex-dupefinder-wrapped-module.md) | plex_dupefinder integrated as a wrapped module (port later) | Accepted |

## Invariants
The non-negotiable rules these decisions enforce live in
[`docs/INVARIANTS.md`](docs/INVARIANTS.md). Chief among them: **nothing is deleted
directly — everything destructive goes through `core/fs` into quarantine.**
