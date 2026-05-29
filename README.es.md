# plex_dupefinder — Gestor seguro de duplicados para Plex

[English](README.md) | **Español**

<img src="assets/logo.svg" width="600" alt="Plex DupeFinder">

Un gestor de duplicados para Plex Media Server, diseñado con la seguridad como prioridad y con cuarentena por defecto.

---

## Visión general

Las bibliotecas de Plex acumulan entradas de medios duplicadas con el tiempo — reimportaciones tras migraciones de disco, mejoras de Radarr, transcodificaciones de Tdarr que conviven con los originales, o desajustes de metadatos que engañan a Plex para que trate el mismo archivo como dos elementos distintos. Si no se gestionan, estos duplicados desperdician almacenamiento y ensucian tu biblioteca. plex_dupefinder automatiza la limpieza.

La filosofía central es **conservadora por diseño**: un falso negativo (dejar un duplicado en su sitio) siempre es preferible a un falso positivo (destruir la única copia buena). Cada decisión pasa por un motor de puntuación que prioriza la eficiencia del códec y la calidad de la codificación por encima del tamaño bruto del archivo, una canalización de dos pasadas que revalida el estado antes de actuar, y una capa de cuarentena que mueve los archivos en lugar de borrarlos — de modo que toda eliminación es completamente reversible.

La herramienta está pensada para homelabs de Plex modernos que ejecutan Plex junto con Radarr, Sonarr, Tdarr o Unraid. Entiende bibliotecas centradas en MKV, contenido HDR y Dolby Vision, pistas de audio multiidioma y las señales de calidad de codificación incrustadas en los nombres de archivo por herramientas como Radarr y Tdarr.

**En una instalación nueva, `DRY_RUN=true` y `QUARANTINE_MODE=true` son los valores por defecto.** La primera ejecución nunca borrará ni moverá ningún archivo. Debes establecer explícitamente `DRY_RUN=false` en `config.json` para permitir cualquier acción, y el modo cuarentena garantiza que los archivos se mueven —no se destruyen— hasta que verifiques los resultados y vacíes tú mismo el directorio de cuarentena.

---

## Cómo funciona

plex_dupefinder opera en tres pasadas secuenciales:

```text
[PASADA 0]  opcional — dispara Plex analyze() + comparación de instantáneas
    │     (asegura que la puntuación use metadatos de códec/bitrate frescos)
    │
    ▼
[PASADA 1]  Descubrimiento  (solo lectura — sin efectos secundarios)
    │  ├─ obtiene los grupos de duplicados de Plex
    │  ├─ puntúa cada archivo candidato
    │  ├─ aplica filtros de seguridad (antigüedad, validez, umbrales)
    │  ├─ selecciona un guardado tentativo
    │  └─ escribe el fichero de plan JSON
    │
    ▼
[PASADA 2]  Revalidación y acción
       ├─ vuelve a obtener cada grupo de Plex
       ├─ vuelve a puntuar y compara con la instantánea de la PASADA 1
       ├─ comprobación de estabilidad (detecta escrituras activas)
       └─ cuarentena  ──o──  registro DRY_RUN
```

**PASADA 0** (desactivada por defecto, `PRE_ANALYZE_DUPLICATES=true`) llama a `analyze()` en cada elemento duplicado y sondea Plex hasta que los metadatos se asientan. Los grupos que agotan el tiempo o fallan el análisis se marcan como omitidos (stubs) y nunca pasan a puntuación. Esto evita decisiones basadas en metadatos obsoletos con bitrate cero o códec desconocido.

**PASADA 1** es totalmente de solo lectura. Obtiene los grupos de duplicados de Plex, calcula una puntuación para cada archivo candidato, ejecuta todos los filtros de seguridad, selecciona un guardado tentativo y escribe un fichero de plan JSON en `plans/`. No se toca ningún archivo ni se realiza ninguna escritura en Plex.

**PASADA 2** vuelve a obtener cada grupo de Plex de forma independiente, lo vuelve a puntuar y compara el estado fresco con la instantánea de la PASADA 1. Si algo cambió entre las dos pasadas —tamaños de archivo, códecs, banderas de existencia, o cuál es ahora el guardado según Plex— el grupo se omite. Solo los grupos que superan esta comprobación de consistencia y una comprobación final de estabilidad (tamaños de archivo estables durante una breve ventana de leer-esperar-leer) pasan a la acción.

---

## Capas de seguridad

Diez capas de seguridad independientes protegen contra la pérdida de datos. Cualquiera de ellas puede abortar un grupo de forma independiente de las demás:

- **DRY_RUN / AUDIT_MODE** — Todas las mutaciones no tienen efecto por defecto; `AUDIT_MODE` fuerza el modo simulación incluso con `DRY_RUN=false` en la configuración, sin escribir en disco.
- **Preanálisis de la PASADA 0** — Los grupos con llamadas `analyze()` que agotan el tiempo o fallan se omiten antes de puntuar.
- **Comprobación de existencia con el sistema de archivos como autoridad** — Tanto Plex como el sistema de archivos local deben coincidir en que un archivo existe; el desacuerdo se trata como AUSENTE (MISSING), evitando la eliminación de fantasmas por metadatos obsoletos.
- **Periodo de enfriamiento por antigüedad** — Los archivos más recientes que `MIN_FILE_AGE_HOURS` (por defecto 24 h) se omiten, protegiendo descargas activas y copias en pleno proceso de importación.
- **Comprobación de validez de metadatos** — Cualquier candidato con duración cero, bitrate cero o un códec desconocido provoca que se omita todo el grupo.
- **Umbral de puntuación** — Si la diferencia de puntuación entre los dos mejores candidatos está por debajo de `MIN_SCORE_DIFFERENCE`, el grupo se omite; no se actúa ante ambigüedad en la puntuación.
- **Protección por ratio de tamaño** — Si un no-guardado es más de `MAX_SIZE_RATIO` (por defecto 5×) mayor que el guardado, el grupo se omite; un hermano desproporcionadamente mayor sugiere un emparejamiento erróneo.
- **Revalidación de la PASADA 2** — Archivos, tamaños, existencia, duración, bitrate, códec y hashes parciales opcionales se comparan entre la PASADA 1 y la PASADA 2; cualquier cambio aborta el grupo.
- **Comprobación de estabilidad** — Se leen los tamaños de archivo, ocurre una breve espera y se vuelven a leer; cualquier cambio de tamaño (escritura activa) omite el grupo.
- **Cuarentena** — Las eliminaciones mueven los archivos a `QUARANTINE_DIR` con un fichero adjunto `.dupefinder_meta.json` que contiene un `restore_command` listo para ejecutar; nada se borra de forma definitiva salvo que se establezca explícitamente `QUARANTINE_MODE=false`.

Consulta [SAFETY_MODEL.es.md](SAFETY_MODEL.es.md) para una descripción completa de cada capa.

---

## Inicio rápido

### Requisitos

- Python 3.8 o posterior
- Plex Media Server con **Allow media deletion** (Permitir eliminación de medios) activado (Settings → Server → Library)
- Dependencias: `pip install -r requirements.txt`

### Primera ejecución

```bash
# 1. Copia la configuración de ejemplo
cp config_sample.json config.json

# 2. Establece los campos mínimos requeridos en config.json:
#    PLEX_SERVER, PLEX_TOKEN, PLEX_LIBRARIES

# 3. Ejecuta — DRY_RUN=true por defecto, no se borrará nada
python3 plex_dupefinder.py
```

Revisa la salida en consola e inspecciona `plans/dupefinder_plan_<run_id>_<timestamp>.json` para ver exactamente qué haría la herramienta antes de activar el modo real.

### Encontrar tu token de Plex

Consulta el artículo oficial de soporte de Plex: <https://support.plex.tv/articles/204059436>

### Activar el modo real (cuarentena)

```json
{
  "DRY_RUN": false,
  "QUARANTINE_MODE": true,
  "QUARANTINE_DIR": "/mnt/user/quarantine"
}
```

Los archivos se **mueven** a `QUARANTINE_DIR`, nunca se borran de forma definitiva. Cada archivo movido tiene un fichero adjunto `.dupefinder_meta.json` escrito a su lado que contiene la ruta original y un `restore_command` listo para la shell. Para restaurar un archivo, abre su fichero adjunto y ejecuta el campo `restore_command`.

---

## Cuarentena

Cuando `QUARANTINE_MODE=true`, el directorio de cuarentena replica la estructura original de la biblioteca anclada en el directorio del título:

```
Original  : /mnt/user/Media/TV/Breaking Bad/Season 01/Episode.mkv
Cuarentena: QUARANTINE_DIR/Breaking Bad/Season 01/Episode.mkv
```

Se escribe un fichero adjunto `.dupefinder_meta.json` junto a cada archivo en cuarentena:

```json
{
  "original_path": "/mnt/user/Media/TV/Breaking Bad/Season 01/Episode.mkv",
  "quarantine_path": "/mnt/user/quarantine/Breaking Bad/Season 01/Episode.mkv",
  "quarantine_timestamp": "2024-05-01T12:00:00+00:00",
  "run_id": "a3f9c2d1e4b8",
  "reason": "duplicate (keeper id=12345, highest score (87500) among existing files)",
  "restore_command": "mv '/mnt/user/quarantine/Breaking Bad/Season 01/Episode.mkv' '/mnt/user/Media/TV/Breaking Bad/Season 01/Episode.mkv'",
  "keeper": {
    "files": ["/mnt/user/Media/TV/Breaking Bad/Season 01/Episode.2160p.BluRay.mkv"],
    "score": 87500,
    "score_breakdown": {
      "video_codec": 12000,
      "resolution": 20000,
      "filename": 22000,
      "audio_codec": 4500,
      "bitrate": 8200,
      "audio_channels": 6000,
      "hdr": 3000
    }
  }
}
```

Para restaurar un archivo en cuarentena, copia el valor de `restore_command` y ejecútalo en una shell — no se necesita ningún script.

El script no purga automáticamente el directorio de cuarentena. Tras verificar los resultados durante tu ventana de retención (`QUARANTINE_RETENTION_DAYS` es informativo), vacía el directorio de cuarentena manualmente. Para ayudarte a decidir cuándo, cada ejecución imprime un resumen del estado actual de la cuarentena —número de archivos, tamaño total, antigüedad del archivo más viejo y cuántos archivos superan `QUARANTINE_RETENTION_DAYS`— y escribe las mismas cifras en el reporte JSON bajo la clave `quarantine`. Esto es solo informativo; nada se borra nunca de forma automática.

---

## Puntuación

El motor de puntuación está diseñado para la eficiencia del códec y la calidad de codificación, no para el tamaño bruto del archivo. Un remux HEVC compacto siempre debería puntuar más que un rip H.264 inflado del mismo contenido. `SCORE_FILESIZE` es `false` por defecto exactamente por esta razón.

`get_score()` devuelve una tupla `(total_score, breakdown_dict)`. El desglose registra cada componente que contribuyó al total — códec, resolución, patrones de nombre de archivo, bitrate, dimensiones, canales de audio, bonificaciones HDR/Dolby Vision y recuentos de pistas — para que puedas auditar por qué la herramienta prefirió un archivo sobre otro. El desglose se almacena en el fichero de plan, en el reporte JSON y en el fichero adjunto de cuarentena.

La jerarquía de códecs sitúa AV1 (14 000) y HEVC/H.265 (12 000) muy por encima de H.264 (8 000), penaliza los formatos heredados (mpeg4, VC-1, WMV, MPEG-2) y da una señal fuerte a Dolby Vision (bonificación de 5 000) y HDR (bonificación de 3 000). El contenedor MKV recibe una bonificación por patrón de nombre; AVI y VOB se penalizan.

Consulta [SCORING.es.md](SCORING.es.md) para las tablas de puntuación completas y la guía de ajuste.

---

## Modos

| Modo | DRY_RUN | QUARANTINE_MODE | Efecto |
|------|---------|-----------------|--------|
| Vista previa segura (por defecto) | `true` | cualquiera | Simula todo, solo registra — no se toca ningún archivo |
| Cuarentena (recomendado) | `false` | `true` | Mueve los archivos a `QUARANTINE_DIR`; los metadatos de Plex se eliminan tras un movimiento correcto |
| Borrado directo | `false` | `false` | Llama a la API DELETE de medios de Plex — con **Allow media deletion** activado (requerido), Plex elimina el archivo del disco. Irreversible — sin cuarentena, sin fichero adjunto, sin restauración |
| Auditoría | `AUDIT_MODE=true` | cualquiera | Canalización completa de dos pasadas incluyendo reportes; `DRY_RUN` se fuerza a `true` en tiempo de ejecución |

El modo de borrado directo se proporciona para configuraciones donde Plex se ejecuta en un host remoto y el script no puede alcanzar el sistema de archivos para realizar los movimientos de cuarentena por sí mismo. En este modo Plex realiza la eliminación: con **Allow media deletion** activado, el archivo subyacente se elimina permanentemente del disco. Es irreversible — usa el modo cuarentena siempre que el script tenga acceso al sistema de archivos. La única excepción es el modo `FIND_DUPLICATE_FILEPATHS_ONLY`, donde todas las entradas comparten un único archivo físico y solo se limpia el metadato redundante de Plex.

---

## Configuración

`config.json` mínimo requerido:

```json
{
  "PLEX_SERVER": "https://plex.your-server.com",
  "PLEX_TOKEN": "your-plex-token",
  "PLEX_LIBRARIES": ["Movies", "TV Shows"],
  "DRY_RUN": true
}
```

Todas las demás claves tienen valores por defecto seguros. Ejecuta primero con `DRY_RUN=true` y revisa el fichero de plan antes de activar el modo real.

Consulta [CONFIGURATION.es.md](CONFIGURATION.es.md) para todas las opciones de configuración con valores por defecto, tipos y descripciones.

---

## Reportes JSON

Tras cada ejecución se escriben dos ficheros (cuando están configurados):

- **Fichero de plan** — Se escribe tras la PASADA 1, antes de cualquier acción. Se guarda en `plans/dupefinder_plan_<run_id>_<timestamp>.json`. Contiene la instantánea completa de la PASADA 1: cada grupo de duplicados, puntuaciones, desgloses de puntuación, comprobaciones de existencia y la decisión tentativa de guardado. Se escribe siempre — incluso una ejecución abortada en el aviso de confirmación deja un plan auditable.

- **Reporte de ejecución** — Se escribe al final de la ejecución en `JSON_REPORT_DIR/dupefinder_report_<run_id>_<timestamp>.json`. Cubre todos los contadores de fase (veredictos de la PASADA 0, grupos encontrados/procesados/omitidos), registros por grupo, resultados de integraciones (refresco de Plex, Radarr, Sonarr), un resumen del estado actual de la cuarentena (bajo la clave `quarantine`), un resumen y una copia censurada de la configuración (tokens y claves de API reemplazados por `<redacted>`).

Establece `JSON_REPORT_DIR` en `config.json` para habilitar los reportes de ejecución. El directorio se crea automáticamente si no existe.

---

## Integraciones

### Radarr

Establece `RADARR_RESCAN_AFTER=true`, `RADARR_URL` y `RADARR_API_KEY` en `config.json`. Tras completarse la ejecución, plex_dupefinder envía un comando `RescanMovie` a Radarr para que pueda detectar y reimportar cualquier contenido afectado por la limpieza.

### Sonarr

Establece `SONARR_RESCAN_AFTER=true`, `SONARR_URL` y `SONARR_API_KEY`. Tras la ejecución, se envía un comando `RescanSeries` a Sonarr.

### Refresco de la biblioteca de Plex

Establece `PLEX_REFRESH_AFTER=true`. Tras la ejecución, plex_dupefinder llama a `section.update()` en cada biblioteca escaneada para refrescar el índice de metadatos de Plex.

---

## Configuración de Plex

**Allow media deletion** (Permitir eliminación de medios) debe estar activado en Plex antes de que plex_dupefinder pueda eliminar entradas de metadatos duplicadas:

1. Abre Plex Web → Settings → Server → Library
2. Activa **Allow media deletion**
3. Haz clic en **Save Changes**

Sin este ajuste, Plex rechazará las peticiones HTTP DELETE que plex_dupefinder emite al eliminar entradas de metadatos duplicadas.

---

## Tests

Una batería mínima de tests de seguridad cubre las funciones que pueden borrar medios — `get_score`, `select_keeper`, `check_file_exists`, `_quarantine_logical_path` y `detect_inconsistencies`:

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

Los tests stubean Plex/`requests`/`tabulate`, así que se ejecutan con solo `pytest` instalado — sin servidor Plex ni red.

---

## Documentación

- [CONFIGURATION.es.md](CONFIGURATION.es.md) — todas las opciones de configuración, valores por defecto y tipos
- [SCORING.es.md](SCORING.es.md) — sistema de puntuación, tablas de códecs y guía de ajuste
- [SAFETY_MODEL.es.md](SAFETY_MODEL.es.md) — las diez capas de seguridad en detalle
- [MIGRATION.es.md](MIGRATION.es.md) — diferencias con el proyecto original l3uddz/plex_dupefinder
- [RELEASE_NOTES.es.md](RELEASE_NOTES.es.md) — registro de cambios
