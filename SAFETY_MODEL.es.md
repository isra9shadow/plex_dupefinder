# Modelo de seguridad

[English](SAFETY_MODEL.md) | **Español**

## Principio central

**Preferir los falsos negativos sobre los falsos positivos.**

Es mejor omitir un grupo de duplicados por completo —dejando ambos archivos intactos— que
eliminar accidentalmente el archivo equivocado. Cada capa de este documento existe para detectar
un modo de fallo específico del mundo real y abortar con elegancia en lugar de adivinar. Un grupo
omitido no cuesta nada; una eliminación incorrecta puede ser irrecuperable.

---

## Visión general de la canalización

```text
Inicio
  │
  ├─[L0] DRY_RUN / AUDIT_MODE ──────────────── simula todo, sin escrituras
  │
  ├─[PASADA 0] Preanálisis (opcional)
  │   └─[L1] comparación de instantáneas ───── omite si los metadatos no están demostrablemente frescos
  │
  ├─[PASADA 1] Descubrimiento
  │   ├─[L2] Validación del sistema de archivos ─ omite el archivo si Plex dice que existe pero el FS no coincide
  │   ├─[L3] Enfriamiento por antigüedad ───── omite el grupo si el archivo más reciente es demasiado nuevo
  │   ├─[L4] Validez de metadatos ──────────── no puede ser guardado si duración/bitrate/códec inválidos
  │   ├─[L5] Umbral de puntuación ──────────── omite el grupo si el margen del ganador es muy pequeño
  │   └─[L6] Protección por ratio de tamaño ── omite el grupo si la diferencia de tamaño es demasiado extrema
  │
  └─[PASADA 2] Revalidación y acción
      ├─[L7] Comparación de revalidación ───── omite el grupo si el estado cambió desde la PASADA 1
      ├─[L8] Comprobación de estabilidad ───── omite el grupo si los archivos aún se están escribiendo
      └─[L9] Cuarentena ───────────────────── mueve a QUARANTINE_DIR, nunca borra de forma definitiva
```

---

## Detalles de las capas

---

### Capa 0: DRY_RUN y AUDIT_MODE

**Función:** `remove_item()`, bloque de arranque principal
**Clave(s) de configuración:** `DRY_RUN` (por defecto: `true`), `AUDIT_MODE` (por defecto: `false`)
**Fallo evitado:** Pérdida de datos no intencionada en una instalación nueva o durante la validación de la puntuación.
**Cuándo se activa:** Siempre se evalúa antes de cualquier movimiento de archivo o llamada DELETE a Plex.

`DRY_RUN=true` (el valor por defecto) ejecuta la canalización completa de dos pasadas —secciones
escaneadas, puntuaciones calculadas, decisiones de guardado tomadas, decisiones registradas— pero
`remove_item()` cortocircuita antes de llamar a `quarantine_files()` o `remove_plex_metadata()`.
No se mueve ningún archivo y no se emite ninguna DELETE a la API de Plex. El fichero de plan y el
reporte JSON se siguen escribiendo, de modo que la ejecución produce un registro auditable completo
de lo que *habría* ocurrido.

`AUDIT_MODE=true` logra el mismo efecto pero se establece en tiempo de ejecución en lugar de
persistirse en `config.json`. En concreto, el script ejecuta `cfg['DRY_RUN'] = True` en memoria
inmediatamente después de cargar la configuración. Esto significa que un fichero de configuración con
`DRY_RUN=false` aún no puede causar eliminaciones mientras `AUDIT_MODE=true`. Usa `AUDIT_MODE` para
validar cambios de puntuación contra una biblioteca real sin tocar `DRY_RUN` en tu configuración
almacenada.

**Cuándo desactivarlo:** Pon `DRY_RUN=false` solo tras revisar el fichero de plan de al menos una
ejecución en modo simulación y confirmar que las decisiones de guardado/eliminación previstas son
correctas. Nunca desactives `AUDIT_MODE` — no está "activado" por defecto; desactívalo solo si
quieres intencionadamente que `DRY_RUN` vuelva a controlar el modo de acción.

---

### Capa 1: PASADA 0 — Comparación de instantáneas del preanálisis

**Función:** `refresh_plex_item()`, `_snapshot_media_metadata()`, `_snapshot_diff()`
**Clave(s) de configuración:** `PRE_ANALYZE_DUPLICATES` (por defecto: `false`), `ANALYZE_TIMEOUT_SECONDS` (por defecto: `60`)
**Fallo evitado:** Decisiones de puntuación basadas en metadatos obsoletos de códec, bitrate o
resolución cacheados en Plex de una importación, transcodificación o análisis interrumpido anterior.
**Cuándo se activa:** Solo cuando `PRE_ANALYZE_DUPLICATES=true`. Los grupos que fallan el análisis se
convierten en omitidos (stubs) y nunca llegan a la puntuación de la PASADA 1.

**Cómo funciona:**

1. Captura una instantánea de metadatos antes de llamar a `item.analyze()`.
2. Llama a `item.analyze()` para pedir a Plex que vuelva a leer los flujos del archivo.
3. Sondea vía `item.reload()` hasta que `updatedAt` cambie o transcurra `ANALYZE_TIMEOUT_SECONDS`.
4. Captura una segunda instantánea y compara las dos con `_snapshot_diff()`.
5. Emite uno de cuatro veredictos:

| Veredicto | Significado | Resultado |
|---|---|---|
| `sane_and_changed` | Los metadatos cambiaron tras analyze — demostrablemente frescos | El grupo pasa a la PASADA 1 |
| `sane_unchanged` | Metadatos válidos pero sin cambio detectado | El grupo continúa con una advertencia visible |
| `timeout` | `updatedAt` no cambió dentro de `ANALYZE_TIMEOUT_SECONDS` | El grupo se omite |
| `analyze_failed` | Excepción durante analyze o reload | El grupo se omite |

**Ambigüedad documentada — `sane_unchanged`:**
La llamada `analyze()` de Plex es asíncrona y no proporciona ninguna señal de finalización. Un
resultado `sane_unchanged` significa que `_item_metadata_sane()` pasó (bitrate y duración positivos,
códec conocido) pero `_snapshot_diff()` no devolvió campos cambiados en ninguno de los sondeos.
Esto es irreduciblemente ambiguo vía la API de Plex: podría significar que los metadatos del archivo
ya eran correctos y analyze no encontró nada que actualizar (seguro proceder), o podría significar
que analyze se encoló pero aún no se procesó dentro de la ventana de sondeo (la puntuación puede
usar datos obsoletos). El script acepta `sane_unchanged` en lugar de rechazarlo, porque rechazarlo
omitiría todos los grupos de una biblioteca sana. La ambigüedad se registra en
`run_report['phases']['pass0']['note']`.

**Cuándo desactivarlo:** Deja `PRE_ANALYZE_DUPLICATES=false` (el valor por defecto) para la mayoría
de las ejecuciones. Actívalo cuando sospeches que Plex tiene metadatos obsoletos tras una
transcodificación de Tdarr o una importación fallida — es lento en bibliotecas grandes.

---

### Capa 2: Validación del sistema de archivos

**Función:** `check_file_exists()`
**Clave(s) de configuración:** `REQUIRE_LOCAL_FS_ACCESS` (por defecto: `false`)
**Fallo evitado:** Eliminar un archivo real y accesible porque los metadatos de Plex apuntaban a una
entrada fantasma que casualmente puntuó más alto.

**El bug de metadatos obsoletos que esta capa fue escrita para corregir:**

El almacén de metadatos de Plex puede reportar `part.exists=True` y `part.accessible=True` mucho
tiempo después de que un archivo haya sido movido, borrado, o esté en un recurso de red desmontado.
En el código original anterior, la puntuación usaba solo las banderas de Plex. Si una entrada
fantasma obsoleta acumulaba una puntuación mayor que el archivo real —posible cuando Plex había
cacheado metadatos de alta calidad para un archivo que ya no existía— el script emitiría una DELETE
de Plex para el archivo real dejando intacta la entrada fantasma. El resultado era pérdida de datos
más una entrada de biblioteca rota.

**La corrección:**

`check_file_exists()` hace del sistema de archivos local la fuente autoritativa:

- Si tanto Plex como el sistema de archivos local son alcanzables, **ambos deben coincidir** en que
  el archivo existe. Cualquier desacuerdo → el archivo se trata como `MISSING` (AUSENTE).
- Si solo el sistema de archivos local es alcanzable (banderas de Plex no disponibles), se usa el veredicto del sistema de archivos.
- Si solo Plex reporta (el sistema de archivos no es alcanzable desde este host), se usan las banderas de Plex pero se registra
  la limitación explícitamente.
- Si ninguna fuente tiene información, se asume `MISSING` por seguridad.

Cualquier elemento de medio donde `check_file_exists()` devuelve `exists=False` se excluye de la
candidatura a guardado en `select_keeper()`. Si no queda ningún candidato existente, el grupo se omite.

`REQUIRE_LOCAL_FS_ACCESS=true` añade un requisito más estricto: si ninguna ruta de archivo del grupo
es alcanzable vía `os.path.exists()` en este host, se omite todo el grupo. Úsalo cuando el script no
se ejecuta en el servidor de Plex y no quieres confiar en los reportes de existencia solo de Plex
para ningún grupo.

**Cuándo desactivarlo:** No lo desactives. `REQUIRE_LOCAL_FS_ACCESS` es `false` por defecto porque
muchos despliegues ejecutan el script en el host de Plex donde todas las rutas son alcanzables;
ponlo en `true` solo si ejecutas el script en una máquina separada y quieres imponer la
alcanzabilidad local como prerrequisito.

---

### Capa 3: Enfriamiento por antigüedad de archivo

**Función:** `select_keeper()`
**Clave(s) de configuración:** `MIN_FILE_AGE_HOURS` (por defecto: `24`)
**Fallo evitado:** Actuar sobre un archivo que está en pleno proceso de importación, copia o
transcodificación — escenarios donde el archivo existe en Plex pero aún no está completo.

**Escenarios del mundo real que esto detecta:**

- Radarr o Sonarr acaban de descargar un archivo y todavía lo están copiando a su ubicación final.
- Tdarr ha comenzado a transcodificar un archivo y la salida está parcialmente escrita.
- Un escaneo de la biblioteca de Plex descubrió un archivo que apareció en el sistema de archivos hace milisegundos.
- Un usuario copió manualmente un archivo y Plex lo escaneó antes de que la copia terminara.

En todos estos casos el archivo puede tener metadatos de aspecto válido (pero incompletos) y una
puntuación razonable. El enfriamiento por antigüedad proporciona un margen temporal: si el archivo
más reciente de un grupo tiene menos de `MIN_FILE_AGE_HOURS` (medido vía el `mtime` del sistema de
archivos), se omite todo el grupo con un `skip_reason` de la forma:

```
cooldown: '<path>' is X.XXh old, below threshold Y.YYh
```

**Cuándo desactivarlo:** Pon `MIN_FILE_AGE_HOURS=0` solo si tu biblioteca está totalmente asentada y
no se ejecuta ninguna automatización de importación. En una configuración activa de Radarr/Sonarr,
el valor por defecto de 24 horas es una elección conservadora pero segura.

---

### Capa 4: Validez de metadatos

**Función:** `has_sane_metadata()`
**Clave(s) de configuración:** Ninguna — siempre activa para todos los candidatos existentes.
**Fallo evitado:** Seleccionar un guardado (o puntuar un candidato) basándose en metadatos de Plex de
relleno o corruptos que se escribieron durante o inmediatamente después de un escaneo, antes de que
Plex haya terminado de analizar el archivo.

**Qué constituye metadatos no válidos:**

| Campo | Condición no válida |
|---|---|
| `video_duration` | `<= 0` |
| `video_bitrate` | `<= 0` |
| `video_codec` | cadena vacía o `"unknown"` |

Plex a veces escribe entradas con bitrate cero o duración cero como relleno durante un análisis en
curso. Un archivo que parece un clip de 0 segundos con 0 Kbps de bitrate y un códec desconocido
todavía podría puntuar de forma no trivial por resolución, nombre de archivo y señales HDR. Sin
esta comprobación, ese relleno podría convertirse en el guardado nominado.

Un candidato con metadatos no válidos no puede seleccionarse como guardado. Si tras la exclusión no
queda ningún candidato válido, el grupo se omite. El `skip_reason` tiene la forma:

```
candidate <id> has invalid metadata: <reason> (Plex analysis may be incomplete)
```

**Cuándo desactivarlo:** Esta comprobación no es configurable. Siempre está activa.

---

### Capa 5: Umbral de puntuación

**Función:** `select_keeper()`
**Clave(s) de configuración:** `MIN_SCORE_DIFFERENCE` (por defecto: `0`, recomendado: `>= 1000`)
**Fallo evitado:** Eliminar un archivo cuando dos candidatos están efectivamente empatados en
calidad — una situación en la que el modelo de puntuación no puede distinguirlos con confianza.

El sistema de puntuación suma muchos componentes (códec, resolución, patrón de nombre de archivo,
bitrate, dimensiones, HDR, canales de audio y más). Cuando dos candidatos están cerca en calidad —
por ejemplo, dos codificaciones 1080p H.264 de distintas fuentes con bitrates similares— la
diferencia de puntuación puede deberse enteramente a pequeñas diferencias de nombre de archivo o
efectos de redondeo en lugar de a una distinción de calidad significativa.

`MIN_SCORE_DIFFERENCE` establece una diferencia mínima requerida entre el candidato con mayor
puntuación y el segundo. Si `top_score - second_score < MIN_SCORE_DIFFERENCE` (y el umbral es
distinto de cero), el grupo se omite con:

```
score delta N below threshold M
```

**El valor por defecto es 0 — esto es agresivo.** Con un umbral de 0, cualquier diferencia de
puntuación distinta de cero es suficiente para actuar. Para la mayoría de las bibliotecas, un umbral
de 1000–5000 proporciona un margen significativo. Un umbral de alrededor de 10000 requiere al menos
un nivel completo de resolución de separación entre candidatos (p.ej. 720p vs 1080p).

**Cuándo desactivarlo:** Poner `MIN_SCORE_DIFFERENCE=0` desactiva la comprobación. Hazlo solo si has
revisado tu configuración de puntuación a fondo y confías en que cualquier diferencia de puntuación
distinta de cero refleja una diferencia de calidad real.

---

### Capa 6: Protección por ratio de tamaño

**Función:** `select_keeper()`
**Clave(s) de configuración:** `MAX_SIZE_RATIO` (por defecto: `5.0`)
**Fallo evitado:** Que una codificación pequeña y eficiente gane sobre un archivo grande y de alta
calidad debido a un caso límite de la puntuación — por ejemplo, una codificación HEVC de 2 GB
superando a un Remux de 80 GB en códec y resolución cuando el Remux es una fuente de calidad muy
superior.

El modelo de puntuación prefiere fuertemente los códecs modernos eficientes (HEVC, AV1) y las
resoluciones altas. En algunas configuraciones esto puede hacer que una transcodificación HEVC
eficiente supere a un Remux del mismo contenido porque las bonificaciones de códec y nombre de
archivo dominan. La comprobación de ratio de tamaño proporciona una última verja de cordura: aunque
la puntuación favorezca claramente a un candidato, un hermano que sea más de `MAX_SIZE_RATIO` veces
mayor que el guardado seleccionado es una señal de que el emparejamiento puede ser una anomalía de
puntuación en lugar de una comparación de calidad genuina.

Cuando cualquier candidato no-guardado es más de `MAX_SIZE_RATIO` veces mayor que el guardado, el
grupo se omite con:

```
size ratio N.Nx exceeds threshold M.Mx (keeper=X bytes, sibling id=<id>=Y bytes)
```

La comprobación solo se aplica cuando ambos tamaños de archivo son distintos de cero.
`MIN_SCORE_DIFFERENCE` se evalúa primero; `MAX_SIZE_RATIO` solo se alcanza si la diferencia de
puntuación ya está por encima del umbral.

**Cuándo desactivarlo:** Pon `MAX_SIZE_RATIO=0` para desactivarlo. Considera desactivarlo solo si tu
biblioteca contiene disparidades de tamaño intencionadas — por ejemplo, una biblioteca de Remux
emparejada con una biblioteca de copias comprimidas — y has confirmado que tu configuración de
puntuación las gestiona correctamente.

---

### Capa 7: Comparación de revalidación de la PASADA 2

**Función:** `detect_inconsistencies()`
**Clave(s) de configuración:** Ninguna — siempre activa. `PARTIAL_HASH_ENABLED` (por defecto:
`false`) habilita una comprobación adicional a nivel de hash dentro de esta capa.
**Fallo evitado:** Condiciones de carrera entre la PASADA 1 (descubrimiento) y la PASADA 2 (acción) —
un archivo fue reemplazado, movido, recodificado o modificado en la ventana entre las dos pasadas.

**El problema que esto aborda:**

La PASADA 1 y la PASADA 2 están separadas en el tiempo. Entre ellas, Tdarr puede haber terminado una
transcodificación (cambiando códec y bitrate), Radarr puede haber mejorado un archivo (cambiando
ruta y tamaño), un usuario puede haber reorganizado manualmente su biblioteca, o Plex puede haber
reanalizado un archivo y actualizado sus metadatos. Actuar sobre la instantánea de la PASADA 1 en
cualquiera de estas situaciones podría eliminar el archivo equivocado.

**Campos comparados entre la instantánea de la PASADA 1 y la lectura fresca de la PASADA 2:**

| Campo | Notas |
|---|---|
| Pertenencia al conjunto de medios | Detecta elementos de medio nuevos o desaparecidos |
| Rutas de archivo | Por elemento de medio |
| Tamaño de archivo | Por elemento de medio |
| Bandera `exists` | Por elemento de medio |
| `video_duration` | Por elemento de medio |
| `video_bitrate` | Por elemento de medio |
| `video_codec` | Por elemento de medio |
| Hash parcial | Por parte, solo cuando `PARTIAL_HASH_ENABLED=true` |
| Selección de guardado | Si el `select_keeper()` fresco elige un guardado diferente o ahora quiere omitir |

Cualquier lista de diferencias no vacía provoca que el grupo se omita. Se imprimen hasta seis
diferencias en stdout; la lista completa se registra en el reporte de la ejecución.

`PARTIAL_HASH_ENABLED=true` calcula un SHA-256 de los primeros y últimos `PARTIAL_HASH_BYTES`
(por defecto 1 MiB) de cada archivo durante ambas pasadas. Cualquier diferencia de hash —indicando
que el contenido del archivo cambió— provoca que el grupo se omita. Esto detecta transcodificaciones
in situ y sobrescrituras parciales que no cambian la ruta o el tamaño del archivo de forma visible
dentro de la ventana de sondeo.

**Cuándo desactivarlo:** Esta capa no es configurable. Habilita `PARTIAL_HASH_ENABLED` para
protección adicional en entornos donde la modificación de archivos in situ es posible.

---

### Capa 8: Comprobación de estabilidad

**Función:** `is_files_stable()`
**Clave(s) de configuración:** `STABILITY_CHECK_SECONDS` (por defecto: `2.0`)
**Fallo evitado:** Tdarr, una operación de copia, o cualquier otro proceso escribiendo activamente en
un archivo candidato en el momento exacto de la acción — una ventana que la comparación de
revalidación de la Capa 7 puede no detectar si la modificación comenzó tras la obtención de la PASADA 2.

**Cómo funciona:**

Inmediatamente antes de llamar a `remove_item()`, el script lee el tamaño en disco de cada archivo
candidato, espera `STABILITY_CHECK_SECONDS` y vuelve a leer los tamaños. Cualquier cambio de tamaño
entre las dos lecturas provoca que se omita todo el grupo. Los archivos que no se pueden leer en
absoluto se dejan a la Capa 2 (validación del sistema de archivos) para gestionarlos.

Esta es la última línea de defensa antes de que cualquier archivo se mueva a cuarentena. Para cuando
esta comprobación se ejecuta, el grupo ya ha pasado la puntuación de descubrimiento, la revalidación
y la comparación completa — esta capa solo detecta el caso estrecho en el que una escritura comenzó
en los últimos segundos antes de la acción.

**Cuándo desactivarlo:** Pon `STABILITY_CHECK_SECONDS=0` para desactivarlo. El valor por defecto de 2
segundos añade un tiempo insignificante a una ejecución. Desactivarlo solo es apropiado si todos los
archivos candidatos están en almacenamiento de solo lectura o inmutable donde las escrituras en
curso son estructuralmente imposibles.

---

### Capa 9: Cuarentena

**Función:** `quarantine_files()`, `_write_quarantine_sidecar()`
**Clave(s) de configuración:** `QUARANTINE_MODE` (por defecto: `true`), `QUARANTINE_DIR` (requerido cuando está activo), `QUARANTINE_RETENTION_DAYS` (por defecto: `30`, solo informativo)
**Fallo evitado:** Pérdida de datos permanente e irrecuperable.

Esta no es una capa de detección — es una **capa de recuperación**. Cada capa anterior decide si
actuar; esta capa determina qué significa "actuar".

Cuando `QUARANTINE_MODE=true`, los archivos nunca se borran de forma definitiva. En su lugar:

1. `quarantine_files()` mueve cada archivo eliminado a `QUARANTINE_DIR` usando
   `_quarantine_logical_path()` para reconstruir una estructura de directorios con sentido anclada en
   el componente de directorio del título.
2. Una gestión de colisiones de tres pasos garantiza que ningún archivo en cuarentena se sobrescriba:
   - Primer intento: ruta lógica simple bajo `QUARANTINE_DIR`.
   - Si esa ruta existe: añade `__<LIBRARY_NAME>` a la carpeta de nivel superior.
   - Si esa también existe: añade `__<unix_timestamp>` a la raíz del nombre de archivo.
3. Se escribe un fichero adjunto `.dupefinder_meta.json` junto a cada archivo movido. Contiene:
   - `original_path` y `quarantine_path`
   - `run_id`, `media_id`, `reason`
   - `original_size` y `original_mtime` en el momento de la cuarentena
   - `keeper.files`, `keeper.score`, `keeper.score_breakdown`
   - `restore_command`: un comando de shell listo para ejecutar (`mv '<quarantine_path>' '<original_path>'`)

**Para restaurar un archivo en cuarentena:** abre el fichero adjunto `.dupefinder_meta.json`, copia
el valor de `restore_command` y ejecútalo en una shell. No se necesita ningún script.

Si todos los archivos de un grupo fallan al ponerse en cuarentena (errores de movimiento en cada
archivo), `remove_item()` retorna sin llamar a `remove_plex_metadata()` — la entrada de Plex se
preserva para que la biblioteca permanezca coherente con los archivos (no movidos).

`QUARANTINE_RETENTION_DAYS` es un campo informativo registrado en la documentación y en el fichero
adjunto para referencia del operador. El script no impone una purga automática; la retención es
responsabilidad del operador.

**Cuándo desactivarlo:** Pon `QUARANTINE_MODE=false` solo cuando operes en modo
`FIND_DUPLICATE_FILEPATHS_ONLY` (rutas de archivo idénticas, limpieza solo de metadatos) o cuando el
almacenamiento de cuarentena no esté genuinamente disponible. Cuando está desactivado, `remove_item()`
llama únicamente a `remove_plex_metadata()`, que emite la DELETE de medios de Plex. Con **Allow media
deletion** activado en Plex (requerido para que el script funcione en absoluto), Plex **elimina
permanentemente el archivo subyacente del disco** — no hay fichero adjunto ni ruta de restauración;
la recuperación es imposible vía este script. La única excepción es el modo
`FIND_DUPLICATE_FILEPATHS_ONLY`: como cada entrada apunta al mismo archivo físico, solo se limpia el
metadato redundante de Plex y el archivo se deja en su sitio.

---

## Limitaciones documentadas

El modelo de seguridad es intencionadamente conservador, pero tiene límites que los operadores
deberían entender.

### Ambigüedad del analyze asíncrono de Plex

`PRE_ANALYZE_DUPLICATES=true` no puede confirmar de forma fiable que Plex haya reanalizado un archivo.
La llamada a la API `analyze()` es asíncrona y Plex no proporciona ninguna señal de finalización. Un
veredicto `sane_unchanged` —que significa que los metadatos eran válidos pero no cambiaron tras
analyze— es irreduciblemente ambiguo: Plex puede no haber encontrado nada que actualizar
(comportamiento correcto en un archivo sano) o puede no haber procesado la petición de analyze dentro
de la ventana de sondeo. El script procede con `sane_unchanged` con una advertencia visible porque
rechazarlo omitiría todos los grupos de una biblioteca bien mantenida.

### Rutas de red sin acceso al sistema de archivos local

Cuando `REQUIRE_LOCAL_FS_ACCESS=false` (el valor por defecto) y el script se ejecuta en una máquina
que no puede alcanzar las rutas de medios de Plex vía `os.path.exists()`, la Capa 2 recurre a las
banderas `exists` y `accessible` de Plex. Estas banderas pueden estar obsoletas. El bug de metadatos
obsoletos que la Capa 2 fue escrita para corregir puede resurgir en esta topología de despliegue. Si
ejecutas el script fuera del host, pon `REQUIRE_LOCAL_FS_ACCESS=true` para omitir cualquier grupo
donde ningún archivo sea alcanzable localmente, o monta las rutas de medios en el host del script
antes de ejecutarlo.

### Duplicados entre bibliotecas

Plex reporta duplicados por sección de biblioteca. Una película presente tanto en una biblioteca
"Movies" como en una biblioteca "4K Movies" no aparecerá en el mismo grupo de duplicados — el script
nunca los verá como duplicados entre sí. Usa `SKIP_LIST` para proteger las bibliotecas que no quieras
gestionar, o escanea cada biblioteca de forma independiente con configuraciones de puntuación
apropiadas.

### Mantenimiento de SKIP_LIST

El script no puede determinar automáticamente qué directorios o rutas de archivo deberían protegerse.
`SKIP_LIST` es una lista de coincidencia por subcadena contra las rutas de archivo, mantenida
enteramente por el operador. Los archivos en directorios no cubiertos por `SKIP_LIST` son elegibles
para eliminación. Revisa y actualiza `SKIP_LIST` siempre que cambie la estructura de la biblioteca.

---

## Desactivar capas — Resumen de riesgos

| Capa | Clave de configuración para desactivar | Riesgo si se desactiva |
|---|---|---|
| L0: DRY_RUN | `DRY_RUN=false` | Habilita movimientos reales de archivos y DELETEs de Plex — intencionado, pero debe ser deliberado |
| L0: AUDIT_MODE | `AUDIT_MODE=false` (por defecto) | `DRY_RUN` en config.json se convierte en el único control; se elimina la anulación en tiempo de ejecución |
| L1: Instantánea del preanálisis | `PRE_ANALYZE_DUPLICATES=false` (por defecto) | La puntuación procede con los metadatos que Plex tenga actualmente; los metadatos obsoletos no se detectan |
| L2: Validación del sistema de archivos | No configurable (lógica central) | No se puede desactivar por completo; `REQUIRE_LOCAL_FS_ACCESS=false` permite reportes de existencia solo de Plex en despliegues fuera del host |
| L3: Enfriamiento por antigüedad | `MIN_FILE_AGE_HOURS=0` | Los archivos en pleno proceso de importación y transcodificación pasan a ser elegibles para la acción |
| L4: Validez de metadatos | No configurable | No se puede desactivar |
| L5: Umbral de puntuación | `MIN_SCORE_DIFFERENCE=0` (por defecto) | Cualquier diferencia de puntuación distinta de cero es suficiente para actuar; los cuasi-empates no están protegidos |
| L6: Ratio de tamaño | `MAX_SIZE_RATIO=0` | Los hermanos de gran tamaño de archivos pequeños con puntuación alta pasan a ser elegibles para eliminación |
| L7: Comparación de revalidación | No configurable (siempre activa); `PARTIAL_HASH_ENABLED=false` desactiva la subcomprobación de hash | Sin la comprobación de hash, los cambios de contenido in situ que preservan tamaño y ruta no se detectan |
| L8: Comprobación de estabilidad | `STABILITY_CHECK_SECONDS=0` | Los archivos que se están escribiendo activamente en el momento de la acción no se detectan |
| L9: Cuarentena | `QUARANTINE_MODE=false` | La eliminación de archivos pasa a ser permanente e irrecuperable vía este script; sin fichero adjunto ni ruta de restauración |
