# Referencia de puntuación

[English](SCORING.md) | **Español**

## Filosofía

La puntuación determina qué duplicado **conservar** — el candidato con la mayor puntuación total es
el guardado, y todos los demás son candidatos a eliminación. Cuando dos candidatos puntúan dentro de
`MIN_SCORE_DIFFERENCE` el uno del otro, se omite todo el grupo en lugar de adivinar cuál es mejor.

**Las características reales del medio dominan; el nombre solo desempata.** Resolución, códec de
vídeo, HDR/Dolby Vision, audio y (una pequeña parte de) bitrate deciden el ganador. El **source** del
release (REMUX/BluRay/WEB-DL/…) es una dimensión dedicada de primera clase, y los patrones de nombre
restantes (contenedor/edición) son desempates acotados que no pueden anular una señal real de calidad.

Cada componente se registra en un desglose por componente almacenado en el fichero de plan, el
reporte JSON y el fichero adjunto de cuarentena, para que siempre puedas auditar por qué se conservó
o eliminó un archivo.

Orden de preferencia objetivo que produce este modelo:

```
2160p DV/HDR HEVC  >  2160p HEVC  >  1080p REMUX  >  1080p HEVC  >  1080p AVC  >  720p AVC
```

La resolución domina entre tiers; dentro de un tier de resolución, gana el REMUX.

---

## Componentes de la puntuación

### Resolución de vídeo (dominante)

Clave: `VIDEO_RESOLUTION_SCORES`

| Resolución | Puntuación |
|---|---|
| `4k` | 20000 |
| `1080` | 10000 |
| `720` | 5000 |
| `480` | 3000 |
| `sd` | 1000 |
| `Unknown` | 0 |

La diferencia de 10000 puntos entre 4K y 1080p es mayor que cualquier señal individual no-resolución
(el source REMUX vale 8000), de modo que una resolución mayor gana entre tiers salvo penalizaciones.

---

### Códec de vídeo

Clave: `VIDEO_CODEC_SCORES` (sin distinguir mayúsculas/minúsculas). Se incluyen alias para que el
nombre del encoder puntúe igual que el códec que produce.

| Códec | Puntuación | Alias |
|---|---|---|
| `av1` | 14000 | |
| `hevc` | 12000 | `h265`, `x265` |
| `h264` | 8000 | `x264`, `avc` |
| `vp9` | 6000 | |
| `Unknown` | 0 | |
| `mpeg4` | -3000 | |
| `vc1` | -2000 | |
| `mpeg1video` / `mpeg2video` | -5000 | |
| `wmv2` / `wmv3` / `msmpeg4*` | -8000 | |

La diferencia HEVC→H264 (4000), combinada con el peso bajo del bitrate (más abajo), garantiza que un
**HEVC más eficiente gana a un AVC equivalente**, incluso cuando el AVC usa más bitrate.

> Plex reporta el *códec* (`hevc`, `h264`) en `videoCodec`, no el encoder (`x265`, `x264`). Los alias
> son por robustez; la puntuación de códec siempre viene de la metadata de Plex, nunca del nombre.

---

### Source (primera clase, valor único)

Clave: `SOURCE_SCORES`

Plex no expone un campo de "source", así que se parsea del nombre — pero como **valor único**: gana
el source de mayor calidad detectado, nunca se suman, y los tiers se prueban de mejor a peor (así
`BluRay.REMUX` puntúa como REMUX).

| Source | Puntuación | Detectado por (tokens / subcadenas, sin distinguir mayúsculas) |
|---|---|---|
| REMUX | 8000 | `remux`, `bdremux`, `brremux` |
| BluRay | 3000 | `bluray`, `blu ray`, `bdrip`, `brrip` |
| WEB-DL | 2000 | `web-dl`, `webdl` |
| WEBRip | 1000 | `webrip`, `web rip` |
| HDTV | -3000 | `hdtv`, `pdtv`, `hdrip`, `dsr` |
| DVD | -3000 | `dvdrip`, `dvd` |
| CAM | -15000 | `cam`, `hdcam`, `telesync`, `telecine`, `hdts` |
| (ninguno) | 0 | nombres limpios estilo filebot sin etiqueta de source |

REMUX (8000) está **por debajo** de la diferencia de resolución (10000) a propósito, para que un
2160p HEVC gane a un 1080p REMUX, mientras que un REMUX sigue ganando a un no-REMUX de la **misma**
resolución.

---

### Códec de audio

Clave: `AUDIO_CODEC_SCORES`

| Códec | Puntuación |
|---|---|
| `truehd` | 4500 |
| `dca-ma` | 4000 |
| `pcm` / `flac` | 2500 |
| `dca` | 2000 |
| `opus` | 1500 |
| `eac3` | 1250 |
| `aac` / `ac3` / `mp3` | 1000 |
| `mp2` | 500 |
| `wmapro` | 200 |
| `Unknown` | 0 |

### Canales de audio

`audio_channels × 1000`, donde **`audio_channels` es el número de canales de la pista más rica (MAX),
no la suma de todas las pistas**. Sumar era un bug: un release multi-dub (7.1 + 5.1 + 2.0 = 16
canales) puntuaría muy por encima de un equivalente de una sola pista. El premio por "más pistas" lo
gestiona aparte `AUDIO_TRACK_SCORE`.

### HDR y Dolby Vision

Claves: `HDR_SCORE` (3000), `DOLBY_VISION_SCORE` (5000). Detectados de la metadata de streams de Plex
(`colorTrc` = `smpte2084`/`arib-std-b67` para HDR; `DOVIPresent` para DV). Un fichero DV con metadata
HDR recibe ambos. Con el peso de bitrate reducido, producen de forma fiable **DV > HDR10 > SDR** a
igualdad de fuente.

### Pistas de subtítulos y audio

`subtitle_count × 50` y `audio_track_count × 100` — pequeños desempates por completitud.

---

### Patrones de nombre (solo desempate)

Claves: `FILENAME_SCORES`, `FILENAME_SCORE_CAP`

Tras sacar source y resolución, `FILENAME_SCORES` conserva solo señales de **contenedor y edición**.
Los patrones se comparan (`fnmatch` sin distinguir mayúsculas) contra el nombre base; las
coincidencias positivas se **suman y luego se acotan a `FILENAME_SCORE_CAP`** para que apilar no
domine. Las penalizaciones negativas de contenedores heredados son señales reales de calidad y **no**
se acotan.

| Patrón | Puntuación |
|---|---|
| `*.mkv` | 800 |
| `*.mp4` | 300 |
| `*REPACK*` / `*PROPER*` / `*EXTENDED*` | 500 c/u |
| `*.wmv` | -8000 |
| `*.ts` | -5000 |
| `*.avi` / `*.vob` / `*.flv` | -10000 |

`FILENAME_SCORE_CAP` por defecto: **2000**. (Las etiquetas de resolución y source no están aquí
deliberadamente — las puntúan `VIDEO_RESOLUTION_SCORES` y `SOURCE_SCORES`.)

---

### Bitrate (desempate pequeño)

Fórmula: `int(video_bitrate × BITRATE_SCORE_WEIGHT)`, peso por defecto **0.1**.

El bitrate correlaciona con la **ineficiencia** del códec tanto como con la calidad — un AVC necesita
mucho más bitrate que un HEVC para la misma calidad. Un peso alto premia al AVC inflado y le deja
ganar al HEVC, y deja que un SDR de alto bitrate gane al HDR. El peso se mantiene bajo para que el
bitrate solo separe candidatos por lo demás iguales. Ajústalo con `BITRATE_SCORE_WEIGHT`.

### Tamaño de archivo

Clave: `SCORE_FILESIZE` (por defecto `False`). Si se activa, `int(file_size / 100000)`. Desactivado
por defecto — el tamaño premia el "inflado", contrario a "máxima calidad por GB".

### Otras contribuciones menores

| Componente | Fórmula | Nota |
|---|---|---|
| Dimensiones de vídeo | `(width + height) × 2` | Refuerza la resolución (en gran parte redundante); constante dentro de un tier |
| Duración de vídeo | `int(video_duration / 300)` | Casi idéntica entre duplicados del mismo título, así que se cancela |

---

## Desglose de puntuación

`get_score()` devuelve `(int, dict)`. Claves presentes cuando son distintas de cero (más los
componentes base siempre presentes):

| Clave | Contenido |
|---|---|
| `resolution`, `video_codec`, `audio_codec` | búsquedas en tabla |
| `source` | valor de `SOURCE_SCORES` del source detectado (valor único) |
| `source_type` | clave del source detectado (p.ej. `remux`) cuando hubo coincidencia |
| `filename` | suma de `FILENAME_SCORES`, parte positiva acotada a `FILENAME_SCORE_CAP` |
| `filename_matches` | lista de `{pattern, score}` que coincidieron |
| `bitrate` | `int(video_bitrate × BITRATE_SCORE_WEIGHT)` |
| `audio_channels` | `canales_pista_max × 1000` |
| `dimensions`, `duration` | como arriba |
| `hdr` / `dolby_vision` | bonus cuando se detecta |
| `subtitle_tracks` / `audio_tracks` | cuando son distintos de cero |
| `file_size` | cuando `SCORE_FILESIZE=True` |

---

## Ejemplos de decisiones (modelo nuevo)

### 2160p HEVC HDR WEB-DL  vs  1080p REMUX (AVC, TrueHD 7.1)

| Componente | 2160p HEVC HDR | 1080p REMUX |
|---|---|---|
| Resolución | 20000 | 10000 |
| Códec vídeo | 12000 (hevc) | 8000 (h264) |
| Source | 2000 (web-dl) | 8000 (remux) |
| HDR | 3000 | 0 |
| Códec audio | 1250 (eac3) | 4500 (truehd) |
| Canales audio | 6000 (5.1) | 8000 (7.1) |
| Bitrate (×0.1) | 1800 | 3000 |
| **Total** (incl. dims/duración) | **70850** | **60300** |

**Ganador: 2160p HEVC HDR.** La resolución domina; el REMUX no puede superar un tier completo de
resolución. (Con el modelo antiguo el REMUX 1080p ganaba por puntos de nombre — el comportamiento que
corrige esta reescritura.)

### 1080p HEVC 8 Mbps  vs  1080p AVC 25 Mbps (equivalentes por lo demás)

El HEVC gana por ~3000+ porque la ventaja de códec (4000) ya no se cancela con el mayor bitrate del
AVC (ahora ponderado ×0.1). El modelo antiguo daba al HEVC un margen frágil de ~250 puntos que un AVC
algo más alto volteaba.

### release scene  vs  fichero renombrado por filebot (media idéntico)

La diferencia cae de ~20000 (antiguo, dominado por el nombre) a la diferencia de un solo tier de
source (≤ unos pocos miles). Con `MIN_SCORE_DIFFERENCE ≥ 3000` el grupo se **omite** — el media
idéntico se deja intacto en lugar de borrarse por un tecnicismo del nombre.

---

## Recomendaciones de ajuste

| Objetivo | Recomendación |
|---|---|
| No actuar en cuasi-empates / media idéntico | `MIN_SCORE_DIFFERENCE = 3000` (≥ `FILENAME_SCORE_CAP` y los tiers de source menores) |
| HEVC-first / no premiar el inflado AVC | Mantén `BITRATE_SCORE_WEIGHT` bajo (0.1); no subas `h264` por encima de `hevc` |
| Preferencia 4K más fuerte | Aumenta el valor `4k` o `HDR_SCORE`/`DOLBY_VISION_SCORE` |
| Preferir REMUX más fuerte dentro del tier | Sube `SOURCE_SCORES["remux"]` (mantenlo **por debajo** de la diferencia 4k−1080 de 10000 para preservar el dominio de la resolución) |
| Validar antes de producción | Ejecuta `AUDIT_MODE=true` + `CONFIRM_BEFORE_ACTION=false`, luego `python tools/compare_plans.py plan_antiguo.json plan_nuevo.json` |
| Inspeccionar una decisión | Revisa el `score_breakdown` (incl. `source`/`source_type`) en el fichero de plan |
