# Migración desde el plex_dupefinder original

[English](MIGRATION.md) | **Español**

## Por qué existe este fork

El proyecto original `l3uddz/plex_dupefinder` tiene un bug crítico que puede borrar
permanentemente el archivo equivocado cuando los metadatos internos de Plex están obsoletos. Este
fork corrige ese bug y añade múltiples capas de seguridad independientes para evitar la pérdida de
datos en casos límite que el código original nunca gestionó.

---

## Corrección de bug crítico: el problema de los metadatos obsoletos

### En qué consistía el bug

Plex cachea los resultados del análisis de medios (códec, bitrate, duración, ruta de archivo y
banderas de existencia) en su propia base de datos. Cuando un archivo se elimina del disco o vive en
una unidad desmontada, Plex puede seguir reportándolo como presente (`exists=True`,
`accessible=True`) durante un periodo prolongado — a veces indefinidamente hasta un escaneo manual de
la biblioteca.

El script original tomaba todas las decisiones de puntuación y eliminación usando solo esas banderas
de Plex. El resultado:

1. Plex reporta dos duplicados. Uno es una entrada fantasma (el archivo ya no está en disco);
   el otro es el archivo real que el usuario quiere conservar.
2. La entrada fantasma puntúa bien porque sus metadatos de Plex aún parecen válidos.
3. El script identifica el fantasma como el guardado y el archivo real como el duplicado.
4. El archivo real se borra. La biblioteca queda con una entrada fantasma rota.

### Cómo se corrige: `check_file_exists()`

`check_file_exists` hace **autoritativo al sistema de archivos local**. La lógica es:

- Si tanto el sistema de archivos local como Plex son alcanzables, **ambos deben coincidir** en que el archivo
  existe: `exists = os.path.exists(path) AND part.exists AND part.accessible`.
- Cualquier desacuerdo se trata como MISSING, sin importar qué lado discrepe.
- Si solo el sistema de archivos local es alcanzable (banderas de Plex no disponibles), se usa el
  veredicto del sistema de archivos solo.
- Si solo Plex reporta (el sistema de archivos no es alcanzable desde el host), se usa Plex solo
  pero se registra la limitación.
- Si ninguna fuente reporta, el archivo se trata como MISSING por seguridad.

Un candidato de medio cuya comprobación de existencia devuelve `exists=False` se excluye de la
candidatura en `select_keeper`. Un grupo donde el candidato con mayor puntuación está ausente
se omite por completo — no se toma ninguna acción.

### Por qué "sistema de archivos como autoridad"

El sistema de archivos es la verdad fundamental sobre qué bytes existen realmente en disco. Los
metadatos de Plex son consultivos: reflejan la última vez que Plex analizó o escaneó el archivo con
éxito. Ambos pueden discrepar, y cuando lo hacen, la única interpretación segura es que el registro
de Plex está obsoleto.

---

## Cambios de arquitectura

### De una pasada a dos pasadas

| | Original | Este fork |
|---|---|---|
| Descubrimiento | Puntúa y actúa inmediatamente en el mismo bucle | PASADA 1: solo lectura; puntúa y escribe un fichero de plan; sin escrituras en Plex |
| Acción | Borrado inmediato tras puntuar | PASADA 2: vuelve a obtener cada elemento de Plex, compara con la instantánea de la PASADA 1, luego actúa |
| Protección de condiciones de carrera | Ninguna | `detect_inconsistencies()` compara archivos, tamaños, existencia, duración, bitrate, códec y opcionalmente hashes parciales entre las dos pasadas |
| Rastro de auditoría | Ninguno | `plans/dupefinder_plan_<run_id>_<ts>.json` escrito tras la PASADA 1 independientemente de si la PASADA 2 se ejecuta |

Cualquier cambio de estado detectado entre la PASADA 1 y la PASADA 2 (un archivo fue transcodificado,
llegó una nueva importación, un tamaño cambió) provoca que el grupo se omita sin acción.

### PASADA 0 opcional: preanálisis

Cuando `PRE_ANALYZE_DUPLICATES=True`, se ejecuta una PASADA 0 opcional antes de puntuar. Llama a
`item.analyze()` de Plex en cada duplicado y sondea hasta que los metadatos se confirman estables.
Los grupos que agotan el tiempo o fallan el análisis se convierten en omitidos (stubs) y nunca
llegan a la puntuación de la PASADA 1. Está desactivada por defecto porque es lenta en bibliotecas
grandes y solo se necesita cuando se sabe que los metadatos de Plex están obsoletos.

### De borrado directo a cuarentena primero

| | Original | Este fork |
|---|---|---|
| Método de eliminación | Llama a la API DELETE de Plex; el archivo se elimina permanentemente | Mueve el archivo a `QUARANTINE_DIR` con `shutil.move`, luego llama a la DELETE de Plex |
| Reversibilidad | Ninguna | Cada archivo en cuarentena obtiene un fichero adjunto `.dupefinder_meta.json` |
| Procedimiento de restauración | N/A | Copia el campo `restore_command` del fichero adjunto y ejecútalo en una shell |
| Comportamiento por defecto | Borrar | Cuarentena (`QUARANTINE_MODE=True` por defecto) |

El directorio de cuarentena replica la ruta lógica del archivo original (directorio del título
y por debajo), de modo que los archivos de distintas bibliotecas con el mismo título se mantienen
separados. Si ocurre una colisión (volver a ejecutar sobre la misma cuarentena), el destino se
desambigua con un sufijo de biblioteca y luego un sufijo de timestamp.

El fichero adjunto `.dupefinder_meta.json` contiene:

- `original_path` y `quarantine_path`
- `run_id`, `media_id`, `reason`
- `keeper.files`, `keeper.score`, `keeper.score_breakdown`
- `restore_command`: un comando de shell `mv '<quarantine>' '<original>'` listo para ejecutar

Para habilitar la DELETE directa de Plex sin cuarentena, establece `QUARANTINE_MODE=False` y
`DRY_RUN=False` explícitamente.

### Valores por defecto cambiados

| Ajuste | Por defecto original | Por defecto en este fork | Razón |
|---|---|---|---|
| `DRY_RUN` | `false` (implícito — la primera ejecución actúa) | `true` | Una instalación nueva no puede destruir datos sin un cambio explícito de configuración |
| `QUARANTINE_MODE` | No presente | `true` | Todas las eliminaciones son recuperables por defecto |
| `SCORE_FILESIZE` | `true` | `false` | Las codificaciones eficientes no deberían perder frente a archivos más grandes y de menor calidad |
| `VIDEO_CODEC_SCORES h264` | ~10000 (el más alto) | `8000` | H.264 ya no es el techo de calidad |
| `VIDEO_CODEC_SCORES hevc/h265` | ~5000 | `12000` | HEVC es el estándar actual para codificaciones eficientes en calidad |
| Peso del bitrate | `int(bitrate)` completo | `int(bitrate * 0.5)` | El peso a la mitad impide que un H.264 inflado supere a un HEVC eficiente solo por bitrate |

---

## Modernización de la puntuación

El modelo de puntuación original se construyó cuando H.264 era el códec dominante. Este fork
recalibra los valores por defecto para reflejar la práctica de codificación actual.

### Cambios de puntuación de códec

| Códec | Puntuación original | Este fork | Justificación |
|---|---|---|---|
| `av1` | No puntuado | 14000 | El códec moderno más eficiente |
| `hevc` / `h265` | ~5000 | 12000 | Eficiente y ampliamente compatible |
| `h264` | ~10000 (ganador) | 8000 | Todavía bueno pero ya no el techo |
| `mpeg4` | 0 o positivo | -3000 | Heredado, penalizado |
| `vc1` | 0 | -2000 | Heredado, penalizado |
| `mpeg1video` / `mpeg2video` | 0 | -5000 | Formatos obsoletos |
| `wmv2` / `wmv3` / `msmpeg4*` | 0 | -8000 | Fuertemente penalizados |

### El valor por defecto de SCORE_FILESIZE cambió a `false`

El tamaño de archivo es un indicador del bitrate, no de la calidad. Un archivo H.264 grande puede
superar a una codificación HEVC eficiente solo por tamaño. El códec, la resolución y las señales del
nombre de archivo son indicadores de calidad más fiables. El tamaño de archivo se mantiene como
desempate opcional vía `SCORE_FILESIZE=true`.

### El peso del bitrate se redujo a la mitad

El bitrate ahora se pondera al `0.5×` (`int(video_bitrate * 0.5)`) en lugar del peso completo
original. Esto mantiene el bitrate como desempate mientras impide que un rip H.264 de alto bitrate
supere a una codificación HEVC de igual calidad.

### Los desgloses de puntuación ahora son auditables

`get_score` devuelve `(total_score, breakdown_dict)` junto al total entero. El desglose registra cada
componente de puntuación (códec, resolución, coincidencias de nombre de archivo, bitrate,
dimensiones, canales de audio, HDR, Dolby Vision, pistas de subtítulos, pistas de audio) y se
almacena en el fichero de plan, el reporte JSON y el fichero adjunto de cuarentena. Cada decisión de
guardado puede revisarse a posteriori.

### Nuevas señales de puntuación (no en el original)

| Señal | Clave de configuración | Por defecto | Qué la dispara |
|---|---|---|---|
| HDR | `HDR_SCORE` | 3000 | Plex reporta `colorTrc=smpte2084` o `colorTrc=arib-std-b67` |
| Dolby Vision | `DOLBY_VISION_SCORE` | 5000 | Plex reporta `DOVIPresent` |
| Pistas de subtítulos | `SUBTITLE_SCORE_PER_TRACK` | 50 por pista | Recuento de flujos de subtítulos incrustados |
| Pistas de audio | `AUDIO_TRACK_SCORE` | 100 por pista | Recuento de flujos de audio en todas las partes |

---

## Nuevas capas de seguridad

Las siguientes capas no existen en el proyecto original. Todas están activas por defecto
salvo que se indique.

| Capa | ¿Siempre activa? | Clave de configuración | Qué evita |
|---|---|---|---|
| Refresco de metadatos PASADA 0 | Desactivada por defecto | `PRE_ANALYZE_DUPLICATES` | Puntuar con metadatos obsoletos de Plex; los grupos con timeouts o fallos de análisis se omiten |
| Validación del sistema de archivos | Siempre activa | — (`check_file_exists`) | Actuar sobre entradas fantasma de Plex donde el archivo ya no existe en disco |
| Enfriamiento por antigüedad | Activa (24 h por defecto) | `MIN_FILE_AGE_HOURS` | Condiciones de carrera con descargas activas, copias en pleno proceso de importación o escaneos de Plex no asentados |
| Validez de metadatos | Siempre activa | — (`has_sane_metadata`) | Decisiones basadas en entradas con duración cero, bitrate cero o códec desconocido que Plex aún no ha analizado por completo |
| Umbral de puntuación | Desactivada por defecto (0) | `MIN_SCORE_DIFFERENCE` | Eliminaciones donde dos candidatos puntúan demasiado parecido para distinguirse con confianza |
| Protección por ratio de tamaño | Activa (5.0× por defecto) | `MAX_SIZE_RATIO` | Borrar un remux grande porque una copia más pequeña y nueva ganó por poco en la puntuación de códec/resolución |
| Revalidación PASADA 2 | Siempre activa | — (`detect_inconsistencies`) | Actuar sobre un estado que cambió entre el descubrimiento y la acción (transcodificaciones, nuevas importaciones, movimientos de archivos del usuario) |
| Comprobación de estabilidad | Activa (2 s por defecto) | `STABILITY_CHECK_SECONDS` | Actuar sobre un archivo que se está escribiendo activamente en el momento de la eliminación |
| Modo auditoría | Desactivado por defecto | `AUDIT_MODE` | Ejecutar la canalización completa de dos pasadas y el reporte sin mutaciones, independientemente del ajuste `DRY_RUN` en config.json |
| Cuarentena | Activa por defecto | `QUARANTINE_MODE` | Pérdida de datos permanente; toda eliminación es reversible vía el `restore_command` del fichero adjunto |
| Confirmar antes de actuar | Activa por defecto | `CONFIRM_BEFORE_ACTION` | Que ejecuciones desatendidas con `AUTO_DELETE` actúen sin una confirmación humana final |

---

## Nuevas claves de configuración

Las siguientes claves no existían en el proyecto original. Todas se añaden automáticamente
a un `config.json` existente mediante `upgrade_settings()` en el primer arranque tras actualizar
(ver Autoactualización de configuración más abajo).

| Clave | Por defecto | Descripción |
|---|---|---|
| `QUARANTINE_MODE` | `true` | Mover archivos a `QUARANTINE_DIR` en lugar de borrarlos permanentemente |
| `QUARANTINE_DIR` | `""` | Ruta absoluta para el área de preparación de cuarentena; requerida cuando `QUARANTINE_MODE=true` |
| `QUARANTINE_RETENTION_DAYS` | `30` | Referencia informativa para la limpieza manual; el script no purga automáticamente |
| `MIN_SCORE_DIFFERENCE` | `0` | Diferencia de puntuación mínima requerida para actuar; `0` la desactiva |
| `MIN_FILE_AGE_HOURS` | `24` | Omitir grupos donde algún archivo es más reciente que estas horas; `0` lo desactiva |
| `MAX_SIZE_RATIO` | `5.0` | Omitir grupos donde algún no-guardado es más de este múltiplo mayor que el guardado; `0` lo desactiva |
| `REQUIRE_LOCAL_FS_ACCESS` | `false` | Omitir cualquier grupo donde ninguna ruta del sistema de archivos sea alcanzable localmente |
| `STABILITY_CHECK_SECONDS` | `2.0` | Releer los tamaños de archivo tras estos segundos y omitir si algún tamaño cambió; `0` lo desactiva |
| `AUDIT_MODE` | `false` | Forzar `DRY_RUN=true` en tiempo de ejecución sin persistir en disco; usar para validar la puntuación |
| `PARTIAL_HASH_ENABLED` | `false` | Calcular SHA-256 de cabecera+cola durante ambas pasadas y marcar cualquier cambio de hash como inconsistencia |
| `PARTIAL_HASH_BYTES` | `1048576` | Bytes leídos de cabecera y cola para el hash parcial (por defecto 1 MiB de cada lado) |
| `CONFIRM_BEFORE_ACTION` | `true` | Pedir `YES` antes de que la PASADA 2 actúe sobre cualquier grupo en modo `AUTO_DELETE` |
| `PRE_ANALYZE_DUPLICATES` | `false` | Llamar a `item.analyze()` antes de la puntuación de la PASADA 1 (PASADA 0); lento en bibliotecas grandes |
| `ANALYZE_TIMEOUT_SECONDS` | `60` | Segundos máximos de espera para los resultados de `analyze()` de la PASADA 0 |
| `JSON_REPORT_DIR` | `""` | Directorio para los reportes JSON por ejecución; cadena vacía desactiva el reporte |
| `HDR_SCORE` | `3000` | Bonificación de puntuación cuando Plex detecta HDR |
| `DOLBY_VISION_SCORE` | `5000` | Bonificación de puntuación cuando Plex detecta Dolby Vision |
| `SUBTITLE_SCORE_PER_TRACK` | `50` | Bonificación de puntuación por cada flujo de subtítulos incrustado |
| `AUDIO_TRACK_SCORE` | `100` | Bonificación de puntuación por cada flujo de audio |
| `PLEX_REFRESH_AFTER` | `false` | Disparar un escaneo de la biblioteca de Plex en todas las bibliotecas configuradas tras la ejecución |
| `RADARR_URL` | `""` | URL base de Radarr (usada con `RADARR_RESCAN_AFTER`) |
| `RADARR_API_KEY` | `""` | Clave de API de Radarr; censurada en reportes y ficheros de plan |
| `RADARR_RESCAN_AFTER` | `false` | Enviar por POST un comando `RescanMovie` a Radarr tras la ejecución |
| `SONARR_URL` | `""` | URL base de Sonarr (usada con `SONARR_RESCAN_AFTER`) |
| `SONARR_API_KEY` | `""` | Clave de API de Sonarr; censurada en reportes y ficheros de plan |
| `SONARR_RESCAN_AFTER` | `false` | Enviar por POST un comando `RescanSeries` a Sonarr tras la ejecución |
| `REQUESTS_TIMEOUT` | `30` | Tiempo de espera en segundos para todas las peticiones HTTP (DELETE de Plex, Radarr, Sonarr) |
| `PLEX_DELETE_DELAY_SECONDS` | `2.0` | Espera entre eliminaciones consecutivas dentro de un grupo durante la PASADA 2 para evitar saturar la API de Plex; `0` la desactiva |
| `LOG_LEVEL` | `"INFO"` | Nivel de detalle de `activity.log` (`DEBUG`/`INFO`/`WARNING`/`ERROR`); el registro se rota por tamaño (10 MiB × 5 copias) en cualquier caso |

---

## Autoactualización de configuración

`upgrade_settings()` en `config.py` se ejecuta automáticamente al arrancar. Compara cada
clave del `base_config` integrado con las claves presentes en el `config.json` del usuario.
Cualquier clave que falte en `config.json` se añade con su valor por defecto.

**Los valores existentes nunca se sobrescriben.** Un usuario que ya haya establecido
`MIN_FILE_AGE_HOURS=48` conservará ese valor tras actualizar.

Cuando se añaden nuevas claves, el script imprime cada clave añadida en stdout y sale,
pidiendo al usuario que revise los nuevos valores por defecto antes de la siguiente ejecución. Esto
significa que actualizar desde el original no requiere edición manual de la configuración — ejecuta
el script una vez, revisa las adiciones impresas, ajusta cualquier valor por defecto según sea
necesario, y vuelve a ejecutar.

---

## Nota sobre `deletefiles.sh`

El repositorio contiene un script `deletefiles.sh` heredado del proyecto original.
Lee `decisions.log` y llama a `rm` sobre las líneas coincidentes. Este script ahora es obsoleto:

- La ruta principal de eliminación es la cuarentena (`shutil.move`), no `rm`.
- El modo de borrado directo usa la API DELETE de Plex, no `rm` de shell.
- El formato de `decisions.log` ha cambiado de maneras que hacen frágil el parseo de shell.

Los usuarios que antes usaban `deletefiles.sh` deberían usar en su lugar el directorio de cuarentena
y el campo `restore_command` de cada fichero adjunto `.dupefinder_meta.json` para revisar y
restaurar archivos según sea necesario.

---

## Lo que no cambia

Los siguientes comportamientos son idénticos al proyecto original:

- Integración con la API de Plex (librería `plexapi`, URL del servidor, autenticación por token)
- Modo interactivo: cuando `AUTO_DELETE=false`, el script muestra una tabla y pregunta
  por grupo
- Coincidencia por subcadena de `SKIP_LIST`: cualquier ruta de archivo que contenga una subcadena
  configurada nunca se elimina
- Modo `FIND_DUPLICATE_FILEPATHS_ONLY`: cuando está activado, solo considera elementos donde todas
  las ubicaciones comparten una ruta idéntica, selecciona el ID de medio más bajo y realiza
  eliminación solo de metadatos sin tocar archivos
