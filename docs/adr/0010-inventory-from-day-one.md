# ADR-0010 — Auto-generated inventory from day one

- Status: Accepted
- Date: 2026-06

## Context
The platform runs **inside** Unraid, so it has read access to `docker ps/inspect`,
`smartctl`, `lsblk`, `df`, `mount`, `/boot/config`, shares and appdata. Manual
infra documentation (homelab-infra) goes stale.

## Decision
Build inventory as first-class, read-only modules that emit living documentation to
`reports/inventory/` (`docker`, `disk`+SMART, `share`, `network`+dependency map).
These replace the hand-maintained homelab-infra docs. All host access goes through
the `adapters/` layer (single subprocess boundary, read-only, timeouts).

## Consequences
- Always-current, zero-maintenance infra docs.
- Highest value / lowest risk early work (read-only) — shipped first.
