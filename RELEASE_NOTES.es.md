# Notas de versión

[English](RELEASE_NOTES.md) | **Español**

## v2.2.0-rc1 — Release de pruebas (2026)

Candidata a versión para validar en una biblioteca real. Agrupa el trabajo de
auditoría, testabilidad, tooling y scoring. El bloque de **estabilidad / config /
logging / reporting** está listo para validar en producción; la **reescritura de
scoring** se incluye pero debe validarse con datos de auditoría reales antes de
ejecuciones no-dry.

### Auditoría y seguridad operativa
- `AUDIT_MODE` tiene dos submodos vía `CONFIRM_BEFORE_ACTION`: `false` = totalmente
  desatendido (cron), `true` = selección manual asistida. Los avisos por grupo solo
  ocurren en una ejecución que actúa; las auditorías nunca se bloquean esperando
  entrada.
- El banner de inicio imprime `INTERACTIVE_MODE / AUDIT_MODE / CONFIRM_BEFORE_ACTION`.
- La cuarentena de un elemento multiparte elimina la entrada de Plex solo si se
  movieron **todas** las partes (sin huérfanos en disco); los fallos parciales
  preservan la entrada y la registran.

### Testabilidad y tests
- El módulo ahora es import-safe: la conexión a Plex, la validación de config y la
  creación del fichero de log se difieren al entrypoint; `config.py` cae a defaults
  si se ejecuta de forma no interactiva.
- `tests/` (pytest, librerías de terceros stubeadas) que cubren `get_score`,
  `select_keeper`, `check_file_exists`, `_quarantine_logical_path`,
  `detect_inconsistencies`, ranking de source, alias de códec, audio MAX y el orden
  de preferencia completo. Añadido `requirements-dev.txt`.

### Tooling (solo lectura, sin impacto en producción)
- `tools/analyze_report.py` — simula el scoring propuesto sobre un plan/report real
  y reporta cambios de keeper, anomalías y decisiones sospechosas.
- `tools/compare_plans.py` — compara las decisiones de keeper entre dos planes.

### Reescritura de scoring (validar antes de producción no-dry)
- El **source** del release es una dimensión de primera clase de valor único
  (`SOURCE_SCORES`), parseada del nombre; gana el source de mayor calidad, nunca se
  suma, acotado por debajo de la diferencia de resolución.
- `BITRATE_SCORE_WEIGHT` (0.1) — el bitrate se reduce a desempate para que un AVC
  inflado ya no gane a un HEVC eficiente.
- `FILENAME_SCORES` reducido a desempates de contenedor/edición; suma positiva
  acotada por `FILENAME_SCORE_CAP`.
- Canales de audio puntuados por la pista más rica (MAX), no la suma.
- Alias de códec: `hevc=h265=x265`, `h264=x264=avc`.
- Orden objetivo: 2160p DV/HDR HEVC > 2160p HEVC > 1080p REMUX > 1080p HEVC >
  1080p AVC > 720p AVC.

> **Nota de actualización:** en el primer arranque tras el pull, `upgrade_settings()`
> añade las claves nuevas a tu `config.json` y sale para que revises. Tus
> `FILENAME_SCORES` existentes se conservan, así que los patrones de source en el
> nombre pueden coexistir con el nuevo `SOURCE_SCORES` (doble conteo) hasta que los
> recortes — valida el scoring con un audit + `tools/analyze_report.py` antes de ir
> a no-dry.

---

## v2.1.0 — Endurecimiento operativo (2026)

Versión incremental. Sin cambios de arquitectura, sin cambios que rompan compatibilidad.

### Correcciones

- **Mensaje de borrado directo corregido.** El banner en tiempo de ejecución antes afirmaba
  "los archivos permanecen en disco, sin seguimiento" en modo de borrado directo. Esto era
  incorrecto: con **Allow media deletion** activado en Plex (requerido), la DELETE de medios de
  Plex elimina el archivo del disco. El banner, `README.md` y `SAFETY_MODEL.md` ahora indican
  claramente que el borrado directo es irreversible y elimina el archivo (excepto en modo
  `FIND_DUPLICATE_FILEPATHS_ONLY`, que es solo de metadatos porque todas las entradas comparten un
  único archivo físico).
- **Bug del asistente `build_config()`.** Responder correctamente al aviso "Auto Delete duplicates?"
  a la primera dejaba `AUTO_DELETE` en `false` independientemente de la respuesta (la asignación
  vivía dentro del bucle de entrada inválida). Corregido.
- **`PLEX_DELETE_DELAY_SECONDS` añadido a `base_config`.** Estaba documentado y se usaba pero
  faltaba en los valores por defecto, de modo que `upgrade_settings()` nunca lo añadía a las
  configuraciones existentes. Ahora es un valor por defecto de primera clase (`2.0`).

### Funciones operativas

- **Registros rotativos + `LOG_LEVEL`.** `activity.log` ahora usa un `RotatingFileHandler`
  limitado a 10 MiB × 5 copias de respaldo, de modo que las ejecuciones programadas desatendidas
  en bibliotecas grandes no puedan llenar el disco. Nueva clave de configuración `LOG_LEVEL`
  (por defecto `INFO`; usa `DEBUG` para el rastreo por parte de medio).
- **Validación de nombres de biblioteca.** Al arrancar, cada nombre en `PLEX_LIBRARIES` se
  comprueba contra las bibliotecas que existen en el servidor. Un error tipográfico ahora aborta
  (código de salida 2) con la lista de bibliotecas disponibles, en lugar de no hacer nada en
  silencio con esa biblioteca.
- **Resumen de cuarentena.** Cada ejecución reporta el contenido actual de la cuarentena —número
  de archivos, tamaño total, antigüedad del archivo más viejo y cuántos superan
  `QUARANTINE_RETENTION_DAYS`— en stdout y en el reporte JSON (clave `quarantine`). Solo lectura;
  nada se purga nunca de forma automática.

---

## v2.0.0 — Reescritura con la seguridad como prioridad (2026)

Reescritura completa centrada en la seguridad operativa para homelabs de Plex en producción.

### Corrección de bug crítico

**Bug de borrado por metadatos obsoletos**

El script original podía borrar el archivo equivocado cuando la caché de metadatos de Plex estaba
obsoleta: una entrada fantasma (archivo ya eliminado del disco) podía superar en puntuación al
archivo real, causando que el archivo real se borrara.

Corrección: `check_file_exists()` ahora requiere que tanto las banderas `exists`/`accessible` de
Plex COMO `os.path.exists()` coincidan. Cualquier desacuerdo trata el archivo como ausente y
omite todo el grupo.

---

### Arquitectura: validación de dos pasadas

**PASADA 1 — Descubrimiento (solo lectura)**

- Obtiene los grupos de duplicados, puntúa cada candidato, selecciona un guardado tentativo.
- Aplica todos los filtros de seguridad (existencia, antigüedad, validez, umbrales).
- Escribe un fichero de plan JSON — sin efectos secundarios en esta pasada.

**PASADA 2 — Revalidación y acción**

- Vuelve a obtener cada grupo de Plex.
- Compara el estado actual con la instantánea de la PASADA 1 (`detect_inconsistencies`).
- Ejecuta una comprobación de estabilidad justo antes de actuar.
- Solo actúa si el estado es totalmente coherente con la PASADA 1.

**PASADA 0 — Preanálisis opcional**

- Dispara `analyze()` de Plex en cada elemento duplicado antes de puntuar.
- Enfoque de comparación de instantáneas: captura los metadatos antes y después, compara para
  detectar si `analyze()` produjo datos frescos.
- Estados: `sane_and_changed` (demostrablemente fresco), `sane_unchanged` (ambiguo
  — limitación documentada de la API de Plex, anotada en `run_report`).

---

### Sistema de cuarentena

Los archivos nunca se borran de forma definitiva por defecto — se mueven a `QUARANTINE_DIR`.

- Estructura de ruta lógica preservada: `QUARANTINE/Show Name/Season/ep.mkv`.
- Se escribe un fichero adjunto `.dupefinder_meta.json` junto a cada archivo en cuarentena:
  - `original_path`, `quarantine_path`, `restore_command` (copia y ejecuta en una
    shell para restaurar el archivo — no se necesita ningún script).
  - `keeper.files`, `keeper.score`, `keeper.score_breakdown`.
  - `original_size`, `original_mtime` (campos de verificación de integridad).
  - `library`, `title`, `year` (contexto opcional de Plex, cuando está disponible).
- Gestión de colisiones: sufijo `__LIBRARY` para colisiones de mismo título entre bibliotecas;
  fallback `__<unix_timestamp>` para ejecuciones repetidas sobre la misma cuarentena.
- `DRY_RUN=true` y `QUARANTINE_MODE=true` son los valores por defecto — seguro
  desde el primer momento en una instalación nueva.

---

### Capas de seguridad añadidas

| Capa | Clave de configuración | Protege contra |
|---|---|---|
| Validación del sistema de archivos | siempre activa | Puntuaciones de archivos fantasma (metadatos obsoletos de Plex) |
| Enfriamiento por antigüedad | `MIN_FILE_AGE_HOURS` | Importaciones activas y archivos en plena transcodificación |
| Validez de metadatos | siempre activa | Metadatos con duración cero o códec de relleno |
| Umbral de puntuación | `MIN_SCORE_DIFFERENCE` | Borrados por elección equivocada en cuasi-empates |
| Protección por ratio de tamaño | `MAX_SIZE_RATIO` | Un archivo grande perdiendo frente a un hermano más pequeño |
| Revalidación PASADA 2 | siempre activa | Cambio de estado entre el descubrimiento y la acción |
| Comprobación de estabilidad | `STABILITY_CHECK_SECONDS` | Escrituras activas en el momento de la acción |
| Modo auditoría | `AUDIT_MODE` | Fuerza `DRY_RUN=true` en memoria sin persistir en disco |

---

### Mejoras de puntuación

**Jerarquía de códecs moderna**

El original puntuaba H264 como el más alto (10000) y penalizaba HEVC (5000). Esta versión
invierte esa prioridad para premiar los formatos eficientes en almacenamiento:

| Códec | Puntuación |
|---|---|
| AV1 | 14000 |
| HEVC / H265 | 12000 |
| H264 | 8000 |
| VP9 | 6000 |
| MPEG-4 | -3000 |
| VC-1 | -2000 |
| MPEG-1 / MPEG-2 | -5000 |
| Variantes WMV / MS-MPEG-4 | -8000 |

**Diccionario de desglose de puntuación**

`get_score()` ahora devuelve `(int, dict)`. Cada decisión de guardado es totalmente auditable
— el desglose se almacena en el fichero de plan, el reporte JSON y el fichero adjunto de
cuarentena.

**Valor por defecto `SCORE_FILESIZE=false`**

El tamaño de archivo no indica calidad de forma inherente. Las codificaciones HEVC eficientes no
deben perder frente a rips H264 inflados. El tamaño de archivo está disponible solo como desempate
opcional.

**Bitrate ponderado al 0.5x**

El bitrate bruto no es un indicador de calidad. El peso se reduce a la mitad para evitar que
archivos grandes pero ineficientes dominen las señales de códec y resolución.

**Bonificaciones de HDR, Dolby Vision, subtítulos y pistas de audio**

| Característica | Clave de configuración | Puntuación por defecto |
|---|---|---|
| HDR (smpte2084 / arib-std-b67) | `HDR_SCORE` | 3000 |
| Dolby Vision (`DOVIPresent`) | `DOLBY_VISION_SCORE` | 5000 |
| Pistas de subtítulos | `SUBTITLE_SCORE_PER_TRACK` | 50 por pista |
| Pistas de audio | `AUDIO_TRACK_SCORE` | 100 por pista |

---

### Funciones operativas

**Reportes JSON de ejecución**

Escritos en `JSON_REPORT_DIR` por ejecución. Incluyen todas las decisiones de grupo, puntuaciones,
registros por grupo, elementos eliminados, resultados de integraciones y errores. Las claves
sensibles (`PLEX_TOKEN`, `RADARR_API_KEY`, `SONARR_API_KEY`) siempre se censuran.

Formato del nombre de fichero: `dupefinder_report_<run_id>_<YYYYMMDDTHHMMSSZ>.json`

**Fichero de plan JSON**

Escrito tras la PASADA 1 en `plans/dupefinder_plan_<run_id>_<ts>.json` antes de que la PASADA 2
actúe. Una ejecución abortada en la verja de confirmación deja igualmente un plan auditable completo.

**`AUDIT_MODE`**

Ejecuta la canalización completa de dos pasadas incluyendo el fichero de plan y el reporte JSON pero
fuerza `DRY_RUN=true` en memoria sin modificar `config.json`. Úsalo para validar la puntuación
y para pruebas de regresión.

**Autoactualización de configuración**

`upgrade_settings()` fusiona cualquier clave nueva de `base_config` en un `config.json` existente
en cada arranque sin sobrescribir los valores del usuario. Las claves añadidas se imprimen y se
pide al usuario que las revise antes de que la ejecución continúe.

**Identificador por ejecución**

Cada línea de registro, fichero de plan, reporte JSON y fichero adjunto de cuarentena se sella con
un `run_id` hexadecimal de 12 caracteres (`uuid4().hex[:12]`) para una correlación fiable entre
artefactos.

---

### Mejoras de integración

- **Reescaneo de Radarr**: `RADARR_RESCAN_AFTER=true` envía por POST un comando `RescanMovie` a
  `/api/v3/command` al final de cada ejecución.
- **Reescaneo de Sonarr**: `SONARR_RESCAN_AFTER=true` envía por POST un comando `RescanSeries`.
- **Refresco de la biblioteca de Plex**: `PLEX_REFRESH_AFTER=true` llama a `section.update()` en
  todas las bibliotecas escaneadas tras la limpieza.
- **Consistencia por hash parcial**: `PARTIAL_HASH_ENABLED=true` calcula un SHA-256 de
  los primeros y últimos N bytes de cada archivo en ambas pasadas; cualquier cambio de hash entre
  la PASADA 1 y la PASADA 2 provoca que el grupo se omita.

---

### Configuración

- `config_sample.json`: configuración de referencia completa que documenta las más de 35 claves
  con comentarios en línea.
- Todas las claves sensibles se censuran en cada artefacto de salida JSON.
- `PLEX_DELETE_DELAY_SECONDS` (por defecto `2.0`): espera entre llamadas consecutivas a
  `remove_item` dentro de un grupo para evitar saturar la API de Plex.

---

### Cambios que rompen compatibilidad vs el original

| Área | Original (`l3uddz/plex_dupefinder`) | Este fork |
|---|---|---|
| Canalización | Una pasada: descubrir y actuar en un bucle | Dos pasadas: PASADA 1 solo lectura, PASADA 2 revalidar y actuar |
| Eliminación de archivos | Solo API DELETE de Plex (permanente) | Cuarentena por defecto (`shutil.move`); DELETE de Plex solo tras un movimiento correcto |
| Valor por defecto de `DRY_RUN` | `false` (la primera ejecución actuaría inmediatamente) | `true` — una instalación nueva no puede destruir datos sin un cambio explícito de configuración |
| Valor por defecto de `SCORE_FILESIZE` | `true` | `false` |
| Puntuación de códecs | Sesgada a H264 | HEVC/AV1 preferidos; códecs heredados penalizados |
| `deletefiles.sh` | Activo y relevante | Obsoleto — reemplazado por el flujo de trabajo del `restore_command` del fichero adjunto de cuarentena |

---

### Calidad de código

- Todas las cláusulas `except:` desnudas reemplazadas por `except Exception:`.
- Validación de entrada por todas partes (`isdigit()` comprobado antes de la conversión con `int()`).
- La tupla `REDACTED_KEYS` garantiza que `PLEX_TOKEN`, `RADARR_API_KEY` y
  `SONARR_API_KEY` nunca se escriben en ficheros de plan ni reportes JSON.
- Registro estructurado y exhaustivo en `activity.log` y `decisions.log`
  (registro legible de conservar/eliminar por grupo). Ver v2.1.0 para la rotación de registros
  y el `LOG_LEVEL` configurable.
