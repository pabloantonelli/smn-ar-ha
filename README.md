# SMN Argentina para Home Assistant

Integración de Home Assistant para el clima, pronóstico y alertas del
[Servicio Meteorológico Nacional](https://www.smn.gob.ar) de Argentina, más
un add-on que resuelve el acceso a su API no oficial.

## Por qué dos componentes

`smn.gob.ar` no tiene una API pública. Su frontend consume una API JSON no
documentada (`ws1.smn.gob.ar/v1`) protegida por **Cloudflare**: hace falta
un JWT que solo se puede obtener resolviendo el challenge de Cloudflare con
un navegador real. Eso no es viable embebido en el proceso de Home
Assistant, así que se separa en dos partes:

- **`addons/smn_proxy`** — add-on de Home Assistant OS (Chromium headless +
  Selenium) que resuelve el challenge, mantiene el JWT vigente, y expone un
  proxy HTTP local con la API del SMN.
- **`custom_components/smn_ar`** — la integración de HA (weather,
  binary_sensors de alertas, sensor de resumen en texto, cámara de radar),
  que habla con ese proxy local en vez de pegarle directo a SMN.

## Instalación

1. Instalar el add-on **SMN Proxy** desde Supervisor → Add-on Store → (⋮) →
   Repositorios → agregar `https://github.com/pabloantonelli/smn-ar-ha`.
   Iniciarlo y esperar a que `http://<host-del-addon>:6942/smn/health`
   devuelva `"session_ready": true` (puede tardar ~30-40s la primera vez,
   mientras resuelve el challenge de Cloudflare).
2. Instalar la integración **SMN - Servicio Meteorológico Nacional** vía
   HACS: HACS → (⋮) → Repositorios personalizados → pegar la misma URL,
   categoría **Integration** → buscarla e instalarla.
3. Reiniciar Home Assistant.
4. Ajustes → Dispositivos y Servicios → Agregar integración → "SMN", con la
   latitud/longitud deseada y la URL del proxy (por defecto
   `http://localhost:6942`, ajustar al hostname real del add-on si hace
   falta — se ve en la pestaña "Info" del add-on).

## Entidades que expone

Esta sección es la referencia completa de cada entidad — qué representa,
qué la enciende/actualiza, y qué hay en sus atributos. Se ve directo en
HA vía Ajustes → Dispositivos y Servicios → SMN → Documentación, o desde
HACS al ver el repositorio (`render_readme: true`).

**Sobre los nombres de entidad (`entity_id`)**: más abajo se listan por
plataforma y nombre visible, no por `entity_id` exacto — Home Assistant
genera el `entity_id` a partir del nombre traducido **en el momento en
que la entidad se crea por primera vez**, y no lo vuelve a cambiar
después aunque cambie el idioma. Según cuándo/con qué idioma activo
instalaste la integración, puede terminar siendo
`sensor.<algo>_short_term_forecast`, `sensor.<algo>_pronostico_de_corto_plazo`,
etc. Para encontrar el `entity_id` real de cualquiera de estas: Ajustes →
Herramientas de desarrollo → Estados, y filtrar por el nombre del
dispositivo o una palabra del nombre visible (ej. "corto plazo").

| Plataforma | Nombre visible | Qué es | Actualiza cada |
|---|---|---|---|
| `weather` | (el nombre que le pusiste al configurar) | Clima actual + pronóstico de 7 días | 30 min |
| `binary_sensor` | Alerta meteorológica | ¿Hay alguna alerta por evento activa hoy? | 30 min |
| `binary_sensor` ×11 | Alerta por tormenta / lluvia / nieve / viento / viento zonda / altas y bajas temperaturas / niebla / polvo / humo / ceniza volcánica | Una por tipo de evento | 30 min |
| `binary_sensor` | Alerta por granizo | ¿Algún aviso vigente menciona caída de granizo? | 10-30 min |
| `binary_sensor` | Alerta a corto plazo | **¿Tu ubicación exacta está dentro de una zona de alerta activa ahora?** | 10 min |
| `sensor` | Pronóstico de corto plazo | Resumen en una frase de la situación de corto plazo | 10 min |
| `sensor` | Avisos por provincia (país) | Avisos activos en todo el país, agrupados por provincia | 10 min |
| `sensor` | Temperatura | Temperatura actual, como entidad propia (separada de `weather`) | 30 min |
| `sensor` | Sensación térmica | Temperatura percibida actual | 30 min |
| `sensor` | Humedad | Humedad relativa actual (%) | 30 min |
| `sensor` | Velocidad del viento | Velocidad del viento actual (km/h) | 30 min |
| `sensor` | Dirección del viento | Dirección del viento actual, como punto cardinal (ej. "NE"); el valor en grados queda en el atributo `degrees` | 30 min |
| `sensor` | Pronóstico de hoy | Condición de hoy; máxima/mínima en los atributos `temp_max`/`temp_min` | 30 min |
| `sensor` | Pronóstico de mañana | Condición de mañana; máxima/mínima en los atributos `temp_max`/`temp_min` | 30 min |
| `sensor` | Próxima lluvia | Cuándo es el próximo período con probabilidad de lluvia relevante (timestamp) | 30 min |
| `camera` | Radar | Foto con radar de precipitación + zona de alerta + clima actual y próximas horas | 10 min |
| `camera` | Satélite | Foto satelital (GOES-East) alrededor de tu ubicación, con flecha de deriva de nubes | 10 min |
| `camera` | Satélite (animado) | GIF de la última hora de imágenes satelitales — pensado para compartir como **URL**, no como adjunto | 20 min |
| `camera` | Satélite Argentina | Foto satelital de todo el país, con su contorno dibujado | 15 min |
| `camera` | Satélite provincia | Foto satelital de tu provincia (según la resuelve SMN), con su contorno dibujado | 15 min |

### `weather`: clima actual y pronóstico

La entidad de clima estándar de HA. `state`/atributos: temperatura,
sensación térmica, humedad, presión, viento, visibilidad. Pestaña
"Pronóstico" (`async_forecast_daily`/`async_forecast_hourly`): 7 días,
con máxima/mínima diaria y, por franja horaria (madrugada/mañana/
tarde/noche), condición, temperatura, humedad, viento y probabilidad de
lluvia. Es el mismo pronóstico que muestra `smn.gob.ar` — la API no
ofrece nada "extendido" más allá de eso (se probaron endpoints
candidatos como `forecast/week`/`tendency`, ninguno existe).

### `binary_sensor` "Alerta por `<evento>`" (los 11 sensores por tipo)

Uno por cada tipo de evento del sistema de alerta temprana del SMN
(`warning/alert/location/{id}`). `on` (`Unsafe`) = ese evento tiene nivel
> 1 (amarillo/naranja/rojo) para hoy; `off` (`Safe`) = sin alerta de ese
tipo. "Alerta meteorológica" es el "resumen": `on` si cualquiera de los
11 está activo.

### `binary_sensor` "Alerta por granizo"

El SMN **no tiene un tipo de evento propio para granizo** en su sistema
de alertas (a diferencia de lluvia, viento, ceniza, polvo, etc., que sí
son eventos con su propio id en `warning/alert/location/{id}`) —
verificado contra un aviso real: la caída de granizo aparece únicamente
como texto libre dentro del evento "Tormenta" (ej. "...ocasional
granizo...") o en el título de un aviso a muy corto plazo (ej.
"TORMENTAS FUERTES CON LLUVIAS INTENSAS Y OCASIONAL CAIDA DE GRANIZO").
Este sensor busca la palabra "granizo" en esos dos textos. `on` = algún
aviso activo la menciona ahora mismo. Atributos: `match_count` y
`matching_texts` con el/los textos que hicieron match, para ver el
contexto exacto sin tener que ir a buscarlo en otro sensor.

### `binary_sensor` "Alerta a corto plazo": ¿estoy en zona de peligro?

**Esta es la respuesta directa a "¿mi ubicación está dentro de algún
polígono de alerta ahora mismo?"** Usa `warning/shortterm/location/{id}`,
que es el propio SMN haciendo ese cálculo (punto-dentro-del-polígono)
contra la lat/lon exacta configurada — no es una aproximación nuestra.

- `on` (`Unsafe`): tu ubicación está dentro de al menos un aviso a muy
  corto plazo vigente (tormenta/granizo/etc., validez 1-2h).
- `off` (`Safe`): no lo está — aunque haya avisos activos en otras zonas
  cercanas (para eso está "Avisos por provincia (país)", ver abajo).

Atributos cuando está `on`: `alert_count`, y `alerts` con el detalle
completo de cada aviso (título, vigencia, zonas, severidad, y
`instructions` — las medidas de protección, tal cual las publica SMN).

Usalo para automatizaciones tipo "avisame por notificación si la Alerta
a corto plazo pasa a `on`" — es la señal más precisa y específica a tu
ubicación que expone esta integración.

### `sensor` "Pronóstico de corto plazo": el aviso de SMN en texto

Es exactamente el mismo texto que muestra el propio mapa de SMN al hacer
clic en un aviso (ej. "TORMENTAS FUERTES CON LLUVIAS INTENSAS Y OCASIONAL
CAIDA DE GRANIZO", con zonas y hora de validez) — el `state` es ese
título, y el atributo `avisos_corto_plazo` trae el resto del detalle.
Sintetiza en una frase (para leer directo en un dashboard, una
notificación o un TTS) todo lo que ya exponen los binary_sensors de
alerta, con esta prioridad:

1. Si hay un **aviso a muy corto plazo** vigente (tormenta, granizo, etc. —
   validez 1-2h): el título del aviso, hasta qué hora es válido, y la
   primera zona afectada. El texto completo con **todas las medidas de
   protección** (el mismo que publica `smn.gob.ar`, campo `instructions`
   de la API — no hace falta scrapear la página, ya viene en el JSON) va
   en el atributo `instrucciones`, y el resto de los avisos activos en
   `avisos_corto_plazo`.
2. Si no, pero hay una **alerta por evento** activa hoy (tormenta, viento,
   etc., nivel > 1): qué evento y qué nivel (amarillo/naranja/rojo), con
   el detalle en el atributo `alertas_activas`.
3. Si no, pero hay **alerta de ola de calor/frío**: lo indica.
4. Si no hay nada de lo anterior: un resumen del pronóstico de hoy
   (condición, máxima/mínima).

### `sensor` "Avisos por provincia (país)": avisos en todo el país

A diferencia de todo lo anterior (específico a tu ubicación), este usa
`warning/shortterm/` **sin** filtro de ubicación — los avisos a muy corto
plazo vigentes en cualquier parte de Argentina en este momento. `state`:
cantidad total + provincias afectadas (ej. "3 avisos vigentes en
Córdoba, Santa Fe"). Atributo `por_provincia`: diccionario con el detalle
de cada aviso agrupado por provincia. Es la misma entidad que alimenta
los polígonos que dibuja la cámara de radar (ver abajo) — mismos datos,
dos formas de verlos (texto vs. mapa).

### `sensor` temperatura / sensación térmica / humedad / viento / pronóstico de hoy y mañana

Los mismos datos ya están en la entidad `weather` (como `native_temperature`,
`humidity`, `wind_speed`, `wind_bearing`, y en `async_forecast_daily`), pero
ahí sólo son atributos/forecast de una única entidad — no se pueden graficar
en una tarjeta de historial, usar en una condición simple de automatización,
ni exponer a Alexa/Google como un sensor suelto. Estos sensores exponen lo
mismo como entidades independientes:

- **Temperatura**, **Sensación térmica**, **Humedad**, **Velocidad del
  viento**: `state` es el valor actual, con su `device_class`/unidad
  correspondiente (se pueden graficar directo en Historial/Estadísticas de
  HA).
- **Dirección del viento**: `state` es el punto cardinal (ej. `"NE"`,
  `"SSO"`... en realidad se devuelve en inglés de 16 puntos, ej. `"NNE"`),
  más legible que un número en una tarjeta; el valor exacto en grados queda
  en el atributo `degrees` para quien lo necesite preciso.
- **Pronóstico de hoy** / **Pronóstico de mañana**: `state` es la condición
  (ej. "nublado"), con `temp_max`/`temp_min`/`date` como atributos —
  pensado para mostrar "mañana: nublado, 18°/9°" sin tener que leer el
  `forecast` completo de la entidad `weather`.

### `sensor` "Próxima lluvia"

SMN no publica un pronóstico realmente horario — son 4 períodos por día
(madrugada/mañana/tarde/noche). Este sensor recorre esos períodos (de hoy
en adelante) y devuelve el primero cuya probabilidad de lluvia llega a
30% o más (`NEXT_RAIN_PROBABILITY_THRESHOLD` en `const.py`, por si se
quiere un umbral distinto).

`state` es un **timestamp** (`device_class: timestamp`), no texto — así
Home Assistant lo muestra solo como "en 3 horas" / "mañana" en el
dashboard, y se puede usar directo en una condición de automatización
(ej. "disparar 30 min antes de este timestamp"). Atributos:
`rain_expected` (`true`/`false`), y si es `true`: `probability` (el % de
ese período) y `condition`. Si no hay ningún período con probabilidad
suficiente dentro del horizonte de pronóstico de SMN, `state` queda
`unknown` y `rain_expected` es `false` — no significa "no va a llover
nunca", sólo que no hay nada así de probable todavía en el pronóstico
disponible.

### Evento `smn_ar_shortterm_alert_changed`: avisos a muy corto plazo en tiempo real

Además del sensor "Pronóstico de corto plazo" (que hay que consultar o
mirar cuando cambia su `state`), la integración dispara un **evento de Home
Assistant** cada vez que cambia el conjunto de avisos a muy corto plazo
vigentes para tu ubicación — aparece uno nuevo, o se levanta uno existente.
Sirve para que una automatización reaccione al instante (ej. enviar una
notificación push) en vez de tener que sondear el sensor.

Se puede escuchar con un trigger de tipo **Evento**, evento
`smn_ar_shortterm_alert_changed`. Datos del evento:

```yaml
entry_id: "<id de esta instancia de la integración>"
added:        # avisos nuevos desde la última actualización (puede estar vacío)
  - title: "Aviso por tormentas fuertes"
    date: "2026-10-02T18:00:00"
    end_date: "2026-10-02T20:00:00"
    zones: ["CORDOBA: Capital, ..."]
    instructions: "..."
removed_count: 0   # cantidad de avisos que estaban vigentes y dejaron de estarlo
current:       # lista completa de avisos vigentes después del cambio
  - ...
```

No se dispara en el primer refresh tras un reinicio de HA (no hay un
"antes" con qué comparar todavía), así que no genera un aviso falso por
cada aviso que ya estaba activo cuando arrancó Home Assistant — sólo avisa
de cambios reales mientras la integración está corriendo.

Ejemplo de automatización (notificar sólo cuando aparece un aviso nuevo):

```yaml
trigger:
  - trigger: event
    event_type: smn_ar_shortterm_alert_changed
condition:
  - condition: template
    value_template: "{{ trigger.event.data.added | length > 0 }}"
action:
  - action: notify.mobile_app_tu_telefono
    data:
      title: "Nuevo aviso SMN"
      message: "{{ trigger.event.data.added[0].title }}"
```

### `camera` "Radar": foto del radar + clima

**No viene de SMN** — `mapa.smn.gob.ar` tiene su propio challenge de
Cloudflare que no se pudo resolver de forma confiable (detalle en
`addons/smn_proxy/README.md`), y el `robots.txt` del SMN pide
explícitamente que agentes tipo Claude no accedan al sitio. En su lugar,
esta cámara arma una foto (JPEG) con tiles de precipitación de la
[API pública de RainViewer](https://www.rainviewer.com/api.html) (gratis
para uso personal, sin API key, requiere solo atribución) compuestos sobre
un mapa base de [OpenStreetMap](https://www.openstreetmap.org/copyright)
(tiles estándar, sin key, con `User-Agent` identificando el proyecto) —
sin el mapa base, cuando no hay lluvia en la zona el radar es 100%
transparente y se ve como un cuadro en blanco, así que el mapa de fondo es
necesario para que se vea "un mapa" y no "nada". Atribución de ambas
fuentes incluida como `attribution` de la entidad. Solo cubre radar de
precipitación, no imagen satelital (RainViewer no la ofrece).

**Es una foto estática, no un GIF animado** (antes lo era). Se cambió
porque la mayoría de integraciones de notificación de terceros que
permiten adjuntar una `camera` (Telegram, bots de WhatsApp basados en
Baileys, etc.) asumen que una cámara de Home Assistant entrega una foto
fija, igual que el tipo de contenido por defecto de HA (`image/jpeg`) —
un GIF animado se descartaba silenciosamente en varias de ellas. Como
además la cobertura de RainViewer en Argentina es limitada (ver abajo),
la animación rara vez mostraba movimiento real, así que una sola imagen
con más información útil (temperatura, condición y pronóstico de las
próximas horas, dibujados arriba de la imagen) es mejor trade-off.

**Importante — cobertura de RainViewer en Argentina es limitada.** Se
verificó que su red de radares tiene huecos notorios en el país: hubo
tormentas reales sobre Córdoba (confirmadas por Windy, Meteored y el mapa
del propio SMN) que RainViewer no mostraba en absoluto. Por eso la cámara
también dibuja, **siempre**, la zona de alerta activa (si hay alguna) como
un polígono rojo con un ícono de tormenta y el texto del aviso (título +
hora de validez) — usando el campo `geometry` de `warning/shortterm`, la
misma data ya validada que usa "Pronóstico de corto plazo". A diferencia
del radar de RainViewer, esto es 100% confiable para Argentina porque
sale directo de la API del SMN, no de un agregador de terceros con
cobertura pareja a nivel mundial pero floja en esta región. Los avisos
dibujados son los que se superponen con el área visible del mapa (no
solo los que caen exactamente sobre tu punto — usa la misma data que
"Avisos por provincia (país)"), así que se ve cualquier zona de alerta
cercana aunque tu ubicación puntual no esté dentro del polígono.

**Zoom**: la grilla es de 5×5 tiles a zoom 9 (`RADAR_ZOOM`/`RADAR_TILE_GRID`
en `const.py`), cubriendo aproximadamente 300km alrededor de tu ubicación
— suficiente para ver tu ciudad y alrededores con detalle, más avisos en
zonas vecinas. Si querés más o menos zoom, se ajusta ahí.

**Rendimiento**: armar la imagen implica ~25 tiles de mapa + los del radar
— se piden todos en paralelo (no uno por uno), y el resultado se recalcula
**en segundo plano cada 10 minutos**, no recién cuando alguien pide la
imagen. Esto importa si pensás usar esta cámara para adjuntarla a una
notificación (`camera_entity` en un servicio `notify`, o
`camera.snapshot`): como ya está construida de antes, la respuesta es
prácticamente instantánea en vez de tardar varios segundos y arriesgarse a
un timeout del servicio de notificación.

El pronóstico de 7 días ya viene incluido en la entidad `weather` — se ve
en la pestaña "Pronóstico" de su diálogo de más información, o en
cualquier tarjeta de clima de Lovelace. No hace falta nada adicional para
tenerlo: es el mismo pronóstico diario que muestra `smn.gob.ar`, la API no
ofrece un pronóstico "extendido" separado (se probaron varios endpoints
candidatos — `forecast/week`, `tendency`, etc. — ninguno existe; 7 días es
el máximo que da el SMN).

### `camera` "Satélite" / "Satélite (animado)" / "Satélite Argentina" / "Satélite provincia"

Cuatro cámaras, todas con imágenes reales del satélite geoestacionario
**GOES-East** (el mismo que usa `mapa.smn.gob.ar`), servidas por
[**NASA GIBS**](https://www.earthdata.nasa.gov/gibs) — un servicio WMTS/XYZ
público y oficial (sin API key), a diferencia del CDN de NOAA STAR
(`cdn.star.nesdis.noaa.gov`), que solo publica un puñado de imágenes de
tamaño fijo sin georreferenciar, sin posibilidad real de zoom. GIBS
actualiza cada ~10 min. De día usa la capa GeoColor (color real); de noche
cae automáticamente a la capa de infrarrojo limpio (Banda 13), porque
GeoColor de noche es directamente una imagen negra.

- **Satélite**: foto estática, zoom fijo alrededor de tu ubicación (misma
  escala que la cámara de Radar, ~700km). Si hay una señal de movimiento de
  nubes confiable entre el frame actual y el anterior (correlación de fases
  entre las dos imágenes), dibuja una flecha amarilla indicando hacia dónde
  se están desplazando — es una aproximación visual del desplazamiento de
  la textura en la imagen, **no un dato de viento real**; con nubosidad muy
  uniforme/difusa (un día totalmente cubierto, por ejemplo) no encuentra
  señal confiable y no dibuja nada, a propósito, antes que inventar una
  dirección.
- **Satélite (animado)**: GIF con los últimos 6 frames (última hora).
  Actualiza cada 20 min (arma 6 mosaicos de tiles por vez, más caro que la
  foto estática). **Deliberadamente no pensada para adjuntarse** como foto
  de una notificación — varias integraciones de notificación asumen que una
  `camera` es JPEG y manejan mal un adjunto GIF (mismo motivo por el que la
  cámara de Radar dejó de ser animada, ver arriba). En cambio, se comparte
  como **URL** (ver la sección siguiente).
- **Satélite Argentina** / **Satélite provincia**: a diferencia de las dos
  anteriores (zoom fijo), estas calculan el zoom necesario para que entre
  **todo** el país o **toda** tu provincia en el cuadro, a partir del
  contorno real de cada una (ver más abajo). "Satélite provincia" usa la
  provincia que el propio SMN resuelve para tu ubicación configurada — si
  todavía no se resolvió o no matchea el dataset de contornos, la entidad
  simplemente no tiene imagen aún (no cae al mapa de todo el país, para no
  confundir bajo el nombre "provincia").

**Contorno dibujado (borde fino, blanco/gris claro)**: las cuatro cámaras
satelitales, y también la de Radar, dibujan un contorno de referencia
geográfica:

- **Radar** y **Satélite** (zoom fijo, sin control de zoom): siempre
  dibujan el contorno de **toda Argentina**, nunca el de una provincia —
  a esa escala fija (~700km) una provincia normalmente no entra completa
  en el cuadro o queda irreconocible, así que el país entero es la
  referencia que mejor funciona ahí.
- **Satélite Argentina**: contorno de Argentina, con el zoom ajustado para
  que el país entero entre en el cuadro.
- **Satélite provincia**: contorno de tu provincia, con el zoom ajustado
  para que la provincia entera entre en el cuadro — esta es la única de
  las cinco donde tiene sentido dibujar un límite provincial, porque es la
  única que efectivamente "hace zoom" a la escala de una provincia.

Los contornos vienen del dataset abierto ADM1/ADM0 de
[**geoBoundaries.org**](https://www.geoboundaries.org) (CC BY 4.0, fuente
IGN/Wikimedia), empaquetado localmente en
`custom_components/smn_ar/data/ar_provincias.geojson` (~107KB) — no
se consulta en vivo porque los límites provinciales no cambian.

**Subdivisiones internas (línea fina gris, sin halo)**: además del contorno
principal, "Satélite Argentina" dibuja las 24 provincias/CABA como líneas
internas de referencia, y "Satélite provincia" dibuja los departamentos (o
partidos, comunas, según cómo se llamen en cada provincia) de la provincia
que corresponda — generalizado igual que el resto: si tu ubicación está en
Buenos Aires, ves los partidos bonaerenses; en Mendoza, sus departamentos;
etc. Vienen del dataset ADM2 de geoBoundaries, empaquetado como
`data/ar_departamentos.geojson` (~450KB) con la provincia de cada
departamento resuelta con un cruce espacial offline (ADM2 no trae esa
relación). Quedan afuera unos pocos casos límite donde esa resolución no
dio un resultado confiable (las comunas de CABA, por una imprecisión en el
propio contorno de CABA del dataset) — no afecta el contorno principal,
sólo que esos no muestran subdivisión interna.

**Más cuadros y texto legible en cualquier tamaño**: las animaciones usan
ahora 10 cuadros (~100 min) en vez de 6; está limitado por cuánto tile
fetching es razonable hacer por refresh, no por disponibilidad de GIBS (que
en teoría permite ir bastante más atrás en el tiempo). Y como "Satélite
provincia"/"Satélite Argentina" arman una imagen mucho más grande (6×6
tiles) que "Satélite" (3×3 tiles), el texto del horario, el contador de
cuadros, la leyenda infrarroja y la etiqueta del pin escalan su tamaño en
proporción al tamaño real de cada imagen — antes tenían un tamaño de letra
fijo en píxeles, que se veía bien en una cámara y chico/apenas legible en
la otra.

#### Cómo obtener la URL del GIF animado para compartirlo

La cámara "Satélite (animado)" (y, en general, cualquier `camera` de Home
Assistant) expone su imagen actual en el atributo `entity_picture`, que ya
viene como una URL firmada por HA (el mismo mecanismo que usan las
tarjetas de cámara del dashboard — no hace falta login para acceder a
ella).

1. **Encontrar el `entity_id` exacto**: Ajustes → Herramientas de
   desarrollo → Estados, buscar "Satélite (animado)" (ver la nota sobre
   `entity_id` más arriba en este README si no aparece con ese nombre
   exacto).
2. **Ver la URL**: en esa misma pantalla, mirar el atributo
   `entity_picture` del estado — algo como
   `/api/camera_proxy/camera.<tu_nombre>_satelite_animado?token=xxxxxxxx`.
   Ese token es temporal y HA lo renueva solo; no hace falta (ni conviene)
   guardarlo como fijo en ningún lado.
3. **Convertirla en una URL completa y accesible desde afuera de tu red**:
   anteponer tu URL externa de Home Assistant (Ajustes → Sistema →
   General → "URL externa de Home Assistant", o tu dominio de Nabu Casa si
   la usás):
   ```
   https://tu-dominio-externo.com/api/camera_proxy/camera.<tu_nombre>_satelite_animado?token=xxxxxxxx
   ```
   Si HA no tiene una URL externa configurada (sin Nabu Casa, reverse
   proxy o port-forward), esa URL solo funciona dentro de tu red local —
   eso depende de tu configuración de HA, no de esta integración.

**Para automatizar el envío** (ej. mandarla por Telegram/WhatsApp cada
tanto), en una plantilla Jinja de una automatización:

```yaml
{{ state_attr('camera.<tu_nombre>_satelite_animado', 'entity_picture') }}
```

Esto da el *path* relativo (`/api/camera_proxy/...?token=...`); si el
servicio al que se la mandás necesita la URL absoluta, hay que
concatenarle el dominio externo a mano en la plantilla, ej.:

```yaml
{{ 'https://tu-dominio-externo.com' ~ state_attr('camera.<tu_nombre>_satelite_animado', 'entity_picture') }}
```

## Idioma: por qué a veces aparece en inglés

Los archivos de traducción (`translations/es.json`) ya cubren toda la
integración (entidades, servicios). Si las ves en inglés a pesar de tener
Home Assistant en español, es casi siempre porque **el idioma se resuelve
por perfil de usuario, no por el idioma general del sistema**: Perfil
(ícono abajo a la izquierda) → Idioma → Español. Un cambio ahí basta, sin
tocar nada de la integración.

## Qué significa "Safe" / "Unsafe" en los sensores de alerta

No es texto nuestro: es la traducción nativa de Home Assistant para el
`device_class: safety` de los binary sensors (así se llama en cualquier
integración que use esa clase, no solo esta). El significado:

- **Safe / Seguro** (`off`): no hay alerta activa de ese tipo.
- **Unsafe / Inseguro** (`on`): hay una alerta activa de ese tipo ahora
  mismo (por eso en la captura "Short-term alert", "Thunderstorm alert" y
  "Weather alert" aparecían en `Unsafe` — había tormenta activa).

Una vez corregido el idioma del perfil (ver arriba), Home Assistant lo
traduce solo a "Seguro"/"Inseguro" — es parte del core, no de esta
integración.

## Ícono de la integración

El placeholder gris "icon not available" que se ve en la tarjeta de la
integración se soluciona con
`custom_components/smn_ar/brand/{icon.png,logo.png}`, que ya están
en este repo. Desde Home Assistant 2026.3 las integraciones custom pueden
traer su propio ícono así, con prioridad automática sobre el CDN de
[`home-assistant/brands`](https://github.com/home-assistant/brands) — no
hace falta ningún PR externo ni configuración adicional en
`manifest.json`. Alcanza con reiniciar Home Assistant después de
actualizar la integración para que el ícono aparezca. En versiones de HA
anteriores a 2026.3 va a seguir mostrando el placeholder (no hay forma de
evitarlo salvo actualizar HA).

## Estado actual (validado con un spike real, no solo en teoría)

✅ **Funciona end-to-end**: clima actual, pronóstico de 7 días,
amanecer/atardecer, alertas por evento, avisos a muy corto plazo, y la
resolución de ubicación por lat/lon (`georef/location/coord`) — probado
con `curl` contra el proxy real corriendo en Docker, todos devuelven 200
con datos reales.

## Créditos

Basado en el trabajo de [`catastrophicode/ha-ar-smn`](https://github.com/catastrophicode/ha-ar-smn)
(MIT) para la integración, y [`nixietab/OpenSMN`](https://github.com/nixietab/OpenSMN)
(GPL-2.0) para el enfoque del proxy de Cloudflare.
