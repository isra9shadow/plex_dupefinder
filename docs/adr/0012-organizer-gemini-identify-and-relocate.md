# ADR-0012 — organizer: Gemini-based identification + `core/fs.relocate` mover

- Status: Accepted
- Date: 2026-06
- Deciders: project owner (solo operator)

## Context

After the dedupe pass, an unsorted "manuales" dump (`paths.organizer_source`)
still holds media that never went through Radarr/Sonarr: hand-downloaded movies
and episodes with arbitrary names, mixed with junk sidecars (`.nfo`/`.txt`/`.url`),
zero-byte files and empty directories. Two jobs need doing safely:

1. clear the junk so the dump only contains real media, and
2. identify each media file (title / year / type / season+episode) and move it
   to a canonical location under the Movies/Series roots.

Identification is the hard part. The legacy approach was filename regex, which is
brittle on the messy names this dump contains. We considered a metadata pipeline
(filename parse → TMDb/TVmaze lookup → match-scoring) but that is a large build,
needs another API key, and still fails on names with no usable tokens.

Mutation is the dangerous part. INVARIANT I1 forbids direct deletion; today the
only sanctioned mover is `core/fs.quarantine` (one-way, into the recoverable
quarantine tree). Relocating a confident file to its canonical home is a
*different* move — destination is a real library path, not quarantine — and the
existing primitive does not model it.

## Decision

**(a) Use Google Gemini free-tier (`gemini-2.0-flash`) for identification, with
the model's self-reported confidence as the gate — no external verification
step.** The `organizer` module sends each media filename (batched,
`integrations.gemini.batch_size`, default 50) to Gemini and receives structured
`type` / `title` / `year` / `season` / `episode` / `confidence`. Suggestions at
or above `integrations.gemini.confidence_threshold` (default 90) become
actionable canonical target paths; everything else goes to `needs_review`. We
accept that confidence is model-self-reported and is **not** cross-checked
against a metadata provider — the safety net is the high default threshold plus
report-only-by-default applying (decision b), not a second source of truth. The
API key is read by name via `core/secrets` (`api_key_ref`, default
`GEMINI_API_KEY`) from `.env`/ENV; it is never hardcoded (INVARIANT I3). A
metadata-verification pass (TMDb/TVmaze) is left as a future follow-up if the
plan reports show the model misclassifying.

**(b) Add `core/fs.relocate(src, dest, *, reason)` as the second sanctioned
mover, report-only by default behind a parity window.** `relocate` moves a
confident media file to its canonical library path. It **never overwrites** an
existing destination (raises `SafetyError`) and **never deletes** — it is a move,
consistent with I1. The organizer's `APPLY` step is gated by
`integrations.gemini.apply` (default `false`): with `apply=false` the module only
writes the plan/report; with `apply=true` confident + resolvable suggestions are
relocated. Following the `modules/arr/orphans.py` precedent, `apply` stays off
until a parity window confirms the plan matches operator expectation.

The `CLEANUP` step (junk sidecars, zero-byte files, empty dirs) does act under
`DRY_RUN` from day one, because it only ever **quarantines** via the existing
`core/fs.quarantine` primitive — fully recoverable and auto-purged after the
retention window.

## Consequences

- Identification quality is bounded by Gemini, not by hand-tuned regex; it
  improves for free as the model improves, with zero metadata-provider plumbing.
- No second API dependency now (TMDb/TVmaze not required); the trade-off is no
  independent verification of the title — mitigated by the threshold + parity.
- `core/fs` gains a clearly-scoped second mover. Both movers share the I1
  guarantee (move, never delete) and the no-overwrite safety; `relocate` is the
  only one that targets real library paths, so it carries the extra
  `SafetyError`-on-collision guard.
- Default posture is safe: cleanup is reversible (quarantine), identification is
  read-only, relocation is opt-in after parity. A misconfigured run produces a
  plan, not data loss.
- Cost: a Gemini round-trip per batch and a free-tier rate/quota dependency; the
  plan is still produced for `needs_review` items even when the model abstains.
