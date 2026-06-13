# ADR-0002 — Quarantine over direct deletion; single filesystem chokepoint

- Status: Accepted
- Date: 2026-06
- Deciders: project owner (solo operator)

## Context

The platform runs unattended, 24/7, on irreplaceable media and on production
*arr SQLite databases. The legacy code was full of unguarded destruction:
`rm -rf` driven by an editable blacklist file, `chmod -R 777 /mnt/user/media`,
`rsync --remove-source-files`, `find -delete`, and `delete_files=True` on
torrents — with no dry-run defaults, no locks, and the repo not even under
version control. A single mistake (a bad blacklist line, a scoring quirk) could
destroy a library with no recovery path.

The owner's #1 and #6 priorities are reliability and safety against accidental
deletion.

## Decision

1. **No module ever deletes or moves user data directly.** The only code allowed
   to do so is `core/fs`.
2. **Destruction is quarantine, not deletion.** `core/fs` *moves* targets into
   `quarantine/` and writes a sidecar JSON containing a ready-to-run
   `restore_command`. The sole real `delete` is the retention purge inside
   `core/fs`, which is audited (count + reclaimed bytes reported) and respects
   `DRY_RUN`.
3. **A shared safety gauntlet** (DRY_RUN/AUDIT → MIN_FILE_AGE → STABILITY_CHECK →
   MAX_SIZE_RATIO → quarantine) is provided by `core/safety` and inherited by all
   modules.

This generalises the model `plex_dupefinder` already proved.

## Consequences

- Every destructive decision is reversible within the retention window → safe
  autonomous operation and a trivial rollback story.
- Auditable: every action leaves a report entry and a restore sidecar.
- A grep-based security test can mechanically prove no module deletes directly
  (`tests/security/test_no_direct_delete.py`) — enforceable in CI.
- Cost: extra disk headroom for quarantine and a small per-action overhead.
  Acceptable given the data at stake.
