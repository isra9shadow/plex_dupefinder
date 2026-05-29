# Referencia de puntuación

[English](SCORING.md) | **Español**

## Filosofía

La puntuación determina qué duplicado **conservar** — el candidato con la mayor puntuación total
se designa como el guardado, y todos los demás son candidatos a eliminación. Cuando dos candidatos
puntúan dentro de `MIN_SCORE_DIFFERENCE` el uno del otro, se omite todo el grupo en lugar de
adivinar qué archivo es realmente mejor. Este diseño conservador significa que los falsos negativos
(omitir un grupo y dejar los duplicados en su sitio) siempre se prefieren sobre los falsos positivos
(borrar el archivo equivocado). Cada componente de una puntuación se registra en un diccionario de
desglose por componente que se almacena en el fichero de plan, el reporte JSON y el fichero adjunto
de cuarentena — para que siempre puedas auditar exactamente por qué se conservó o eliminó un archivo.

---

## Componentes de la puntuación

### Códec de vídeo

El códec de vídeo es la señal de calidad más fuerte de la configuración por defecto. Los códecs
modernos y eficientes (AV1, HEVC) se premian; los códecs heredados o con pérdidas se penalizan con
puntuaciones negativas. HEVC y H265 son alias del mismo códec en Plex y comparten la misma puntuación.

Clave de configuración: `VIDEO_CODEC_SCORES` (dict, búsqueda sin distinguir mayúsculas/minúsculas)

| Códec | Puntuación |
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

Jerarquía: AV1 > HEVC/H265 > H264 > VP9 > Unknown > heredados/propietarios (negativos).
Los códecs heredados de Microsoft (WMV, MSMPEG4) reciben las mayores penalizaciones para garantizar
que nunca se prefieran sobre cualquier codificación moderna.

---

### Resolución de vídeo

Clave de configuración: `VIDEO_RESOLUTION_SCORES` (dict, búsqueda sin distinguir mayúsculas/minúsculas)

| Resolución | Puntuación |
|---|---|
| `4k` | 20000 |
| `1080` | 10000 |
| `720` | 5000 |
| `480` | 3000 |
| `sd` | 1000 |
| `Unknown` | 0 |

Jerarquía: 2160p/4K > 1080p > 720p > 480p > SD. La gran diferencia entre 4K (20000) y 1080p
(10000) significa que un archivo 4K vencerá a un archivo 1080p solo por resolución, salvo que otras
penalizaciones (códec, nombre de archivo) inviertan el resultado.

---

### Códec de audio

Clave de configuración: `AUDIO_CODEC_SCORES` (dict, búsqueda sin distinguir mayúsculas/minúsculas)

| Códec | Puntuación |
|---|---|
| `truehd` | 4500 |
| `dca-ma` | 4000 |
| `dca` | 2000 |
| `pcm` | 2500 |
| `flac` | 2500 |
| `opus` | 1500 |
| `eac3` | 1250 |
| `aac` | 1000 |
| `ac3` | 1000 |
| `mp3` | 1000 |
| `mp2` | 500 |
| `wmapro` | 200 |
| `Unknown` | 0 |

Jerarquía de audio sin pérdida/basado en objetos: TrueHD > DTS-MA > FLAC/PCM > DTS > EAC3/Atmos >
AAC/AC3/MP3 > MP2 > WMA Pro. Los códecs sin pérdida puntúan notablemente más alto que sus
equivalentes con pérdidas, pero las puntuaciones de códec de audio son menores en magnitud que las
de códec de vídeo y resolución, por lo que el audio es un desempate más que un factor decisivo.

---

### HDR y Dolby Vision

Claves de configuración: `HDR_SCORE` (por defecto `3000`), `DOLBY_VISION_SCORE` (por defecto `5000`)

Ambos se detectan inspeccionando los metadatos del flujo de vídeo de Plex — sin E/S adicional más
allá de lo que la API de Plex ya proporciona. El HDR se identifica por valores de `colorTrc`
`smpte2084` o `arib-std-b67`. El Dolby Vision se identifica por `DOVIPresent`.

| Señal | Bonificación de puntuación |
|---|---|
| HDR | +3000 |
| Dolby Vision | +5000 |

El Dolby Vision se trata como un superconjunto del HDR — un archivo DV que también tiene metadatos
HDR recibe ambas bonificaciones. Estas bonificaciones son lo bastante grandes para inclinar la
mayoría de las decisiones 1080p vs 4K HDR a favor de la versión HDR, pero no tanto como para anular
una decisión de remux vs. rip HDTV.

---

### Pistas de subtítulos y audio

Claves de configuración: `SUBTITLE_SCORE_PER_TRACK` (por defecto `50`), `AUDIO_TRACK_SCORE` (por defecto `100`)

| Señal | Fórmula |
|---|---|
| Pistas de subtítulos | `subtitle_count × 50` |
| Pistas de audio | `audio_track_count × 100` |

Más pistas indican un archivo más rico y completo. Un archivo con 5 idiomas de subtítulos y 3
pistas de audio contribuye `5×50 + 3×100 = 550` al total — una recompensa pequeña pero medible
por la completitud. Estas puntuaciones por pista son deliberadamente pequeñas para que actúen como
desempates en lugar de invertir una clara ventaja de códec o resolución.

---

### Patrones de nombre de archivo

Clave de configuración: `FILENAME_SCORES` (dict de patrón glob de `fnmatch` → puntuación entera)

Los patrones se comparan sin distinguir mayúsculas/minúsculas contra el **nombre base** de cada ruta
de archivo usando `fnmatch`. Varios patrones pueden coincidir con el mismo archivo; sus puntuaciones
se suman. Esto permite que el nombre de archivo codifique señales de fuente y calidad que no están
directamente disponibles en los metadatos de Plex — por ejemplo, `*Remux*` identifica de forma
fiable los archivos remux sin pérdida, y `*HDTV*` identifica capturas de TV de menor calidad.

Ejemplo: `Movie.2021.1080p.BluRay.Remux.mkv` coincidiría tanto con `*Remux*` (+25000) como con
`*1080p*BluRay*` (+15000), contribuyendo +40000 a la puntuación total.

| Patrón | Puntuación |
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
| `*.mp4` | 500 |
| `*EXTENDED*` | 500 |
| `*DVDRip*` | -3000 |
| `*dvd*` | -3000 |
| `*HDTV*` | -5000 |
| `*TS*` | -5000 |
| `*.ts` | -5000 |
| `*.wmv` | -8000 |
| `*.avi` | -10000 |
| `*.vob` | -10000 |
| `*.flv` | -10000 |
| `*CAM*` | -20000 |

Las penalizaciones de contenedor (`.avi`, `.vob`, `.flv`) son negativas y lo bastante grandes para
anular la mayoría de las ventajas de códec y resolución. Las grabaciones CAM reciben la mayor
penalización de la tabla.

---

### Bitrate

Fórmula: `int(video_bitrate * 0.5)`

El bitrate se pondera al **0.5×** (la mitad del valor bruto) de forma intencionada. El bitrate bruto
premia las codificaciones grandes e ineficientes — un rip H264 inflado a 20 Mbps acumularía 20000
puntos solo por bitrate, superando potencialmente a un archivo HEVC bien codificado a 8 Mbps. Reducir
el peso a la mitad hace que el bitrate actúe como desempate entre candidatos por lo demás iguales en
lugar de como señal principal.

Combinado con `MAX_SIZE_RATIO`, este diseño impide que una codificación H264 grande supere a una
codificación HEVC eficiente del mismo contenido.

---

### Tamaño de archivo

Clave de configuración: `SCORE_FILESIZE` (por defecto `False`)

Fórmula cuando está habilitada: `int(file_size / 100000)`

**Por defecto está desactivada.** El tamaño de archivo es un indicador del bitrate, y el bitrate ya
se puntúa al 0.5×. Habilitar `SCORE_FILESIZE` premiaría aún más a los archivos grandes incluso cuando
su tamaño es producto de la ineficiencia del códec y no de la calidad. Para bibliotecas modernas y
eficientes en almacenamiento, el códec, la resolución y las señales del nombre de archivo son
indicadores de calidad más fiables.

Cuándo habilitarla: si quieres romper empates de forma consistente a favor de archivos más grandes —
por ejemplo, en una biblioteca donde todos los archivos usan el mismo códec y resolución, y más
grande significa que se preservaron más datos de la fuente.

---

### Otras contribuciones menores

Estos componentes producen puntuaciones pequeñas que actúan como desempates entre candidatos por lo
demás casi iguales.

| Componente | Fórmula | Justificación |
|---|---|---|
| Dimensiones de vídeo | `video_width × 2 + video_height × 2` | Premia las dimensiones reales en píxeles más allá de la etiqueta de resolución; un archivo 1920×1080 vence a uno 1280×720 dentro del mismo grupo `1080` |
| Duración de vídeo | `int(video_duration / 300)` | Ligera bonificación para versiones más largas o completas; ayuda a identificar archivos truncados o cortados |
| Canales de audio | `audio_channels × 1000` | El audio envolvente (6–8 canales) se prefiere fuertemente sobre el estéreo (2 canales); el audio 7.1 contribuye 8000 puntos |

---

## Desglose de puntuación

`get_score()` devuelve `(int, dict)`. El entero es la puntuación total. El diccionario contiene
un desglose por componente. Las claves solo están presentes cuando su contribución es distinta de
cero (excepto `audio_codec`, `video_codec`, `resolution`, `filename`, `bitrate`, `duration` y
`dimensions`, que siempre están presentes).

| Clave | Cuándo está presente | Contenido |
|---|---|---|
| `audio_codec` | Siempre | Puntuación de la búsqueda en `AUDIO_CODEC_SCORES` |
| `video_codec` | Siempre | Puntuación de la búsqueda en `VIDEO_CODEC_SCORES` |
| `resolution` | Siempre | Puntuación de la búsqueda en `VIDEO_RESOLUTION_SCORES` |
| `filename` | Siempre | Suma de todos los patrones coincidentes de `FILENAME_SCORES` |
| `filename_matches` | Cuando hay patrones coincidentes | Lista de `{'pattern': str, 'score': int}` |
| `bitrate` | Siempre | `int(video_bitrate * 0.5)` |
| `duration` | Siempre | `int(video_duration / 300)` |
| `dimensions` | Siempre | `video_width * 2 + video_height * 2` |
| `audio_channels` | Siempre | `audio_channels * 1000` |
| `hdr` | Cuando `has_hdr=True` | Valor de la clave de configuración `HDR_SCORE` |
| `dolby_vision` | Cuando `has_dv=True` | Valor de la clave de configuración `DOLBY_VISION_SCORE` |
| `subtitle_tracks` | Cuando es distinto de cero | `subtitle_count * SUBTITLE_SCORE_PER_TRACK` |
| `audio_tracks` | Cuando es distinto de cero | `audio_track_count * AUDIO_TRACK_SCORE` |
| `file_size` | Cuando `SCORE_FILESIZE=True` | `int(file_size / 100000)` |

Ejemplo de desglose para un archivo Remux 4K HDR:

```json
{
  "video_codec": 12000,
  "resolution": 20000,
  "audio_codec": 4500,
  "hdr": 3000,
  "filename": 25000,
  "filename_matches": [{"pattern": "*Remux*", "score": 25000}],
  "bitrate": 4200,
  "duration": 1440,
  "dimensions": 8560,
  "audio_channels": 8000,
  "audio_tracks": 100
}
```

---

## Ejemplos de decisiones de guardado

### Ejemplo 1: 1080p H264 WEB-DL vs 1080p HEVC BluRay Remux

| Componente | H264 WEB-DL | HEVC Remux |
|---|---|---|
| Códec de vídeo | 8000 (h264) | 12000 (hevc) |
| Resolución | 10000 (1080) | 10000 (1080) |
| Códec de audio | 1000 (aac) | 4500 (truehd) |
| Nombre de archivo | 12000 (`*1080p*WEB-DL*`) | 40000 (`*Remux*` + `*1080p*BluRay*`) |
| Bitrate | 3500 | 6000 |
| `*.mkv` | 2000 | 2000 |
| **Total** | **36500** | **74500** |

**Ganador: HEVC Remux.** La bonificación del nombre Remux (+25000) combinada con el patrón BluRay
(+15000) y la ventaja de códec producen un margen decisivo. Incluso sin las señales del nombre de
archivo, el audio TrueHD y el códec HEVC por sí solos añaden +7500 sobre el WEB-DL.

---

### Ejemplo 2: 720p HDTV vs 1080p HDR WEB-DL

| Componente | 720p HDTV | 1080p HDR WEB-DL |
|---|---|---|
| Códec de vídeo | 8000 (h264) | 12000 (hevc) |
| Resolución | 5000 (720) | 10000 (1080) |
| Códec de audio | 1000 (ac3) | 1250 (eac3) |
| Nombre de archivo | -5000 (`*HDTV*`) | 12000 (`*1080p*WEB-DL*`) |
| HDR | 0 | 3000 |
| Bitrate | 1500 | 2500 |
| **Total** | **10500** | **40750** |

**Ganador: 1080p HDR WEB-DL.** La penalización HDTV (-5000) combinada con la diferencia de
resolución y la bonificación HDR resulta en un margen de 30250 puntos. La bonificación HDR por sí
sola (+3000) supera la puntuación entera de muchos componentes de baja calidad.

---

### Ejemplo 3: Cuasi-empate (protección de MIN_SCORE_DIFFERENCE)

Dos archivos 1080p WEB-DL H264 de distintas fuentes con bitrates similares:

| Componente | Archivo A | Archivo B |
|---|---|---|
| Códec de vídeo | 8000 | 8000 |
| Resolución | 10000 | 10000 |
| Códec de audio | 1000 | 1250 |
| Nombre de archivo | 12000 | 12000 |
| Bitrate | 3200 | 3700 |
| `*.mkv` | 2000 | 2000 |
| **Total** | **36200** | **36950** |

Diferencia de puntuación: **750**

- Con `MIN_SCORE_DIFFERENCE=1000`: la diferencia (750) está por debajo del umbral — el grupo se
  **omite por completo**. Ambos archivos quedan intactos.
- Con `MIN_SCORE_DIFFERENCE=0`: se conserva el Archivo B (bitrate y códec de audio marginalmente
  superiores). Esto es de menor confianza — la diferencia puede ser ruido de los metadatos de Plex
  y no una diferencia de calidad real.

Establecer un `MIN_SCORE_DIFFERENCE` distinto de cero es la forma recomendada de exigir un ganador
claro antes de tomar cualquier acción.

---

## Recomendaciones de ajuste

| Objetivo | Recomendación |
|---|---|
| Exigir un ganador claro antes de actuar | Establece `MIN_SCORE_DIFFERENCE >= 1000` |
| Proteger contra la eliminación de archivos remux grandes | Mantén `MAX_SIZE_RATIO` en `5.0` o menos |
| Biblioteca centrada en 4K HDR | Aumenta `HDR_SCORE` (p.ej. 5000) y `DOLBY_VISION_SCORE` (p.ej. 8000) |
| Biblioteca solo MKV | El patrón `*.mkv` (+2000) ya gestiona los contenedores; no hace falta ningún cambio |
| Preferir fuertemente el audio sin pérdida | Aumenta `AUDIO_TRACK_SCORE` o añade un patrón personalizado para las convenciones de nombrado de códec de audio |
| Romper empates a favor de archivos más grandes | Habilita `SCORE_FILESIZE=True` solo tras verificar que tu biblioteca es coherente en códec |
| Evitar puntuar H264 inflado por encima de HEVC | No subas el valor de `h264` en VIDEO_CODEC_SCORES por encima de `hevc` |
| Validar la puntuación sin hacer cambios | Ejecuta con `AUDIT_MODE=True` — fuerza `DRY_RUN=True` en tiempo de ejecución sin modificar `config.json` |
| Inspeccionar qué puntuación recibió cada archivo | Revisa el fichero de plan en `plans/` tras una ejecución — el `score_breakdown` de cada candidato se registra allí |
