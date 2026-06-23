# BACKLOG.md — `izumi` (execution-ready, simplified)

> One repo · one config · one logging · one reporting · one deploy · one
> observability. Single operator → **simplicity over sophistication** (ADR-0008/0009).
> Tasks are file-isolated for conflict-free parallel merges. Read first:
> `AI_CONTEXT.md` → `docs/INVARIANTS.md` → `AGENTS.md` → this file → `core/CONTRACT.md`.

**Effort:** XS (≤1h) · S (2–3h) · M (~half day) · L (~1 day).
**Riesgo:** 🟢 bajo · 🟡 medio · 🔴 destructivo/datos. **Par:** SI = paralelizable tras deps.
**DoD global:** `AGENTS.md` §8. **Branching:** `feature/<ID>-slug` off `develop`.

---

## Task catalogue (by epic)

### EPIC-A · Consolidation (Phase A — gates code)
| ID | Objetivo | Riesgo | Esf | Deps | Criterios de aceptación | Archivos | Par |
|---|---|---|---|---|---|---|---|
| A-01 | Importar `homelab-infra` → `infra/ inventory/ templates/ docs/` | 🟡 | S | — | árbol presente, historia etiquetada | `infra/ inventory/ templates/ docs/` | SI |
| A-02 | `config/disk_map.json` desde inventario (array disk1–5 + `/mnt/cache`; **sin disk6/7**) | 🔴 | S | A-01 | coincide con `inventory/hardware.md`; 0 disk6/7 | `config/disk_map.json` | SI |
| A-03 | Corregir rutas (`/mnt/cache/appdata/izumi`, cuarentena en array) | 🟡 | XS | A-01 | 0 `/mnt/user/appdata` en defaults | `config/config_sample.json` | NO* |
| A-04 | ADRs 0005–0009 (single repo, inventory=SoT, rename, simplicity, housekeeping) | 🟢 | XS | — | 5 ADRs | `docs/adr/` | SI |
| A-05 | Rename repo → `izumi` (remoto, run.sh, User Script) | 🟡 | XS | A-03,A-04 | redirige; automatización intacta | `deploy/`, `README.md` | NO |

### EPIC-B · Core Platform (base común) — **B-00 BLOQUEA todo el código**
| ID | Objetivo | Riesgo | Esf | Deps | Criterios de aceptación | Archivos | Par |
|---|---|---|---|---|---|---|---|
| B-00 | **FREEZE-2**: `core/types.py`(modelos+Protocols+events+Context) + `CONTRACT.md` + stubs | 🟡 | M | — | imports resuelven; `mypy core`+`ruff` verdes | `core/types.py`, `core/CONTRACT.md`, stubs | **NO·BLOQUEA todo** |
| B-01 | `core/config.py` en capas + dataclass (sin jsonschema; pipelines en config) | 🟡 | M | B-00,A-03 | precedencia ENV>json>defaults; clave mala→`ConfigError` | `core/config.py`+test | SI |
| B-02 | `core/logging.py` JSON+rotación+run_id | 🟢 | S | B-00 | salida JSON con campos+run_id | `core/logging.py`+test | SI |
| B-03 | `core/locks.py` flock single-instance | 🟢 | S | B-00 | 2ª adquisición→`LockError` | `core/locks.py`+test | SI |
| B-04 | `core/safety.py` modo+guards+retención | 🔴 | M | B-00 | default DRY_RUN; guards en su frontera | `core/safety.py`+test | SI |
| B-05 | `core/fs.py` quarantine+sidecar+restore+purge | 🔴 | M | B-00 | mueve (no borra); restore round-trip; purge auditada | `core/fs.py`+test | SI |
| B-06 | `core/notify.py` Telegram único + niveles | 🟢 | S | B-00 | token vía secrets; respeta nivel; no relanza en fallo | `core/notify.py`+test | SI |
| B-07 | `core/report.py` (dataclass→JSON, **incluye métricas**) | 🟢 | S | B-00 | report por run; tipado | `core/report.py`+test | SI |
| B-08 | `core/docker.py` stop/start seguro | 🟡 | S | B-00 | args-list (no shell=True); idempotente | `core/docker.py`+test | SI |
| B-09 | `run.py` dispatcher + `build_context` + `health` + pipelines de config | 🟡 | M | B-01..B-07 | despacha; desconocido→exit≠0; `health` exit codes | `run.py`+test | NO |

### EPIC-C · Integraciones
| ID | Objetivo | Riesgo | Esf | Deps | Criterios de aceptación | Archivos | Par |
|---|---|---|---|---|---|---|---|
| C-01 | `integrations/radarr.py` | 🟢 | S | B-00 | mock HTTP; retry/timeout; key vía secrets | `integrations/radarr.py`+test | SI |
| C-02 | `integrations/sonarr.py` | 🟢 | S | B-00 | ídem | `integrations/sonarr.py`+test | SI |
| C-03 | `integrations/qbittorrent.py` | 🟢 | S | B-00 | login; torrents/tag; creds vía secrets | `integrations/qbittorrent.py`+test | SI |
| C-04 | `integrations/tmdb.py` (TV+movies; cache) | 🟢 | S | B-00 | search+runtime; mock HTTP | `integrations/tmdb.py`+test | SI |

### EPIC-D · Módulos read-only
| ID | Objetivo | Riesgo | Esf | Deps | Criterios de aceptación | Archivos | Par |
|---|---|---|---|---|---|---|---|
| D-01 | `modules/media_integrity.py` (ffprobe+TMDb→veredicto, report) | 🔴 | M | B-04,C-04 | OK/DUDABLE/MALO; report-only | `modules/media_integrity.py`,`adapters/ffprobe.py`+test | SI |
| D-02 | `modules/arr_orphans.py` (no mapeadas, report) | 🟡 | S | C-01,C-02 | lista huérfanos; sin mover | `modules/arr_orphans.py`+test | SI |
| D-03 | `modules/disk_monitor.py` (alerta llenado, **read-only**; reemplaza balanceo) | 🟢 | S | A-02,B-06 | alerta por umbral; no mueve nada | `modules/disk_monitor.py`+test | SI |

### EPIC-E · Housekeeping (acción vía cuarentena) — *fusión de 3 módulos (ADR-0009)*
| ID | Objetivo | Riesgo | Esf | Deps | Criterios de aceptación | Archivos | Par |
|---|---|---|---|---|---|---|---|
| E-01 | `modules/housekeeping.py` = decompress + cleanup + series_blacklist (todo vía core/fs; **sin 777, sin rm**) | 🔴 | M | B-05 | rars OK extrae/fallo→cuarentena; basura/symlinks vía fs; blacklist→cuarentena | `modules/housekeeping.py`+test | SI |

### EPIC-F · ARR ops
| ID | Objetivo | Riesgo | Esf | Deps | Criterios de aceptación | Archivos | Par |
|---|---|---|---|---|---|---|---|
| F-01 | `modules/arr_db.py` (**simplificado**: corrupción→restaura backup zip de *arr→si no, para+alerta) | 🔴 | M | B-08,C-01,C-02 | restaura desde backup nativo; nunca rebuild manual | `modules/arr_db.py`+test | SI |
| F-02 | `modules/perms.py` (**APLAZADO** — solo si se observa drift; contenedores ya 99:100) | 🔴 | S | B-08 | drift detect; sin 777 | `modules/perms.py`+test | SI (diferido) |
| F-03 | `modules/downloads_watchdog.py` (stalled/48h→blocklist/tag) | 🟡 | M | C-03,C-01,C-02 | report-only; borrado de cola opt-in | `modules/downloads_watchdog.py`+test | SI |

### EPIC-G · Plex
| ID | Objetivo | Riesgo | Esf | Deps | Criterios de aceptación | Archivos | Par |
|---|---|---|---|---|---|---|---|
| G-01 | Migrar `modules/plex_dupefinder.py` sobre core | 🟡 | M | B-01,B-02,B-05 | suite actual pasa; acciones vía core/fs | `modules/plex_dupefinder.py` | SI |
| G-02 | `modules/media/organizer.py` (cleanup→cuarentena + Gemini identify + relocate opt-in) — **ENTREGADO** (ADR-0012) | 🔴 | M | B-05,C-04? | cleanup vía core/fs; plan confident/needs_review; relocate report-only | `modules/media/organizer.py`, `core/fs.py` | SI |

#### Follow-ups del organizer (G-02)
| ID | Objetivo | Riesgo | Esf | Deps | Criterios de aceptación | Archivos | Par |
|---|---|---|---|---|---|---|---|
| G-03 | Verificación de títulos de Gemini contra TMDb/TVmaze (reduce falsos del modelo antes de `confident`) | 🟡 | M | G-02,C-04 | toda sugerencia `confident` cruzada con proveedor; desajuste → `needs_review` | `modules/media/organizer.py`, `integrations/tmdb.py` | SI |
| G-04 | Ventana de paridad del organizer → activar `integrations.gemini.apply` tras ≥2 sem de planes correctos | 🔴 | S | G-02 | diff de planes estable 2 sem; flip de flag documentado | `config/`, `docs/` | NO (+2 sem) |
| G-05 | Tests del organizer (cleanup→cuarentena, plan split por umbral, `relocate` no-overwrite→`SafetyError`) | 🟡 | S | G-02,I-01 | fakes de Gemini/FS; sin red; cubre relocate collision | `tests/unit/test_organizer.py`, `tests/unit/test_fs_relocate.py` | SI |
| G-06 | `core/fs.relocate` — endurecer (containment del destino bajo movies/series roots, colisión por uuid) | 🔴 | S | G-02 | destino contenido en root configurado; colisión mismo-ms resuelta | `core/fs.py`+test | SI |
| G-07 | Manejo de cuota/rate-limit free-tier de Gemini (backoff + reanudar plan; items sin respuesta → `needs_review`) | 🟡 | S | G-02 | 429/timeout no aborta el run; se reanuda por batch | `integrations/gemini.py`, `modules/media/organizer.py` | SI |

### EPIC-H · Observabilidad & despliegue
| ID | Objetivo | Riesgo | Esf | Deps | Criterios de aceptación | Archivos | Par |
|---|---|---|---|---|---|---|---|
| H-01 | `deploy/run.sh` auto-update + **único** User Script | 🟡 | S | B-09 | git-ff+re-exec; un entrypoint | `deploy/` | SI |
| H-02 | CI con filtro por ruta + gate cobertura core>90% | 🟢 | S | — | doc/compose no dispara suite Python | `.github/workflows/` | SI |

### EPIC-I · Testing
| ID | Objetivo | Riesgo | Esf | Deps | Criterios de aceptación | Archivos | Par |
|---|---|---|---|---|---|---|---|
| I-01 | `tests/fakes.py` + fixtures (FS temporal, fakes Plex/ARR/qBit) | 🟢 | M | B-00 | fakes importan/usables | `tests/fakes.py`, `tests/unit/conftest.py` | SI |
| I-02 | Security tests (no-rm AST, no-secrets) | 🟡 | S | B-05 | falla ante borrado directo inyectado | `tests/security/` | SI |
| I-03 | Smoke/dry-run (`run.py <mod> --dry-run`) | 🟢 | S | B-09 | snapshot FS idéntico; exit 0 | `tests/smoke/` | SI |
| I-04 | `tools/shadow_diff.py` (legacy vs nuevo) | 🟡 | S | I-01 | diff de acciones intencionadas | `tools/shadow_diff.py` | SI |

### EPIC-J · Documentación
| ID | Objetivo | Riesgo | Esf | Deps | Criterios de aceptación | Archivos | Par |
|---|---|---|---|---|---|---|---|
| J-01 | Consolidar docs (fusionar `homelab-infra/docs`; un set, sin duplicados) | 🟢 | M | A-01 | una sola fuente | `docs/`, root | SI |
| J-02 | OPERATIONS/RUNBOOK/TROUBLESHOOTING | 🟢 | M | módulos | procedimientos por incidente | `docs/` | SI |

### EPIC-K · Migración & cutover
| ID | Objetivo | Riesgo | Esf | Deps | Criterios de aceptación | Archivos | Par |
|---|---|---|---|---|---|---|---|
| K-01 | `SCHEDULE.md`: inventario User Scripts Unraid | 🟡 | S | — | qué corre y cuándo | `SCHEDULE.md` | SI |
| K-02 | Shadow runner + captura de paridad | 🟡 | M | I-04, módulos | legacy ∥ nuevo report-only + diff | `tools/`,`deploy/` | NO |
| K-03 | Cutover módulo a módulo (≥2 sem paridad) | 🔴 | L | K-02 | flip flag tras paridad; legacy off | — | NO (+2 sem) |
| K-04 | Decomiso legacy (219MB+scripts) + completar compose `infra/*` | 🟢 | S | K-03 | legacy retirado; stacks en git | `infra/` | NO |

---

## Plan de Sprints

> **Sprint 1 construye SOLO la infraestructura común. Cero módulos de negocio, cero integraciones.**

### Sprint 0.5 — Phase A (Consolidation) · ~1–2 días
`A-01 A-02 A-03 A-04 A-05` · `H-02` (CI) · `K-01` (SCHEDULE) · `J-01` (docs). *Gating: A-02/A-03 antes de Sprint 1.*

### Sprint 1 — Plataforma base (común) · ~2 días
**Único objetivo: los 8 ficheros de core + el armazón.**
`B-00` (FREEZE) → `B-01 config · B-02 logging · B-03 locks · B-04 safety · B-05 fs · B-06 notify · B-07 report · B-08 docker` → `B-09 run.py` · soporte `I-01 fakes · I-02 security · I-03 smoke`. (`secrets` ya hecho en Sprint 0.)
**Salida:** `python run.py health` y `run.py <noop> --dry-run` verdes; cobertura core>90%. **Sin integrations, sin modules.**

### Sprint 2 — Integraciones + módulos read-only · ~2 días
`C-01..C-04` (clientes) · `D-01 media_integrity · D-02 arr_orphans · D-03 disk_monitor` (todos report-only) · `G-01 plex_dupefinder` (migración) · `I-04 shadow_diff`.

### Sprint 3 — Acción + ARR ops + cutover · ~1 semana + paridad
`E-01 housekeeping · F-01 arr_db · F-03 downloads_watchdog` (`F-02 perms` solo si hay drift) · `H-01 deploy` · `J-02 runbooks` · `K-02 shadow → K-03 cutover (≥2 sem) → K-04 decommission`.

---

## Distribución por agentes (8 agentes Opus en paralelo)

| Agente | Objetivo | Tareas | Dependencias | Conflictos potenciales (y mitigación) |
|---|---|---|---|---|
| **Arquitectura** | Congelar contratos + corregir topología | A-01..A-05, **B-00**, A-04 ADRs, `ARCHITECTURE.md` | va PRIMERO | Posee `core/types.py`/`CONTRACT.md`/`disk_map.json` (congelados) → **nadie más los edita**; si otro necesita un tipo, lo pide aquí |
| **Core Platform** | Los 8 ficheros core + run.py | B-01..B-06, B-08, B-09 | B-00 | `run.py`↔`report.py` se tocan solo por contrato (sin solape de fichero); `config.py` defaults vs `config_sample.json` (A-03) → coordinar claves en B-00 |
| **ARR** | Clientes *arr + módulos ARR | C-01,C-02,C-03; D-02; F-01,F-03 | B-00; clientes antes que módulos | Usa `tests/fakes.py` (lo posee Testing) en modo lectura → pedir altas de fakes a Testing, no editarlo |
| **Plex** | Dedupe + integridad de medios | C-04; D-01; G-01 | B-00; B-05 para G-01 | Mueve el `plex_dupefinder.py` raíz → a `modules/`; nadie más toca ese fichero |
| **Storage** | Housekeeping + monitor de disco | D-03; E-01 (housekeeping); F-02 (diferido) | B-05; A-02 (disk_map) | Solo consume `core/fs` y `disk_map` (lectura) → sin solape |
| **Observabilidad** | Reporting + despliegue + CI | B-07; H-01,H-02; K-01 | B-00; B-09 para H-01 | Posee `core/report.py` y `.github/` y `deploy/` exclusivamente; `run.py` lo posee Core (se encuentran por contrato) |
| **Testing** | Infra de tests compartida | I-01..I-04 | B-00 (fakes); B-05 (security) | **Posee `tests/{fakes,conftest,security,smoke}` y `tools/shadow_diff`**; cada otro agente posee su `tests/unit/test_<suyo>.py`. Regla: 1 fichero de test por dueño |
| **Documentación** | Docs narrativas + runbooks | J-01, J-02, root govdocs (no ADR/ARCHITECTURE) | A-01 | `docs/adr` y `ARCHITECTURE.md` los posee Arquitectura; Documentación posee el resto de `docs/` + README |

### Qué lanzar en paralelo sin conflictos
- **t0 (antes del FREEZE):** Arquitectura (A-01,A-02,B-00) + Documentación (J-01) + Observabilidad (H-02,K-01). Ficheros disjuntos.
- **Tras mergear B-00 (~M):** **los 8 agentes a la vez** — propiedad de ficheros disjunta. Únicos congelados: `core/types.py`, `CONTRACT.md`, `disk_map.json` (solo Arquitectura).
- **Los 3 únicos puntos de roce a vigilar:** (1) ficheros congelados de Arquitectura; (2) `tests/fakes.py` (lo posee Testing, los demás piden altas); (3) reparto de `docs/` (Arquitectura=ADR+ARCHITECTURE, Documentación=resto).

**Camino crítico:** `A-02 → B-00 → B-05(fs) → B-09(run) → Sprint 2/3 → K-02 → [2 sem paridad] → K-03`. Desarrollo neto **~4–5 días**; el suelo es la paridad (K-03), no el código.
