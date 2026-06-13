# Backlog import — GitHub Issues / Projects

`github_issues.csv` is the executable backlog (67 stories), one row per issue.
Columns: `id, title, body, labels, milestone, effort_days, depends_on`.
The narrative source of truth is [`../../BACKLOG.md`](../../BACKLOG.md); this CSV
is the machine-importable mirror.

## Conventions
- **labels** (comma-separated inside the quoted field): `epic-0NN`, priority
  (`P0/P1/P2`), `risk-low/med/high`, plus type tags (`story`, `core`, `module`,
  `integration`, `adapter`, `security`, `qa`, `ci`, `docs`, `observability`,
  `migration`, `freeze`, `safety`).
- **milestone** = DAG wave: `Wave-0-Foundation`, `Wave-1-Core`,
  `Wave-2-Modules`, `Wave-3-Migration`, `Wave-4-Release`.
- **depends_on** = `;`-separated STORY ids; an issue is **Ready** only when all
  its deps are closed (Definition of Ready, `AGENTS.md` §8).

## Import options

**A) `gh` CLI (no extra tooling).** Create the milestones + labels first, then
loop the CSV. Example sketch (review before running; do not run unattended):

```bash
# labels and milestones are created once, by hand or a small script.
# then, per row:
gh issue create --title "<id> <title>" --body "<body>\n\nDeps: <depends_on>" \
  --label <labels> --milestone "<milestone>"
```

**B) github-csv-tools / csv2github.** Point it at `github_issues.csv`. Map
`title`→title, `body`→body, `labels`→labels, `milestone`→milestone.

**C) Jira.** Import the CSV; map `id`→Issue Key/Summary prefix, `labels`→Labels,
`milestone`→Sprint/Fix Version, `depends_on`→"blocks/blocked by" links,
`effort_days`→Story Points (1 day ≈ 1 point).

## Suggested board columns
`Backlog → Ready (deps met) → In progress → In review → Done`.
Pull only from **Ready**. Respect the freeze points: nothing in EPIC-003/004/005/006
becomes Ready until `STORY-016` (FREEZE-2) is Done.
