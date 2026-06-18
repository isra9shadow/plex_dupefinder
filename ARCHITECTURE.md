# ARCHITECTURE.md — `izumi` homelab platform

> Target architecture for the **single** repository that consolidates three:
> `scripts` (legacy automation), `plex_dupefinder` (this repo → platform spine),
> and `homelab-infra` (declarative IaC + inventory + docs). One repo that both
> **describes** (inventory, infra-as-code, docs) and **operates** (the Python
> automation platform) the Unraid Tower homelab.

---

## Contents
1. Goal & principles
2. The server (real topology, from `homelab-infra` inventory)
3. Repository layout (the single repo)
4. The two halves & how they meet
5. Core spine, safety, observability
6. Naming (rename DEFERRED)
7. Invariants & decisions
8. Simplicity decisions (what we deliberately do NOT build)

---

## 1. Goal & principles

Priorities: **Reliability → Operational simplicity → Observability →
Maintainability → Automation → Safety against accidental deletion.**

- **One source of truth.** Disks, shares, services, networks live ONCE, in
  `inventory/`. The runtime platform reads them; it never hardcodes topology.
- **GitOps-simple for one operator.** Everything in git, nothing only in the
  Unraid UI. Secrets out of git. Reproducible deploy. No scattered docs.
- **Python-first** runtime (bash only as Unraid wrappers). **Declarative** infra
  (Docker Compose in git).
- **Safety is a platform property** (DRY_RUN + quarantine + retention, inherited
  by every module; `core/fs` is the only code that moves/deletes).

---

## 2. The server (from `homelab-infra` inventory — authoritative)

- **Host:** Tower, Unraid 7.2.4 Pro · IP 192.168.6.62 · domain izumiportal.com
  (Cloudflare). CPU i5-12400F, 32 GB, RTX 4060 (transcode).
- **Storage (CORRECTED — this is the real topology):**
  - **HDD array (parity):** `disk1–disk5` (16/16/16/16/28 TB ≈ 92 TB). xfs.
    Media + backups. **There is NO disk6/disk7.**
  - **Cache pool (NVMe, btrfs, no parity):** `/mnt/cache` (2 devices ≈ 5.1 TB).
    appdata, repos, docker, downloads (fast). Unraid **mover** handles cache↔array.
- **Shares:** appdata, repos, domains, scripts, system, downloads (cache);
  media, backups (array).
- **Docker networks:** `proxy` (Traefik), `backend` (apps+mysql+redis),
  `monitoring`.
- **Stacks:** infra (traefik/mysql/redis/cloudflared), CI/CD (Jenkins),
  observability (grafana/prometheus/cadvisor), **media** (plex/radarr/sonarr/
  prowlarr/qbittorrent/overseerr/tautulli/kometa/unmanic/huntarr/cleanuparr/
  filebot/flaresolverr), photos (immich+postgres), web projects (weboda + PHP
  template: sonae/dyalf/nexum).

> ⚠️ The legacy `scripts` disk-balancing assumed `disk5/disk6/disk7` SSDs — that
> topology does not exist. Storage modules are redesigned around array + cache pool.

---

## 3. Repository layout (the single repo)

The Python automation app stays at the repo root (unchanged from Sprint 0);
`homelab-infra` is absorbed as sibling top-level directories.

```
izumi/                       (= plex_dupefinder renamed)
│  ── governance (root) ──
├── README.md AGENTS.md CLAUDE.md AI_CONTEXT.md
├── ARCHITECTURE.md ROADMAP.md MIGRATION_PLAN.md BACKLOG.md SECURITY.md SCHEDULE.md
│
│  ── the platform (Python automation runtime) ──
├── run.py  pipelines.yaml  pyproject.toml
├── core/         config logging locks safety fs notify report secrets errors
│                 types docker   (Protocols+events+Context folded into types; +CONTRACT.md)
├── modules/      plex_dupefinder media_integrity arr_orphans arr_db downloads_watchdog
│                 disk_monitor housekeeping(=decompress+cleanup+blacklist)  ·  perms (deferred)
├── integrations/ radarr sonarr qbittorrent tmdb   (Plex via plexapi inside dupefinder module)
├── adapters/bash/ newperms.sh filebot.sh
├── config/       config.json(git-ignored) · disk_map.json(derived from inventory)
├── tests/        unit integration security smoke regression
├── tools/  deploy/ (run.sh + compose helpers)
│
│  ── infrastructure-as-code (from homelab-infra) ──
├── infra/        infra/ jenkins/ monitoring/ media/   (docker-compose per stack)
├── templates/    php/ + bootstrap-project.sh          (project scaffolding)
├── inventory/    hardware system servicios docker redis-db-allocations + disk_map source
└── docs/         architecture network ci-cd services/ runbooks/ adr/
```

**Dependency direction (runtime):** modules → integrations/core → adapters/OS.
**Config direction:** `inventory/` → `config/disk_map.json` → modules. Nothing
hardcodes topology.

---

## 4. The two halves & how they meet

| Half | What | Cadence | CI |
|---|---|---|---|
| **Platform** (`core/ modules/ integrations/ run.py`) | Python automation that *acts* on media/disks/ARR | fast-changing | ruff+mypy+pytest+coverage on `**/*.py` |
| **Infra** (`infra/ inventory/ templates/ docs/`) | Declarative compose + inventory + docs that *describe/deploy* the server | slow-changing | compose-validate + markdown lint on those paths |

**Path-filtered CI** keeps them decoupled: a doc/compose edit never triggers the
Python suite and vice-versa. They meet at exactly one seam: **`inventory/` →
`config/disk_map.json`**, which the platform's storage modules consume.

---

## 5. Core spine, safety, observability

Lean core: `core/{config,logging,locks,safety,fs,notify,report,secrets,docker}`
(Protocols + events + `Context` all live in `core/types.py`, frozen in
`core/CONTRACT.md`). `health` is a `run.py health` subcommand; metrics are counters
inside the JSON report; pipelines are a section of `config.json` — **no separate
metrics/health/pipelines/context modules**.
Safety gauntlet: DRY_RUN/AUDIT → MIN_FILE_AGE → STABILITY_CHECK → MAX_SIZE_RATIO →
move-to-quarantine (never delete) → audited retention purge. Observability: one
structured JSON log, one `reports/<run_id>.json`, Prometheus textfile metrics,
`run.py health`, single Telegram notifier. Details in `docs/INVARIANTS.md` and ADRs.

**Corrected runtime paths:** `/mnt/cache/appdata/izumi/{logs,reports,metrics}`,
repos under `/mnt/cache/repos`, quarantine on the array beside media
(`/mnt/user/Temp/izumi_quarantine`).

---

## 6. Naming (rename DEFERRED — decision)

The repo will eventually outgrow the name (candidate: **`izumi`**, the existing
brand — izumiportal.com). **But we do NOT rename now.** `plex_dupefinder` is already
deployed, tested, in CI, documented and in use. Rule: never rename + refactor +
migrate at the same time. Sequence:
`plex_dupefinder → modular platform → homelab ops → rename LAST`.
The dedupe logic becomes `modules/media/plex_dupefinder.py`; the repo keeps its
current name until the platform is stable. See MIGRATION_PLAN §8.4 and ADR-0007.

---

## 7. Invariants & decisions

Hard rules in `docs/INVARIANTS.md` (chief among them: nothing is deleted directly —
everything destructive goes through `core/fs` into quarantine). New consolidation
decisions recorded as ADRs: `0005-single-homelab-repo`, `0006-inventory-as-source-of-truth`,
`0007-defer-rename`, `0008-simplicity-cuts`, `0010-inventory-from-day-one`.

---

## 8. Simplicity decisions (what we deliberately do NOT build)

Solo operator, reliability + speed of evolution over sophistication. Cuts vs the
earlier design (ADR-0008):

| Cut / merged | Instead | Why |
|---|---|---|
| `core/metrics.py` + Prometheus textfile | Counters live in the JSON report | No Grafana yet; the report has the numbers. Re-add only if dashboards arrive. |
| `core/health.py` module | `run.py health` subcommand | A healthcheck is ~30 lines, not a module + contract. |
| `core/pipelines.py` + `pipelines.yaml` | `"pipelines"` section in `config.json` | One config file, no second format/loader. |
| `core/context.py`, `contracts.py`, `events.py` | Folded into `core/types.py` | One types module; fewer frozen files to coordinate. |
| `jsonschema` validation of config/report | Typed dataclasses (fail clearly) | Drops a dependency; types are the schema. |
| `integrations/prowlarr`, `integrations/tvmaze` | — (YAGNI) | No module uses Prowlarr; TMDb covers TV. Add when a module needs them. |
| **`modules/disk_balance` + `share_rebalance`** | `modules/disk_monitor` (read-only fullness alert) | Real topology = array + `/mnt/cache` pool; **Unraid's mover handles cache↔array**. Custom rsync balancing across non-existent disk6/7 was risky and unnecessary. **Removes the 2 riskiest 🔴 modules.** |
| `arr_db` custom SQLite `.recover`/swap surgery | Detect corruption → restore from *arr's own scheduled backup zip → else stop+alert | Uses *arr-native backups; far simpler and safer than rebuilding a DB by hand. |
| `modules/{decompress,cleanup,series_blacklist}` (3 modules) | One `modules/housekeeping.py` with three functions (ADR-0009) | Same code, one file, one lock, one report — easier to schedule and reason about. |
| `modules/perms` (always-on) | **Deferred** — only built if drift is observed | Containers already run PUID 99/PGID 100 consistently; no drift to fix yet. |
| `core/docker.py` as a "core" piece | Candidate to become an `adapters/` function | Only 1–2 modules need stop/start; not a cross-cutting concern. |

Net effect: core 16→~11 files, integrations 7→4, **modules lose the two riskiest
storage movers AND collapse 3 housekeeping modules into 1**, perms is deferred, and
the DB path stops doing hand surgery. Less code, fewer 🔴, same capability.
