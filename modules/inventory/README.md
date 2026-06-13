# modules/inventory

Auto-generated, living homelab inventory — replaces hand-maintained `homelab-infra`
docs (which go stale). Read-only: collects facts from inside Unraid via the
`adapters/` host layer and emits machine- and human-readable artifacts.

| Module | Reads (via adapters) | Emits |
|---|---|---|
| `docker_inventory` | `docker ps -a`, `docker inspect` | `reports/inventory/docker_inventory.{json,md}` |
| `disk_inventory` *(next)* | `lsblk`, `df`, `smartctl`, `/boot/config/disk.cfg` | `reports/inventory/disk_inventory.{json,md}` |
| `share_inventory` *(next)* | `/boot/config/shares/*.cfg` | `reports/inventory/share_inventory.{json,md}` |
| `network_inventory` *(next)* | `docker network`, inspect | `reports/inventory/network_inventory.{json,md}` |

## Usage
```bash
python run.py docker_inventory --config config/config.json
# → writes reports/inventory/docker_inventory.{json,md}
```

## Contract & safety
- Read-only. Never moves or deletes anything; only writes report artifacts.
- All host access goes through `adapters/command` (the single subprocess boundary);
  inventory modules never call `subprocess` directly (enforced by a security test).
- Idempotent: re-running overwrites the report with the current state.
