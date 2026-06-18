# ADR-0006 — Inventory is the single source of truth for topology

- Status: Accepted
- Date: 2026-06

## Context
Disk/share/container/network facts were duplicated across hand-written docs and
hardcoded in scripts (and were wrong — legacy assumed disk6/disk7 that do not
exist). Stale, duplicated truth causes data-movement risk.

## Decision
Topology lives **once**, in `inventory/` (and is increasingly auto-generated into
`reports/inventory/`). The platform reads it (e.g. `config/disk_map.json` derives
from `inventory/hardware`); it never hardcodes disks/shares/paths.

## Consequences
- One authoritative description of the server, always current.
- Storage modules consume `disk_map`; correcting the inventory corrects every module.
