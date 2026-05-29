# Referencia de configuración

[English](CONFIGURATION.md) | **Español**

## Visión general

Todos los ajustes viven en `config.json`, ubicado en el mismo directorio que el script. En la primera ejecución sin un `config.json`, un asistente de configuración te guía para crearlo de forma interactiva. En cada ejecución posterior, `upgrade_settings()` fusiona automáticamente cualquier clave nueva introducida por la versión actual en tu `config.json` existente sin sobrescribir los valores que ya hayas establecido — imprime las claves añadidas y sale para que puedas revisarlas antes de continuar.

---

## Configuración rápida — Configuración mínima requerida

Las únicas claves que el script necesita para arrancar son los datos de conexión a Plex y la lista de bibliotecas. Todos los demás ajustes tienen un valor por defecto seguro.

```json
{
  "PLEX_SERVER": "http://192.168.1.100:32400",
  "PLEX_TOKEN": "your-token-here",
  "PLEX_LIBRARIES": ["Movies", "TV Shows"]
}
```

Todas las claves restantes se leerán de sus valores por defecto integrados. Como `DRY_RUN` es `true` por defecto, una instalación nueva **no puede borrar ni mover ningún archivo** hasta que cambies explícitamente ese ajuste.

---

## Valores por defecto seguros

El script se distribuye con una postura de seguridad conservadora. Un `config.json` recién creado con solo las tres claves requeridas anteriores se comportará así:

| Comportamiento | Por defecto | Por qué |
|---|---|---|
| `DRY_RUN=true` | No se borra, mueve ni escribe nada en Plex | Evita la pérdida de datos por una mala configuración |
| `QUARANTINE_MODE=true` | Con el modo real activado, los archivos se **mueven**, no se borran | Toda eliminación es reversible |
| `AUTO_DELETE=false` | Modo interactivo — el script pregunta antes de cada grupo | Te da control sobre cada decisión |
| `CONFIRM_BEFORE_ACTION=true` | Incluso en modo automático, requiere escribir `YES` antes de que actúe la PASADA 2 | Punto de control humano final antes de cualquier mutación |
| `AUDIT_MODE=false` | Ponlo en `true` para ejecutar la canalización completa de dos pasadas (incluyendo reportes JSON) sin ningún efecto secundario | Útil para validar la puntuación antes de pasar al modo real |

---

## Secciones de configuración

### 1. Conexión

**`PLEX_SERVER`**
- Por defecto: `"https://plex.your-server.com"`
- Tipo: string
- Descripción: URL base de tu Plex Media Server. Usa `http://` para acceso en LAN (p.ej. `http://192.168.1.100:32400`) o tu dirección HTTPS pública. El script llama a `PlexServer(PLEX_SERVER, PLEX_TOKEN)` al arrancar y aborta si la conexión falla.
- Riesgo: 🟡 Una URL incorrecta causa un aborto inmediato al arrancar — no se toca ningún dato.

---

**`PLEX_TOKEN`**
- Por defecto: `""`
- Tipo: string
- Descripción: Token de autenticación de Plex. Requerido — `validate_config` aborta si está vacío. Este valor se censura (se reemplaza por `"<redacted>"`) en todos los ficheros de plan y reportes JSON para que puedas compartir registros con seguridad. Consulta [Encontrar tu token de Plex](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
- Riesgo: 🔴 Trátalo como una contraseña. Consulta la sección de Notas de seguridad.

---

**`PLEX_LIBRARIES`**
- Por defecto: `[]`
- Tipo: lista de strings
- Descripción: Nombres de las secciones de biblioteca de Plex a escanear en busca de duplicados. Deben coincidir exactamente con los nombres de biblioteca tal como aparecen en Plex. Requerido — `validate_config` aborta si la lista está vacía. Ejemplo: `["Movies", "TV Shows", "4K Movies"]`. Tras conectar, `validate_libraries()` comprueba cada nombre configurado contra las bibliotecas que realmente existen en el servidor.
- Riesgo: 🟡 Si algún nombre configurado no existe en el servidor de Plex, el script aborta al arrancar (código de salida 2) e imprime la lista de bibliotecas disponibles, en lugar de no hacer nada en silencio con el nombre mal escrito.

---

**`REQUESTS_TIMEOUT`**
- Por defecto: `30`
- Tipo: entero (segundos)
- Descripción: Tiempo de espera en segundos aplicado a todas las peticiones HTTP realizadas con la librería `requests` — cubre las llamadas DELETE de Plex, Radarr `/api/v3/command` y Sonarr `/api/v3/command`. Auméntalo en redes lentas o para instancias de Plex grandes.
- Riesgo: 🟢 Seguro de ajustar.

---

### 2. Seguridad y comportamiento

**`DRY_RUN`**
- Por defecto: `true`
- Tipo: booleano
- Descripción: La barrera de seguridad principal. Cuando es `true`, se ejecuta la canalización completa de descubrimiento y puntuación de dos pasadas, pero no se mueve ni borra ningún archivo y no se elimina ningún metadato de Plex. Todas las decisiones se registran. Ponlo en `false` solo tras revisar un reporte en modo simulación. `AUDIT_MODE=true` lo vuelve a forzar a `true` en tiempo de ejecución sin escribir en disco.
- Riesgo: 🔴 Ponerlo en `false` habilita operaciones reales sobre archivos. Asegúrate de que `QUARANTINE_MODE=true` y `QUARANTINE_DIR` esté establecido antes de hacerlo.

---

**`AUDIT_MODE`**
- Por defecto: `false`
- Tipo: booleano
- Descripción: Cuando es `true`, fuerza `DRY_RUN=true` en memoria en tiempo de ejecución (sin modificar `config.json`) incluso si `config.json` tiene `DRY_RUN=false`. Se ejecuta la canalización completa de dos pasadas incluyendo ficheros de plan y reportes JSON, pero no se toca ningún archivo y no se hacen llamadas a Plex. Úsalo para validar cambios de puntuación antes de pasar al modo real.
- Riesgo: 🟢 Seguro — por diseño no puede producir efectos secundarios.

---

**`QUARANTINE_MODE`**
- Por defecto: `true`
- Tipo: booleano
- Descripción: Cuando es `true` (y `DRY_RUN=false`), los archivos seleccionados para eliminación se **mueven** a `QUARANTINE_DIR` usando `shutil.move` en lugar de borrarse permanentemente. Se escribe un fichero adjunto `.dupefinder_meta.json` junto a cada archivo movido que contiene la ruta original, un `restore_command` listo para ejecutar y el desglose completo de la puntuación. Los metadatos de Plex solo se eliminan tras un movimiento correcto. Si todos los movimientos a cuarentena fallan, el grupo se aborta y Plex no se toca. Ponlo en `false` solo si estás seguro de querer la eliminación permanente.
- Riesgo: 🔴 Ponerlo en `false` hace la eliminación permanente. No hay forma de deshacerla.

---

**`QUARANTINE_DIR`**
- Por defecto: `""`
- Tipo: string (ruta absoluta)
- Descripción: Ruta absoluta del directorio donde se preparan los archivos en cuarentena. Se crea automáticamente si no existe y el directorio padre tiene permisos de escritura. Requerido cuando `QUARANTINE_MODE=true` y `DRY_RUN=false` — `validate_config` aborta al arrancar si esta combinación está activa y el valor está vacío. Elige una ruta con espacio libre suficiente para contener los archivos que esperas eliminar.
- Riesgo: 🟡 Debe estar en el mismo sistema de archivos que los archivos de medios para que `shutil.move` evite una copia completa entre dispositivos. Si es entre dispositivos, el movimiento sigue funcionando pero es más lento y duplica temporalmente el uso de disco.

---

**`QUARANTINE_RETENTION_DAYS`**
- Por defecto: `30`
- Tipo: entero
- Descripción: Informativo — el script **no** purga automáticamente el directorio de cuarentena. Este valor es para tu propia referencia y planificación operativa. Tras revisar los archivos en cuarentena, bórralos manualmente (o mediante una tarea programada) una vez estés conforme con las decisiones de eliminación.
- Riesgo: 🟢 Cambiar este valor no tiene efecto en tiempo de ejecución.

---

**`AUTO_DELETE`**
- Por defecto: `false`
- Tipo: booleano
- Descripción: Cuando es `false` (por defecto), el script se detiene en cada grupo de duplicados y muestra una tabla de candidatos, el guardado recomendado y el desglose de puntuación. Puedes aceptar la recomendación, elegir un guardado diferente u omitir el grupo. Cuando es `true`, el script actúa según la recomendación de puntuación sin avisos por grupo. `CONFIRM_BEFORE_ACTION` proporciona un punto de control final incluso en modo automático.
- Riesgo: 🟡 Ponlo en `true` solo tras validar la puntuación en tu biblioteca con ejecuciones en modo simulación.

---

**`CONFIRM_BEFORE_ACTION`**
- Por defecto: `true`
- Tipo: booleano
- Descripción: Cuando es `true` (y `AUTO_DELETE=true` y `DRY_RUN=false`), el script se detiene tras el descubrimiento de la PASADA 1, muestra un resumen de todas las acciones planificadas y requiere que escribas `YES` antes de que la PASADA 2 comience a actuar. Este es el último punto de control humano antes de que ocurra cualquier movimiento de archivos o borrado en Plex. Se ignora cuando `AUTO_DELETE=false` (el modo interactivo tiene sus propios avisos por grupo).
- Riesgo: 🟡 Ponerlo en `false` elimina el punto de control final en modo automatizado.

---

**`FIND_DUPLICATE_FILEPATHS_ONLY`**
- Por defecto: `false`
- Tipo: booleano
- Descripción: Cuando es `true`, solo considera grupos de duplicados donde todas las ubicaciones de medios son idénticas (el mismo archivo físico ha sido escaneado en Plex más de una vez como entradas de metadatos separadas). En este modo, el script selecciona la entrada con el ID de medio más bajo para guardarla y elimina únicamente los metadatos de Plex — **no se mueve ni borra ningún archivo**. Útil para limpiar los propios errores de importación de Plex sin tocar ningún archivo en disco.
- Riesgo: 🟢 Este modo es solo de metadatos; no puede afectar a los archivos en disco.

---

**`PLEX_DELETE_DELAY_SECONDS`**
- Por defecto: `2.0`
- Tipo: float (segundos)
- Descripción: Duración de la espera entre llamadas consecutivas a `remove_item` dentro de un grupo durante la PASADA 2. Evita saturar la API HTTP de Plex cuando un grupo tiene varios candidatos a eliminar. Auméntalo si observas errores de límite de tasa de Plex en el registro de actividad.
- Riesgo: 🟢 Aumentarlo ralentiza la ejecución; reducirlo por debajo de 1.0 puede causar errores transitorios de la API de Plex en servidores ocupados.

---

**`SKIP_LIST`**
- Por defecto: `[]`
- Tipo: lista de strings
- Descripción: Lista de subcadenas. Cualquier candidato cuya ruta de archivo contenga alguna entrada de esta lista nunca se elimina — se omite en silencio y se registra como `mode='skipped_skip_list'` en el reporte JSON. La coincidencia es una comprobación simple de contención de subcadena (no glob ni regex) contra la ruta de archivo completa. Una coincidencia omite solo el candidato coincidente; los demás candidatos del mismo grupo siguen procesándose. Ejemplo: `["/mnt/protected/", "remux", ".iso"]`.
- Riesgo: 🟢 Solo evita la eliminación — nunca la causa.

---

### 3. Umbrales de seguridad

**`MIN_FILE_AGE_HOURS`**
- Por defecto: `24`
- Tipo: float (horas)
- Descripción: Los archivos más recientes que estas horas provocan que se omita todo el grupo. Evita condiciones de carrera con descargas activas, copias en pleno proceso de importación o escaneos de Plex que aún no se han asentado. El motivo de omisión se registra como: `"cooldown: '<path>' is X.XXh old, below threshold Y.YYh"`. Ponlo en `0` para desactivarlo.
- Modo de fallo evitado: Actuar sobre un archivo que aún se está escribiendo o que Plex todavía no ha indexado por completo.
- Riesgo: 🟢 Aumentar este valor siempre es más seguro. Reducirlo por debajo de `1.0` arriesga actuar sobre archivos que aún están cambiando.

---

**`MAX_SIZE_RATIO`**
- Por defecto: `5.0`
- Tipo: float
- Descripción: Si algún candidato no-guardado es más de este múltiplo del tamaño del archivo guardado, el grupo se omite. Protege contra emparejamientos erróneos por peculiaridades de la puntuación, donde un remux grande o un archivo 4K es superado por una codificación más pequeña y reciente. La comprobación es `other_size / keeper_size > MAX_SIZE_RATIO`. El motivo de omisión se registra como: `"size ratio N.Nx exceeds threshold M.Mx (keeper=X, sibling id=<id>=Y)"`. Ponlo en `0` para desactivarlo.
- Modo de fallo evitado: Eliminar accidentalmente un remux grande y de alta calidad porque un archivo más pequeño y nuevo ganó en la puntuación de códec o resolución.
- Riesgo: 🟡 Desactivarlo (`0`) o ponerlo muy alto elimina la protección contra emparejamientos erróneos.

---

**`MIN_SCORE_DIFFERENCE`**
- Por defecto: `0`
- Tipo: entero
- Descripción: Diferencia de puntuación mínima requerida entre el candidato con mayor puntuación (el guardado) y el segundo mejor antes de que el script actúe. Si la diferencia es menor que este umbral, el grupo se omite con el motivo: `"score delta N below threshold M"`. `0` desactiva la comprobación (cualquier diferencia distinta de cero es suficiente). Valor inicial recomendado: `1000`.
- Modo de fallo evitado: Borrar un archivo cuando dos candidatos puntúan casi idénticamente — una señal de que las señales de puntuación son ambiguas y un humano debería revisarlo.
- Riesgo: 🟡 Ponerlo en `0` permite al script actuar sobre cuasi-empates. Considera al menos `500`–`1000` para uso en producción.

---

**`STABILITY_CHECK_SECONDS`**
- Por defecto: `2.0`
- Tipo: float (segundos)
- Descripción: Antes de actuar sobre un grupo en la PASADA 2, el script lee los tamaños de todos los archivos candidatos, espera estos segundos y los vuelve a leer. Si algún tamaño cambió, el grupo se omite. Detecta archivos que superaron el enfriamiento de `MIN_FILE_AGE_HOURS` pero que aún se están escribiendo activamente (p.ej. una transcodificación de Tdarr que comenzó tras la ventana de enfriamiento). Solo activo cuando `DRY_RUN=false` y este valor es `> 0`. Ponlo en `0` para desactivarlo.
- Modo de fallo evitado: Actuar sobre un archivo en plena transcodificación o copia que parece lo bastante antiguo pero todavía está cambiando.
- Riesgo: 🟢 Aumentarlo añade latencia. Reducirlo por debajo de `1.0` puede no detectar archivos que cambian rápido.

---

**`REQUIRE_LOCAL_FS_ACCESS`**
- Por defecto: `false`
- Tipo: booleano
- Descripción: Cuando es `true`, cualquier grupo donde ninguna ruta de archivo candidata sea legible localmente en el host que ejecuta el script se omite por completo. Úsalo cuando ejecutes el script en una máquina que no tiene acceso directo al sistema de archivos de todas las rutas de medios de Plex (p.ej. un contenedor Docker separado sin todos los montajes). Cuando es `false`, el script recurre a las propias banderas `exists`/`accessible` de Plex para las rutas que no puede alcanzar localmente.
- Modo de fallo evitado: Tomar decisiones de eliminación basándose únicamente en los metadatos de Plex cuando el sistema de archivos no está disponible — lo que puede llevar a actuar sobre entradas obsoletas de Plex.
- Riesgo: 🟡 Dejarlo en `false` en un host sin acceso al sistema de archivos significa que la capa de seguridad de metadatos obsoletos (`check_file_exists`) no puede operar por completo.

---

### 4. Puntuación

La puntuación determina qué duplicado se conserva. El candidato con la mayor puntuación total es el guardado; todos los demás son candidatos a eliminación. Cada componente suma o resta al total. El desglose completo se almacena en ficheros de plan, reportes JSON y ficheros adjuntos de cuarentena.

---

**`SCORE_FILESIZE`**
- Por defecto: `false`
- Tipo: booleano
- Descripción: Cuando es `true`, suma `int(file_size / 100,000)` a la puntuación de un candidato. Desactivado por defecto porque el tamaño bruto de archivo premia las codificaciones infladas — un rip H.264 grande puede superar significativamente a una codificación HEVC eficiente de calidad percibida igual o mejor. El códec, la resolución y las señales del nombre de archivo son indicadores de calidad más fiables. Actívalo solo si tu biblioteca tiene una razón específica para preferir archivos más grandes (p.ej. coleccionas exclusivamente remuxes sin pérdida y el tamaño de archivo es un indicador de completitud).
- Riesgo: 🟡 Activarlo puede hacer que el script conserve archivos más grandes y de menor calidad por encima de codificaciones más pequeñas y eficientes.

---

**`HDR_SCORE`**
- Por defecto: `3000`
- Tipo: entero
- Descripción: Bonificación añadida a la puntuación de un candidato cuando Plex reporta contenido HDR (`colorTrc = smpte2084` o `arib-std-b67`). Auméntalo para preferir más fuertemente las versiones HDR. Ponlo en `0` para ignorar el HDR en la puntuación.
- Riesgo: 🟢 Ajustar este valor solo afecta a qué duplicado se conserva, no a si se toma una acción.

---

**`DOLBY_VISION_SCORE`**
- Por defecto: `5000`
- Tipo: entero
- Descripción: Bonificación añadida cuando Plex reporta Dolby Vision (`DOVIPresent`). Mayor que `HDR_SCORE` por defecto porque DV es una mejora sobre el HDR10 simple. Ajústalo según las capacidades de tu pantalla.
- Riesgo: 🟢 Ajustar este valor solo afecta a qué duplicado se conserva.

---

**`SUBTITLE_SCORE_PER_TRACK`**
- Por defecto: `50`
- Tipo: entero
- Descripción: Bonificación por cada pista de subtítulos en todas las partes de un elemento de medio. Se aplica como `subtitle_count * SUBTITLE_SCORE_PER_TRACK`. Pequeña por defecto para actuar como desempate en lugar de como señal dominante.
- Riesgo: 🟢 Seguro de ajustar.

---

**`AUDIO_TRACK_SCORE`**
- Por defecto: `100`
- Tipo: entero
- Descripción: Bonificación por cada pista de audio en todas las partes de un elemento de medio. Se aplica como `audio_track_count * AUDIO_TRACK_SCORE`. Premia las versiones con varias pistas de audio en distintos idiomas.
- Riesgo: 🟢 Seguro de ajustar.

---

**`FILENAME_SCORES`**
- Por defecto: Ver tabla siguiente
- Tipo: objeto (patrón glob → entero)
- Descripción: Mapea patrones glob de `fnmatch` a puntuaciones enteras. Se aplican al **nombre base** de la ruta de archivo de cada candidato, sin distinguir mayúsculas/minúsculas. Varios patrones pueden coincidir con el mismo archivo; sus puntuaciones se suman. Las puntuaciones positivas premian fuentes de alta calidad; las negativas penalizan fuentes de baja calidad. Estas son las señales con mayor peso en el modelo de puntuación por defecto.

| Patrón | Puntuación por defecto |
|---|---|
| `*Remux*` | 25000 |
| `*2160p*BluRay*` | 20000 |
| `*4K*BluRay*` | 20000 |
| `*1080p*BluRay*` | 15000 |
| `*2160p*WEB-DL*` | 14000 |
| `*4K*WEB-DL*` | 14000 |
| `*1080p*WEB-DL*` | 12000 |
| `*720p*BluRay*` | 8000 |
| `*WEB-DL*` | 6000 |
| `*WEBRip*` | 4000 |
| `*REPACK*` | 1500 |
| `*PROPER*` | 1500 |
| `*.mkv` | 2000 |
| `*EXTENDED*` | 500 |
| `*.mp4` | 500 |
| `*HDTV*` | -5000 |
| `*TS*` | -5000 |
| `*.ts` | -5000 |
| `*DVDRip*` | -3000 |
| `*dvd*` | -3000 |
| `*.wmv` | -8000 |
| `*.avi` | -10000 |
| `*.vob` | -10000 |
| `*.flv` | -10000 |
| `*CAM*` | -20000 |

- Riesgo: 🟡 Ajusta los patrones para que coincidan con tus convenciones de nomenclatura. Un patrón mal configurado que coincida con los archivos equivocados puede hacer que se conserve el duplicado incorrecto.

---

**`VIDEO_CODEC_SCORES`**
- Por defecto: Ver tabla siguiente
- Tipo: objeto (string de códec → entero)
- Descripción: Mapea los strings `videoCodec` de Plex (en minúsculas) a puntuaciones enteras. La búsqueda no distingue mayúsculas/minúsculas. La puntuación por defecto prefiere fuertemente los códecs orientados a la eficiencia y penaliza los formatos heredados.

| Códec | Puntuación por defecto |
|---|---|
| `av1` | 14000 |
| `hevc` | 12000 |
| `h265` | 12000 |
| `h264` | 8000 |
| `vp9` | 6000 |
| `Unknown` | 0 |
| `mpeg4` | -3000 |
| `vc1` | -2000 |
| `mpeg1video` | -5000 |
| `mpeg2video` | -5000 |
| `wmv2` | -8000 |
| `wmv3` | -8000 |
| `msmpeg4` | -8000 |
| `msmpeg4v2` | -8000 |
| `msmpeg4v3` | -8000 |

- Riesgo: 🟡 Cambiar las puntuaciones de códec afecta a qué duplicado se conserva. Revísalo con una ejecución en modo simulación tras cualquier cambio.

---

**`VIDEO_RESOLUTION_SCORES`**
- Por defecto: Ver tabla siguiente
- Tipo: objeto (string de resolución → entero)
- Descripción: Mapea los strings `videoResolution` de Plex a puntuaciones enteras. Por defecto gana la mayor resolución nativa.

| Resolución | Puntuación por defecto |
|---|---|
| `4k` | 20000 |
| `1080` | 10000 |
| `720` | 5000 |
| `480` | 3000 |
| `sd` | 1000 |
| `Unknown` | 0 |

- Riesgo: 🟡 Modificar estos valores cambia qué resolución se prefiere. Asegúrate de que los valores sean coherentes con tus `FILENAME_SCORES` y `VIDEO_CODEC_SCORES`.

---

**`AUDIO_CODEC_SCORES`**
- Por defecto: Ver tabla siguiente
- Tipo: objeto (string de códec → entero)
- Descripción: Mapea los strings `audioCodec` de Plex a puntuaciones enteras. Los formatos sin pérdida y de audio basado en objetos ocupan los primeros puestos por defecto.

| Códec | Puntuación por defecto |
|---|---|
| `truehd` | 4500 |
| `dca-ma` | 4000 |
| `pcm` | 2500 |
| `flac` | 2500 |
| `dca` | 2000 |
| `opus` | 1500 |
| `eac3` | 1250 |
| `aac` | 1000 |
| `ac3` | 1000 |
| `mp3` | 1000 |
| `mp2` | 500 |
| `wmapro` | 200 |
| `Unknown` | 0 |

- Riesgo: 🟡 Ajústalo según tu hardware de audio. Por ejemplo, si tu receptor no decodifica TrueHD, quizás prefieras bajar su puntuación respecto a EAC3.

---

### 5. PASADA 0 — Preanálisis

**`PRE_ANALYZE_DUPLICATES`**
- Por defecto: `false`
- Tipo: booleano
- Descripción: Cuando es `true`, habilita la PASADA 0: antes de puntuar cada grupo de duplicados, el script llama a `item.analyze()` en cada elemento de medio en Plex y sondea en busca de metadatos frescos. Esto obliga a Plex a reexaminar los datos de códec, bitrate y duración antes de que el script tome cualquier decisión. Desactivado por defecto porque es secuencial y lento en bibliotecas grandes (una llamada analyze por elemento, sondeando hasta `ANALYZE_TIMEOUT_SECONDS`). Los grupos que agotan el tiempo (`timeout`) o fallan (`analyze_failed`) se convierten en omitidos (stubs) y nunca llegan a la puntuación de la PASADA 1.
- Riesgo: 🟡 Aumenta significativamente el tiempo de ejecución en bibliotecas grandes. Actívalo cuando sospeches que Plex tiene metadatos de códec o bitrate obsoletos (p.ej. tras una transcodificación por lotes de Tdarr).

---

**`ANALYZE_TIMEOUT_SECONDS`**
- Por defecto: `60`
- Tipo: float (segundos)
- Descripción: Tiempo máximo de espera para que `item.analyze()` produzca metadatos frescos para un solo elemento en la PASADA 0. El script sondea con `item.reload()` hasta que los metadatos cambian o se agota este tiempo. Los elementos que agotan el tiempo se omiten con estado `timeout`. Los elementos que devuelven metadatos válidos pero sin cambios (`sane_unchanged`) se aceptan con una advertencia — este estado es **ambiguo**: puede significar que los metadatos ya eran correctos (seguro proceder) o que Plex aún no ha procesado la llamada `analyze()` dentro de la ventana de sondeo (la puntuación todavía podría usar datos obsoletos). El script no puede distinguir estos casos vía la API de Plex.
- Riesgo: 🟢 Auméntalo en servidores de Plex lentos o muy cargados.

---

### 6. Hashing parcial

**`PARTIAL_HASH_ENABLED`**
- Por defecto: `false`
- Tipo: booleano
- Descripción: Cuando es `true`, calcula un hash SHA-256 de los primeros y últimos `PARTIAL_HASH_BYTES` bytes de cada archivo candidato durante la PASADA 1 y la PASADA 2. Si algún hash cambia entre las dos pasadas, el grupo se trata como inconsistente y se omite. Proporciona protección adicional contra la modificación de un archivo entre el descubrimiento y la acción (p.ej. una transcodificación activa de Tdarr que no detectó la comprobación de estabilidad). Añade sobrecarga de lectura del sistema de archivos proporcional a `PARTIAL_HASH_BYTES * 2 * número_de_candidatos`.
- Riesgo: 🟢 Activarlo solo añade una comprobación de seguridad — no puede causar eliminaciones incorrectas. Añade sobrecarga de E/S.

---

**`PARTIAL_HASH_BYTES`**
- Por defecto: `1048576` (1 MiB)
- Tipo: entero (bytes)
- Descripción: Número de bytes a leer desde el principio **y** el final de cada archivo para el hash parcial. La lectura total por archivo es `PARTIAL_HASH_BYTES * 2`. El valor por defecto de 1 MiB de cabecera + 1 MiB de cola detecta cambios en las cabeceras del contenedor y los pies de flujo sin leer el archivo entero.
- Riesgo: 🟢 Aumentarlo proporciona una detección de deriva ligeramente más fuerte a costa de más E/S. Solo relevante cuando `PARTIAL_HASH_ENABLED=true`.

---

### 7. Reportes

**`JSON_REPORT_DIR`**
- Por defecto: `""` (desactivado)
- Tipo: string (ruta absoluta)
- Descripción: Directorio donde se escriben los reportes JSON por ejecución. Cuando está vacío, no se escribe ningún reporte. Cuando se establece, `validate_config` crea el directorio automáticamente si no existe. El nombre del fichero de reporte es `dupefinder_report_<run_id>_<YYYYMMDDTHHMMSSZ>.json`. Los reportes incluyen una copia censurada de tu configuración, todos los contadores de fase (PASADA 0, descubrimiento, revalidación, acción), registros por grupo con puntuaciones y decisiones, resultados de integraciones y un resumen legible. Las claves sensibles (`PLEX_TOKEN`, `RADARR_API_KEY`, `SONARR_API_KEY`) se reemplazan por `"<redacted>"`.

**Los ficheros de plan** se escriben siempre (independientemente de este ajuste) en `<script_dir>/plans/` tras completarse la PASADA 1. El fichero de plan captura la instantánea completa de la PASADA 1 antes de que se tome cualquier acción, proporcionando un registro auditable incluso si abortas en el aviso de confirmación. Nombre del fichero de plan: `dupefinder_plan_<run_id>_<YYYYMMDDTHHMMSSZ>.json`.

- Riesgo: 🟢 Activarlo crea ficheros en disco pero no tiene efecto en las eliminaciones.

---

**`LOG_LEVEL`**
- Por defecto: `"INFO"`
- Tipo: string — uno de `DEBUG`, `INFO`, `WARNING`, `ERROR` (sin distinguir mayúsculas/minúsculas)
- Descripción: Nivel de detalle de `activity.log`. `INFO` (por defecto) registra el progreso de las fases y cada decisión. `DEBUG` además registra una línea por cada parte de medio (existencia, antigüedad) — útil para diagnóstico pero grande en bibliotecas grandes. Un valor no reconocido recurre a `INFO`. Independientemente del nivel, `activity.log` se rota por tamaño mediante un `RotatingFileHandler` limitado a **10 MiB × 5 copias de respaldo** (techo ≈60 MiB), de modo que las ejecuciones programadas desatendidas no puedan llenar el disco.
- Riesgo: 🟢 Solo afecta al registro; sin efecto en las eliminaciones.

---

**Resumen de cuarentena** — Al final de cada ejecución (cuando `QUARANTINE_DIR` está establecido), el script reporta el contenido **actual** del directorio de cuarentena: número de archivos, tamaño total, antigüedad del archivo más viejo y cuántos archivos superan `QUARANTINE_RETENTION_DAYS`. Esto es solo visibilidad de lectura — el script nunca purga automáticamente. Las mismas cifras se escriben en el reporte JSON bajo la clave `quarantine`. La antigüedad se deriva del `quarantine_timestamp` de cada fichero adjunto (no del mtime del archivo, que `shutil.move` preserva del original).

---

### 8. Integraciones

**`PLEX_REFRESH_AFTER`**
- Por defecto: `false`
- Tipo: booleano
- Descripción: Cuando es `true`, llama a `section.update()` en cada biblioteca de `PLEX_LIBRARIES` al final de la ejecución. Esto dispara un escaneo de la biblioteca de Plex para detectar cualquier cambio realizado por el proceso de eliminación. Usa el `PLEX_TOKEN` existente — no se necesita configuración adicional.
- Riesgo: 🟢 Solo dispara un escaneo; no modifica ningún metadato.

---

**`RADARR_URL`**
- Por defecto: `""`
- Tipo: string
- Descripción: URL base de tu instancia de Radarr (p.ej. `http://192.168.1.100:7878`). Requerido cuando `RADARR_RESCAN_AFTER=true` — `validate_config` aborta al arrancar si `RADARR_RESCAN_AFTER` está activado pero este valor está vacío.
- Riesgo: 🟢 Solo se usa para el disparo del reescaneo posterior a la ejecución.

---

**`RADARR_API_KEY`**
- Por defecto: `""`
- Tipo: string
- Descripción: Clave de API de Radarr. Se envía como la cabecera `X-Api-Key`. Requerida cuando `RADARR_RESCAN_AFTER=true`. Censurada en todos los ficheros de plan y reportes JSON.
- Riesgo: 🔴 Trátala como una contraseña. Consulta las Notas de seguridad.

---

**`RADARR_RESCAN_AFTER`**
- Por defecto: `false`
- Tipo: booleano
- Descripción: Cuando es `true`, envía por POST un comando `RescanMovie` a `<RADARR_URL>/api/v3/command` al final de la ejecución. Se dispara una vez tras procesar todos los grupos, independientemente de cuántos elementos se hayan eliminado realmente. Requiere que tanto `RADARR_URL` como `RADARR_API_KEY` estén establecidos.
- Riesgo: 🟢 Solo dispara un reescaneo en Radarr.

---

**`SONARR_URL`**
- Por defecto: `""`
- Tipo: string
- Descripción: URL base de tu instancia de Sonarr (p.ej. `http://192.168.1.100:8989`). Requerido cuando `SONARR_RESCAN_AFTER=true`.
- Riesgo: 🟢 Solo se usa para el disparo del reescaneo posterior a la ejecución.

---

**`SONARR_API_KEY`**
- Por defecto: `""`
- Tipo: string
- Descripción: Clave de API de Sonarr. Se envía como la cabecera `X-Api-Key`. Requerida cuando `SONARR_RESCAN_AFTER=true`. Censurada en todos los ficheros de plan y reportes JSON.
- Riesgo: 🔴 Trátala como una contraseña. Consulta las Notas de seguridad.

---

**`SONARR_RESCAN_AFTER`**
- Por defecto: `false`
- Tipo: booleano
- Descripción: Cuando es `true`, envía por POST un comando `RescanSeries` a `<SONARR_URL>/api/v3/command` al final de la ejecución. Mismo comportamiento y requisitos que `RADARR_RESCAN_AFTER`, aplicado a Sonarr.
- Riesgo: 🟢 Solo dispara un reescaneo en Sonarr.

---

## Paso a paso: pasar al modo real con seguridad

Sigue estos pasos en orden. Cada paso añade confianza antes de que el siguiente retire una capa de seguridad.

**Paso 1 — Ejecuta en modo simulación (por defecto)**

Deja `DRY_RUN=true` y ejecuta el script. Revisa la salida en consola y `decisions.log`. No se tocará ningún archivo.

**Paso 2 — Habilita los reportes JSON**

Establece `JSON_REPORT_DIR` en una ruta que puedas explorar. Vuelve a ejecutar en modo simulación y revisa el reporte generado. Comprueba que las selecciones de guardado coinciden con tus expectativas para una muestra de grupos.

**Paso 3 — Ajusta la puntuación si es necesario**

Revisa los desgloses de puntuación por grupo en el reporte JSON. Si se selecciona el duplicado equivocado como guardado para algún grupo, ajusta `VIDEO_CODEC_SCORES`, `FILENAME_SCORES` o `MIN_SCORE_DIFFERENCE` según corresponda. Vuelve a ejecutar en modo simulación hasta quedar conforme.

**Paso 4 — Establece un umbral de puntuación**

Establece `MIN_SCORE_DIFFERENCE` en al menos `1000`. Esto evita que el script actúe sobre cuasi-empates donde la puntuación es ambigua.

```json
"MIN_SCORE_DIFFERENCE": 1000
```

**Paso 5 — Configura el directorio de cuarentena**

Establece `QUARANTINE_DIR` en una ruta absoluta con espacio libre suficiente. Asegúrate de que `QUARANTINE_MODE` permanezca en `true`.

```json
"QUARANTINE_DIR": "/mnt/quarantine/plex_dupefinder",
"QUARANTINE_MODE": true
```

**Paso 6 — Pasa al modo real**

Establece `DRY_RUN=false`. Con `QUARANTINE_MODE=true`, los archivos se mueven a `QUARANTINE_DIR` en lugar de borrarse. Los metadatos de Plex se eliminan solo tras un movimiento correcto.

```json
"DRY_RUN": false
```

**Paso 7 — Revisa el directorio de cuarentena**

Tras la ejecución, explora `QUARANTINE_DIR`. Cada archivo movido tiene un fichero adjunto `.dupefinder_meta.json`. Para restaurar un archivo, copia el campo `restore_command` del fichero adjunto y ejecútalo en una shell:

```sh
mv '/mnt/quarantine/plex_dupefinder/Breaking Bad/Season 01/ep.mkv' '/mnt/media/TV/Breaking Bad/Season 01/ep.mkv'
```

Cuando estés conforme con las eliminaciones, borra el contenido de la cuarentena manualmente.

**Paso 8 — Opcional: habilita el hashing parcial**

Para mayor confianza en la PASADA 2 (especialmente en bibliotecas activas donde Tdarr u otras herramientas pueden estar transcodificando), habilita el hashing parcial:

```json
"PARTIAL_HASH_ENABLED": true,
"PARTIAL_HASH_BYTES": 1048576
```

---

## Notas de seguridad

`config.json` contiene credenciales que conceden acceso completo a tu servidor de Plex y, opcionalmente, a tus instancias de Radarr y Sonarr.

- **Restringe los permisos del fichero.** En Linux/macOS: `chmod 600 config.json`. En Windows: asegúrate de que el fichero solo sea accesible para tu cuenta de usuario.
- **Nunca subas `config.json` al control de versiones.** El `.gitignore` del repositorio lo excluye por defecto. Verifícalo antes de cualquier `git add`.
- **Usa la rotación de `PLEX_TOKEN`.** Los tokens de Plex no caducan automáticamente. Rota tu token periódicamente vía la interfaz web de Plex, especialmente tras cualquier sospecha de exposición.
- **Las claves sensibles se censuran en los reportes.** `PLEX_TOKEN`, `RADARR_API_KEY` y `SONARR_API_KEY` se reemplazan por `"<redacted>"` en todos los ficheros de plan y reportes JSON. No copies manualmente estos valores en reportes o registros.
- **Tokens con privilegios mínimos.** Si tus instancias de Radarr/Sonarr admiten claves de API con alcance limitado, usa una clave que solo pueda disparar reescaneos en lugar de una clave de administrador completo.
