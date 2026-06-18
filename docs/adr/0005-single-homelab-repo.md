# ADR-0005 — One repository for the whole homelab

- Status: Accepted
- Date: 2026-06

## Context
Three repos held overlapping concerns: `scripts` (automation), `plex_dupefinder`
(app), `homelab-infra` (IaC/docs). For a solo operator this triplicates config,
CI, docs and sources of truth.

## Decision
Consolidate all three into `plex_dupefinder` (the target platform). The Python app
stays at the root; `homelab-infra` is absorbed as `infra/ inventory/ templates/
docs/`. `scripts` logic is migrated into `modules/` and then retired.

## Consequences
- One repo, one config, one CI, one doc set.
- Legacy repos are archived read-only only after their replacements are ≥2 weeks
  green (see MIGRATION_PLAN). No repo is deleted before that.
